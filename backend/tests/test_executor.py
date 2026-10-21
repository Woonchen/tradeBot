"""用假交易所 + 記憶體 SQLite 測整條「訊號 → 下單 → 成交 → 部位」流程。"""

import math

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.engine.executor import Executor
from app.exchange.base import AssetBalance, ExchangeAdapter, OrderResult, UnknownSymbol, Wallet
from app.models import Base, BotSettings, Order, Position, Signal, Trade
from app.services.event_bus import EventBus


class FakeAdapter(ExchangeAdapter):
    def __init__(self, market, price=100.0, usdt=10_000.0):
        super().__init__(None, market)
        self.price = price
        self.usdt = usdt
        self.base = 0.0
        self.orders = []

    async def resolve_symbol(self, raw):
        if "BTC" not in raw.upper():
            raise UnknownSymbol(raw)
        return "BTC/USDT" if self.market == "spot" else "BTC/USDT:USDT"

    def amount_to_precision(self, symbol, qty):
        return math.floor(qty * 1000 + 1e-9) / 1000  # 跟交易所一樣無條件捨去到 0.001

    def min_notional(self, symbol):
        return 5.0

    def taker_fee(self, symbol):
        return 0.001

    def base_asset(self, symbol):
        return "BTC"

    async def fetch_price(self, symbol):
        return self.price

    async def fetch_prices(self, symbols):
        return {s: self.price for s in symbols}

    async def fetch_wallet(self):
        return Wallet(
            assets=[AssetBalance("USDT", self.usdt, 0, self.usdt), AssetBalance("BTC", self.base, 0, self.base * self.price)],
            equity_usdt=self.usdt + self.base * self.price,
            free_usdt=self.usdt,
        )

    async def create_market_order(self, symbol, side, qty, client_order_id, reduce_only=False):
        self.orders.append((side, qty, reduce_only))
        d = 1 if side == "buy" else -1
        self.base += d * qty
        self.usdt -= d * qty * self.price
        return OrderResult(f"x{len(self.orders)}", "filled", qty, self.price, qty * self.price * 0.001, "USDT")

    async def fetch_order(self, symbol, exchange_order_id):
        raise AssertionError("should not be called")

    async def close(self):
        pass


@pytest.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as s:
        s.add(BotSettings(id=1, size_mode="fixed", size_value=100, leverage=2, max_daily_loss_usdt=50))
        await s.commit()
    adapters = {"spot": FakeAdapter("spot"), "futures": FakeAdapter("futures")}
    ex = Executor(adapters, EventBus(sf), sf)
    yield ex, sf, adapters
    await engine.dispose()


async def run(ex, **kw):
    sig, _ = await ex.submit(source=kw.pop("source", "tradingview"), **kw)
    await ex.process(sig.id)
    async with ex.sf() as s:
        return await s.get(Signal, sig.id)


async def test_spot_buy_then_sell(env):
    ex, sf, ad = env
    sig = await run(ex, ticker="BTCUSDT", market="spot", action="buy", target="long")
    assert sig.status == "executed" and sig.symbol == "BTC/USDT"
    assert ad["spot"].orders == [("buy", 1.0, False)]

    ad["spot"].price = 110
    sig = await run(ex, ticker="BTCUSDT", market="spot", action="sell", target="flat")
    assert sig.status == "executed"
    async with sf() as s:
        trades = (await s.scalars(select(Trade).order_by(Trade.id))).all()
        assert [t.intent for t in trades] == ["open_long", "close_long"]
        assert trades[1].realized_pnl == pytest.approx(1.0 * 10)
        assert (await s.scalars(select(Position).where(Position.closed_at.is_(None)))).all() == []


async def test_futures_reverse(env):
    ex, sf, ad = env
    await run(ex, ticker="BTCUSDT.P", market="futures", action="buy", target="long")
    sig = await run(ex, ticker="BTCUSDT.P", market="futures", action="sell", target="short")
    assert sig.status == "executed"
    # 開多 2（100 USDT × 2 倍 / 100），然後 reduceOnly 平多，再開空
    assert ad["futures"].orders == [("buy", 2.0, False), ("sell", 2.0, True), ("sell", 2.0, False)]
    async with sf() as s:
        open_pos = (await s.scalars(select(Position).where(Position.closed_at.is_(None)))).all()
        assert [(p.side, p.qty) for p in open_pos] == [("short", 2.0)]


async def test_rejections(env):
    ex, sf, ad = env
    assert (await run(ex, ticker="BTCUSDT", market="spot", action="short", target="short")).status == "rejected"
    assert (await run(ex, ticker="DOGEUSDT", market="spot", action="buy", target="long")).status == "rejected"
    assert (await run(ex, ticker="BTCUSDT", market="spot", action="sell", target="flat")).status == "rejected"
    async with sf() as s:
        (await s.get(BotSettings, 1)).enabled = False
        await s.commit()
    sig = await run(ex, ticker="BTCUSDT", market="spot", action="buy", target="long")
    assert sig.status == "rejected" and "停用" in sig.reject_reason
    # 手動平倉不受 Bot 停用影響（這裡沒有部位，所以是「已經 flat」被拒，而不是「停用」）
    sig = await run(ex, source="manual", ticker="BTCUSDT", market="spot", action="close", target="flat")
    assert "停用" not in sig.reject_reason
    assert ad["spot"].orders == []


async def test_kill_switch(env):
    ex, sf, ad = env
    await run(ex, ticker="BTCUSDT.P", market="futures", action="buy", target="long")
    ad["futures"].price = 70  # 2 × -30 = -60 > 每日上限 50
    sig = await run(ex, ticker="BTCUSDT.P", market="futures", action="sell", target="short")
    assert sig.status == "rejected" and "虧損上限" in sig.reject_reason
    async with sf() as s:
        assert (await s.get(BotSettings, 1)).enabled is False
        assert len((await s.scalars(select(Order))).all()) == 2  # 只有開多 + 平多，沒有反手


async def test_duplicate_signal(env):
    ex, _, _ = env
    a, created_a = await ex.submit(source="tradingview", ticker="BTCUSDT", market="spot", action="buy", target="long", dedupe_key="k1")
    b, created_b = await ex.submit(source="tradingview", ticker="BTCUSDT", market="spot", action="buy", target="long", dedupe_key="k1")
    assert created_a and not created_b and a.id == b.id
