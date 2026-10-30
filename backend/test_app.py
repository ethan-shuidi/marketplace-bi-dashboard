import asyncio
import time
import unittest
from datetime import date
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import HTTPException

from app import LazadaAdapter, ShopeeAdapter


class LazadaAdapterContractTest(unittest.IsolatedAsyncioTestCase):
    def test_lazada_signature_is_sorted_and_uppercase(self):
        adapter = LazadaAdapter.__new__(LazadaAdapter)
        adapter.app_secret = "x" * 64
        signature = adapter._sign("/orders/get", {"timestamp": 2, "app_key": "key", "sign_method": "sha256"})
        self.assertEqual(len(signature), 64)
        self.assertEqual(signature, signature.upper())

    def test_lazada_market_api_base(self):
        adapter = LazadaAdapter.__new__(LazadaAdapter)
        adapter.api_base = ""
        self.assertEqual(adapter._api_url("MY"), "https://api.lazada.com.my/rest")

    async def test_lazada_orders_are_explicitly_marked_as_business_advisor_fallback(self):
        adapter = LazadaAdapter.__new__(LazadaAdapter)
        adapter._request = AsyncMock(return_value={
            "orders": [{
                "created_at": "2026-10-23 10:00:00",
                "price": "100.00",
                "items_count": 2,
                "statuses": "confirmed",
            }],
            "countTotal": 1,
        })
        adapter._iso = lambda value: value.isoformat()
        result = await adapter._shop_dashboard(
            {"market": "MY", "currency": "MYR", "expires_at": int(time.time()) + 3600},
            date(2026, 10, 23),
            date(2026, 10, 23),
        )
        self.assertEqual(result["sales_source"], "Lazada GetOrders API (fallback)")
        self.assertEqual(result["business_advisor_status"], "not_available")
        self.assertEqual(result["sales_scope"], "orders_api_created_orders")
        self.assertIsNone(result["buyers"])


class ShopeeDashboardAggregationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = ShopeeAdapter.__new__(ShopeeAdapter)

    def test_all_sites_authorization_uses_supported_shop_endpoint(self):
        self.adapter.configured = True
        self.adapter.partner_id = "1248207"
        self.adapter.partner_key = "partner-key"
        self.adapter.redirect_uri = "https://example.test/callback"
        self.adapter.api_base = "https://partner.shopeemobile.com"
        self.adapter.oauth_states = {}
        self.adapter.store = Mock()

        url = self.adapter.auth_url("ALL")

        self.assertIn("/api/v2/shop/auth_partner?", url)

    def test_single_site_authorization_uses_shop_endpoint(self):
        self.adapter.configured = True
        self.adapter.partner_id = "1248207"
        self.adapter.partner_key = "partner-key"
        self.adapter.redirect_uri = "https://example.test/callback"
        self.adapter.api_base = "https://partner.shopeemobile.com"
        self.adapter.oauth_states = {}
        self.adapter.store = Mock()

        url = self.adapter.auth_url("MY")

        self.assertIn("/api/v2/shop/auth_partner?", url)

    async def test_shop_dashboard_builds_daily_order_and_ad_metrics(self):
        self.adapter._request = AsyncMock(side_effect=[
            {"order_list": [{"order_sn": "A"}]},
            {"order_list": [{
                "order_sn": "A",
                "create_time": 1792713600,
                "total_amount": 120,
                "order_status": "COMPLETED",
                "item_list": [{"model_quantity_purchased": 3}],
            }]},
        ])
        self.adapter._shop_ad_spend = AsyncMock(return_value=(30.0, "ready", {"2026-10-23": 30.0}))

        result = await self.adapter._shop_dashboard(
            {"shop_id": "1", "market": "MY", "currency": "MYR"},
            date(2026, 10, 23),
            date(2026, 10, 23),
        )

        self.assertEqual(result["daily"], [{
            "date": "2026-10-23",
            "market": "MY",
            "sales": 120.0,
            "orders": 1,
            "units": 3,
            "ad_spend": 30.0,
            "ad_ratio": 25.0,
            "roi": 4.0,
        }])

    async def test_shop_dashboard_prefers_seller_center_business_insights(self):
        self.adapter._principal_request = AsyncMock(return_value={
            "details": [{
                "shop_id": 1,
                "sales": 4412,
                "orders": 5,
                "units_sold": 8,
            }],
        })
        self.adapter._shop_ad_spend = AsyncMock(return_value=(100.0, "ready", {"2026-10-21": 100.0}))

        result = await self.adapter._shop_dashboard(
            {"shop_id": "1", "main_account_id": "5279334", "market": "MY", "currency": "MYR", "expires_at": int(time.time()) + 3600},
            date(2026, 10, 21),
            date(2026, 10, 21),
        )

        self.assertEqual(result["sales"], 4412.0)
        self.assertEqual(result["orders"], 5)
        self.assertEqual(result["units"], 8)
        self.assertEqual(result["sales_source"], "Shopee Business Insights API v2 (PlacedOrder)")
        self.assertEqual(self.adapter._principal_request.await_args.args[0], "/api/v2/principal/get_shop_sales_performance_detail")
        self.assertEqual(self.adapter._principal_request.await_args.args[2]["timezone"], "GMT+8")

    async def test_shop_dashboard_paginates_and_excludes_unconfirmed_orders(self):
        self.adapter._request = AsyncMock(side_effect=[
            {"order_list": [{"order_sn": "A"}], "more": True, "next_cursor": "next"},
            {"order_list": [{"order_sn": "B"}], "more": False},
            {"order_list": [
                {"order_sn": "A", "create_time": 1792713600, "total_amount": 120,
                 "order_status": "COMPLETED", "item_list": [{"item_quantity": 2}]},
                {"order_sn": "B", "create_time": 1792713600, "total_amount": 80,
                 "order_status": "CANCELLED", "item_list": [{"item_quantity": 9}]},
            ]},
        ])
        self.adapter._shop_ad_spend = AsyncMock(return_value=(0.0, "ready", {}))

        result = await self.adapter._shop_dashboard(
            {"shop_id": "1", "market": "MY", "currency": "MYR"},
            date(2026, 10, 23),
            date(2026, 10, 23),
        )

        self.assertEqual(result["sales"], 120.0)
        self.assertEqual(result["orders"], 1)
        self.assertEqual(result["units"], 2)
        self.assertEqual(self.adapter._request.await_args_list[1].args[2]["cursor"], "next")

    def test_same_market_shops_are_merged(self):
        shops = [{"market": "MY"}, {"market": "MY"}]
        rows = [
            {"currency": "MYR", "sales": 0, "orders": 0, "units": 0, "ad_spend": 0, "ads_status": "ready"},
            {"currency": "MYR", "sales": 19041.19, "orders": 33, "units": 38, "ad_spend": 7100.57, "ads_status": "ready"},
        ]

        markets = self.adapter._aggregate_markets(shops, rows)

        self.assertEqual(len(markets), 1)
        self.assertEqual(markets[0]["code"], "MY")
        self.assertEqual(markets[0]["sales"], 19041.19)
        self.assertEqual(markets[0]["orders"], 33)
        self.assertEqual(markets[0]["ad_spend"], 7100.57)
        self.assertEqual(markets[0]["ad_ratio"], 37.29)
        self.assertEqual(markets[0]["roi"], 2.68)

    async def test_main_account_refresh_uses_shop_id_without_merchant_id(self):
        shop = {
            "shop_id": "1897892509",
            "main_account_id": "5279334",
            "access_token": "expired",
            "refresh_token": "refresh",
        }
        sibling = {
            "shop_id": "1903878055",
            "main_account_id": "5279334",
            "access_token": "sibling-access-token",
            "refresh_token": "sibling-refresh-token",
        }
        self.adapter.partner_id = "1248207"
        self.adapter.partner_key = "partner-key"
        self.adapter.api_base = "https://partner.shopeemobile.com"
        self.adapter.oauth_shops = {shop["shop_id"]: shop, sibling["shop_id"]: sibling}
        self.adapter.store = Mock()

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "expire_in": 14400,
        }
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = response

        with patch("app.httpx.AsyncClient", return_value=client) as client_factory:
            await self.adapter._refresh_access_token(shop)

        self.assertTrue(client_factory.call_args.kwargs["follow_redirects"])
        payload = client.post.await_args.kwargs["json"]
        self.assertEqual(payload["shop_id"], 1897892509)
        self.assertNotIn("main_account_id", payload)
        self.assertNotIn("merchant_id", payload)
        self.assertEqual(shop["access_token"], "new-access-token")
        self.assertEqual(sibling["access_token"], "sibling-access-token")

    async def test_dashboard_skips_expired_shop_and_returns_other_shop(self):
        expired_shop = {"shop_id": "1", "market": "MY", "currency": "MYR"}
        active_shop = {"shop_id": "2", "market": "MY", "currency": "MYR"}
        self.adapter.configured = True
        self.adapter.shops = {}
        self.adapter.oauth_shops = {"1": expired_shop, "2": active_shop}
        self.adapter._hydrate_shop_metadata = AsyncMock()
        self.adapter._shop_dashboard = AsyncMock(side_effect=[
            HTTPException(424, "Shopee 店铺授权已失效，请重新连接"),
            {
                "currency": "MYR",
                "sales": 100.0,
                "orders": 2,
                "units": 3,
                "ad_spend": 20.0,
                "ads_status": "ready",
                "daily": [],
            },
        ])

        result = await self.adapter.dashboard(
            date(2026, 10, 23),
            date(2026, 10, 23),
            "MY",
        )

        self.assertEqual(result["totals"]["sales"], 100.0)
        self.assertEqual(result["totals"]["orders"], 2)
        self.assertEqual(result["markets"][0]["sales"], 100.0)
        self.assertEqual(result["connection_warnings"], [{
            "shop_id": "1",
            "message": "Shopee 店铺授权已失效，请重新连接",
        }])

    async def test_dashboard_reuses_fresh_cached_result(self):
        self.adapter.dashboard_cache_ttl = 120
        self.adapter.dashboard_stale_ttl = 900
        self.adapter._dashboard_cache = {}
        self.adapter._dashboard_locks = {}
        self.adapter._dashboard_uncached = AsyncMock(return_value={"totals": {"sales": 100}})

        first = await self.adapter.dashboard(date(2026, 10, 17), date(2026, 10, 23), "MY")
        second = await self.adapter.dashboard(date(2026, 10, 17), date(2026, 10, 23), "MY")

        self.assertEqual(first, second)
        self.adapter._dashboard_uncached.assert_awaited_once()

    async def test_dashboard_merges_concurrent_identical_requests(self):
        self.adapter.dashboard_cache_ttl = 120
        self.adapter.dashboard_stale_ttl = 900
        self.adapter._dashboard_cache = {}
        self.adapter._dashboard_locks = {}
        started = asyncio.Event()
        release = asyncio.Event()

        async def load_dashboard(*_args):
            started.set()
            await release.wait()
            return {"totals": {"sales": 100}}

        self.adapter._dashboard_uncached = AsyncMock(side_effect=load_dashboard)
        first = asyncio.create_task(self.adapter.dashboard(date(2026, 10, 17), date(2026, 10, 23), None))
        await started.wait()
        second = asyncio.create_task(self.adapter.dashboard(date(2026, 10, 17), date(2026, 10, 23), None))
        release.set()

        self.assertEqual(await first, await second)
        self.adapter._dashboard_uncached.assert_awaited_once()

    async def test_dashboard_uses_stale_cache_when_shopee_is_rate_limited(self):
        cache_key = "2026-10-17:2026-10-23:ALL"
        self.adapter.dashboard_cache_ttl = 120
        self.adapter.dashboard_stale_ttl = 900
        self.adapter._dashboard_cache = {
            cache_key: {
                "stored_at": time.monotonic() - 121,
                "value": {"totals": {"sales": 100}},
            }
        }
        self.adapter._dashboard_locks = {}
        self.adapter._dashboard_uncached = AsyncMock(side_effect=HTTPException(503, "rate limited"))

        result = await self.adapter.dashboard(date(2026, 10, 17), date(2026, 10, 23), None)

        self.assertEqual(result["totals"]["sales"], 100)
        self.assertEqual(result["cache_status"], "stale")
        self.assertIn("限流", result["cache_note"])

    async def test_request_retries_http_429_and_succeeds(self):
        self.adapter.partner_id = "1"
        self.adapter.partner_key = "partner-key"
        self.adapter.api_base = "https://partner.example.com"
        self.adapter.api_request_interval = 0
        self.adapter._refresh_access_token = AsyncMock()
        shop = {
            "shop_id": "2",
            "access_token": "access-token",
            "expires_at": int(time.time()) + 3600,
        }
        request = httpx.Request("GET", "https://partner.example.com/api/v2/ads/performance")
        rate_limited = httpx.Response(
            429,
            headers={"Retry-After": "0"},
            json={"error": "error_rate_limit"},
            request=request,
        )
        success = httpx.Response(200, json={"response": {"ok": True}}, request=request)
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.get.side_effect = [rate_limited, success]

        with patch("app.httpx.AsyncClient", return_value=client), patch("app.asyncio.sleep", new_callable=AsyncMock) as sleep:
            result = await self.adapter._request("/api/v2/ads/performance", shop, {}, method="GET")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.get.await_count, 2)
        sleep.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
