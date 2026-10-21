import math
import time

import pytest
from sqlalchemy import select

from app.models import Signal, StrategyConfig
from app.strategies import INTERVAL_SECONDS, REGISTRY, Candle, build_strategy
from app.strategies.backtest import run_backtest
from app.strategies.indicators import atr, ema, rsi, sma
from app.workers.strategy_runner import StrategyRunner

from .test_executor import env  # noqa: F401  (pytest fixture)


def candles_from(closes, start=0, step=3600):
    return [Candle(start + i * step, c, c, c, c, 1.0) for i, c in enumerate(closes)]


# ------------------------------------------------------------------ 指標
def test_sma_ema():
    assert sma([1, 2, 3, 4, 5], 3) == [None, None, 2, 3, 4]
    e = ema([1, 2, 3, 4, 5], 3)
    assert e[:2] == [None, None] and e[2] == 2 and e[3] == pytest.approx(3) and e[4] == pytest.approx(4)


def test_rsi_bounds_and_extremes():
    up = rsi([float(i) for i in range(1, 40)], 14)
    assert up[13] is None and up[14] == 100
    down = rsi([float(i) for i in range(40, 1, -1)], 14)
    assert down[-1] == pytest.approx(0)
    mixed = rsi([10, 11, 10.5, 11.5, 11, 12, 11.2, 12.4, 12, 13, 12.5, 13.5, 13, 14, 13.4, 14.2], 14)
    assert all(v is None or 0 <= v <= 100 for v in mixed)


def test_atr_constant_range():
    a = atr([11.0] * 30, [9.0] * 30, [10.0] * 30, 14)
    assert a[12] is None and a[13] == pytest.approx(2) and a[-1] == pytest.approx(2)


def test_indicators_are_causal():
    """改動未來的資料，不能影響過去的指標值（回測不偷看未來）。"""
    base = [100 + math.sin(i / 3) * 5 for i in range(200)]
    changed = base[:150] + [x * 3 for x in base[150:]]
    for f in (lambda v: ema(v, 12), lambda v: rsi(v, 14), lambda v: sma(v, 20)):
        assert f(base)[:150] == f(changed)[:150]


# ------------------------------------------------------------------ 策略
def test_registry_and_params():
    assert {"ema_cross", "rsi_reversion"} <= set(REGISTRY)
    s = build_strategy("ema_cross", {"fast": "5", "slow": 20})
    assert s.p["fast"] == 5 and s.p["slow"] == 20 and s.p["atr_mult"] == 2.0
    with pytest.raises(ValueError):
        build_strategy("ema_cross", {"fast": 30, "slow": 20})
    with pytest.raises(ValueError):
        build_strategy("ema_cross", {"nope": 1})
    with pytest.raises(ValueError):
        build_strategy("rsi_reversion", {"oversold": 60})
    with pytest.raises(ValueError):
        build_strategy("missing")


def test_ema_cross_targets():
    closes = [100.0] * 30 + [100 + i * 2 for i in range(1, 30)] + [158 - i * 3 for i in range(1, 40)]
    s = build_strategy("ema_cross", {"fast": 3, "slow": 8, "atr_mult": 0})
    t = s.targets(candles_from(closes))
    assert len(t) == len(closes)
    assert t[29] == "flat" and "long" in t[30:59] and t[-1] == "short"
    s2 = build_strategy("ema_cross", {"fast": 3, "slow": 8, "atr_mult": 0, "allow_short": False})
    assert s2.targets(candles_from(closes))[-1] == "flat"


def test_targets_are_causal():
    closes = [100 + math.sin(i / 4) * 10 + i * 0.05 for i in range(400)]
    for key in REGISTRY:
        s = build_strategy(key)
        full = s.targets(candles_from(closes))
        cut = s.targets(candles_from(closes[:300]))
        assert full[:300] == cut, key


# ------------------------------------------------------------------ 回測
class Flip:
    """固定的判斷序列，用來驗證回測的成交與損益計算。"""

    def __init__(self, seq):
        self.seq = seq

    def targets(self, candles):
        return self.seq


