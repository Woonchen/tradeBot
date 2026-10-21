import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import auth, bot, history, market, portfolio, webhook, ws
from .config import settings
from .db import SessionLocal, engine
from .engine.executor import Executor
from .engine.risk import load_bot_settings
from .exchange.factory import build_adapters
from .models import Base
from .services.event_bus import EventBus
from .workers.sync import SyncWorkers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as s:
        await load_bot_settings(s)
        await s.commit()

    bus = EventBus(SessionLocal)
    adapters = build_adapters(settings, SessionLocal)
    executor = Executor(adapters, bus, SessionLocal)
    app.state.executor = executor

    mode = "Paper 模擬" if settings.execution_mode == "paper" else ("Binance Demo" if settings.testnet else "Binance 正式帳戶")
    await bus.emit("log", category="system", message=f"系統啟動，執行模式：{mode}")
    await executor.requeue_pending()
    tasks = [asyncio.create_task(executor.run()), *SyncWorkers(executor).start(settings)]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        for a in adapters.values():
            await a.close()
        await engine.dispose()


app = FastAPI(title="TradeBot", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
for r in (auth.router, webhook.router, market.router, history.router, portfolio.router, bot.router, ws.router):
    app.include_router(r)


@app.get("/health")
async def health():
    return {"ok": True, "mode": settings.execution_mode, "testnet": settings.testnet}
