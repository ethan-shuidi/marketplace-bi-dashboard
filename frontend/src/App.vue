<script setup>
import { computed, onMounted, ref } from "vue"

const loading = ref(true)
const error = ref("")
const authMessage = ref("")
const connecting = ref(false)
const days = ref(7)
const market = ref("")
const activePlatform = ref("shopee")
const data = ref(null)
const lastLoaded = ref(null)
const connection = ref(null)
const apiBase = ref("")
const accessToken = ref(localStorage.getItem("dashboard_access_token") || "")
const showAuth = ref(false)
const accessTokenInput = ref(accessToken.value)
const hoveredTrendIndex = ref(null)
let loadSequence = 0

const currencySymbols = { MYR: "RM", PHP: "₱", SGD: "S$", THB: "฿", USD: "$" }
const marketDefinitions = [
  { code: "MY", name: "马来西亚", currency: "MYR" },
  { code: "PH", name: "菲律宾", currency: "PHP" },
  { code: "SG", name: "新加坡", currency: "SGD" },
  { code: "TH", name: "泰国", currency: "THB" },
]

const money = (value) => new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(Number(value || 0))
const currencyMoney = (value, currency) => currency === "MIXED" ? "多币种" : `${currencySymbols[currency] || currency || ""} ${money(value)}`.trim()
const number = (value) => new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(Number(value || 0))
const pct = (value) => `${Number(value || 0).toFixed(1)}%`
const compactNumber = (value) => new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(Number(value || 0))
const optionalMoney = (value, currency) => value == null ? "—" : currencyMoney(value, currency)
const optionalPct = (value) => value == null ? "—" : pct(value)
const optionalRoi = (value) => value == null ? "—" : `${Number(value).toFixed(2)}x`
const platformLabel = computed(() => activePlatform.value === "lazada" ? "Lazada" : "Shopee")
const lastLabel = computed(() => lastLoaded.value ? new Date(lastLoaded.value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }) : "—")
const marketRows = computed(() => marketDefinitions.map(definition => {
  const row = (data.value?.markets || []).find(item => item.code === definition.code)
  return { ...definition, ...row, currency: row?.currency || definition.currency, available: Boolean(row) }
}))
const cards = computed(() => {
  const t = data.value?.totals || {}
  const singleCurrency = data.value?.currency && data.value.currency !== "MIXED"
  if (activePlatform.value === "lazada") {
    const advisorReady = data.value?.business_advisor_status === "ready"
    return [
      { label: "买家数", value: advisorReady && t.buyers != null ? number(t.buyers) : "未接入", detail: advisorReady ? "Business Advisor" : "Seller Center 生意参谋 API 未公开", tone: "teal" },
      { label: "订单数", value: number(t.orders), detail: "当前为 GetOrders 降级口径", tone: "blue" },
      { label: "平均订单价值", value: advisorReady && t.average_order_value != null ? currencyMoney(t.average_order_value, data.value?.currency) : "未接入", detail: advisorReady ? "Business Advisor" : "订单 API 估算不等同于生意参谋", tone: "amber" },
      { label: "售出件数", value: number(t.units), detail: "当前为订单明细聚合", tone: "violet" },
      { label: "购物篮子数", value: advisorReady && t.basket_size != null ? Number(t.basket_size).toFixed(2) : "未接入", detail: advisorReady ? "Business Advisor" : "Seller Center 生意参谋 API 未公开", tone: "blue" },
    ]
  }
  const adsReady = data.value?.ads_status === "ready" && singleCurrency
  return [
    { label: "销售额", value: currencyMoney(t.sales, data.value?.currency), detail: singleCurrency ? "当前筛选周期" : "请切换到单个站点查看", tone: "teal" },
    { label: "商品销量", value: number(t.units), detail: `${number(t.orders)} 笔订单`, tone: "blue" },
    { label: "广告花费", value: adsReady ? currencyMoney(t.ad_spend, data.value?.currency) : (singleCurrency ? "待授权" : "多币种"), detail: adsReady ? `占销售额 ${pct(t.ad_ratio)}` : (singleCurrency ? "Shopee Ads API 权限" : "请切换到单个站点查看"), tone: "amber" },
    { label: "整体 ROI", value: adsReady && t.roi != null ? `${Number(t.roi).toFixed(2)}x` : "—", detail: singleCurrency ? "销售额 / 广告花费" : "请切换到单个站点查看", tone: "violet" },
  ]
})

