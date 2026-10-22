# tradeBot

用 **TradingView** 策略訊號驅動的加密貨幣自動交易機器人（Binance 現貨 + USDⓈ-M 永續合約），附帶一個網頁儀表板，可以看到：

- **K 線圖 + 買賣點**：TradingView Lightweight Charts，K 線透過 Binance WebSocket 即時推送，標出每一筆成交（開多／平多／開空／平空，平倉標註已實現損益），以及持倉的開倉價、停損、停利、強平價線
- **倉位**：持倉中與歷史部位、即時標記價格與未實現損益，可以一鍵平倉
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
      │                    MarketHub：Binance 公開 WebSocket → 圖表 K 線、持倉即時價格、逐 tick 檢查停損停利
      │                    Workers：餘額快照(60s)、部位同步(15s，WebSocket 斷線時的備援)、未成交訂單追蹤(20s)
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
- **停損／停利**：Binance 合約會嘗試在交易所掛條件單；失敗時，以及現貨／paper 模式，改用軟體停損：每收到一筆即時價格（合約用標記價格，約每秒一次）就檢查一次；WebSocket 斷線時退回每 15 秒用 REST 價格檢查
- **交易所自行平倉**（原生停損、強平、在 Binance 手動平倉）會被部位同步偵測到，並以標記價格補記一筆成交
- 停用 Bot 只會擋 TradingView 訊號；手動平倉、緊急全部平倉、停損停利不受影響

## 內建策略（不需要 TradingView）

除了接 TradingView 的訊號，也可以直接在機器人裡跑 Python 策略。儀表板的「策略」頁可以選策略、交易對、K 線週期、調參數、**回測**，再啟用。

- 每根 K 棒**收盤後**計算一次，策略判斷（多 / 空 / 空手）**改變時**才送出訊號，跟 TradingView 警示走同一條下單流程（風控、Bot 開關、停損停利都適用）
- 剛啟用或修改參數時只記下目前判斷，**等下一次改變才進場**，不會一啟動就追單
- 回測用同一份策略程式：訊號在收盤產生、下一根開盤成交、扣手續費（不含滑價、資金費率、強平）
- 內建：`ema_cross`（EMA 均線交叉 + ATR 停損）、`rsi_reversion`（RSI 超買超賣反轉）、`rules`（**條件組合器**）

### 條件組合器（在網頁上組合規則，不用寫程式）

策略選「條件組合器（自訂規則）」，就能用「指標 比較 指標／數值」組出進出場條件：

- 指標：價格（開高低收、成交量）、SMA、EMA、RSI、ATR、前 N 根最高／最低價、布林通道上／中／下軌、MACD 線／訊號線／柱狀圖、固定數值
- 比較：大於、小於、大於等於、小於等於、向上穿越、向下穿越
- 四組條件：做多進場、做多出場、做空進場、做空出場，每組可選「全部成立」或「任一成立」（最多 10 個）
- 持倉中出現反方向的進場條件會直接反手；出場的那根 K 棒不會再同方向進場
- 策略內建停損／停利 %（以收盤價計算）；現貨只有做多條件
- 有範本可以一鍵套用：RSI 超賣 + 趨勢過濾、均線交叉、布林通道突破、通道突破（海龜）、MACD 動能

規則以 JSON 存在策略參數裡，伺服器只計算白名單內的指標，**不會執行任何使用者提供的程式碼**。

### 自己寫一個策略

在 `backend/app/strategies/` 新增檔案，例如 `breakout.py`：

```python
from .base import Candle, Param, Strategy, Target, register


@register
class Breakout(Strategy):
    key = "breakout"
    label = "N 根高點突破"
    description = "收盤價突破前 N 根最高價做多，跌破前 N 根最低價平倉。"
    params = [Param("length", "N", "int", 20, min=2, max=500)]

    def targets(self, candles: list[Candle]) -> list[Target]:
        n, pos, out = self.p["length"], "flat", []
        for i, c in enumerate(candles):
            if i >= n:
                hi = max(x.high for x in candles[i - n:i])
                lo = min(x.low for x in candles[i - n:i])
                if c.close > hi:
                    pos = "long"
                elif c.close < lo:
                    pos = "flat"
            out.append(pos)
        return out
```

再到 `backend/app/strategies/__init__.py` 的 import 加上 `breakout`，重新啟動（或重新部署）後就會出現在「策略」頁，參數表單會依 `params` 自動產生。規則：`targets()` 對每一根 K 棒回傳「收盤後該持有的部位」，**只能用到第 i 根（含）以前的資料**；`app/strategies/indicators.py` 有 EMA、SMA、RSI、ATR、交叉判斷可以用。

## 即時行情（免費）

行情來自 Binance 公開 WebSocket，不需要 API key。後端每個市場只開一條連線，依需要動態訂閱，斷線（包含 Binance 每 24 小時的主動斷線）會自動重連：

| 用途 | Stream | 更新頻率 |
|---|---|---|
| 圖表 K 線（合約） | `<symbol>@kline_<interval>` | 約 250ms |
| 圖表 K 線（現貨） | `<symbol>@kline_<interval>` | 約 1–2 秒 |
| 持倉價格（合約） | `<symbol>@markPrice@1s` | 1 秒 |
| 持倉價格（現貨） | `<symbol>@miniTicker` | 1 秒 |

