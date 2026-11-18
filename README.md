# Marketplace BI Dashboard

面向 Shopee、Lazada、TikTok Shop 等平台的销售数据看板。当前已接入 Shopee 和 Lazada 订单数据，广告指标按平台权限状态降级，统一指标模型为后续平台复用。

## 目录

- `frontend/`：Vue 3 + Vite 看板
- `backend/`：Python 3.12 + FastAPI 聚合服务，兼容 IdeaDock Backend Service Hosting

## 本地运行

```bash
cd frontend
npm install
npm run dev
```

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

默认前端使用 `/api`，开发时由 Vite 代理到 `http://127.0.0.1:8000`。未配置 Shopee 凭证时，后端返回演示数据，便于先完成看板设计和接口联调。

## 生产配置

真实数据接入前配置 Shopee OAuth 和店铺 Token。密钥只放在 IdeaDock Secret 或云端环境变量，不要提交 `.env`。

```text
SHOPEE_PARTNER_ID
SHOPEE_PARTNER_KEY
SHOPEE_API_BASE
SHOPEE_REDIRECT_URI
SHOPEE_SHOPS_JSON
SHOPEE_CONFIRMED_ORDER_STATUSES
```

订单指标按 Shopee 商业分析中“已确认订单”的口径汇总：使用店铺 GMT+08
自然日，排除未付款和取消订单，并对订单列表分页、详情分批处理。确认状态默认包含
`READY_TO_SHIP,PROCESSED,SHIPPED,TO_CONFIRM_RECEIVE,COMPLETED`；如 Shopee 站点状态定义
有差异，可通过 `SHOPEE_CONFIRMED_ORDER_STATUSES` 覆盖（逗号分隔）。

Sandbox V2 测试应用使用 `SHOPEE_API_BASE=https://openplatform.sandbox.test-stable.shopee.sg`。
本项目的测试凭证在旧测试域名 `partner.test-stable.shopeemobile.com` 返回 `error_sign`，
相同凭证和 HMAC-SHA256 签名在 Sandbox V2 域名返回 302 授权跳转。
正式环境使用 `https://partner.shopeemobile.com`，并配套正式凭证。
官方说明：https://open.shopee.com/developer-guide/644 。修改运行配置后需重新部署。

`SHOPEE_SHOPS_JSON` 的结构预留为：

```json
[{"platform":"shopee","market":"MY","shop_id":"...","access_token":"...","refresh_token":"...","currency":"MYR"}]
```

## Lazada 接入

在 IdeaDock 环境变量中配置以下普通变量和 Secret（不要提交到代码库）：

```text
LAZADA_APP_KEY
LAZADA_APP_SECRET       # Secret
LAZADA_REDIRECT_URI
LAZADA_AUTH_BASE        # 可选，默认 https://auth.lazada.com
LAZADA_TOKEN_BASE       # 可选，默认使用 LAZADA_AUTH_BASE
LAZADA_API_BASE         # 可选；不设置时按站点选择官方 API 域名
```

回调地址使用 `LAZADA_REDIRECT_URI`，并在 Lazada App Console 中登记。看板的“连接 Lazada 店铺”按钮按站点发起 OAuth，授权完成后 token 会写入 IdeaDock MySQL。Lazada 是分站点授权：MY/PH/SG/TH 需要分别授权；Seller In-house 应用不支持 Crossborder。

看板 Lazada 指标与 Shopee 对齐：销售额、商品销量、广告花费、整体 ROI。销售额和销量来自 `GetOrders`，已排除未付款、取消、失败、退货和丢失订单；广告花费来自 Lazada Sponsored Max（SMAX）报表接口 `sponsor/solution/smax/report/getReportOverviewMetric`。启用广告数据需要：

- 在 App Console 为应用申请/开通 Sponsored Solutions / Sponsored Max API 权限；
- 开通后重新授权店铺（token 权限在授权时快照）；
- 店铺本身有 Sponsored Discovery 广告账户。

如果权限未开通或没有广告账户，接口返回 `ads_status=permission_required` 或 `not_available`，前端显示“待授权”或“未接入”，不会把广告花费伪造成 0 以外的值。

Lazada access token 在 Testing 状态约 1 天有效，Online 状态为 30 天、refresh token 180 天。后端会在过期前调用 `/auth/token/refresh` 自动刷新；正式使用建议在 App Console 提交 Apply Online，避免每天重新授权。

Lazada Seller Center 的“数据洞察 → 生意参谋”指标（买家数、平均订单价值、购物篮子数）目前没有公开 API，因此看板不再展示这些指标。

Lazada API 路由：`/api/lazada/auth/start`、`/api/lazada/oauth/callback`、`/api/lazada/status`；统一看板通过 `/api/dashboard?platform=lazada` 读取数据，原有 `/api/dashboard` 仍默认读取 Shopee。