function niceMaximum(value) {
  if (value <= 0) return 1
  const exponent = 10 ** Math.floor(Math.log10(value))
  const fraction = value / exponent
  const niceFraction = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10
  return niceFraction * exponent
}

function smoothPath(points) {
  if (!points.length) return ""
  if (points.length === 1) return `M ${points[0].x} ${points[0].y}`
  return points.slice(1).reduce((path, point, index) => {
    const previous = points[index]
    const middleX = (previous.x + point.x) / 2
    return `${path} C ${middleX} ${previous.y}, ${middleX} ${point.y}, ${point.x} ${point.y}`
  }, `M ${points[0].x} ${points[0].y}`)
}

const trendChart = computed(() => {
  const rows = data.value?.daily || []
  const width = 720
  const height = 270
  const padding = { top: 26, right: 58, bottom: 42, left: 66 }
  const plotWidth = width - padding.left - padding.right
  const plotHeight = height - padding.top - padding.bottom
  const maxSales = niceMaximum(Math.max(...rows.map(row => Number(row.sales || 0)), 1))
  const maxAdRatio = niceMaximum(Math.max(...rows.map(row => Number(row.ad_ratio || 0)), 1))
  const x = (index) => padding.left + (rows.length <= 1 ? plotWidth / 2 : index * plotWidth / (rows.length - 1))
  const y = (value, max) => padding.top + plotHeight - (Number(value || 0) / max) * plotHeight
  const step = rows.length <= 1 ? plotWidth : plotWidth / (rows.length - 1)
  const sales = rows.map((row, index) => ({ x: x(index), y: y(row.sales, maxSales), value: Number(row.sales || 0), row }))
  const ads = rows.map((row, index) => ({ x: x(index), y: y(row.ad_ratio, maxAdRatio), value: Number(row.ad_ratio || 0), row }))
  const hoverZones = rows.map((row, index) => ({
    index,
    x: rows.length <= 1 ? padding.left : Math.max(padding.left, x(index) - step / 2),
    width: rows.length <= 1 ? plotWidth : Math.min(width - padding.right, x(index) + step / 2) - Math.max(padding.left, x(index) - step / 2),
  }))
  const salesPath = smoothPath(sales)
  const baseline = padding.top + plotHeight
  return {
    width,
    height,
    padding,
    baseline,
    rows,
    sales,
    ads,
    hoverZones,
    salesPath,
    adPath: smoothPath(ads),
    salesAreaPath: sales.length ? `${salesPath} L ${sales.at(-1).x} ${baseline} L ${sales[0].x} ${baseline} Z` : "",
    gridLines: [0, 1, 2, 3, 4].map(index => ({
      y: padding.top + index * plotHeight / 4,
      sales: maxSales * (4 - index) / 4,
      ads: maxAdRatio * (4 - index) / 4,
    })),
    labelY: height - 12,
  }
})

const hoveredTrend = computed(() => {
  const index = hoveredTrendIndex.value
  if (index == null || !trendChart.value.rows[index]) return null
  const sales = trendChart.value.sales[index]
  const ads = trendChart.value.ads[index]
  return {
    index,
    row: trendChart.value.rows[index],
    x: sales.x,
    y: Math.max(88, Math.min(190, Math.min(sales.y, ads.y) + 42)),
    sales,
    ads,
  }
})

async function loadRuntimeConfig() {
  try {
    const response = await fetch(new URL("./ideadock.runtime.json", window.location.href))
    if (response.ok) {
      const runtime = await response.json()
      apiBase.value = runtime.backend_base_url || ""
    }
  } catch {}
}

function apiUrl(path) {
  return `${apiBase.value}${path}`
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {})
  if (accessToken.value) headers.set("Authorization", `Bearer ${accessToken.value}`)
  const response = await fetch(apiUrl(path), { ...options, headers })
  if (response.status === 401) showAuth.value = true
  return response
}

function saveAccessToken() {
  accessToken.value = accessTokenInput.value.trim()
  localStorage.setItem("dashboard_access_token", accessToken.value)
  showAuth.value = false
  load()
}

