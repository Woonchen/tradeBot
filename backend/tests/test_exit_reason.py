"""部位平倉原因：下單平倉看是哪種訊號，交易所端平倉用標記價格研判。"""

from types import SimpleNamespace

import ccxt.async_support as ccxt
import pytest
from sqlalchemy import select

from app.engine.risk import load_bot_settings
from app.engine.sizing import external_exit_reason
from app.exchange.base import ExitInfo
from app.exchange.binance import BinanceAdapter
from app.models import Position, Trade
from app.workers.sync import SyncWorkers

from .test_executor import env, run  # noqa: F401  (pytest fixture)


async def closed_reasons(sf):
    async with sf() as s:
        rows = (await s.scalars(select(Position).where(Position.closed_at.is_not(None)).order_by(Position.id))).all()
        return [p.exit_reason for p in rows]


async def open_long(ex):
    await run(ex, ticker="BTCUSDT.P", market="futures", action="buy", target="long")


@pytest.mark.parametrize("kw, reason", [
    (dict(source="tradingview", action="sell", target="flat"), "signal"),
    (dict(source="strategy", action="flat", target="flat"), "signal"),
    (dict(source="manual", action="close", target="flat"), "manual"),
    (dict(source="protection", action="close", target="flat", strategy="stop"), "stop"),
    (dict(source="protection", action="close", target="flat", strategy="take_profit"), "take_profit"),
])
async def test_close_records_reason(env, kw, reason):  # noqa: F811
    ex, sf, _ = env
    await open_long(ex)
    assert (await run(ex, ticker="BTCUSDT.P", market="futures", **kw)).status == "executed"
    assert await closed_reasons(sf) == [reason]


async def test_reverse_closes_with_signal_reason(env):  # noqa: F811
    ex, sf, _ = env
    await open_long(ex)
    await run(ex, ticker="BTCUSDT.P", market="futures", action="sell", target="short")
    assert await closed_reasons(sf) == ["signal"]


async def test_breaker_close_all_is_risk(env):  # noqa: F811
    ex, sf, _ = env
    await open_long(ex)
    async with sf() as s:
        ids = await ex.close_all(s, await load_bot_settings(s), "熔斷 {n}", reason="risk")
    for i in ids:
        await ex.process(i)
    assert await closed_reasons(sf) == ["risk"]


async def test_external_close_infers_reason(env):  # noqa: F811
    ex, sf, ad = env
    await open_long(ex)
    async with sf() as s:
        p = await s.scalar(select(Position))
        p.stop_price, p.take_profit_price = 95.0, 110.0
        await s.commit()
    ad["futures"].price = 95.1  # 原生停損觸發後價格彈回一點點，還在容許範圍內
    ad["futures"].fetch_positions = lambda: _empty()
    hub = SimpleNamespace(fresh_price=lambda m, s: None, on_price=lambda *a: None)
    await SyncWorkers(ex, hub).sync_positions()
    assert await closed_reasons(sf) == ["stop"]


async def _empty():
    return []


@pytest.mark.parametrize("side, price, reason", [
    ("long", 94.0, "stop"),
    ("long", 95.2, "stop"),  # 差 0.2%，算碰到
    ("long", 111.0, "take_profit"),
    ("long", 100.0, "external"),
    ("long", 80.0, "stop"),  # 停損優先於強平
    ("short", 106.0, "stop"),
    ("short", 89.0, "take_profit"),
])
def test_external_exit_reason(side, price, reason):
    stop, tp = (95.0, 110.0) if side == "long" else (105.0, 90.0)
    assert external_exit_reason(side, price, stop, tp, 82.0 if side == "long" else 118.0) == reason


def test_external_liquidation_without_stop():
    assert external_exit_reason("long", 81.0, None, None, 82.0) == "liquidation"
    assert external_exit_reason("short", 100.0, None, None, 118.0) == "external"


# ---------------------------------------------------------------- 查交易所訂單紀錄

