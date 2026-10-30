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

回调地址使用 `LAZADA_REDIRECT_URI`，并在 Lazada App Console 中登记。看板的“连接 Lazada 店铺”按钮按站点发起 OAuth，授权完成后 token 会写入 IdeaDock MySQL。当前销售额、订单数和售出件数来自 `GetOrders`，属于订单明细降级口径；平均订单价值和购物篮子数仅由该口径估算，买家数不会伪造为订单数。

Lazada Seller Center 的“数据洞察 → 生意参谋”指标（买家数、平均订单价值、售出件数、购物篮子数及其趋势）目前没有出现在 Lazada Open Platform 的公开 API 目录中。公开目录提供的 Sponsored Solutions 报表只覆盖广告指标，不能替代生意参谋。接口返回 `business_advisor_status=not_available`、`sales_scope=orders_api_created_orders` 和 `sales_scope_note`，前端会明确显示降级口径；后续如果 Lazada 为该页面开放正式 API，再替换适配器即可。

Lazada API 路由：`/api/lazada/auth/start`、`/api/lazada/oauth/callback`、`/api/lazada/status`；统一看板通过 `/api/dashboard?platform=lazada` 读取数据，原有 `/api/dashboard` 仍默认读取 Shopee。
