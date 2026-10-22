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


async def test_runner_skips_flat_without_position(env, monkeypatch):  # noqa: F811
    """啟用時判斷是持倉（沒有下單），之後轉回空手：帳上沒部位，不送訊號。"""
    ex, sf, ad = env
    sec = INTERVAL_SECONDS["1h"]
    now_bar = int(time.time()) // sec * sec
    seq = iter(["long", "flat", "short"])
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
        s.add(StrategyConfig(name="t", strategy="ema_cross", symbol="BTCUSDT", market="futures",
                             interval="1h", params={}, enabled=True))
        await s.commit()
    runner = StrategyRunner(ex)
    for _ in range(3):
        await runner.evaluate(1)
        while not ex.queue.empty():
            await ex.process(await ex.queue.get())
    async with sf() as s:
        sigs = (await s.scalars(select(Signal).order_by(Signal.id))).all()
        cfg = await s.get(StrategyConfig, 1)
    assert [(x.target, x.status) for x in sigs] == [("short", "executed")]
    assert cfg.last_target == "short"


async def test_runner_spot_maps_short_to_flat(env, monkeypatch):  # noqa: F811
    ex, sf, ad = env
    sec = INTERVAL_SECONDS["1h"]
    now_bar = int(time.time()) // sec * sec
    seq = iter(["flat", "long", "short"])
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
    for _ in range(3):
        await runner.evaluate(1)
        while not ex.queue.empty():
            await ex.process(await ex.queue.get())
    async with sf() as s:
        sigs = (await s.scalars(select(Signal).order_by(Signal.id))).all()
    assert [(x.target, x.status) for x in sigs] == [("long", "executed"), ("flat", "executed")]


def test_bb_squeeze_enters_on_breakout_after_squeeze_and_exits_below_middle():
    wide = [100 + (8 if i % 2 else -8) for i in range(150)]  # 大幅震盪：寬度大
    tight = [100 + (0.3 if i % 2 else -0.3) for i in range(60)]  # 盤整：寬度遠低於平均
    breakout = [101, 103, 106, 110, 115]  # 擴張並上穿上軌
    fall = [114, 111, 107, 103, 99, 96, 94, 93]  # 回落到中軌下方、通道開始縮小
    closes = wide + tight + breakout + fall
    s = build_strategy("bb_squeeze")
    t = s.targets(candles_from(closes))
    start = len(wide) + len(tight)
    assert set(t[:start]) == {"flat"}  # 盤整中、大幅震盪中都不進場
    assert t[start] == "long"  # 第一根突破就進場
    assert t[-1] == "flat" and "short" not in t  # 跌回中軌下方出場；只做多

    # 沒有先盤整：同樣的突破不進場
    t2 = s.targets(candles_from(wide + breakout + fall))
    assert "long" not in t2


def test_bb_squeeze_trend_filter_blocks_breakouts_below_ema():
    # 先在高價區大幅震盪，讓 EMA200 遠高於之後的盤整區
    wide = [200 + (8 if i % 2 else -8) for i in range(250)]
    tight = [100 + (0.3 if i % 2 else -0.3) for i in range(60)]
    breakout = [101, 103, 106, 110, 115]
    closes = wide + tight + breakout
    start = len(wide) + len(tight)
    assert build_strategy("bb_squeeze").targets(candles_from(closes))[start] == "long"  # 預設不過濾
    assert "long" not in build_strategy("bb_squeeze", {"trend_ema": 200}).targets(candles_from(closes))  # 在 EMA200 之下不做多


def test_backtest_warmup_skips_bars_and_waits_for_a_change():
    c = [Candle(i, 100 + i, 100 + i, 100 + i, 100 + i, 1) for i in range(8)]
    # 暖機 3 根；起點當下的判斷是 long（策略早就在多單裡），要等判斷改變才算
    seq = ["flat", "long", "long", "long", "flat", "long", "long", "flat"]
    r = run_backtest(Flip(seq), c, market="futures", size_value=100, fee_rate=0.0, warmup=3)
    assert r.stats["bars"] == 5 and r.stats["from"] == 3  # 暖機的 K 棒不計入
    assert [(t["entry_time"], t["exit_time"]) for t in r.trades] == [(6, 7)]  # 第 5 根轉 long → 第 6 根開盤進場

    # 起點是空手就跟沒有暖機一樣，正常進場
    r2 = run_backtest(Flip(["flat", "flat", "flat", "long", "long", "flat", "flat", "flat"]), c,
                      market="futures", size_value=100, fee_rate=0.0, warmup=2)
    assert [(t["entry_time"], t["exit_time"]) for t in r2.trades] == [(4, 6)]


