# tradeBot

用 **TradingView** 策略訊號驅動的加密貨幣自動交易機器人（Binance 現貨 + USDⓈ-M 永續合約），附帶一個網頁儀表板，可以看到：

- **K 線圖 + 買賣點**：TradingView Lightweight Charts，標出每一筆成交（開多／平多／開空／平空，平倉標註已實現損益），以及持倉的開倉價、停損、停利、強平價線
- **倉位**：持倉中與歷史部位、未實現損益，可以一鍵平倉
- **錢包**：總權益曲線（每分鐘快照）、區間報酬與最大回撤、資產明細
- **操作軌跡**：所有事件的時間軸，點開可以看「訊號 → 訂單 → 成交」的完整鏈路
- **設定**：下單大小、槓桿、停損停利、每日虧損上限（Kill switch）、交易對白名單、啟用／停用、緊急全部平倉

```
TradingView (Pine Script → Alert)
      │ HTTPS POST /webhook/tradingview
      ▼
FastAPI ── 驗證 secret、去重、立刻回 200 ──► asyncio 佇列
      │                                       │
      │                    Executor：風控 → 算數量 → ccxt 下單 → 成交入帳 → 部位帳本
      │                    Workers：餘額快照(60s)、部位/標記價格/軟體停損(15s)、未成交訂單追蹤(20s)
      ▼
SQLite / PostgreSQL ◄── REST API + WebSocket 即時推播 ──► React 儀表板
```

## 執行模式

