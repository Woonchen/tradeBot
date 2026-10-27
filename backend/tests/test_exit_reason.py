"""部位平倉原因：下單平倉看是哪種訊號，交易所端平倉用標記價格研判。"""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.engine.risk import load_bot_settings
from app.engine.sizing import external_exit_reason
from app.models import Position
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