async def test_backtest_api_warmup_and_earlier_period(env, monkeypatch):  # noqa: F811
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import strategies as api
    from app.api.deps import get_executor, require_user
    from app.db import get_session

    ex, sf, ad = env
    fetched = {}

    async def fake_fetch(adapter, symbol, interval, bars):
        fetched["bars"] = bars
        return candles_from([100 + (i % 7) for i in range(bars)], step=900)

    class Scripted(Flip):
        warmup = 30

        def __init__(self):
            self.p = {}

        def targets(self, candles):
            return ["long" if i % 10 < 5 else "flat" for i in range(len(candles))]

    monkeypatch.setattr(api, "fetch_candles", fake_fetch)
    monkeypatch.setattr(api, "build_strategy", lambda k, p: Scripted())

    async def session():
        async with sf() as s:
            yield s

    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides.update({require_user: lambda: "admin", get_executor: lambda: ex, get_session: session})
    body = {"strategy": "x", "symbol": "BTCUSDT", "market": "futures", "interval": "15m", "bars": 200,
            "compare_earlier": True, "size_mode": "fixed", "size_value": 500, "leverage": 3}
    r = TestClient(app).post("/api/strategies/backtest", json=body)
    assert r.status_code == 200, r.text
    d = r.json()
    assert fetched["bars"] == 2 * 200 + 30  # 兩段 + 暖機
    assert d["warmup"] == 30 and len(d["candles"]) == 200 and d["stats"]["bars"] == 200
    assert d["sizing"] == {"market": "futures", "size_mode": "fixed", "size_value": 500, "leverage": 3, "start_equity": 10000,
                           "stop_loss_pct": 0, "take_profit_pct": 0}  # 沒填：用設定頁的（測試資料是 0）
    assert d["earlier"]["bars"] == 200 and d["earlier"]["to"] < d["stats"]["from"]  # 更早那段在前面、不重疊

    from app.models import BotSettings

    async with sf() as s:
        (await s.get(BotSettings, 1)).stop_loss_pct = 7
        await s.commit()
    body["compare_earlier"] = False
    assert TestClient(app).post("/api/strategies/backtest", json=body).json()["sizing"]["stop_loss_pct"] == 7
    body["stop_loss_pct"] = 0  # 明確填 0 = 不設停損，不會被設定頁的值蓋掉
    assert TestClient(app).post("/api/strategies/backtest", json=body).json()["sizing"]["stop_loss_pct"] == 0

    body["bars"] = 20001
    assert TestClient(app).post("/api/strategies/backtest", json=body).status_code == 422


def bar(t, o, h, l, c):  # noqa: E741
    return Candle(t, o, h, l, c, 1)


def test_backtest_stop_loss_and_no_reentry_until_signal_changes():
    c = [
        bar(0, 100, 100, 100, 100),
        bar(1, 100, 101, 99, 100),   # 第 0 根判斷 long → 開盤 100 進場，停損 95
        bar(2, 100, 100, 94, 96),    # 最低 94 觸及 95 → 以 95 停損出場
        bar(3, 96, 97, 95, 96),      # 判斷還是 long：不重新進場
        bar(4, 96, 97, 95, 96),      # 判斷轉 flat（第 3 根）
        bar(5, 96, 97, 95, 96),      # 第 4 根判斷又轉 long → 這根開盤重新進場
        bar(6, 96, 96, 96, 96),
    ]
    seq = ["long", "long", "long", "flat", "long", "long", "long"]
    r = run_backtest(Flip(seq), c, market="futures", size_value=100, fee_rate=0.0, stop_loss_pct=5)
    assert [(t["entry_time"], t["exit_time"], t["exit"], t["exit_reason"]) for t in r.trades] == [
        (1, 2, 95, "stop"), (5, 6, 96, "end"),
    ]


def test_backtest_gap_take_profit_and_same_bar_conservative():
    seq = ["long"] * 4
    gap = [bar(0, 100, 100, 100, 100), bar(1, 100, 100, 100, 100), bar(2, 90, 92, 88, 91), bar(3, 91, 91, 91, 91)]
    (t,) = run_backtest(Flip(seq), gap, market="futures", size_value=100, fee_rate=0.0, stop_loss_pct=5).trades
    assert (t["exit"], t["exit_reason"]) == (90, "stop")  # 開盤就跳空到停損價以下：以開盤價出場

    tp = [bar(0, 100, 100, 100, 100), bar(1, 100, 100, 100, 100), bar(2, 100, 111, 99, 105), bar(3, 105, 105, 105, 105)]
    (t,) = run_backtest(Flip(seq), tp, market="futures", size_value=100, fee_rate=0.0, take_profit_pct=10).trades
    assert t["exit"] == pytest.approx(110) and t["exit_reason"] == "take_profit"

    both = [bar(0, 100, 100, 100, 100), bar(1, 100, 100, 100, 100), bar(2, 100, 111, 94, 100), bar(3, 100, 100, 100, 100)]
    (t,) = run_backtest(Flip(seq), both, market="futures", size_value=100, fee_rate=0.0,
                        stop_loss_pct=5, take_profit_pct=10).trades
    assert t["exit_reason"] == "stop"  # 同一根碰到兩邊：保守當作先停損

    short = [bar(0, 100, 100, 100, 100), bar(1, 100, 100, 100, 100), bar(2, 100, 106, 99, 100), bar(3, 100, 100, 100, 100)]
    (t,) = run_backtest(Flip(["short"] * 4), short, market="futures", size_value=100, fee_rate=0.0, stop_loss_pct=5).trades
    assert t["exit"] == pytest.approx(105) and t["exit_reason"] == "stop"
