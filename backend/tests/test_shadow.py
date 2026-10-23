import time
from datetime import timezone

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


async def test_shadow_futures_charges_funding_like_backtest(env, monkeypatch):  # noqa: F811
    ex, sf, ad = env
    H = 3600
    start = (int(time.time()) // 86400 - 30) * 86400  # 00:00 UTC，跟結算時間對齊
    state = {"targets": [], "bars": [Candle(start, 100, 100, 100, 100, 1)]}

    async def fake_fetch(adapter, symbol, interval, n):
        return list(state["bars"])

    class Scripted:
        warmup = 10

        def targets(self, candles):
            return [state["targets"][-1]] * len(candles)

    class FakeSentiment:
        async def attach(self, strategy, symbol):
            pass

        async def funding_series(self, symbol):
            # 每 8 小時結算；第 8 小時 0.01%、第 16 小時 0.02%、第 24 小時 −0.01%
            return [(start + 8 * H, 0.01), (start + 16 * H, 0.02), (start + 24 * H, -0.01)]

    monkeypatch.setattr("app.workers.strategy_runner.fetch_candles", fake_fetch)
    monkeypatch.setattr("app.workers.strategy_runner.build_strategy", lambda k, p: Scripted())
    async with sf() as s:
        bot = await s.get(BotSettings, 1)
        bot.size_mode, bot.size_value, bot.leverage, bot.stop_loss_pct = "fixed", 1000, 1, 0
        s.add(StrategyConfig(id=1, name="shadow", strategy="x", symbol="BTCUSDT", market="futures", interval="1h",
                             params={}, enabled=True, mode="shadow", enter_on_enable=True))
        await s.commit()
    runner = StrategyRunner(ex, FakeSentiment())

    async def step(target, price=100.0):
        last = state["bars"][-1].time
        state["bars"].append(Candle(last + H, price, price, price, price, 1))
        state["targets"].append(target)
        await runner.evaluate(1)

    async def trade():
        async with sf() as s:
            return (await s.scalars(select(ShadowTrade))).one()

    # 第 7 根（07:00 開盤）收盤在 08:00 → 立即進場；08:00 的結算在開倉那一刻，不付
    for _ in range(6):
        state["bars"].append(Candle(state["bars"][-1].time + H, 100, 100, 100, 100, 1))
    await step("long")
    t = await trade()
    # SQLite 取回的時間不帶時區（都是 UTC）
    assert t.entry_time.replace(tzinfo=timezone.utc).timestamp() == start + 8 * H and t.qty == 10 and t.funding == 0
    for _ in range(8):
        await step("long")                 # 走到 16:00：付 1000 × 0.02% = 0.2
    assert (await trade()).funding == pytest.approx(0.2)
    await step("long")                     # 再判斷一次不會重複扣
    assert (await trade()).funding == pytest.approx(0.2)
    for _ in range(6):
        await step("long")
    await step("flat", price=100)          # 23:00 那根收盤 = 24:00 平倉：24:00 的結算（負費率）先收 0.1
    t = await trade()
    assert t.exit_time is not None and t.funding == pytest.approx(0.2 - 0.1)
    assert t.pnl == 0 and (t.pnl - t.entry_fee - t.exit_fee - t.funding) == pytest.approx(-0.4 - 0.4 - 0.1)
    assert ad["futures"].orders == []      # 影子模式不下單