async function load() {
  const sequence = ++loadSequence
  loading.value = true
  error.value = ""
  try {
    const query = new URLSearchParams({ days: String(days.value) })
    if (market.value) query.set("market", market.value)
    query.set("platform", activePlatform.value)
    const response = await request(`/api/dashboard?${query}`)
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}))
      throw new Error(payload.detail || "读取看板失败")
    }
    const result = await response.json()
    if (sequence !== loadSequence) return
    data.value = result
    lastLoaded.value = new Date().toISOString()
  } catch (err) {
    if (sequence !== loadSequence) return
    error.value = err.message
  } finally {
    if (sequence === loadSequence) loading.value = false
  }
}

async function connectMarketplace() {
  const popup = window.open("about:blank", `${activePlatform.value}-auth`, "popup,width=980,height=760")
  connecting.value = true
  authMessage.value = `正在打开 ${platformLabel.value} 授权页`
  error.value = ""
  try {
    // Authorize all Shopee shops in one flow; the selected site remains a
    // dashboard filter, not an OAuth scope. Principal ID is persisted when the
    // approved app returns it during token exchange.
    const selectedMarket = activePlatform.value === "shopee" ? "ALL" : (market.value || "MY")
    const response = await request(`/api/${activePlatform.value}/auth/start?market=${selectedMarket}`)
    const payload = await response.json().catch(() => ({}))
    if (!response.ok || !payload.authorization_url) {
      throw new Error(payload.detail || `无法发起 ${platformLabel.value} 授权`)
    }
    if (!popup) {
      window.location.assign(payload.authorization_url)
      return
    }
    popup.location.href = payload.authorization_url
    authMessage.value = market.value
      ? `已打开 ${selectedMarket} 授权页，完成授权后返回并点击刷新数据`
      : activePlatform.value === "shopee"
        ? `已打开 ${platformLabel.value} 全部站点主账号授权页，完成授权后返回并点击刷新数据`
        : `已打开 ${platformLabel.value} ${selectedMarket} 授权页，完成授权后返回并点击刷新数据`
  } catch (err) {
    if (popup) popup.close()
    error.value = err.message
    authMessage.value = ""
  } finally {
    connecting.value = false
  }
}

async function loadStatus() {
  const response = await request(`/api/${activePlatform.value}/status`)
  if (response.ok) connection.value = await response.json()
}

async function switchPlatform(platform) {
  if (activePlatform.value === platform || loading.value) return
  activePlatform.value = platform
  data.value = null
  await Promise.all([load(), loadStatus()])
}

onMounted(async () => { await loadRuntimeConfig(); load(); loadStatus() })
</script>

