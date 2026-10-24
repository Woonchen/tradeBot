"""加碼：實際下單（執行器）、策略執行（判斷何時加碼）、影子模式。"""

import time

import pytest
from sqlalchemy import select

from app.models import BotSettings, Position, ShadowTrade, Signal, StrategyConfig, Trade
from app.strategies import INTERVAL_SECONDS, Candle
from app.workers.strategy_runner import StrategyRunner

from .test_executor import env, run  # noqa: F401  (pytest fixture)


async def open_pos(sf):
    async with sf() as s:
        return await s.scalar(select(Position).where(Position.closed_at.is_(None)))


async def strategy_cfg(sf, **kw):
    async with sf() as s:
        cfg = StrategyConfig(name="t", strategy="ema_cross", symbol="BTCUSDT", market="futures", interval="1h", params={},
                             enabled=True, size_mode="fixed", size_value=100, leverage=2,
                             pyramid_atr=1.0, pyramid_max=2, pyramid_frac=0.5, **kw)
        s.add(cfg)
        await s.commit()
        return cfg.id


async def test_executor_adds_to_existing_position(env):  # noqa: F811
    ex, sf, ad = env
    cid = await strategy_cfg(sf)
    raw = {"strategy_id": cid}
    assert (await run(ex, source="strategy", ticker="BTCUSDT", market="futures", action="long", target="long", raw=raw)).status == "executed"
    pos = await open_pos(sf)
    assert pos.qty == 2 and pos.adds == 0 and pos.last_fill_price == 100  # 100 × 2 倍 ÷ 100

    ad["futures"].price = 110
    sig = await run(ex, source="strategy", ticker="BTCUSDT", market="futures", action="add", target="long", raw=raw)
    assert sig.status == "executed"
    pos = await open_pos(sf)
    # 加半份：50 × 2 ÷ 110 = 0.909 → 交易所精度 0.909；平均成本 (2×100 + 0.909×110) ÷ 2.909
    assert pos.qty == pytest.approx(2.909) and pos.adds == 1 and pos.last_fill_price == 110
    assert pos.entry_price == pytest.approx((200 + 0.909 * 110) / 2.909)
    async with sf() as s:
        intents = [t.intent for t in (await s.scalars(select(Trade).order_by(Trade.id))).all()]
    assert intents == ["open_long", "add_long"]

    # 平倉：整個部位（含加碼）一次平掉
    assert (await run(ex, source="strategy", ticker="BTCUSDT", market="futures", action="flat", target="flat", raw=raw)).status == "executed"
    assert await open_pos(sf) is None and ad["futures"].orders[-1] == ("sell", pytest.approx(2.909), True)


async def test_add_rejections(env):  # noqa: F811
    ex, sf, _ = env
    cid = await strategy_cfg(sf)
    raw = {"strategy_id": cid}
    sig = await run(ex, source="strategy", ticker="BTCUSDT", market="futures", action="add", target="long", raw=raw)
    assert sig.status == "rejected" and "沒有long部位" in sig.reject_reason
    await run(ex, source="strategy", ticker="BTCUSDT", market="futures", action="short", target="short", raw=raw)
    assert (await run(ex, source="strategy", ticker="BTCUSDT", market="futures", action="add", target="long", raw=raw)).status == "rejected"
    async with sf() as s:
        bot = await s.get(BotSettings, 1)
        bot.max_open_positions, bot.enabled = 1, False
        await s.commit()
    sig = await run(ex, source="strategy", ticker="BTCUSDT", market="futures", action="add", target="short", raw=raw)
    assert sig.reject_reason == "Bot 已停用"
    async with sf() as s:
        (await s.get(BotSettings, 1)).enabled = True
        await s.commit()
    # 持倉數已達上限也能加碼（不會多一個持倉）
    assert (await run(ex, source="strategy", ticker="BTCUSDT", market="futures", action="add", target="short", raw=raw)).status == "executed"


def scripted(monkeypatch, state):
    sec = INTERVAL_SECONDS["1h"]
    now_bar = int(time.time()) // sec * sec

    async def fake_fetch(adapter, symbol, interval, bars):
        prices = state["prices"]
        end = now_bar + state.get("bars", 0) * sec  # 每一步往後一根，才是「新的 K 棒」
        # 每根 K 棒波動 ±1，ATR(14) 約 2（含跳動）
        return [Candle(end - (len(prices) - i) * sec, p, p + 1, p - 1, p, 1) for i, p in enumerate(prices)]

    class Scripted:
        warmup = 10

        def targets(self, candles):
            return [state["target"]] * len(candles)

    monkeypatch.setattr("app.workers.strategy_runner.fetch_candles", fake_fetch)
    monkeypatch.setattr("app.workers.strategy_runner.build_strategy", lambda k, p: Scripted())


async def test_runner_adds_when_price_moves_k_atr(env, monkeypatch):  # noqa: F811
    ex, sf, ad = env
    state = {"prices": [100.0] * 30, "target": "long"}
    scripted(monkeypatch, state)
    cid = await strategy_cfg(sf, enter_on_enable=True)
    runner = StrategyRunner(ex)

    async def step(price):
        state["prices"] = state["prices"][1:] + [price]
        state["bars"] = state.get("bars", 0) + 1
        ad["futures"].price = price
        await runner.evaluate(cid)
        while not ex.queue.empty():
            await ex.process(await ex.queue.get())

    await runner.evaluate(cid)  # 啟用時立即進場
    while not ex.queue.empty():
        await ex.process(await ex.queue.get())
    assert (await open_pos(sf)).adds == 0
    await step(101)   # 只漲 1，ATR 約 2：還不到 1 個 ATR
    assert (await open_pos(sf)).adds == 0
    await step(104)   # 比上次成交 100 多 4：加碼
    assert (await open_pos(sf)).adds == 1
    await step(105)   # 比上次成交 104 只多 1：不加
    await step(108)   # 再加一次
    await step(120)   # 已經加滿 2 次：不再加
    pos = await open_pos(sf)
    assert pos.adds == 2 and pos.last_fill_price == 108
    async with sf() as s:
        adds = (await s.scalars(select(Signal).where(Signal.action == "add"))).all()
    assert [x.status for x in adds] == ["executed", "executed"]

    # 別人開的部位不加碼
    await run(ex, source="manual", ticker="BTCUSDT", market="futures", action="flat", target="flat")
    await run(ex, source="manual", ticker="BTCUSDT", market="futures", action="long", target="long")
    await step(140)
    assert (await open_pos(sf)).adds == 0


async def test_shadow_adds_and_recomputes_stop(env, monkeypatch):  # noqa: F811
    ex, sf, ad = env
    state = {"prices": [100.0] * 30, "target": "long"}
    scripted(monkeypatch, state)
    cid = await strategy_cfg(sf, enter_on_enable=True, mode="shadow", stop_loss_pct=5)
    runner = StrategyRunner(ex)
    await runner.evaluate(cid)
    for p in (101, 104, 108):
        state["prices"] = state["prices"][1:] + [p]
        state["bars"] = state.get("bars", 0) + 1
        await runner.evaluate(cid)
    async with sf() as s:
        t = await s.scalar(select(ShadowTrade))
    assert t.adds == 2 and t.last_fill_price == 108 and t.exit_time is None
    assert t.entry_price == pytest.approx((100 * 2 + 104 * (100 / 104) + 108 * (100 / 108)) / (2 + 100 / 104 + 100 / 108))
    assert t.stop_price == pytest.approx(t.entry_price * 0.95)  # 以平均成本重算
    assert ad["futures"].orders == []  # 影子模式不下單
