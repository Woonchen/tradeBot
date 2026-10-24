"""投資組合回測：跟單一策略回測一致、共用帳戶的部位大小、持倉上限、回撤熔斷。"""

import pytest

from app.strategies import Candle
from app.strategies.backtest import run_backtest
from app.strategies.portfolio import Leg, run_portfolio

from .test_executor import env  # noqa: F401  (pytest fixture)

H4 = 14400


class Scripted:
    """照給定的判斷序列進出場。"""
    warmup = 1

    def __init__(self, targets):
        self.t = targets

    def targets(self, candles):
        return self.t[:len(candles)]


def candles(prices, start=0):
    return [Candle(start + i * H4, p, p * 1.01, p * 0.99, p, 1) for i, p in enumerate(prices)]


def leg(i, prices, targets, mode="equity", value=50, lev=1, funding=None):
    cs = candles(prices)
    res = run_backtest(Scripted(targets), cs, market="futures", size_mode="fixed", size_value=1, leverage=1,
                       start_equity=1e9, warmup=1, funding=funding)
    return Leg(i, f"s{i}", f"S{i}USDT", "futures", mode, value, lev, cs[1:], res.trades)


PRICES = [100, 100, 100, 110, 121, 121, 110, 100, 100, 105]
TARGETS = ["flat", "flat", "long", "long", "flat", "flat", "long", "flat", "flat", "flat"]


def test_single_strategy_matches_regular_backtest():
    funding = [(H4 * 4, 0.05), (H4 * 7 + 100, -0.02)]
    cs = candles(PRICES)
    ref = run_backtest(Scripted(TARGETS), cs, market="futures", size_mode="percent", size_value=100, leverage=2,
                       start_equity=10_000, warmup=1, funding=funding).stats
    out = run_portfolio([leg(1, PRICES, TARGETS, "percent", 100, 2, funding)], start_equity=10_000)
    assert out.stats["final_equity"] == pytest.approx(ref["final_equity"], rel=1e-9)
    assert out.stats["trades"] == ref["trades"] == 2
    assert out.legs[0]["net"] == pytest.approx(ref["final_equity"] - 10_000, rel=1e-9)


def test_shared_account_sizing_and_limits():
    flat = ["flat"] * len(PRICES)
    a = leg(1, PRICES, TARGETS, "equity", 50)           # 第 3 根開盤進場
    b = leg(2, [50] * len(PRICES), ["flat", "flat", "long"] + ["long"] * 7, "equity", 50)  # 同時進場
    c = leg(3, PRICES, TARGETS, "fixed", 1000)           # 可用餘額已被 a、b 用光（b 只能用剩下的 99%）
    out = run_portfolio([a, b, c, leg(4, PRICES, flat)], start_equity=10_000)
    assert out.stats["skipped"] == {"可用餘額不足": 2}  # c 的兩筆都沒有錢進場
    assert out.legs[2]["trades"] == 0 and out.stats["max_concurrent"] == 2
    # a：第一筆用權益 50% = 5,000，下一根開盤 110 進、121 出，每 1 USDT 淨賺 0.09916；
    # 第二筆時權益約 10,495.8 → 部位 5,247.9，100 進 100 出只扣手續費 0.0008
    first = 5000 * 0.09916
    assert out.legs[0]["net"] == pytest.approx(first - (10_000 + first) * 0.5 * 0.0008, abs=0.05)
    limited = run_portfolio([a, b], start_equity=10_000, max_open=1)
    assert limited.stats["skipped"] == {"持倉數已達上限": 1} and limited.legs[1]["trades"] == 0


def test_drawdown_breaker_closes_everything_and_stops():
    crash = [100, 100, 100, 80, 60, 50, 50, 50, 60, 70]
    tg = ["flat", "flat"] + ["long"] * 8
    out = run_portfolio([leg(1, crash, tg, "equity", 100, 2)], start_equity=10_000, max_drawdown_pct=30)
    assert out.stats["stopped_at"] is not None
    stop_i = next(i for i, p in enumerate(out.equity) if p["time"] == out.stats["stopped_at"])
    assert {round(p["value"], 6) for p in out.equity[stop_i:]} == {round(out.equity[stop_i]["value"], 6)}  # 之後不再交易
    assert out.stats["trades"] == 1


async def test_portfolio_api_uses_enabled_live_strategies(env, monkeypatch):  # noqa: F811
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import strategies as api
    from app.api.deps import get_executor, require_user
    from app.db import get_session
    from app.models import StrategyConfig

    ex, sf, _ = env

    async def fake_fetch(adapter, symbol, interval, bars):
        return [Candle(i * H4, 100 + (i % 50), 101 + (i % 50), 99 + (i % 50), 100 + (i % 50), 1) for i in range(bars)]

    monkeypatch.setattr(api, "fetch_candles", fake_fetch)
    async with sf() as s:
        for i, (sym, mode, on) in enumerate([("BTCUSDT", "live", True), ("BTCUSDT", "shadow", True), ("BTCUSDT", "live", False)]):
            s.add(StrategyConfig(name=f"t{i}", strategy="ma_trend", symbol=sym, market="futures", interval="4h",
                                 params={"length": 20}, enabled=on, mode=mode, size_mode="equity", size_value=50, leverage=2))
        await s.commit()

    async def session():
        async with sf() as s:
            yield s

    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides.update({require_user: lambda: "admin", get_executor: lambda: ex, get_session: session})
    c = TestClient(app)
    r = c.post("/api/strategies/portfolio-backtest", json={"bars": 500})
    assert r.status_code == 200, r.text
    d = r.json()
    assert [x["name"] for x in d["legs"]] == ["t0"]  # 預設只跑啟用中、實際下單的
    assert d["stats"]["trades"] > 0 and len(d["equity"]) > 400
    d2 = c.post("/api/strategies/portfolio-backtest", json={"ids": [1, 2, 3], "bars": 500}).json()
    assert len(d2["legs"]) == 3
    assert c.post("/api/strategies/portfolio-backtest", json={"ids": [99]}).status_code == 422
    # 這次回測統一用別的下單規則：不改策略本身；部位加倍 → 損益變大（虧損會複利縮小本金，所以不到兩倍）
    big = c.post("/api/strategies/portfolio-backtest", json={"bars": 500, "size_value": 100, "leverage": 2}).json()
    assert big["legs"][0]["size_mode"] == "equity" and big["legs"][0]["size_value"] == 100 and big["legs"][0]["leverage"] == 2
    assert abs(big["legs"][0]["net"]) > abs(d["legs"][0]["net"]) * 1.2
    async with sf() as s:
        assert (await s.get(StrategyConfig, 1)).size_value == 50