| `EXECUTION_MODE` | `TESTNET` | 說明 |
|---|---|---|
| `paper`（預設） | — | 本地模擬撮合，使用 Binance 正式環境的即時價格，**不需要 API key**。現貨、合約各有 `PAPER_START_USDT` 起始資金 |
| `binance` | `true` | 送單到 **Binance Demo Trading**（demo.binance.com）。到 [API 管理](https://demo.binance.com/en/my/settings/api-management) 建立 key，同一組 key 可以用在現貨和合約 |
| `binance` | `false` | ⚠ **正式帳戶，真金白銀** |

> Binance 舊的 futures testnet（testnet.binancefuture.com）已經停用，ccxt 也不再支援，所以這裡改用 Demo Trading。

## 本機開發

一鍵啟動前後端（會自動安裝缺少的套件、建立 `.env`、打開瀏覽器，Ctrl+C 會同時關閉兩個服務）：

```bash
./dev.sh             # 加 --no-open 就不會自動開瀏覽器
```

預設登入帳號 `admin`、密碼 `change-me`（在 `.env` 的 `DASHBOARD_USER` / `DASHBOARD_PASSWORD` 修改）。

或者手動分別啟動：

```bash
cp .env.example .env          # 至少改 WEBHOOK_SECRET、DASHBOARD_PASSWORD、JWT_SECRET

# 後端（Python 3.11+，使用 uv）
cd backend
uv sync
uv run uvicorn app.main:app --reload --port 8000

# 前端（另開一個終端機）
cd frontend
npm install
npm run dev                   # http://localhost:5173 ，/api、/ws 會代理到 :8000
```

送一個測試訊號（不經過 TradingView）：

```bash
curl -X POST localhost:8000/webhook/tradingview -d '{
  "secret":"change-me","strategy":"test","symbol":"BTCUSDT.P","market":"futures",
  "action":"buy","position":"long","time":"2026-01-01T00:00:00Z"}'
```

測試：`cd backend && uv run pytest`

## 串接 TradingView

1. 把 `pine/example_strategy.pine` 貼到 Pine Editor，加到圖表（或使用你自己的策略）
2. 讓後端可以從外部連線（TradingView 只會送到 **80/443 port**，而且 Webhook 需要付費方案）：
   - 開發時：`ngrok http 8000` 或 `cloudflared tunnel --url http://localhost:8000`
   - 正式環境：見下面的部署章節
3. 建立警示：條件選你的策略 →「Order fills only」→ 通知分頁勾選 **Webhook URL** 並填入 `https://<你的網域>/webhook/tradingview`
4. **Message** 貼上下面的模板（`secret` 要跟 `.env` 的 `WEBHOOK_SECRET` 一樣；現貨把 `market` 改成 `spot`）：

```json
{
  "secret": "change-me",
  "strategy": "ema_cross",
  "symbol": "{{ticker}}",
  "market": "futures",
  "action": "{{strategy.order.action}}",
  "position": "{{strategy.market_position}}",
  "price": {{close}},
  "time": "{{timenow}}",
  "id": "{{strategy.order.id}}"
}
```

### 訊號怎麼被解讀

後端把每個訊號轉成「**目標部位**」（long / short / flat），再跟目前部位比較：

| action | position | 目標 |
|---|---|---|
| `buy` / `sell` | `long` / `short` / `flat` | 等於 `position`（建議用法：直接跟著策略的部位走） |
| `buy` | （未提供） | long |
| `sell` | （未提供） | flat |
| `long` / `short` | — | long / short |
| `close` / `exit` / `flat` | — | flat |

- 目前部位和目標相同 → 略過（不會重複開倉）
- 反向（多 → 空）→ 先 reduceOnly 平倉，再開新倉
- 現貨不能做空，`short` 會被拒絕
- `symbol` 可以是 `BTCUSDT`、`BTCUSDT.P`、`BINANCE:BTCUSDT.P` 或 `BTC/USDT`
- 用 `strategy|symbol|action|position|id|time` 去重，TradingView 重送也不會重複下單

### 下單與風控

- **數量**：每筆固定 N USDT 保證金，或是可用餘額的 N%；合約的實際部位 = 保證金 × 槓桿。數量依交易所精度無條件捨去，低於最小下單金額時拒絕
- **開倉前檢查**：Bot 是否啟用、白名單、最大持倉數、每日虧損上限、可用餘額
- **Kill switch**：今日（UTC）已實現損益扣掉手續費後達到上限，就自動停用 Bot 並通知
- **停損／停利**：Binance 合約會嘗試在交易所掛條件單；失敗時，以及現貨／paper 模式，改由背景程序每 15 秒檢查標記價格（軟體停損）
- **交易所自行平倉**（原生停損、強平、在 Binance 手動平倉）會被部位同步偵測到，並以標記價格補記一筆成交
- 停用 Bot 只會擋 TradingView 訊號；手動平倉、緊急全部平倉、停損停利不受影響

## 部署（VPS + Docker）

```bash
cp .env.example .env    # 填好 key 與密碼
SITE_ADDRESS=bot.example.com docker compose up -d --build
```

- `web`（Caddy）會自動申請 HTTPS 憑證，並提供前端與反向代理 `/api`、`/ws`、`/webhook`
- `backend` 使用 PostgreSQL（`postgres` 服務）
- 資料表在啟動時自動建立（`create_all`）；之後如果要修改 schema，建議引入 Alembic

## 安全建議

- Binance API key **只開「讀取 + 交易」，不要開提領**，並設定 IP 白名單（VPS 的 IP）
- `.env` 已列在 `.gitignore`，不要 commit
- 一定要改 `WEBHOOK_SECRET`、`DASHBOARD_PASSWORD`、`JWT_SECRET`
- 可以設定 `WEBHOOK_IP_ALLOWLIST` 只接受 TradingView 的 IP（以 TradingView 官方文件為準，目前是 `52.89.214.238,34.212.75.30,54.218.53.128,52.32.178.7`）
- 先在 paper → Demo Trading 穩定跑一段時間，再用小額資金上正式帳戶

## 選用：Telegram 通知

在 `.env` 設定 `TELEGRAM_BOT_TOKEN` 與 `TELEGRAM_CHAT_ID`，成交和錯誤就會推播通知。

## 專案結構

```
backend/app/
  api/          webhook、auth、klines、history（訊號/訂單/成交/軌跡）、portfolio、bot、ws
  engine/       executor（下單流程）、ledger（部位帳本/損益）、sizing、risk
  exchange/     base 介面、binance（ccxt）、paper（模擬撮合）、factory
  workers/      餘額快照、部位同步與軟體停損、訂單追蹤
  services/     event_bus（操作軌跡 + WebSocket 推播）
frontend/src/
  pages/        總覽、K 線圖、倉位、錢包、操作軌跡、設定
  components/   CandleChart、EquityChart、ActivityTimeline
pine/           範例策略
```
