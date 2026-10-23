import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import auth, bot, history, market, portfolio, sentiment, strategies, webhook, ws
from .api.totp import decode_secret
from . import migrate
from .config import settings
from .db import SessionLocal, engine
from .engine.executor import Executor
from .engine.risk import load_bot_settings
from .exchange.factory import build_adapters, build_market_data
from .services.event_bus import EventBus
from .services.market_stream import MarketHub
from .services.sentiment import Sentiment, tracked_tickers
from .workers.strategy_runner import StrategyRunner
from .workers.sync import SyncWorkers, every

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


def check_secrets():
    """連到 Binance（Demo 或正式）時，密鑰不能是預設值，否則拒絕啟動；Paper 模擬只警告。"""
    if settings.dashboard_totp_secret:
        decode_secret(settings.dashboard_totp_secret)  # 金鑰打錯就拒絕啟動，不要變成誰都登不進去
    weak = settings.insecure_secrets()
    if not weak:
        return
    msg = f"{', '.join(weak)} 仍是預設值或空白，請在 .env 設定成隨機字串"
    if settings.execution_mode == "binance":
        raise RuntimeError(f"拒絕啟動：{msg}")
    log.warning("%s（Paper 模式暫時允許）", msg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_secrets()
    async with engine.begin() as conn:
        await conn.run_sync(migrate.upgrade)  # 把資料庫升級到最新結構（Alembic）
    async with SessionLocal() as s:
        await load_bot_settings(s)
        await s.commit()

    bus = EventBus(SessionLocal)
    adapters = build_adapters(settings, SessionLocal)
    executor = Executor(adapters, bus, SessionLocal)
    executor.market_data = build_market_data(settings, adapters)
    hub = MarketHub(executor, settings)
    sentiment = Sentiment(SessionLocal)
    runner = StrategyRunner(executor, sentiment)
    app.state.sentiment = sentiment
    app.state.executor = executor
    app.state.hub = hub
    app.state.strategy_runner = runner

    mode = "Paper 模擬" if settings.execution_mode == "paper" else ("Binance Demo" if settings.testnet else "Binance 正式帳戶")
    await bus.emit("log", category="system", message=f"系統啟動，執行模式：{mode}")
    await executor.requeue_pending()
    tasks = [
        asyncio.create_task(executor.run()),
        *hub.start(),
        *SyncWorkers(executor, hub).start(settings),
        asyncio.create_task(runner.run()),
        # 市場情緒：每小時更新恐懼貪婪、資金費率，並記錄 Binance 只保留 30 天的合約數據
        asyncio.create_task(every(3600, lambda: _refresh_sentiment(sentiment), "sentiment")),
    ]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        for a in {*adapters.values(), *executor.market_data.values()}:
            await a.close()
        await sentiment.close()
        await engine.dispose()


# 不開 CORS：正式環境前後端同網域（Caddy），開發時由 Vite 的 proxy 轉發，都不是跨網域請求
async def _refresh_sentiment(sentiment: Sentiment):
    await sentiment.refresh_all(await tracked_tickers(SessionLocal))


app = FastAPI(title="TradeBot", lifespan=lifespan)
for r in (
    auth.router, webhook.router, market.router, history.router, portfolio.router, bot.router,
    strategies.router, sentiment.router, ws.router,
):
    app.include_router(r)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "mode": settings.execution_mode,
        "testnet": settings.testnet,
        "market_stream": app.state.hub.status,
    }