class FakeCcxt:
    """只實作 find_exit 用到的：一般訂單和條件單（algo order）的歷史。"""

    def __init__(self, orders, algos=(), fail=False):
        self.orders, self.algos, self.fail = list(orders), list(algos), fail
        self.calls = []

    def milliseconds(self):
        return 10_000_000_000

    async def fetch_orders(self, symbol, since=None, limit=None, params={}):
        self.calls.append((since, dict(params)))
        if self.fail:
            raise ccxt.NetworkError("boom")
        return self.algos if params.get("trigger") else self.orders


def order(id, side="sell", filled=0.5, ts=1000, avg=95.0, client="x-bot", **info):
    return {"id": id, "side": side, "filled": filled, "timestamp": ts, "average": avg, "clientOrderId": client,
            "info": {"orderId": int(id), "clientOrderId": client, **info}}


def algo(order_type, actual):
    return {"id": "a" + actual, "info": {"orderType": order_type, "actualOrderId": actual}}


async def find(ex, side="long", since=0):
    return await BinanceAdapter(ex, "futures").find_exit("BTC/USDT:USDT", side, since)


async def test_find_exit_matches_triggered_algo_order():
    ex = FakeCcxt(
        [order("1", side="buy", ts=500), order("2", ts=900, avg=94.0), order("3", ts=1000, avg=109.8)],
        [algo("STOP_MARKET", ""), algo("TAKE_PROFIT_MARKET", "3")],
    )
    assert await find(ex) == ExitInfo("take_profit", 109.8)
    ex.algos = [algo("STOP_MARKET", "3")]
    assert await find(ex) == ExitInfo("stop", 109.8)


async def test_find_exit_liquidation_and_manual():
    assert await find(FakeCcxt([order("7", client="autoclose-159610762004")])) == ExitInfo("liquidation", 95.0)
    assert await find(FakeCcxt([order("7", client="adl_autoclose")])) == ExitInfo("liquidation", 95.0)
    assert await find(FakeCcxt([order("7", client="web_abc")], [algo("STOP_MARKET", "")])) == ExitInfo("external", 95.0)


async def test_find_exit_short_uses_buy_side_and_ignores_unfilled():
    ex = FakeCcxt([order("1", side="sell"), order("2", side="buy", filled=0), order("3", side="buy", avg=105.0)],
                  [algo("STOP_MARKET", "3")])
    assert await find(ex, side="short") == ExitInfo("stop", 105.0)


async def test_find_exit_gives_up_cleanly():
    assert await find(FakeCcxt([order("1", side="buy")])) is None  # 沒有反方向成交
    assert await find(FakeCcxt([], fail=True)) is None
    assert await BinanceAdapter(FakeCcxt([order("1")]), "spot").find_exit("BTC/USDT", "long", 0) is None


async def test_find_exit_clamps_window_to_seven_days():
    ex = FakeCcxt([order("1")], [algo("STOP_MARKET", "1")])
    await find(ex, since=0)
    assert all(since >= ex.milliseconds() - 7 * 24 * 3600 * 1000 for since, _ in ex.calls)


async def test_external_close_prefers_exchange_record(env):  # noqa: F811
    ex, sf, ad = env
    await open_long(ex)
    async with sf() as s:
        p = await s.scalar(select(Position))
        p.stop_price, p.take_profit_price = 95.0, 110.0
        await s.commit()
    ad["futures"].price = 100.0  # 標記價格看不出原因
    ad["futures"].fetch_positions = lambda: _empty()

    async def find_exit(symbol, side, since_ms):
        assert side == "long" and since_ms > 0
        return ExitInfo("take_profit", 110.2)
    ad["futures"].find_exit = find_exit
    hub = SimpleNamespace(fresh_price=lambda m, s: None, on_price=lambda *a: None)
    await SyncWorkers(ex, hub).sync_positions()
    assert await closed_reasons(sf) == ["take_profit"]
    async with sf() as s:
        trade = (await s.scalars(select(Trade).order_by(Trade.id.desc()))).first()
        assert trade.price == 110.2 and trade.realized_pnl == pytest.approx(2 * 10.2)
