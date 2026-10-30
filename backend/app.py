from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import hashlib
import hmac
import secrets
import time
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx
import pymysql
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware


app = FastAPI(title="Marketplace BI Dashboard API", version="0.1.0")
logger = logging.getLogger("marketplace-bi-dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["Content-Type"],
)


class MarketplaceAdapter(Protocol):
    platform: str

    async def dashboard(self, start_date: date, end_date: date, market: str | None) -> dict[str, Any]: ...


MARKETS = {
    "MY": {"name": "Malaysia", "currency": "MYR", "flag": "MY"},
    "PH": {"name": "Philippines", "currency": "PHP", "flag": "PH"},
    "SG": {"name": "Singapore", "currency": "SGD", "flag": "SG"},
    "TH": {"name": "Thailand", "currency": "THB", "flag": "TH"},
}

SHOP_TIMEZONE = ZoneInfo("Asia/Singapore")
# Business Insights "confirmed orders" excludes unpaid and cancelled orders.
# Keep the list configurable because Shopee can add an intermediate status.
CONFIRMED_ORDER_STATUSES = {
    value.strip().upper()
    for value in os.getenv(
        "SHOPEE_CONFIRMED_ORDER_STATUSES",
        "READY_TO_SHIP,PROCESSED,SHIPPED,TO_CONFIRM_RECEIVE,COMPLETED",
    ).split(",")
    if value.strip()
}


def _shops() -> list[dict[str, Any]]:
    raw = os.getenv("SHOPEE_SHOPS_JSON", "").strip()
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