<template>
  <div class="app-shell">
    <div v-if="showAuth" class="auth-backdrop">
      <form class="auth-dialog" @submit.prevent="saveAccessToken">
        <p class="eyebrow">INTERNAL ACCESS</p>
        <h2>登录看板</h2>
        <p>请输入企业内部看板访问令牌。令牌只保存在当前浏览器中。</p>
        <input v-model="accessTokenInput" type="password" autocomplete="off" placeholder="Dashboard access token" autofocus>
        <button type="submit">继续查看</button>
      </form>
    </div>
    <header class="topbar">
      <div class="brand"><div class="brand-mark"><span></span><span></span><span></span></div><div><strong>Market Pulse</strong><small>多平台经营看板</small></div></div>
      <div class="platform-tabs"><button :class="{ active: activePlatform === 'shopee' }" @click="switchPlatform('shopee')">Shopee</button><button :class="{ active: activePlatform === 'lazada' }" @click="switchPlatform('lazada')">Lazada</button><button disabled>TikTok Shop · 即将接入</button></div>
      <div class="actions"><button class="connect" :disabled="connecting" :title="`管理员操作：重新授权 ${platformLabel} 店铺`" @click="connectMarketplace"><span :class="{ spin: connecting }">↗</span> {{ connecting ? "打开授权中" : `连接 ${platformLabel} 店铺` }}</button><button class="refresh" :disabled="loading" @click="load"><span :class="{ spin: loading }">↻</span> 刷新数据</button></div>
    </header>

    <main class="page">
      <section class="hero"><div><p class="eyebrow">运营总览 / LIVE VIEW</p><h1>跨境销售脉搏</h1><p class="subtitle">把不同平台的销售与投放数据放进同一张可解释的经营视图。</p></div><div class="live-status"><i></i><span>{{ loading ? "正在拉取" : "已连接" }}</span><small>最近更新 {{ lastLabel }}</small></div></section>

      <section class="toolbar"><label>平台<select :value="activePlatform" :disabled="loading" @change="switchPlatform($event.target.value)"><option value="shopee">Shopee</option><option value="lazada">Lazada</option></select></label><label>站点<select v-model="market" :disabled="loading" @change="load"><option value="">全部站点</option><option value="MY">MY · 马来西亚</option><option value="PH">PH · 菲律宾</option><option value="SG">SG · 新加坡</option><option value="TH">TH · 泰国</option></select></label><div class="periods"><button v-for="item in [7,14,30]" :key="item" :disabled="loading" :class="{ selected: days === item }" @click="days = item; load()">近 {{ item }} 天</button></div></section>

      <p v-if="error" class="error">{{ error }}</p>
      <p v-if="authMessage" class="auth-message" role="status">{{ authMessage }}</p>
      <section v-if="loading && !data" class="loading">正在读取平台数据<span></span></section>
      <div v-else class="dashboard-content" :class="{ 'is-loading': loading }" :aria-busy="loading">
        <div v-if="loading" class="loading-overlay" role="status"><span></span><b>正在切换站点并拉取实时数据</b></div>
        <section v-if="!market" class="panel market-table-panel"><div class="panel-head"><div><p class="eyebrow">ALL MARKETS</p><h2>全部站点经营表现</h2></div><span class="unit">{{ data?.period.start }} — {{ data?.period.end }} · 各站点本币</span></div><div class="market-table-wrap"><table class="market-table"><thead><tr v-if="activePlatform === 'lazada'"><th>国家</th><th>买家数</th><th>订单数</th><th>平均订单价值</th><th>售出件数</th><th>购物篮子数</th></tr><tr v-else><th>国家</th><th>销量</th><th>销售额</th><th>广告花费</th><th>广告占比</th><th>整体 ROI</th></tr></thead><tbody><tr v-for="row in marketRows" :key="row.code"><td><b>{{ row.code }}</b><span>{{ row.name }}</span></td><template v-if="activePlatform === 'lazada'"><td>{{ row.buyers == null ? '未接入' : number(row.buyers) }}</td><td>{{ number(row.orders) }}</td><td class="money-cell">{{ row.average_order_value == null ? '未接入' : currencyMoney(row.average_order_value, row.currency) }}</td><td>{{ number(row.units) }}</td><td>{{ row.basket_size == null ? '未接入' : Number(row.basket_size).toFixed(2) }}</td></template><template v-else><td>{{ number(row.units) }}</td><td class="money-cell">{{ optionalMoney(row.sales, row.currency) }}</td><td class="money-cell">{{ optionalMoney(row.ad_spend, row.currency) }}</td><td>{{ optionalPct(row.ad_ratio) }}</td><td>{{ optionalRoi(row.roi) }}</td></template></tr></tbody></table></div><p class="table-note">{{ activePlatform === 'lazada' ? 'Lazada 订单数与售出件数来自 GetOrders；买家数和购物篮子数等待 Seller Center 生意参谋官方 API。' : '金额按各站点本币展示，不进行跨币种合计。' }}</p></section>
        <section v-if="market" class="metric-grid"><article v-for="card in cards" :key="card.label" class="metric" :class="card.tone"><span>{{ card.label }}</span><strong>{{ card.value }}</strong><small>{{ card.detail }}</small></article></section>

        <section v-if="market" class="content-grid single-market"><article class="panel trend-panel"><div class="panel-head"><div><p class="eyebrow">TREND</p><h2>销售与投放趋势</h2></div><span class="unit">{{ data?.currency }} · {{ data?.period.start }} — {{ data?.period.end }}</span></div><div v-if="trendChart.rows.length" class="chart" @mouseleave="hoveredTrendIndex = null"><svg :viewBox="`0 0 ${trendChart.width} ${trendChart.height}`" role="img" aria-label="销售额与广告占比趋势折线图"><defs><linearGradient id="sales-area-gradient" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#17927e" stop-opacity=".2"/><stop offset="100%" stop-color="#17927e" stop-opacity="0"/></linearGradient></defs><g class="chart-grid"><g v-for="line in trendChart.gridLines" :key="line.y"><line :x1="trendChart.padding.left" :x2="trendChart.width - trendChart.padding.right" :y1="line.y" :y2="line.y"/><text class="axis-label axis-left" :x="trendChart.padding.left - 10" :y="line.y + 4" text-anchor="end">{{ compactNumber(line.sales) }}</text><text v-if="activePlatform !== 'lazada'" class="axis-label axis-right" :x="trendChart.width - trendChart.padding.right + 10" :y="line.y + 4">{{ pct(line.ads) }}</text></g></g><text class="axis-title" :x="trendChart.padding.left" y="14">销售额（{{ data?.currency }}）</text><text v-if="activePlatform !== 'lazada'" class="axis-title axis-title-right" :x="trendChart.width - trendChart.padding.right" y="14" text-anchor="end">广告占比</text><path class="sales-area" :d="trendChart.salesAreaPath"/><path class="trend-line sales-line" :d="trendChart.salesPath"/><path v-if="activePlatform !== 'lazada'" class="trend-line ad-line" :d="trendChart.adPath"/><line v-if="hoveredTrend" class="hover-line" :x1="hoveredTrend.x" :x2="hoveredTrend.x" :y1="trendChart.padding.top" :y2="trendChart.baseline"/><g class="sales-points"><circle v-for="(point, index) in trendChart.sales" :key="`sales-${point.row.date}`" :class="{ active: hoveredTrendIndex === index }" :cx="point.x" :cy="point.y" r="4"/></g><g v-if="activePlatform !== 'lazada'" class="ad-points"><circle v-for="(point, index) in trendChart.ads" :key="`ads-${point.row.date}`" :class="{ active: hoveredTrendIndex === index }" :cx="point.x" :cy="point.y" r="4"/></g><text v-for="point in trendChart.sales" :key="`label-${point.row.date}`" class="chart-label" :x="point.x" :y="trendChart.labelY" text-anchor="middle">{{ point.row.date.slice(5) }}</text><g class="hover-zones"><rect v-for="zone in trendChart.hoverZones" :key="zone.index" :x="zone.x" :y="trendChart.padding.top" :width="zone.width" :height="trendChart.baseline - trendChart.padding.top" @mouseenter="hoveredTrendIndex = zone.index"/></g></svg><div v-if="hoveredTrend" class="chart-tooltip" :class="{ first: hoveredTrend.index === 0, last: hoveredTrend.index === trendChart.rows.length - 1 }" :style="{ left: `${hoveredTrend.x / trendChart.width * 100}%`, top: `${hoveredTrend.y / trendChart.height * 100}%` }"><b>{{ hoveredTrend.row.date.slice(5) }}</b><span><i class="teal-dot"></i>销售额<strong>{{ currencyMoney(hoveredTrend.sales.value, data?.currency) }}</strong></span><span v-if="activePlatform !== 'lazada'"><i class="amber-dot"></i>广告占比<strong>{{ pct(hoveredTrend.ads.value) }}</strong></span></div></div><div v-else class="chart-empty">当前周期暂无趋势数据</div><div class="legend"><span><i class="teal-dot"></i>销售额</span><span v-if="activePlatform !== 'lazada'"><i class="amber-dot"></i>广告占比</span></div></article></section>

        <section v-if="market" class="panel source-panel"><div class="panel-head"><div><p class="eyebrow">DATA CONTRACT</p><h2>平台接入状态</h2></div><span class="unit">为后续平台预留统一口径</span></div><div class="source-grid"><div><b>{{ platformLabel }}</b><span :class="{ ready: data?.ads_status === 'ready' || data?.business_advisor_status === 'ready' }">{{ activePlatform === "lazada" ? (data?.business_advisor_status === "ready" ? "Business Advisor 已接入" : "订单已接入 · 生意参谋 API 未公开") : (data?.ads_status === "ready" ? "订单 + 广告已接入" : `订单已接入 · ${platformLabel} 广告待授权`) }}</span></div><div><b>销售数据来源</b><span>{{ data?.sources?.orders || "—" }}</span></div><div><b>口径说明</b><span>{{ data?.sales_scope_note || "—" }}</span></div><div><b>统一指标</b><span>{{ activePlatform === "lazada" ? "买家数 · 订单数 · 平均订单价值 · 售出件数 · 购物篮子数" : "销售额 · 销量 · 广告花费 · ROI" }}</span></div></div></section>
      </div>
    </main>
  </div>
</template>