正式環境的合約 K 線與標記價格在 `wss://fstream.binance.com/market/ws`（舊的 `/ws` 已不再推送這些資料）；`TESTNET=true` 時改連 Demo Trading 的 stream。`GET /health` 的 `market_stream` 會顯示目前連線狀態。前端與後端的即時連線中斷時，K 線圖會自動退回每 5 秒輪詢。

## 部署（VPS + Docker）

```bash
cp .env.example .env    # 填好 key 與密碼
SITE_ADDRESS=bot.example.com docker compose up -d --build
```

- `web`（Caddy）會自動申請 HTTPS 憑證，並提供前端與反向代理 `/api`、`/ws`、`/webhook`
- `backend` 使用 PostgreSQL（`postgres` 服務）
- 資料庫結構由 Alembic 管理（`backend/app/migrations/`），後端啟動時自動升級到最新版；
  導入 Alembic 前就存在的資料庫會自動標記成 baseline（`0001`），資料不會動
- 改了 `backend/app/models.py` 之後要產生 migration，檢查內容後一起 commit：
  `cd backend && uv run alembic revision --autogenerate -m "說明這次改了什麼"`。
  忘了產生的話 `tests/test_migrations.py` 會失敗
- 每個容器的 log 最多保留 5 個 10 MB 的檔案（`docker-compose.yml` 的 `x-logging`）

## 資料庫備份

每天台灣時間 03:30 由 systemd timer 執行 `deploy/backup.sh`：`pg_dump` → 確認備份檔讀得回來 → 上傳 Cloud Storage。
成功或失敗都會寫進儀表板的「操作軌跡」。

- VM 的 service account 對 bucket **只有建立權限**（`roles/storage.objectCreator`），讀不到也刪不掉既有備份，VM 被入侵時備份仍安全
- bucket 禁止公開存取，生命週期規則會刪除 30 天前的備份
- 另外 VM 的磁碟本身也有每日快照（`zushun-daily`，保留 14 天），但還原快照會影響同一台 VM 上的所有服務，只適合整台機器壞掉時使用

第一次設定（在有 GCP 權限的電腦上）：

```bash
B=gs://tradebot-db-backups-510610
echo '{"rule":[{"action":{"type":"Delete"},"condition":{"age":30}}]}' > lifecycle.json
gcloud storage buckets create $B --location asia-east1 --uniform-bucket-level-access \
  --public-access-prevention --lifecycle-file lifecycle.json
gcloud storage buckets add-iam-policy-binding $B --role roles/storage.objectCreator \
  --member serviceAccount:zushun-vm@zushun-510610.iam.gserviceaccount.com
```

然後在 VM 上：`~/tradebot/.env` 加上 `BACKUP_BUCKET=tradebot-db-backups-510610`，執行 `sudo ~/tradebot/deploy/install-backup.sh`。

常用指令（VM 上）：

```bash
sudo systemctl start tradebot-backup.service          # 立刻備份一次
journalctl -u tradebot-backup.service -n 20 --no-pager  # 看備份紀錄
systemctl list-timers tradebot-backup.timer --no-pager   # 下次什麼時候跑
```

還原（VM 讀不到備份，要用有權限的帳號下載）：

```bash
gcloud storage ls -r gs://tradebot-db-backups-510610/            # 找要還原的那份
gcloud storage cp gs://tradebot-db-backups-510610/2026/10/tradebot-YYYYMMDD-HHMMSS.dump ./restore.dump
gcloud compute scp --zone asia-east1-c --tunnel-through-iap restore.dump zushum-prod:/tmp/restore.dump
# 在 VM 上：先停後端避免寫入，再整個覆蓋還原
docker stop tradebot-backend-1
docker exec -i tradebot-postgres-1 pg_restore -U tradebot -d tradebot --clean --if-exists --no-owner < /tmp/restore.dump
docker start tradebot-backend-1 && rm /tmp/restore.dump
```

不確定備份內容時，可以先還原到另一個資料庫檢查：
`docker exec tradebot-postgres-1 createdb -U tradebot restore_check`，把上面的 `-d tradebot --clean --if-exists` 換成 `-d restore_check`，看完再 `dropdb`。

## 安全建議

- Binance API key **只開「讀取 + 交易」，不要開提領**，並設定 IP 白名單（VPS 的 IP）
- `.env` 已列在 `.gitignore`，不要 commit
- 一定要改 `WEBHOOK_SECRET`、`DASHBOARD_PASSWORD`、`JWT_SECRET`
- 開啟儀表板的兩步驟驗證：在自己的電腦執行 `uv run deploy/setup-2fa.py`，它會產生金鑰寫進 VM 的 `.env`、重啟後端，
  並在桌面存一張 QR code 圖片給驗證器 App（Google Authenticator、1Password 等）掃描，掃完請刪除圖片。
  手機遺失時用 `uv run deploy/setup-2fa.py --disable` 先關閉，再重新設定
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
