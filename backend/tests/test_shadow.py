import time

import pytest
from sqlalchemy import select

from app.models import BotSettings, ShadowTrade, Signal, StrategyConfig
from app.strategies import INTERVAL_SECONDS, Candle
from app.workers.strategy_runner import StrategyRunner

from .test_executor import env  # noqa: F401  (pytest fixture)
from .test_performance import body, client  # noqa: F401


async def test_shadow_runner_records_virtual_trades_without_orders(env, monkeypatch):  # noqa: F811
    ex, sf, ad = env
    sec = INTERVAL_SECONDS["1h"]
    start = int(time.time()) // sec * sec - 1000 * sec
    state = {"targets": [], "bars": [Candle(start, 100, 100, 100, 100, 1)]}

    async def fake_fetch(adapter, symbol, interval, n):
        return list(state["bars"])

    class Scripted:
        warmup = 10

        def targets(self, candles):
            return [state["targets"][-1]] * len(candles)

    monkeypatch.setattr("app.workers.strategy_runner.fetch_candles", fake_fetch)
    monkeypatch.setattr("app.workers.strategy_runner.build_strategy", lambda k, p: Scripted())
    async with sf() as s:
        bot = await s.get(BotSettings, 1)
        bot.size_mode, bot.size_value, bot.leverage, bot.stop_loss_pct = "fixed", 1000, 1, 5
        s.add(StrategyConfig(id=1, name="shadow", strategy="x", symbol="BTCUSDT", market="futures", interval="1h",
                             params={}, enabled=True, mode="shadow"))
        await s.commit()
    runner = StrategyRunner(ex)

    async def step(target, o=100, h=100, low=100, c=100):
        last = state["bars"][-1].time
        state["bars"].append(Candle(last + sec, o, h, low, c, 1))
        state["targets"].append(target)
        await runner.evaluate(1)

    async def trades():
        async with sf() as s:
            return (await s.scalars(select(ShadowTrade).order_by(ShadowTrade.id))).all()

    await step("long")                 # 第一次：只記下判斷
    assert await trades() == []
    await step("flat")
    await step("long", c=100)          # 判斷改變 → 虛擬開多 @100，停損 95
    (t,) = await trades()
    assert (t.side, t.entry_price, t.qty, t.stop_price, t.exit_time) == ("long", 100, 10, 95, None)
    await step("long", low=94, c=96)   # 最低 94 觸及停損 → 以 95 出場
    await step("long", c=97)           # 判斷沒變：不重新進場
    (t,) = await trades()
    assert (t.exit_price, t.exit_reason) == (95, "stop")
    assert t.pnl == pytest.approx(-50) and t.entry_fee == pytest.approx(0.4)  # 1000 × 0.04%
    await step("flat")
    await step("long", c=110)          # 判斷又變成 long → 重新進場；虛擬帳戶已經扣掉上一筆的虧損
    t2 = (await trades())[-1]
    assert t2.exit_time is None and t2.entry_price == 110

    assert ad["futures"].orders == [] and ad["spot"].orders == []  # 從頭到尾沒有真的下單
    async with sf() as s:
        assert (await s.scalars(select(Signal))).all() == []


async def test_shadow_strategies_skip_one_per_symbol_and_are_cleaned_up(client):  # noqa: F811
    c, sf = client
    assert c.post("/api/strategies", json=body("live")).status_code == 200
    assert c.post("/api/strategies", json={**body("shadow 1"), "mode": "shadow"}).status_code == 200
    assert c.post("/api/strategies", json={**body("shadow 2"), "mode": "shadow"}).status_code == 200
    # 影子改回實際下單：同一個交易對已經有實際下單的策略，要擋
    assert c.put("/api/strategies/2", json=body("shadow 1")).status_code == 409

    from datetime import datetime, timezone
    async with sf() as s:
        s.add(ShadowTrade(strategy_id=2, symbol="BTC/USDT:USDT", market="futures", side="long", qty=1, entry_price=100,
                          entry_bar=0, entry_time=datetime(2026, 1, 1, tzinfo=timezone.utc), entry_fee=0.04,
                          exit_price=110, exit_time=datetime(2026, 1, 2, tzinfo=timezone.utc), exit_fee=0.044, pnl=10))
        await s.commit()
    perf = {x["config"]["id"]: x for x in c.get("/api/strategies/performance").json()["strategies"]}
    assert perf[2]["trades"] == 1 and perf[2]["net"] == pytest.approx(10 - 0.084)
    assert perf[1]["trades"] == 0  # 實際下單的策略不會混到影子交易

    assert c.delete("/api/strategies/2").status_code == 200
    async with sf() as s:
        assert (await s.scalars(select(ShadowTrade))).all() == []