def test_backtest_next_open_fill_and_pnl():
    c = [
        Candle(0, 100, 100, 100, 100, 1),
        Candle(1, 110, 110, 110, 110, 1),  # 第 0 根判斷 long → 這根開盤 110 進場
        Candle(2, 120, 120, 120, 120, 1),
        Candle(3, 130, 130, 130, 130, 1),  # 第 2 根判斷 flat → 這根開盤 130 出場
    ]
    r = run_backtest(Flip(["long", "long", "flat", "flat"]), c, market="futures",
                     size_value=110, leverage=2, fee_rate=0.0, start_equity=1000)
    (t,) = r.trades
    assert (t["side"], t["entry"], t["exit"], t["qty"]) == ("long", 110, 130, 2)
    assert t["pnl"] == pytest.approx(40)
    assert r.stats["final_equity"] == pytest.approx(1040)
    assert [p["value"] for p in r.equity] == pytest.approx([1000, 1000, 1020, 1040])


def test_backtest_spot_ignores_short_and_charges_fees():
    c = [Candle(i, 100, 100, 100, 100, 1) for i in range(4)]
    r = run_backtest(Flip(["short", "short", "long", "flat"]), c, market="spot",
                     size_value=100, fee_rate=0.001, start_equity=1000)
    assert [t["side"] for t in r.trades] == ["long"]
    assert r.stats["final_equity"] == pytest.approx(1000 - 0.2)


# ------------------------------------------------------------------ 自動執行
async def test_runner_only_trades_on_change(env, monkeypatch):  # noqa: F811
    ex, sf, ad = env
    sec = INTERVAL_SECONDS["1h"]
    now_bar = int(time.time()) // sec * sec
    state = {"targets": ["flat"], "bars": 3}

    async def fake_fetch(adapter, symbol, interval, bars):
        n = state["bars"]  # 每多一根，最後一根的時間就往後推一個週期
        return [Candle(now_bar - 1000 * sec + i * sec, 100, 100, 100, 100, 1) for i in range(n)]

    class Scripted:
        warmup = 10

        def targets(self, candles):
            return [state["targets"][-1]] * len(candles)

    monkeypatch.setattr("app.workers.strategy_runner.fetch_candles", fake_fetch)
    monkeypatch.setattr("app.workers.strategy_runner.build_strategy", lambda k, p: Scripted())

    async with sf() as s:
        s.add(StrategyConfig(name="t", strategy="ema_cross", symbol="BTCUSDT", market="futures",
                             interval="1h", params={}, enabled=True))
        await s.commit()
    runner = StrategyRunner(ex)

    async def step(target, new_bar=True):
        state["targets"].append(target)
        if new_bar:
            state["bars"] += 1
        await runner.evaluate(1)
        while not ex.queue.empty():
            await ex.process(await ex.queue.get())

    await step("long", new_bar=False)  # 第一次：只記錄判斷，不下單
    await step("long", new_bar=False)  # 同一根 K 棒：不重複處理
    await step("long")                 # 新 K 棒、判斷沒變：不下單
    assert ad["futures"].orders == []
    await step("short")                # 判斷改變：下單（開空）
    await step("flat")                 # 判斷改變：平倉
    assert [o[0] for o in ad["futures"].orders] == ["sell", "buy"]

    async with sf() as s:
        sigs = (await s.scalars(select(Signal).order_by(Signal.id))).all()
        cfg = await s.get(StrategyConfig, 1)
    assert [(x.source, x.target, x.status) for x in sigs] == [("strategy", "short", "executed"), ("strategy", "flat", "executed")]
    assert cfg.last_target == "flat" and cfg.last_error is None


async def test_runner_spot_maps_short_to_flat(env, monkeypatch):  # noqa: F811
    ex, sf, ad = env
    sec = INTERVAL_SECONDS["1h"]
    now_bar = int(time.time()) // sec * sec
    seq = iter(["long", "short"])
    bars = {"n": 3}

    async def fake_fetch(adapter, symbol, interval, n):
        bars["n"] += 1
        return [Candle(now_bar - 1000 * sec + i * sec, 100, 100, 100, 100, 1) for i in range(bars["n"])]

    class Scripted:
        warmup = 10

        def targets(self, candles):
            return [next(seq)] * len(candles)

    monkeypatch.setattr("app.workers.strategy_runner.fetch_candles", fake_fetch)
    monkeypatch.setattr("app.workers.strategy_runner.build_strategy", lambda k, p: Scripted())
    async with sf() as s:
        s.add(StrategyConfig(name="spot", strategy="ema_cross", symbol="BTCUSDT", market="spot",
                             interval="1h", params={}, enabled=True))
        await s.commit()
    runner = StrategyRunner(ex)
    await runner.evaluate(1)
    await runner.evaluate(1)
    async with sf() as s:
        sig = (await s.scalars(select(Signal))).one()
    assert sig.target == "flat"