class OAuthStore:
    """Persist OAuth state and credentials in IdeaDock-provided MySQL."""

    def __init__(self) -> None:
        self.database_url = os.getenv("DATABASE_URL", "").strip()
        self.host = os.getenv("MYSQL_HOST", "").strip()
        self.port = int(os.getenv("MYSQL_PORT", "3306"))
        self.database = os.getenv("MYSQL_DATABASE", "").strip()
        self.user = os.getenv("MYSQL_USER", "").strip()
        self.password = os.getenv("MYSQL_PASSWORD", "")
        self.enabled = bool(self.database_url or (self.host and self.database and self.user))
        self._ready = False

    @contextmanager
    def connection(self):
        if self.database_url:
            from urllib.parse import urlparse
            parsed = urlparse(self.database_url)
            conn = pymysql.connect(host=parsed.hostname, port=parsed.port or 3306, user=parsed.username, password=parsed.password, database=(parsed.path or "/")[1:], autocommit=True, cursorclass=pymysql.cursors.DictCursor)
        else:
            conn = pymysql.connect(host=self.host, port=self.port, user=self.user, password=self.password, database=self.database, autocommit=True, cursorclass=pymysql.cursors.DictCursor)
        try:
            yield conn
        finally:
            conn.close()

    def init(self) -> None:
        if not self.enabled or self._ready:
            return
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS marketplace_oauth_shops (shop_id VARCHAR(64) PRIMARY KEY, market VARCHAR(8), main_account_id VARCHAR(64), principal_id VARCHAR(64), merchant_id VARCHAR(64), access_token TEXT NOT NULL, refresh_token TEXT NOT NULL, expires_at BIGINT NULL, currency VARCHAR(16) NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP)")
            try:
                cur.execute("ALTER TABLE marketplace_oauth_shops ADD COLUMN merchant_id VARCHAR(64) NULL AFTER main_account_id")
            except pymysql.err.OperationalError as exc:
                if exc.args[0] != 1060:
                    raise
            try:
                cur.execute("ALTER TABLE marketplace_oauth_shops ADD COLUMN principal_id VARCHAR(64) NULL AFTER main_account_id")
            except pymysql.err.OperationalError as exc:
                if exc.args[0] != 1060:
                    raise
            cur.execute("CREATE TABLE IF NOT EXISTS marketplace_oauth_states (state VARCHAR(128) PRIMARY KEY, market VARCHAR(8) NOT NULL, expires_at BIGINT NOT NULL, created_at BIGINT NOT NULL)")
        self._ready = True

    def save_shop(self, shop: dict[str, Any]) -> None:
        self.init()
        if not self.enabled:
            return
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO marketplace_oauth_shops (shop_id, market, main_account_id, principal_id, merchant_id, access_token, refresh_token, expires_at, currency) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE market=VALUES(market), main_account_id=VALUES(main_account_id), principal_id=VALUES(principal_id), merchant_id=VALUES(merchant_id), access_token=VALUES(access_token), refresh_token=VALUES(refresh_token), expires_at=VALUES(expires_at), currency=VALUES(currency)", (shop["shop_id"], shop.get("market"), shop.get("main_account_id"), shop.get("principal_id"), shop.get("merchant_id"), shop.get("access_token", ""), shop.get("refresh_token", ""), shop.get("expires_at"), shop.get("currency")))

    def load_shops(self) -> list[dict[str, Any]]:
        self.init()
        if not self.enabled:
            return []
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT shop_id, market, main_account_id, principal_id, merchant_id, access_token, refresh_token, expires_at, currency FROM marketplace_oauth_shops")
            return list(cur.fetchall())

    def save_state(self, state: str, market: str, expires_at: int) -> None:
        self.init()
        if not self.enabled:
            return
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO marketplace_oauth_states (state, market, expires_at, created_at) VALUES (%s,%s,%s,%s)", (state, market, expires_at, int(time.time())))

    def consume_state(self, state: str) -> dict[str, Any] | None:
        self.init()
        if not self.enabled:
            return None
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT state, market, expires_at, created_at FROM marketplace_oauth_states WHERE state=%s", (state,))
            row = cur.fetchone()
            if row:
                cur.execute("DELETE FROM marketplace_oauth_states WHERE state=%s", (state,))
            return row

    def init_lazada(self) -> None:
        if not self.enabled or getattr(self, "_lazada_ready", False):
            return
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS lazada_oauth_shops (shop_id VARCHAR(128) PRIMARY KEY, market VARCHAR(8) NOT NULL, access_token TEXT NOT NULL, refresh_token TEXT NULL, expires_at BIGINT NULL, refresh_expires_at BIGINT NULL, currency VARCHAR(16) NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP)")
            cur.execute("CREATE TABLE IF NOT EXISTS lazada_oauth_states (state VARCHAR(128) PRIMARY KEY, market VARCHAR(8) NOT NULL, expires_at BIGINT NOT NULL, created_at BIGINT NOT NULL)")
        self._lazada_ready = True

    def save_lazada_shop(self, shop: dict[str, Any]) -> None:
        self.init_lazada()
        if not self.enabled:
            return
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO lazada_oauth_shops (shop_id, market, access_token, refresh_token, expires_at, refresh_expires_at, currency) VALUES (%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE market=VALUES(market), access_token=VALUES(access_token), refresh_token=VALUES(refresh_token), expires_at=VALUES(expires_at), refresh_expires_at=VALUES(refresh_expires_at), currency=VALUES(currency)", (shop["shop_id"], shop.get("market"), shop.get("access_token", ""), shop.get("refresh_token"), shop.get("expires_at"), shop.get("refresh_expires_at"), shop.get("currency")))

    def load_lazada_shops(self) -> list[dict[str, Any]]:
        self.init_lazada()
        if not self.enabled:
            return []
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT shop_id, market, access_token, refresh_token, expires_at, refresh_expires_at, currency FROM lazada_oauth_shops")
            return list(cur.fetchall())

    def save_lazada_state(self, state: str, market: str, expires_at: int) -> None:
        self.init_lazada()
        if not self.enabled:
            return
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO lazada_oauth_states (state, market, expires_at, created_at) VALUES (%s,%s,%s,%s)", (state, market, expires_at, int(time.time())))

    def consume_lazada_state(self, state: str) -> dict[str, Any] | None:
        self.init_lazada()
        if not self.enabled:
            return None
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT state, market, expires_at, created_at FROM lazada_oauth_states WHERE state=%s", (state,))
            row = cur.fetchone()
            if row:
                cur.execute("DELETE FROM lazada_oauth_states WHERE state=%s", (state,))
            return row
class ShopeeAdapter:
    platform = "shopee"

    def __init__(self) -> None:
        self.partner_id = os.getenv("SHOPEE_PARTNER_ID", "").strip()
        self.partner_key = os.getenv("SHOPEE_PARTNER_KEY", "").strip()
        self.redirect_uri = os.getenv("SHOPEE_REDIRECT_URI", "").strip()
        self.api_base = os.getenv("SHOPEE_API_BASE", "https://partner.shopeemobile.com").rstrip("/")
        self.shops = {str(shop.get("shop_id")): shop for shop in _shops() if shop.get("shop_id")}
        self.oauth_shops = {}
        self.oauth_states: dict[str, dict[str, Any]] = {}
        self.store = OAuthStore()
        for shop in self.store.load_shops():
            self.oauth_shops[str(shop["shop_id"])] = dict(shop)
        self.access_token = os.getenv("DASHBOARD_ACCESS_TOKEN", "").strip()
        self.dashboard_cache_ttl = max(15, int(os.getenv("DASHBOARD_CACHE_TTL_SECONDS", "120")))
        self.dashboard_stale_ttl = max(self.dashboard_cache_ttl, int(os.getenv("DASHBOARD_STALE_TTL_SECONDS", "900")))
        self.api_request_interval = max(0.0, float(os.getenv("SHOPEE_REQUEST_INTERVAL_SECONDS", "0.4")))
        self._dashboard_cache: dict[str, dict[str, Any]] = {}
        self._dashboard_locks: dict[str, asyncio.Lock] = {}
        self._api_rate_lock = asyncio.Lock()
        self._next_api_request_at = 0.0
        self.configured = bool(self.partner_id and self.partner_key and self.redirect_uri)

    def require_access(self, authorization: str) -> None:
        expected = self.access_token
        scheme, _, supplied = (authorization or "").partition(" ")
        if not expected or scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip(), expected):
            raise HTTPException(401, "需要登录看板", headers={"WWW-Authenticate": "Bearer"})

    def _sign(self, path: str, timestamp: int, access_token: str = "", shop_id: str = "") -> str:
        payload = f"{self.partner_id}{path}{timestamp}{access_token}{shop_id}".encode()
        return hmac.new(self.partner_key.encode(), payload, hashlib.sha256).hexdigest()

    def _sign_principal(self, path: str, timestamp: int, access_token: str, principal_id: str) -> str:
        payload = f"{self.partner_id}{path}{timestamp}{access_token}{principal_id}".encode()
        return hmac.new(self.partner_key.encode(), payload, hashlib.sha256).hexdigest()

    def auth_url(self, market: str) -> str:
        if not self.configured:
            raise HTTPException(503, "Shopee OAuth 尚未配置 Partner ID、Partner Key 和 Redirect URI")
        # Shopee's production OAuth entry point is the shop authorization URL.
        # Depending on the app's approved scopes and the account used to sign in,
        # the token exchange may also return principal_id_list for Business
        # Insights; that identity is persisted by exchange_code below.
        path = "/api/v2/shop/auth_partner"
        timestamp = int(time.time())
        state = secrets.token_urlsafe(32)
        expires_at = int(time.time() + 600)
        self.oauth_states[state] = {"market": market, "created_at": time.time(), "expires_at": expires_at}
        self.store.save_state(state, market, expires_at)
        redirect_parts = urlsplit(self.redirect_uri)
        redirect_query = dict(parse_qsl(redirect_parts.query, keep_blank_values=True))
        redirect_query["oauth_state"] = state
        callback_redirect = urlunsplit((redirect_parts.scheme, redirect_parts.netloc, redirect_parts.path, urlencode(redirect_query), redirect_parts.fragment))
        query = {"partner_id": self.partner_id, "timestamp": timestamp, "sign": self._sign(path, timestamp), "redirect": callback_redirect, "state": state}
        return f"{self.api_base}{path}?{urlencode(query)}"

    async def exchange_code(self, code: str, shop_id: str, market: str | None = None) -> dict[str, Any]:
        path = "/api/v2/auth/token/get"
        timestamp = int(time.time())
        params = {"partner_id": self.partner_id, "timestamp": timestamp, "sign": self._sign(path, timestamp)}
        payload = {"code": code, "shop_id": int(shop_id), "partner_id": int(self.partner_id)}
        body: dict[str, Any] | None = None
        last_error: Exception | None = None
        # Shopee Sandbox can intermittently delay the token response. Retry
        # transport failures, while keeping the same signed request payload.
        for attempt in range(3):
            try:
                timeout = httpx.Timeout(30.0, connect=10.0)
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=False) as client:
                    response = await client.post(f"{self.api_base}{path}", params=params, json=payload)
                    response.raise_for_status()
                    body = response.json()
                break
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(1 + attempt)
            except httpx.HTTPError as exc:
                raise HTTPException(502, "Shopee OAuth 换 token 请求失败，请重新发起授权") from exc
        if body is None:
            raise HTTPException(502, "Shopee OAuth 换 token 请求超时，请重新发起授权") from last_error
        if body.get("error"):
            raise HTTPException(502, f"Shopee OAuth 换 token 失败: {body.get('message') or body.get('error')}")
        response_body = body.get("response") or body
        principal_ids = [str(value) for value in (response_body.get("principal_id_list") or body.get("principal_id_list") or [])]
        merchant_ids = [str(value) for value in (response_body.get("merchant_id_list") or body.get("merchant_id_list") or [])]
        record = {
            "shop_id": shop_id,
            "market": market,
            "principal_id": principal_ids[0] if len(principal_ids) == 1 else None,
            "merchant_id": merchant_ids[0] if len(merchant_ids) == 1 else None,
            "access_token": response_body.get("access_token", body.get("access_token", "")),
            "refresh_token": response_body.get("refresh_token", body.get("refresh_token", "")),
            "expires_at": int(time.time() + int(response_body.get("expire_in") or 0)),
        }
        self.oauth_shops[shop_id] = record
        self.store.save_shop(record)
        return {"shop_id": shop_id, "market": market, "connected": bool(body.get("access_token")), "expires_in": body.get("expire_in")}

    async def exchange_merchant_code(self, code: str, main_account_id: str) -> dict[str, Any]:
        """Exchange a live merchant authorization code and bind its shops."""
        path = "/api/v2/auth/token/get"
        timestamp = int(time.time())
        params = {"partner_id": self.partner_id, "timestamp": timestamp, "sign": self._sign(path, timestamp)}
        payload = {"code": code, "main_account_id": int(main_account_id), "partner_id": int(self.partner_id)}
        try:
            timeout = httpx.Timeout(30.0, connect=10.0)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=False) as client:
                response = await client.post(f"{self.api_base}{path}", params=params, json=payload)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(502, f"Shopee 商家授权换 token 失败（HTTP {exc.response.status_code}）") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Shopee 商家授权换 token 网络错误：{type(exc).__name__}") from exc
        if body.get("error"):
            logger.warning("Shopee merchant token exchange rejected: error=%s request_id=%s", body.get("error"), body.get("request_id"))
            raise HTTPException(502, "Shopee 商家授权换 token 失败，请重新发起授权")

        access_token = body.get("access_token", "")
        refresh_token = body.get("refresh_token", "")
        shop_ids = [str(value) for value in (body.get("shop_id_list") or [])]
        # Some responses wrap the token payload under response.
        response_body = body.get("response") or body
        access_token = response_body.get("access_token", access_token)
        refresh_token = response_body.get("refresh_token", refresh_token)
        shop_ids = [str(value) for value in (response_body.get("shop_id_list") or shop_ids)]
        principal_ids = [str(value) for value in (response_body.get("principal_id_list") or body.get("principal_id_list") or [])]
        authorized_merchant_ids = {str(value) for value in (response_body.get("merchant_id_list") or body.get("merchant_id_list") or [])}
        partner_shops, partner_merchants = await self._partner_authorizations()
        shop_regions = {
            str(item.get("shop_id")): str(item.get("region") or "").upper()
            for item in partner_shops
            if item.get("shop_id") is not None
        }
        merchants_by_region: dict[str, list[str]] = {}
        for item in partner_merchants:
            merchant_id = str(item.get("merchant_id") or "")
            region = str(item.get("region") or "").upper()
            if not merchant_id or (authorized_merchant_ids and merchant_id not in authorized_merchant_ids):
                continue
            merchants_by_region.setdefault(region, []).append(merchant_id)
        for shop_id in shop_ids:
            market = shop_regions.get(shop_id) or None
            merchant_candidates = merchants_by_region.get(market or "", [])
            merchant_id = merchant_candidates[0] if len(merchant_candidates) == 1 else None
            if not merchant_id and len(authorized_merchant_ids) == 1:
                merchant_id = next(iter(authorized_merchant_ids))
            record = {
                "shop_id": shop_id,
                "market": market,
                "main_account_id": str(main_account_id),
                "principal_id": principal_ids[0] if len(principal_ids) == 1 else None,
                "merchant_id": merchant_id,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": int(time.time() + int(response_body.get("expire_in") or 0)),
                "currency": MARKETS.get(market or "", {}).get("currency"),
            }
            self.oauth_shops[shop_id] = record
            self.store.save_shop(record)
        mapped_merchants = {record.get("merchant_id") for record in self.oauth_shops.values() if record.get("main_account_id") == str(main_account_id) and record.get("merchant_id")}
        return {"main_account_id": str(main_account_id), "shop_ids": shop_ids, "merchant_count": len(mapped_merchants), "connected": bool(access_token), "expires_in": response_body.get("expire_in")}

    async def _partner_authorizations(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        async def fetch(path: str, list_key: str) -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            page_no = 1
            while True:
                timestamp = int(time.time())
                params = {
                    "partner_id": self.partner_id,
                    "timestamp": timestamp,
                    "sign": self._sign(path, timestamp),
                    "page_no": page_no,
                    "page_size": 100,
                }
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True) as client:
                        response = await client.get(f"{self.api_base}{path}", params=params)
                        response.raise_for_status()
                        body = response.json()
                except httpx.HTTPError as exc:
                    logger.warning("Shopee partner authorization lookup failed: path=%s error_type=%s", path, type(exc).__name__)
                    return items
                if body.get("error"):
                    logger.warning("Shopee partner authorization lookup rejected: path=%s error=%s", path, body.get("error"))
                    return items
                result = body.get("response") or body
                page_items = result.get(list_key) or []
                items.extend(item for item in page_items if isinstance(item, dict))
                if not result.get("more"):
                    return items
                page_no += 1

        shops, merchants = await asyncio.gather(
            fetch("/api/v2/public/get_shops_by_partner", "authed_shop_list"),
            fetch("/api/v2/public/get_merchants_by_partner", "authed_merchant_list"),
        )
        return shops, merchants

    async def _refresh_access_token(self, shop: dict[str, Any]) -> None:
        refresh_token = str(shop.get("refresh_token") or "")
        if not refresh_token:
            raise HTTPException(424, "Shopee 店铺授权已失效，请重新连接")
        path = "/api/v2/auth/access_token/get"
        timestamp = int(time.time())
        params = {"partner_id": self.partner_id, "timestamp": timestamp, "sign": self._sign(path, timestamp)}
        payload: dict[str, Any] = {"partner_id": int(self.partner_id), "refresh_token": refresh_token}
        main_account_id = shop.get("main_account_id")
        merchant_id = shop.get("merchant_id")
        if merchant_id:
            payload["merchant_id"] = int(merchant_id)
        else:
            # main_account_id is not a merchant_id. Existing merchant OAuth
            # records do contain authorized shop IDs, which the refresh API
            # accepts as the alternate refresh subject.
            payload["shop_id"] = int(shop["shop_id"])
        body: dict[str, Any] | None = None
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True) as client:
                    response = await client.post(f"{self.api_base}{path}", params=params, json=payload)
                    response.raise_for_status()
                    body = response.json()
                break
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "Shopee token refresh HTTP failure: shop_id=%s status=%s",
                    shop.get("shop_id"),
                    exc.response.status_code,
                )
                raise HTTPException(502, f"Shopee token 刷新请求失败（HTTP {exc.response.status_code}）") from exc
            except httpx.TransportError as exc:
                last_error = exc
                logger.warning(
                    "Shopee token refresh transport failure: shop_id=%s attempt=%s error_type=%s",
                    shop.get("shop_id"),
                    attempt + 1,
                    type(exc).__name__,
                )
                if attempt < 2:
                    await asyncio.sleep(1 + attempt)
        if body is None:
            error_type = type(last_error).__name__ if last_error else "UnknownTransportError"
            raise HTTPException(502, f"Shopee token 刷新请求失败（网络错误：{error_type}）") from last_error
        if body.get("error"):
            logger.warning(
                "Shopee token refresh rejected: shop_id=%s scope=%s error=%s request_id=%s",
                shop.get("shop_id"),
                "merchant" if merchant_id else "shop",
                body.get("error"),
                body.get("request_id"),
            )
            raise HTTPException(424, "Shopee 店铺授权已失效，请重新连接")
        token_body = body.get("response") or body
        access_token = token_body.get("access_token")
        if not access_token:
            raise HTTPException(502, "Shopee token 刷新响应无效")
        expires_at = int(time.time() + int(token_body.get("expire_in") or 0))
        affected = [shop]
        if merchant_id:
            affected = [item for item in self.oauth_shops.values() if str(item.get("merchant_id")) == str(merchant_id)] or [shop]
        for item in affected:
            item["access_token"] = access_token
            item["refresh_token"] = token_body.get("refresh_token") or refresh_token
            item["expires_at"] = expires_at
            self.store.save_shop(item)

    async def _throttle_api_request(self) -> None:
        interval = getattr(self, "api_request_interval", 0.4)
        if interval <= 0:
            return
        lock = getattr(self, "_api_rate_lock", None)
        if lock is None:
            lock = self._api_rate_lock = asyncio.Lock()
            self._next_api_request_at = 0.0
        async with lock:
            delay = getattr(self, "_next_api_request_at", 0.0) - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_api_request_at = time.monotonic() + interval

    async def _request(self, path: str, shop: dict[str, Any], payload: dict[str, Any], method: str = "POST") -> dict[str, Any]:
        if int(shop.get("expires_at") or 0) <= int(time.time()) + 300:
            await self._refresh_access_token(shop)
        shop_id = str(shop["shop_id"])
        access_token = str(shop["access_token"])
        for attempt in range(4):
            await self._throttle_api_request()
            timestamp = int(time.time())
            params = {"partner_id": self.partner_id, "timestamp": timestamp, "shop_id": shop_id, "access_token": access_token, "sign": self._sign(path, timestamp, access_token, shop_id)}
            try:
                async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
                    if method == "GET":
                        response = await client.get(f"{self.api_base}{path}", params={**params, **payload})
                    else:
                        response = await client.post(f"{self.api_base}{path}", params=params, json=payload)
                    response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                error_body = {}
                try:
                    error_body = exc.response.json()
                except ValueError:
                    pass
                logger.warning(
                    "Shopee API HTTP failure: shop_id=%s path=%s status=%s error=%s request_id=%s",
                    shop_id,
                    path,
                    exc.response.status_code,
                    error_body.get("error"),
                    error_body.get("request_id"),
                )
                if exc.response.status_code == 429:
                    retry_after = exc.response.headers.get("Retry-After", "")
                    try:
                        delay = max(float(retry_after), 1.5 * (2 ** attempt))
                    except ValueError:
                        delay = 1.5 * (2 ** attempt)
                    if attempt < 3:
                        await asyncio.sleep(min(delay, 10.0))
                        continue
                    raise HTTPException(503, "Shopee API 请求频率过高，已使用退避重试") from exc
                if exc.response.status_code in (401, 403):
                    raise HTTPException(424, "Shopee 店铺 Token 与店铺不匹配，请重新连接主账号") from exc
                raise HTTPException(502, f"Shopee API 请求失败（HTTP {exc.response.status_code}）") from exc
            except httpx.HTTPError as exc:
                raise HTTPException(502, f"Shopee API 请求失败（网络错误：{type(exc).__name__}）") from exc
            try:
                body = response.json()
            except ValueError as exc:
                raise HTTPException(502, "Shopee API 返回了无效响应") from exc
            if body.get("error") in {"error_rate_limit", "rate_limit", "too_many_requests"}:
                if attempt < 3:
                    await asyncio.sleep(min(1.5 * (2 ** attempt), 10.0))
                    continue
                raise HTTPException(503, "Shopee API 请求频率过高，已使用退避重试")
            if body.get("error"):
                raise HTTPException(502, f"Shopee API 错误: {body.get('message') or body.get('error')}")
            return body.get("response", body)
        raise HTTPException(503, "Shopee API 请求频率过高")

    async def _principal_request(self, path: str, shop: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        """Call Business Insights using the identity returned by merchant OAuth.

        Principal Business Insights uses the merchant's main account identity.
        Some older shop-only authorizations do not have that field; those records
        can fall back to a merchant id when one is available.
        """
        principal_id = str(shop.get("principal_id") or "")
        if not principal_id:
            raise HTTPException(424, "Shopee 商家授权未返回 Principal ID，请重新授权主账号")
        if int(shop.get("expires_at") or 0) <= int(time.time()) + 300:
            await self._refresh_access_token(shop)
        access_token = str(shop.get("access_token") or "")
        for attempt in range(4):
            await self._throttle_api_request()
            timestamp = int(time.time())
            params = {
                "partner_id": self.partner_id,
                "timestamp": timestamp,
                "principal_id": principal_id,
                "access_token": access_token,
                "sign": self._sign_principal(path, timestamp, access_token, principal_id),
            }
            try:
                async with httpx.AsyncClient(timeout=25, follow_redirects=True, trust_env=False) as client:
                    response = await client.post(f"{self.api_base}{path}", params=params, json=payload)
                    response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                error_body: dict[str, Any] = {}
                try:
                    parsed = exc.response.json()
                    if isinstance(parsed, dict):
                        error_body = parsed
                except ValueError:
                    pass
                logger.warning(
                    "Shopee Business Insights HTTP failure: shop_id=%s path=%s principal_type=%s status=%s error=%s message=%s request_id=%s",
                    shop.get("shop_id"),
                    path,
                    "principal_id",
                    exc.response.status_code,
                    error_body.get("error"),
                    error_body.get("message"),
                    error_body.get("request_id"),
                )
                if exc.response.status_code == 429 and attempt < 3:
                    await asyncio.sleep(min(1.5 * (2 ** attempt), 10.0))
                    continue
                if exc.response.status_code in (401, 403):
                    raise HTTPException(424, "Shopee 商业分析 API 无权限或授权已失效") from exc
                raise HTTPException(502, f"Shopee 商业分析 API 请求失败（HTTP {exc.response.status_code}）") from exc
            except httpx.HTTPError as exc:
                raise HTTPException(502, f"Shopee 商业分析 API 请求失败（网络错误：{type(exc).__name__}）") from exc
            try:
                body = response.json()
            except ValueError as exc:
                raise HTTPException(502, "Shopee 商业分析 API 返回了无效响应") from exc
            if body.get("error"):
                error = str(body.get("error"))
                logger.warning(
                    "Shopee Business Insights rejected: shop_id=%s path=%s principal_type=%s error=%s message=%s request_id=%s response_keys=%s",
                    shop.get("shop_id"),
                    path,
                    "principal_id",
                    error,
                    body.get("message"),
                    body.get("request_id"),
                    sorted(str(key) for key in body.keys()),
                )
                if error in {"error_auth", "error_api_permission", "error_api_call_restricted", "invalid_access_token", "invalid_acceess_token"}:
                    raise HTTPException(424, "Shopee 商业分析 API 无权限或授权已失效")
                if error in {"error_rate_limit", "rate_limit", "too_many_requests"} and attempt < 3:
                    await asyncio.sleep(min(1.5 * (2 ** attempt), 10.0))
                    continue
                raise HTTPException(502, f"Shopee 商业分析 API 错误: {body.get('message') or error}")
            return body.get("response", body)
        raise HTTPException(503, "Shopee 商业分析 API 请求频率过高")

    async def _hydrate_shop_metadata(self, shop: dict[str, Any]) -> None:
        market = str(shop.get("market") or "").upper()
        if market in MARKETS:
            shop["market"] = market
            shop["currency"] = MARKETS[market]["currency"]
            return

        info = await self._request("/api/v2/shop/get_shop_info", shop, {}, method="GET")
        market = str(info.get("region") or "").upper()
        if market not in MARKETS:
            logger.warning("Shopee shop region is unsupported: shop_id=%s region=%s", shop.get("shop_id"), market or "missing")
            raise HTTPException(502, "Shopee 店铺站点识别失败")

        shop["market"] = market
        shop["currency"] = MARKETS[market]["currency"]
        if str(shop.get("shop_id")) in self.oauth_shops:
            self.store.save_shop(shop)
        logger.info("Shopee shop metadata updated: shop_id=%s market=%s", shop.get("shop_id"), market)

    def _live_empty_dashboard(self, start_date: date, end_date: date, market: str | None, authorized_shop_count: int) -> dict[str, Any]:
        status = "not_authorized" if authorized_shop_count else "not_connected"
        markets = []
        if market:
            markets.append({
                "code": market,
                "name": MARKETS[market]["name"],
                "currency": MARKETS[market]["currency"],
                "sales": 0,
                "orders": 0,
                "units": 0,
                "ad_spend": None,
                "ad_ratio": None,
                "roi": None,
                "status": status,
            })
        return {
            "platform": "shopee",
            "configured": True,
            "mode": "live",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
            "currency": MARKETS[market]["currency"] if market else "MIXED",
            "currency_note": "当前筛选条件下没有已授权的 Shopee 店铺。",
            "totals": {"sales": 0, "orders": 0, "units": 0, "ad_spend": None, "ad_ratio": None, "roi": None},
            "markets": markets,
            "daily": [],
            "sources": {"orders": "Shopee Order API v2", "ads": "待配置 Shopee Ads API"},
        }

    @staticmethod
    def _daily_row(current: date, market: str) -> dict[str, Any]:
        return {
            "date": current.isoformat(),
            "market": market,
            "sales": 0.0,
            "orders": 0,
            "units": 0,
            "ad_spend": 0.0,
            "ad_ratio": None,
            "roi": None,
        }

    @staticmethod
    def _parse_record_date(value: Any) -> date | None:
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(value, tz=SHOP_TIMEZONE).date()
            except (OSError, OverflowError, ValueError):
                return None
        if not value:
            return None
        text = str(value).strip()
        for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(text[:10], pattern).date()
            except ValueError:
                continue
        return None

    async def _shop_sales_performance(self, shop: dict[str, Any], start_date: date, end_date: date) -> dict[str, Any] | None:
        """Read Seller Center Business Insights metrics for each historical day.

        Shopee's analytics API is the source used by Seller Center's product
        overview. It is T-1, so the current local day is intentionally left for
        the order API fallback instead of being mixed into the historical series.
        """
        if not (shop.get("principal_id") or shop.get("main_account_id")):
            return None
        latest_analytics_date = datetime.now(SHOP_TIMEZONE).date() - timedelta(days=1)
        analytics_end = min(end_date, latest_analytics_date)
        if analytics_end < start_date:
            return None
        daily: dict[str, dict[str, Any]] = {}
        for offset in range((analytics_end - start_date).days + 1):
            current = start_date + timedelta(days=offset)
            response = await self._principal_request(
                "/api/v2/principal/get_shop_sales_performance_detail",
                shop,
                {
                    "start_date": current.isoformat(),
                    "end_date": current.isoformat(),
                    "timezone": "GMT+8",
                    "granularity": "day",
                    "shop_list": [{"shop_id": int(shop["shop_id"]), "currency": "LOCAL"}],
                },
            )
            details = response.get("details") or response.get("shop_sales_performance_detail") or response.get("data") or []
            summary = response.get("summary") or response.get("shop_sales_performance") or []
            if isinstance(details, dict):
                details = details.get("items") or details.get("list") or [details]
            if isinstance(summary, dict):
                summary = summary.get("items") or summary.get("list") or [summary]
            metric = next((item for item in details if str(item.get("shop_id")) == str(shop["shop_id"])), None) or (details[0] if details else None) or (summary[0] if summary else {})
            if not isinstance(metric, dict):
                metric = {}
            sales_value = (
                metric.get("sales")
                or metric.get("sales_amount")
                or metric.get("placed_order_sales")
                or metric.get("confirmed_order_sales")
                or metric.get("gmv")
                or 0
            )
            orders_value = metric.get("orders") or metric.get("orders_count") or metric.get("placed_order_count") or metric.get("confirmed_order_count") or 0
            units_value = metric.get("units_sold") or metric.get("units") or metric.get("items") or metric.get("placed_order_items") or metric.get("confirmed_order_items") or 0
            daily[current.isoformat()] = {
                "date": current.isoformat(),
                "market": str(shop.get("market") or "--"),
                "sales": round(float(sales_value), 2),
                "orders": int(orders_value),
                "units": int(units_value),
            }
        return {"daily": daily, "source": "Shopee Business Insights API v2 (PlacedOrder)"}

    async def _shop_dashboard(self, shop: dict[str, Any], start_date: date, end_date: date) -> dict[str, Any]:
        # Shopee seller analytics displays dates in the shop's GMT+08 timezone.
        # Convert local calendar boundaries to Unix timestamps for the API.
        start_ts = int(datetime.combine(start_date, datetime.min.time(), tzinfo=SHOP_TIMEZONE).timestamp())
        end_ts = int(datetime.combine(end_date, datetime.max.time(), tzinfo=SHOP_TIMEZONE).timestamp())
        market = str(shop.get("market") or "--")
        daily = {
            (start_date + timedelta(days=index)).isoformat(): self._daily_row(start_date + timedelta(days=index), market)
            for index in range((end_date - start_date).days + 1)
        }
        sales_source = "Shopee Order API v2 (fallback; not Seller Center Business Insights)"
        sales_warning = "Shopee Business Insights API 无权限或授权已失效，当前销售指标暂按订单 API 的已确定订单口径返回。开通 Principal Business Insights 权限后会自动切换为卖家中心商业分析口径。"
        try:
            performance = await self._shop_sales_performance(shop, start_date, end_date)
        except HTTPException as exc:
            if exc.status_code != 424:
                raise
            logger.warning("Shopee Business Insights unavailable: shop_id=%s detail=%s", shop.get("shop_id"), exc.detail)
            performance = None

        if performance:
            for day_key, metric in performance["daily"].items():
                daily[day_key].update(metric)
            sales = sum(item["sales"] for item in performance["daily"].values())
            orders = sum(item["orders"] for item in performance["daily"].values())
            units = sum(item["units"] for item in performance["daily"].values())
            sales_source = performance["source"]
        else:
            # Fallback for legacy shop authorizations without a principal ID or
            # apps that have not been granted the Business Insights permission.
            # Shopee does not support an ``ALL`` value for order_status.
            order_records: list[dict[str, Any]] = []
            cursor = ""
            while True:
                payload = {
                    "time_range_field": "create_time",
                    "time_from": start_ts,
                    "time_to": end_ts,
                    "page_size": 100,
                }
                if cursor:
                    payload["cursor"] = cursor
                page = await self._request("/api/v2/order/get_order_list", shop, payload, method="GET")
                order_records.extend(item for item in (page.get("order_list") or []) if item.get("order_sn"))
                if not page.get("more"):
                    break
                next_cursor = str(page.get("next_cursor") or "")
                if not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor

            order_sns = list(dict.fromkeys(item["order_sn"] for item in order_records))
            rows: list[dict[str, Any]] = []
            for offset in range(0, len(order_sns), 50):
                details = await self._request(
                    "/api/v2/order/get_order_detail",
                    shop,
                    {"order_sn_list": ",".join(order_sns[offset : offset + 50]), "response_optional_fields": "order_sn,total_amount,item_list,create_time,pay_time,order_status"},
                    method="GET",
                )
                rows.extend(item for item in (details.get("order_list") or []) if item.get("order_sn"))

            rows_by_sn = {str(item["order_sn"]): item for item in rows if item.get("order_sn")}
            rows = [item for item in rows_by_sn.values() if str(item.get("order_status") or "").upper() in CONFIRMED_ORDER_STATUSES]
            for item in rows:
                order_date = self._parse_record_date(item.get("create_time") or item.get("pay_time"))
                if not order_date or order_date.isoformat() not in daily:
                    continue
                item_units = sum(int(line.get("model_quantity_purchased") or line.get("item_quantity") or 0) for line in (item.get("item_list") or []))
                day = daily[order_date.isoformat()]
                day["sales"] += float(item.get("total_amount") or 0)
                day["orders"] += 1
                day["units"] += item_units
            sales = sum(float(item.get("total_amount") or 0) for item in rows)
            units = sum(int(line.get("model_quantity_purchased") or line.get("item_quantity") or 0) for item in rows for line in (item.get("item_list") or []))
            orders = len(rows)
        ad_spend, ads_status, daily_ad_spend = await self._shop_ad_spend(shop, start_date, end_date)
        for day_key, day in daily.items():
            day["sales"] = round(day["sales"], 2)
            day["ad_spend"] = daily_ad_spend.get(day_key) if ads_status == "ready" else None
            day["ad_ratio"] = round(day["ad_spend"] / day["sales"] * 100, 2) if day["ad_spend"] is not None and day["sales"] else None
            day["roi"] = round(day["sales"] / day["ad_spend"], 2) if day["ad_spend"] else None
        ad_ratio = round(ad_spend / sales * 100, 2) if ad_spend is not None and sales else None
        roi = round(sales / ad_spend, 2) if ad_spend else None
        return {"sales": round(sales, 2), "orders": orders, "units": units, "currency": shop.get("currency", "LOCAL"), "ad_spend": ad_spend, "ad_ratio": ad_ratio, "roi": roi, "ads_status": ads_status, "sales_source": sales_source, "sales_warning": None if performance else sales_warning, "daily": list(daily.values())}

    async def _shop_ad_spend(self, shop: dict[str, Any], start_date: date, end_date: date) -> tuple[float | None, str, dict[str, float]]:
        records: list[dict[str, Any]] = []
        daily = {
            (start_date + timedelta(days=index)).isoformat(): 0.0
            for index in range((end_date - start_date).days + 1)
        }
        cursor = start_date
        try:
            while cursor <= end_date:
                chunk_end = min(cursor + timedelta(days=29), end_date)
                if cursor == chunk_end:
                    response = await self._request(
                        "/api/v2/ads/get_all_cpc_ads_hourly_performance",
                        shop,
                        {"performance_date": cursor.strftime("%d-%m-%Y")},
                        method="GET",
                    )
                else:
                    response = await self._request(
                        "/api/v2/ads/get_all_cpc_ads_daily_performance",
                        shop,
                        {"start_date": cursor.strftime("%d-%m-%Y"), "end_date": chunk_end.strftime("%d-%m-%Y")},
                        method="GET",
                    )
                if isinstance(response, list):
                    chunk_records = [item for item in response if isinstance(item, dict)]
                    records.extend(chunk_records)
                    for item in chunk_records:
                        record_date = self._parse_record_date(
                            item.get("date") or item.get("performance_date") or item.get("report_date") or item.get("statistic_date")
                        )
                        if record_date is None and cursor == chunk_end:
                            record_date = cursor
                        if record_date and record_date.isoformat() in daily:
                            daily[record_date.isoformat()] += float(item.get("expense") or 0)
                cursor = chunk_end + timedelta(days=1)
        except HTTPException as exc:
            detail = str(exc.detail)
            if "permission" in detail.lower() or "no permission" in detail.lower():
                logger.warning("Shopee Ads API permission missing: shop_id=%s", shop.get("shop_id"))
                return None, "permission_required", {}
            if "data not exist" in detail.lower() or "not found" in detail.lower():
                return 0.0, "ready", daily
            raise
        return round(sum(float(item.get("expense") or 0) for item in records), 2), "ready", {key: round(value, 2) for key, value in daily.items()}

    def _aggregate_markets(self, shops: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for shop, row in zip(shops, rows):
            market = str(shop.get("market") or "--")
            current = grouped.setdefault(market, {
                "code": market,
                "name": MARKETS.get(market, {}).get("name", "Shopee"),
                "currency": row["currency"],
                "sales": 0.0,
                "orders": 0,
                "units": 0,
                "ad_spend": 0.0,
                "ads_status": "ready",
                "status": "ready",
            })
            current["sales"] += row["sales"]
            current["orders"] += row["orders"]
            current["units"] += row["units"]
            if row.get("ads_status") == "ready":
                current["ad_spend"] += row.get("ad_spend") or 0
            else:
                current["ads_status"] = "permission_required"

        for current in grouped.values():
            current["sales"] = round(current["sales"], 2)
            if current["ads_status"] != "ready":
                current["ad_spend"] = None
            else:
                current["ad_spend"] = round(current["ad_spend"], 2)
            current["ad_ratio"] = round(current["ad_spend"] / current["sales"] * 100, 2) if current["ad_spend"] is not None and current["sales"] else None
            current["roi"] = round(current["sales"] / current["ad_spend"], 2) if current["ad_spend"] else None
        return list(grouped.values())

    def _aggregate_daily(self, rows: list[dict[str, Any]], start_date: date, end_date: date, market: str | None) -> list[dict[str, Any]]:
        daily = {
            (start_date + timedelta(days=index)).isoformat(): self._daily_row(start_date + timedelta(days=index), market or "ALL")
            for index in range((end_date - start_date).days + 1)
        }
        ads_ready = all(row.get("ads_status") == "ready" for row in rows)
        for row in rows:
            for item in row.get("daily") or []:
                current = daily.get(item.get("date"))
                if not current:
                    continue
                current["sales"] += item.get("sales") or 0
                current["orders"] += item.get("orders") or 0
                current["units"] += item.get("units") or 0
                if ads_ready:
                    current["ad_spend"] += item.get("ad_spend") or 0
        for current in daily.values():
            current["sales"] = round(current["sales"], 2)
            if ads_ready:
                current["ad_spend"] = round(current["ad_spend"], 2)
            else:
                current["ad_spend"] = None
            current["ad_ratio"] = round(current["ad_spend"] / current["sales"] * 100, 2) if current["ad_spend"] is not None and current["sales"] else None
            current["roi"] = round(current["sales"] / current["ad_spend"], 2) if current["ad_spend"] else None
        return list(daily.values())

    async def dashboard(self, start_date: date, end_date: date, market: str | None) -> dict[str, Any]:
        cache_key = f"{start_date.isoformat()}:{end_date.isoformat()}:{market or 'ALL'}"
        cache = getattr(self, "_dashboard_cache", None)
        if cache is None:
            cache = self._dashboard_cache = {}
            self._dashboard_locks = {}
        now = time.monotonic()
        cached = cache.get(cache_key)
        cache_ttl = getattr(self, "dashboard_cache_ttl", 120)
        if cached and now - cached["stored_at"] <= cache_ttl:
            return copy.deepcopy(cached["value"])

        locks = getattr(self, "_dashboard_locks", None)
        if locks is None:
            locks = self._dashboard_locks = {}
        lock = locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = cache.get(cache_key)
            if cached and now - cached["stored_at"] <= cache_ttl:
                return copy.deepcopy(cached["value"])
            try:
                result = await self._dashboard_uncached(start_date, end_date, market)
            except HTTPException as exc:
                stale_ttl = getattr(self, "dashboard_stale_ttl", 900)
                if cached and exc.status_code in (502, 503) and now - cached["stored_at"] <= stale_ttl:
                    result = copy.deepcopy(cached["value"])
                    result["cache_status"] = "stale"
                    result["cache_note"] = "Shopee 接口正在限流，当前展示最近一次成功数据。"
                    return result
                raise
            cache[cache_key] = {"stored_at": time.monotonic(), "value": copy.deepcopy(result)}
            stale_ttl = getattr(self, "dashboard_stale_ttl", 900)
            for key, item in list(cache.items()):
                if time.monotonic() - item["stored_at"] > stale_ttl:
                    cache.pop(key, None)
                    locks.pop(key, None)
            return result

    async def _dashboard_uncached(self, start_date: date, end_date: date, market: str | None) -> dict[str, Any]:
        if not self.configured:
            return demo_dashboard(start_date, end_date, market, configured=False)

        all_shops = {**self.shops, **self.oauth_shops}
        available_shops: list[dict[str, Any]] = []
        connection_warnings: list[dict[str, str]] = []
        for shop in all_shops.values():
            try:
                await self._hydrate_shop_metadata(shop)
                available_shops.append(shop)
            except HTTPException as exc:
                if exc.status_code != 424:
                    raise
                connection_warnings.append({"shop_id": str(shop.get("shop_id") or ""), "message": str(exc.detail)})
        selected = [shop for shop in available_shops if not market or shop.get("market") == market]
        if not selected:
            result = self._live_empty_dashboard(start_date, end_date, market, len(all_shops))
            result["connection_warnings"] = connection_warnings
            return result
        successful_shops: list[dict[str, Any]] = []
        rows: list[dict[str, Any]] = []
        for shop in selected:
            try:
                rows.append(await self._shop_dashboard(shop, start_date, end_date))
                successful_shops.append(shop)
            except HTTPException as exc:
                if exc.status_code != 424:
                    raise
                connection_warnings.append({"shop_id": str(shop.get("shop_id") or ""), "message": str(exc.detail)})
        if not rows:
            result = self._live_empty_dashboard(start_date, end_date, market, len(all_shops))
            result["connection_warnings"] = connection_warnings
            return result
        selected = successful_shops
        totals = {key: sum((row.get(key) or 0) for row in rows) for key in ("sales", "orders", "units")}
        ads_ready = all(row.get("ads_status") == "ready" for row in rows)
        ad_spend = round(sum((row.get("ad_spend") or 0) for row in rows), 2) if ads_ready else None
        totals.update({"ad_spend": ad_spend, "ad_ratio": round(ad_spend / totals["sales"] * 100, 2) if ad_spend is not None and totals["sales"] else None, "roi": round(totals["sales"] / ad_spend, 2) if ad_spend else None})
        result_currency = MARKETS[market]["currency"] if market else "MIXED"
        ads_status = "ready" if ads_ready else "permission_required"
        currency_note = "销售额与广告花费按店铺本币汇总。" if market else "各站点使用不同本币；全部站点汇总仅适合查看订单量，金额指标不可直接相加。"
        sales_sources = {str(row.get("sales_source") or "") for row in rows}
        sales_source = next(iter(sales_sources)) if len(sales_sources) == 1 else "Shopee Business Insights API v2 + Order API fallback"
        exact_business_insights = bool(sales_sources) and all(source.startswith("Shopee Business Insights API") for source in sales_sources)
        sales_scope = "placed_orders" if exact_business_insights else "confirmed_orders_fallback"
        sales_scope_note = "与卖家中心商业分析的已下订单口径一致；包含未支付、取消和退款订单，销售额已扣除卖家折扣。" if exact_business_insights else "当前为订单 API 的已确定订单降级口径；要与卖家中心的已下订单完全一致，需要开通 Shopee Principal Business Insights API 权限。"
        sales_warnings = [str(row["sales_warning"]) for row in rows if row.get("sales_warning")]
        return {"platform": "shopee", "configured": True, "mode": "live", "generated_at": datetime.now(timezone.utc).isoformat(), "period": {"start": start_date.isoformat(), "end": end_date.isoformat()}, "currency": result_currency, "currency_note": currency_note, "totals": totals, "markets": self._aggregate_markets(selected, rows), "daily": self._aggregate_daily(rows, start_date, end_date, market), "ads_status": ads_status, "connection_warnings": connection_warnings, "sales_warnings": list(dict.fromkeys(sales_warnings)), "sources": {"orders": sales_source, "ads": "Shopee Ads API v2"}, "sales_scope": sales_scope, "sales_scope_note": sales_scope_note}


class LazadaAdapter:
    """Lazada Open Platform adapter using the seller's OAuth token."""

    platform = "lazada"
    MARKET_API_BASES = {
        "MY": "https://api.lazada.com.my/rest",
        "PH": "https://api.lazada.com.ph/rest",
        "SG": "https://api.lazada.sg/rest",
        "TH": "https://api.lazada.co.th/rest",
        "ID": "https://api.lazada.co.id/rest",
        "VN": "https://api.lazada.vn/rest",
    }
    # The Seller Center page "Data Insights -> Business Advisor" exposes these
    # metrics, but they are not part of Lazada's public Open Platform API.
    BUSINESS_ADVISOR_METRICS = (
        "buyers",
        "orders",
        "average_order_value",
        "units_sold",
        "basket_size",
    )

    def __init__(self) -> None:
        self.app_key = os.getenv("LAZADA_APP_KEY", "").strip()
        self.app_secret = os.getenv("LAZADA_APP_SECRET", "").strip()
        self.redirect_uri = os.getenv("LAZADA_REDIRECT_URI", "").strip()
        self.api_base = os.getenv("LAZADA_API_BASE", "").strip().rstrip("/")
        self.auth_base = os.getenv("LAZADA_AUTH_BASE", "https://auth.lazada.com").strip().rstrip("/")
        self.token_base = os.getenv("LAZADA_TOKEN_BASE", self.auth_base).strip().rstrip("/")
        self.store = OAuthStore()
        self.oauth_states: dict[str, dict[str, Any]] = {}
        self.shops = {str(shop.get("shop_id")): shop for shop in self.store.load_lazada_shops() if shop.get("shop_id")}
        self.access_token = os.getenv("DASHBOARD_ACCESS_TOKEN", "").strip()
        self.dashboard_cache_ttl = max(15, int(os.getenv("DASHBOARD_CACHE_TTL_SECONDS", "120")))
        self._dashboard_cache: dict[str, dict[str, Any]] = {}
        self._dashboard_locks: dict[str, asyncio.Lock] = {}
        self._api_rate_lock = asyncio.Lock()
        self._next_api_request_at = 0.0
        self.api_request_interval = max(0.0, float(os.getenv("LAZADA_REQUEST_INTERVAL_SECONDS", "0.35")))
        self.configured = bool(self.app_key and self.app_secret and self.redirect_uri)

    def require_access(self, authorization: str) -> None:
        scheme, _, supplied = (authorization or "").partition(" ")
        if not self.access_token or scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip(), self.access_token):
            raise HTTPException(401, "需要登录看板", headers={"WWW-Authenticate": "Bearer"})

    def _sign(self, path: str, params: dict[str, Any]) -> str:
        pairs = [(str(key), str(value)) for key, value in params.items() if key != "sign" and value is not None]
        pairs.sort(key=lambda item: item[0])
        base = path + "".join(key + value for key, value in pairs)
        return hmac.new(self.app_secret.encode(), base.encode(), hashlib.sha256).hexdigest().upper()

    def _api_url(self, market: str | None) -> str:
        if self.api_base:
            return self.api_base
        return self.MARKET_API_BASES.get(str(market or "").upper(), "https://api.lazada.com/rest")

    def auth_url(self, market: str) -> str:
        if not self.configured:
            raise HTTPException(503, "Lazada OAuth 尚未配置 App Key、App Secret 和 Redirect URI")
        market = str(market or "MY").upper()
        state = secrets.token_urlsafe(32)
        expires_at = int(time.time() + 600)
        self.oauth_states[state] = {"market": market, "expires_at": expires_at}
        self.store.save_lazada_state(state, market, expires_at)
        query = {"response_type": "code", "force_auth": "true", "redirect_uri": self.redirect_uri, "client_id": self.app_key, "state": state}
        return f"{self.auth_base}/oauth/authorize?{urlencode(query)}"

    async def exchange_code(self, code: str, state_market: str) -> dict[str, Any]:
        path = "/auth/token/create"
        timestamp = int(time.time() * 1000)
        params = {"app_key": self.app_key, "timestamp": timestamp, "sign_method": "sha256", "code": code}
        params["sign"] = self._sign(path, params)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True, trust_env=False) as client:
                response = await client.get(f"{self.token_base}/rest{path}", params=params)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(502, f"Lazada OAuth 换 token 失败（HTTP {exc.response.status_code}）") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Lazada OAuth 换 token 网络错误：{type(exc).__name__}") from exc
        if str(body.get("code") or "0") not in {"0", "200"} or body.get("error_code"):
            raise HTTPException(502, "Lazada OAuth 换 token 失败，请重新发起授权")
        payload = body.get("access_token_response") or body.get("data") or body
        access_token = str(payload.get("access_token") or "")
        refresh_token = str(payload.get("refresh_token") or "")
        if not access_token:
            raise HTTPException(502, "Lazada OAuth 换 token 响应无效")
        expires_at = int(time.time() + int(payload.get("expires_in") or 0))
        refresh_expires_at = int(time.time() + int(payload.get("refresh_expires_in") or 0)) if payload.get("refresh_expires_in") else None
        country_users = payload.get("country_user_info") or payload.get("country_user_infos") or []
        if not isinstance(country_users, list) or not country_users:
            country_users = [{"country": state_market, "user_id": payload.get("seller_id") or payload.get("user_id") or "default"}]
        records = []
        for item in country_users:
            market = str(item.get("country") or item.get("country_code") or state_market).upper()
            if market not in MARKETS:
                market = state_market
            user_id = str(item.get("user_id") or item.get("seller_id") or item.get("seller_id") or "default")
            shop_id = f"{market}:{user_id}"
            record = {"shop_id": shop_id, "market": market, "access_token": access_token, "refresh_token": refresh_token, "expires_at": expires_at, "refresh_expires_at": refresh_expires_at, "currency": MARKETS[market]["currency"]}
            self.shops[shop_id] = record
            self.store.save_lazada_shop(record)
            records.append({"shop_id": shop_id, "market": market, "connected": True})
        return {"connected": True, "shops": records, "expires_in": payload.get("expires_in")}

    async def _throttle(self) -> None:
        if self.api_request_interval <= 0:
            return
        async with self._api_rate_lock:
            delay = self._next_api_request_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_api_request_at = time.monotonic() + self.api_request_interval

    async def _request(self, path: str, shop: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        if int(shop.get("expires_at") or 0) <= int(time.time()) + 300:
            raise HTTPException(424, "Lazada 店铺授权已过期，请重新连接")
        base_params = {"app_key": self.app_key, "timestamp": int(time.time() * 1000), "access_token": shop.get("access_token", ""), "sign_method": "sha256", **payload}
        base_params["sign"] = self._sign(path, base_params)
        for attempt in range(4):
            await self._throttle()
            try:
                async with httpx.AsyncClient(timeout=25, follow_redirects=True, trust_env=False) as client:
                    response = await client.get(f"{self._api_url(shop.get('market'))}{path}", params=base_params)
                    response.raise_for_status()
                    body = response.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429 and attempt < 3:
                    await asyncio.sleep(min(1.5 * (2 ** attempt), 10.0))
                    continue
                raise HTTPException(502, f"Lazada API 请求失败（HTTP {exc.response.status_code}）") from exc
            except httpx.HTTPError as exc:
                raise HTTPException(502, f"Lazada API 请求失败（网络错误：{type(exc).__name__}）") from exc
            if str(body.get("code") or "0") not in {"0", "200"}:
                error_text = str(body.get("message") or body.get("detail") or body.get("code") or "unknown")
                if "rate" in error_text.lower() and attempt < 3:
                    await asyncio.sleep(min(1.5 * (2 ** attempt), 10.0))
                    continue
                raise HTTPException(502, f"Lazada API 错误: {error_text}")
            return body.get("data") or body
        raise HTTPException(503, "Lazada API 请求频率过高")

    @staticmethod
    def _iso(dt: datetime) -> str:
        return dt.astimezone(SHOP_TIMEZONE).isoformat(timespec="seconds")

    async def _shop_dashboard(self, shop: dict[str, Any], start_date: date, end_date: date) -> dict[str, Any]:
        daily = {(start_date + timedelta(days=index)).isoformat(): {"date": (start_date + timedelta(days=index)).isoformat(), "market": shop["market"], "sales": 0.0, "orders": 0, "units": 0, "buyers": None, "average_order_value": None, "basket_size": None, "ad_spend": None, "ad_ratio": None, "roi": None} for index in range((end_date - start_date).days + 1)}
        orders: list[dict[str, Any]] = []
        offset = 0
        created_after = self._iso(datetime.combine(start_date, datetime.min.time(), tzinfo=SHOP_TIMEZONE))
        created_before = self._iso(datetime.combine(end_date, datetime.max.time(), tzinfo=SHOP_TIMEZONE))
        while True:
            page = await self._request("/orders/get", shop, {"created_after": created_after, "created_before": created_before, "sort_by": "created_at", "sort_direction": "ASC", "offset": offset, "limit": 100})
            page_orders = (page.get("orders") or []) if isinstance(page, dict) else []
            orders.extend(item for item in page_orders if isinstance(item, dict))
            if len(page_orders) < 100 or offset + len(page_orders) >= int(page.get("countTotal") or 0):
                break
            offset += len(page_orders)
        for order in orders:
            order_date = str(order.get("created_at") or "")[:10]
            if order_date not in daily or str(order.get("statuses") or "").lower() in {"canceled", "failed"}:
                continue
            day = daily[order_date]
            day["sales"] += float(order.get("price") or 0)
            day["orders"] += 1
            day["units"] += int(order.get("items_count") or 0)
        sales = round(sum(item["sales"] for item in daily.values()), 2)
        orders_count = sum(item["orders"] for item in daily.values())
        units = sum(item["units"] for item in daily.values())
        return {
            "sales": sales,
            "orders": orders_count,
            "units": units,
            "buyers": None,
            "average_order_value": round(sales / orders_count, 2) if orders_count else None,
            "basket_size": round(units / orders_count, 2) if orders_count else None,
            "currency": shop.get("currency", "LOCAL"),
            "ad_spend": None,
            "ad_ratio": None,
            "roi": None,
            "ads_status": "permission_required",
            "business_advisor_status": "not_available",
            "sales_source": "Lazada GetOrders API (fallback)",
            "sales_scope": "orders_api_created_orders",
            "sales_scope_note": "当前使用 Lazada GetOrders 订单明细聚合；这不是 Seller Center「数据洞察 → 生意参谋」口径。",
            "daily": list(daily.values()),
        }

    async def dashboard(self, start_date: date, end_date: date, market: str | None) -> dict[str, Any]:
        cache_key = f"{start_date.isoformat()}:{end_date.isoformat()}:{market or 'ALL'}"
        cached = self._dashboard_cache.get(cache_key)
        if cached and time.monotonic() - cached["stored_at"] <= self.dashboard_cache_ttl:
            return copy.deepcopy(cached["value"])
        shops = [shop for shop in self.shops.values() if not market or shop.get("market") == market]
        if not shops:
            result = {"platform": "lazada", "configured": self.configured, "mode": "live", "generated_at": datetime.now(timezone.utc).isoformat(), "period": {"start": start_date.isoformat(), "end": end_date.isoformat()}, "currency": MARKETS[market]["currency"] if market in MARKETS else "MIXED", "currency_note": "当前筛选条件下没有已授权的 Lazada 店铺。", "totals": {"sales": 0, "orders": 0, "units": 0, "buyers": None, "average_order_value": None, "basket_size": None, "ad_spend": None, "ad_ratio": None, "roi": None}, "markets": [], "daily": [], "ads_status": "permission_required", "business_advisor_status": "not_available", "sources": {"orders": "Lazada GetOrders API (fallback)", "business_advisor": "未提供公开 API", "ads": "待配置 Lazada Advertising API"}, "sales_scope": "orders_api_created_orders", "sales_scope_note": "Lazada Open Platform 当前未公开 Seller Center「数据洞察 → 生意参谋」接口；页面指标暂不能通过官方 API 精确复刻。"}
            return result
        rows = [await self._shop_dashboard(shop, start_date, end_date) for shop in shops]
        totals = {key: sum(row.get(key) or 0 for row in rows) for key in ("sales", "orders", "units")}
        totals.update({"buyers": None, "average_order_value": round(totals["sales"] / totals["orders"], 2) if totals["orders"] else None, "basket_size": round(totals["units"] / totals["orders"], 2) if totals["orders"] else None})
        grouped: dict[str, dict[str, Any]] = {}
        for shop, row in zip(shops, rows):
            current = grouped.setdefault(shop["market"], {"code": shop["market"], "name": MARKETS[shop["market"]]["name"], "currency": row["currency"], "sales": 0.0, "orders": 0, "units": 0, "buyers": None, "average_order_value": None, "basket_size": None, "ad_spend": None, "ad_ratio": None, "roi": None, "ads_status": "permission_required", "business_advisor_status": "not_available", "status": "ready"})
            for key in ("sales", "orders", "units"):
                current[key] += row[key]
            current["average_order_value"] = round(current["sales"] / current["orders"], 2) if current["orders"] else None
            current["basket_size"] = round(current["units"] / current["orders"], 2) if current["orders"] else None
        daily = {}
        for row in rows:
            for item in row["daily"]:
                current = daily.setdefault(item["date"], {"date": item["date"], "market": market or "ALL", "sales": 0.0, "orders": 0, "units": 0, "buyers": None, "average_order_value": None, "basket_size": None, "ad_spend": None, "ad_ratio": None, "roi": None})
                for key in ("sales", "orders", "units"):
                    current[key] += item[key]
                current["average_order_value"] = round(current["sales"] / current["orders"], 2) if current["orders"] else None
                current["basket_size"] = round(current["units"] / current["orders"], 2) if current["orders"] else None
        totals.update({"ad_spend": None, "ad_ratio": None, "roi": None})
        result = {"platform": "lazada", "configured": self.configured, "mode": "live", "generated_at": datetime.now(timezone.utc).isoformat(), "period": {"start": start_date.isoformat(), "end": end_date.isoformat()}, "currency": MARKETS[market]["currency"] if market in MARKETS else "MIXED", "currency_note": "销售额按 Lazada 店铺本币展示；全部站点不跨币种合计。", "totals": totals, "markets": list(grouped.values()), "daily": list(daily.values()), "ads_status": "permission_required", "business_advisor_status": "not_available", "sources": {"orders": "Lazada GetOrders API (fallback)", "business_advisor": "未提供公开 API", "ads": "待配置 Lazada Advertising API"}, "sales_scope": "orders_api_created_orders", "sales_scope_note": "销售额来自 Lazada GetOrders 的订单 price，取消和失败订单已排除；这不是 Seller Center「数据洞察 → 生意参谋」口径。买家数、购物篮子数等页面指标暂未提供官方 Open Platform API。"}
        self._dashboard_cache[cache_key] = {"stored_at": time.monotonic(), "value": copy.deepcopy(result)}
        return result


ADAPTERS: dict[str, MarketplaceAdapter] = {"shopee": ShopeeAdapter(), "lazada": LazadaAdapter()}


def _day_rows(start_date: date, end_date: date, market: str | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    days = (end_date - start_date).days + 1
    for index in range(days):
        current = start_date + timedelta(days=index)
        pulse = 0.9 + ((index * 7) % 9) / 20
        sales = round((18200 + (index % 5) * 1730) * pulse, 2)
        orders = round(68 + (index % 6) * 11)
        ad_spend = round(sales * (0.078 + (index % 3) * 0.009), 2)
        rows.append({
            "date": current.isoformat(),
            "market": market or "ALL",
            "sales": sales,
            "orders": orders,
            "units": round(orders * 1.34),
            "ad_spend": ad_spend,
            "ad_ratio": round(ad_spend / sales * 100, 2),
            "roi": round(sales / ad_spend, 2),
        })
    return rows


def demo_dashboard(start_date: date, end_date: date, market: str | None, configured: bool) -> dict[str, Any]:
    daily = _day_rows(start_date, end_date, market)
    markets = [market] if market else list(MARKETS)
    multiplier = len(markets) / 4
    sales = round(sum(row["sales"] for row in daily) * multiplier, 2)
    orders = round(sum(row["orders"] for row in daily) * multiplier)
    units = round(sum(row["units"] for row in daily) * multiplier)
    ad_spend = round(sum(row["ad_spend"] for row in daily) * multiplier, 2)
    return {
        "platform": "shopee",
        "configured": configured,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "currency": "USD*" if not market else MARKETS[market]["currency"],
        "currency_note": "演示数据按统一展示币种模拟；真实接入后按站点本币及汇率配置返回。",
        "totals": {"sales": sales, "orders": orders, "units": units, "ad_spend": ad_spend, "ad_ratio": round(ad_spend / sales * 100, 2), "roi": round(sales / ad_spend, 2)},
        "markets": [
            {"code": code, "name": MARKETS[code]["name"], "currency": MARKETS[code]["currency"], "sales": round(sales / len(markets) * (1 + i * 0.08), 2), "orders": round(orders / len(markets) * (1 + i * 0.05)), "ad_spend": round(ad_spend / len(markets) * (1 + i * 0.06), 2), "status": "ready" if configured else "demo"}
            for i, code in enumerate(markets)
        ],
        "daily": daily,
        "sources": {"orders": "Shopee Order API", "ads": "Shopee Ads Daily Performance API"},
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "marketplace-bi-dashboard"}


@app.get("/api/platforms")
async def platforms(authorization: str = Header(default="")) -> dict[str, Any]:
    adapter = ADAPTERS["shopee"]
    adapter.require_access(authorization)
    lazada = ADAPTERS["lazada"]
    return {"platforms": [{"id": "shopee", "name": "Shopee", "status": "connected" if adapter.configured else "demo", "oauth_configured": adapter.configured, "shops": len(adapter.shops) + len(adapter.oauth_shops)}, {"id": "lazada", "name": "Lazada", "status": "connected" if lazada.configured else "planned", "oauth_configured": lazada.configured, "shops": len(lazada.shops)}, {"id": "tiktok_shop", "name": "TikTok Shop", "status": "planned", "oauth_configured": False, "shops": 0}]}


@app.get("/api/shopee/auth/start")
async def shopee_auth_start(market: str = Query("MY"), authorization: str = Header(default="")) -> dict[str, Any]:
    ADAPTERS["shopee"].require_access(authorization)
    market = market.upper()
    # A main-account authorization returns every shop granted to the app. Keep
    # ALL as an explicit state so the UI and callback do not imply MY-only
    # authorization when the user is reconnecting the approved live app.
    if market != "ALL" and market not in MARKETS:
        raise HTTPException(400, "unsupported_market")
    return {"authorization_url": ADAPTERS["shopee"].auth_url(market), "market": market}


@app.get("/api/shopee/status")
async def shopee_status(authorization: str = Header(default="")) -> dict[str, Any]:
    adapter = ADAPTERS["shopee"]
    adapter.require_access(authorization)
    shops = [{
        "shop_id": shop_id,
        "market": shop.get("market"),
        "connected": bool(shop.get("access_token")),
        "business_insights_ready": bool(shop.get("principal_id")),
    } for shop_id, shop in {**adapter.shops, **adapter.oauth_shops}.items()]
    return {
        "configured": adapter.configured,
        "business_insights_ready": bool(shops) and all(shop["business_insights_ready"] for shop in shops),
        "shops": shops,
    }


@app.get("/api/lazada/auth/start")
async def lazada_auth_start(market: str = Query("MY"), authorization: str = Header(default="")) -> dict[str, Any]:
    adapter = ADAPTERS["lazada"]
    adapter.require_access(authorization)
    market = market.upper()
    if market not in MARKETS:
        raise HTTPException(400, "unsupported_market")
    return {"authorization_url": adapter.auth_url(market), "market": market}


@app.get("/api/lazada/status")
async def lazada_status(authorization: str = Header(default="")) -> dict[str, Any]:
    adapter = ADAPTERS["lazada"]
    adapter.require_access(authorization)
    return {"configured": adapter.configured, "shops": [{"shop_id": shop_id, "market": shop.get("market"), "connected": bool(shop.get("access_token"))} for shop_id, shop in adapter.shops.items()]}


@app.get("/api/dashboard")
async def dashboard(days: int = Query(7, ge=1, le=90), market: str | None = Query(None), platform: str = Query("shopee"), authorization: str = Header(default="")) -> dict[str, Any]:
    adapter = ADAPTERS.get(platform.lower())
    if not adapter:
        raise HTTPException(400, "unsupported_platform")
    adapter.require_access(authorization)
    if market and market not in MARKETS:
        return {"error": "unsupported_market"}
    end_date = datetime.now(SHOP_TIMEZONE).date()
    start_date = end_date - timedelta(days=days - 1)
    return await adapter.dashboard(start_date, end_date, market)


@app.get("/api/shopee/oauth/callback")
async def oauth_callback(code: str | None = None, shop_id: str | None = None, main_account_id: str | None = None, state: str | None = None, oauth_state: str | None = None) -> dict[str, Any]:
    adapter = ADAPTERS["shopee"]
    if not code or (not shop_id and not main_account_id):
        raise HTTPException(400, "Shopee OAuth callback 缺少 code 或 shop_id")

    state = state or oauth_state
    if not state:
        raise HTTPException(400, "Shopee OAuth 回调缺少 state，请从看板重新发起授权")
    memory_pending = adapter.oauth_states.pop(state, None)
    stored_pending = adapter.store.consume_state(state)
    pending = memory_pending or stored_pending

    if not pending or pending["expires_at"] < time.time():
        raise HTTPException(400, "Shopee OAuth 授权已过期或无法匹配授权请求，请重新发起连接")
    if main_account_id:
        result = await adapter.exchange_merchant_code(code, main_account_id)
    else:
        result = await adapter.exchange_code(code, shop_id, pending["market"])
    return {"ok": True, "message": "Shopee 店铺已连接。", "connection": result}


@app.get("/api/lazada/oauth/callback")
async def lazada_oauth_callback(code: str | None = None, state: str | None = None, error: str | None = None) -> dict[str, Any]:
    adapter = ADAPTERS["lazada"]
    if error:
        raise HTTPException(400, "Lazada OAuth 授权被取消")
    if not code:
        raise HTTPException(400, "Lazada OAuth 回调缺少 code")
    if not state:
        raise HTTPException(400, "Lazada OAuth 回调缺少 state，请从看板重新发起授权")
    memory_pending = adapter.oauth_states.pop(state, None)
    stored_pending = adapter.store.consume_lazada_state(state)
    pending = memory_pending or stored_pending
    if not pending or int(pending["expires_at"]) < int(time.time()):
        raise HTTPException(400, "Lazada OAuth 授权已过期，请重新发起授权")
    result = await adapter.exchange_code(code, str(pending["market"]))
    return {"ok": True, "message": "Lazada 店铺已连接。", "connection": result}
