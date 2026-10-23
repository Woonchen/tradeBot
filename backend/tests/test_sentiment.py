import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import FearGreed, FundingRate, MarketMetric
from app.services.sentiment import DAY, Sentiment
from app.strategies import Candle, build_strategy
from app.strategies.external import align

from .test_executor import env  # noqa: F401  (pytest fixture)

D0 = 1_760_000_000 // DAY * DAY  # 某天 00:00 UTC


def day_candles(closes, start=D0):
    return [Candle(start + i * DAY, c, c, c, c, 1) for i, c in enumerate(closes)]


def test_align_never_uses_data_published_at_or_after_close():
    candles = day_candles([1, 2, 3])
    # 指數在每天 00:00 公布：第 0 根日 K 在 D0+1天 收盤，同一刻公布的值屬於下一根
    points = [(D0, 10.0), (D0 + DAY, 20.0), (D0 + 2 * DAY, 30.0)]
    assert align(points, candles) == [10.0, 20.0, 30.0]
    assert align([(D0 + DAY, 20.0)], candles) == [None, 20.0, 20.0]
    assert align([], candles) == [None, None, None]


def fake_api(calls: list):
    """假的 Alternative.me 與 Binance 公開 API。"""
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        p = req.url.params
        if "alternative.me" in req.url.host:
            data = [{"timestamp": str(D0 + i * DAY), "value": str(20 + i), "value_classification": "Fear"} for i in range(3)]
            return httpx.Response(200, json={"data": data[::-1]})
        if req.url.path == "/fapi/v1/fundingRate":
            start = int(p["startTime"])
            all_rates = [{"fundingTime": (D0 + i * 8 * 3600) * 1000, "fundingRate": str(0.0001 * (i % 3))} for i in range(1500)]
            page = [r for r in all_rates if r["fundingTime"] >= start][:int(p["limit"])]
            return httpx.Response(200, json=page)
        if req.url.path.startswith("/futures/data/"):
            if p["symbol"] == "SPOTONLYUSDT":
                return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})
            field = {"openInterestHist": "sumOpenInterestValue", "takerlongshortRatio": "buySellRatio"}.get(
                req.url.path.rsplit("/", 1)[-1], "longShortRatio")
            return httpx.Response(200, json=[{"timestamp": (D0 + i * DAY) * 1000, field: str(1 + i / 10)} for i in range(30)])
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_service_stores_history_and_is_idempotent(env):  # noqa: F811
    _, sf, _ = env
    calls: list = []
    svc = Sentiment(sf, fake_api(calls))

    fg = await svc.fear_greed_series()
    assert fg == [(D0, 20.0), (D0 + DAY, 21.0), (D0 + 2 * DAY, 22.0)]

    fr = await svc.funding_series("BTCUSDT.P")  # TradingView 寫法也認得
    assert len(fr) == 1500 and fr[1] == (D0 + 8 * 3600, pytest.approx(0.01))  # 單位是 %
    assert sum("fundingRate" in c for c in calls) == 2  # 1000 + 500 兩頁

    await svc.record_metrics(["BTCUSDT", "SPOTONLYUSDT"])
    await svc.record_metrics(["BTCUSDT"])  # 再跑一次：同一天的資料覆蓋，不會重複
    async with sf() as s:
        assert await s.scalar(select(func.count()).select_from(MarketMetric)) == 4 * 30
        assert await s.scalar(select(func.count()).select_from(MarketMetric).where(MarketMetric.symbol == "SPOTONLYUSDT")) == 0
        assert await s.scalar(select(func.count()).select_from(FearGreed)) == 3
        assert await s.scalar(select(func.count()).select_from(FundingRate)) == 1500
    await svc.close()


def fg_rules(below: float) -> dict:
    fg = {"kind": "fear_greed"}
    return {"rules": {
        "long_entry": {"logic": "and", "conditions": [{"left": fg, "cmp": "<", "right": {"kind": "value", "value": below}}]},
        "long_exit": {"logic": "or", "conditions": [{"left": fg, "cmp": ">", "right": {"kind": "value", "value": 60}}]},
    }}


def test_rules_use_fear_greed():
    s = build_strategy("rules", fg_rules(25))
    assert s.external_needs() == {"fear_greed"}
    candles = day_candles([100] * 5)
    s.ext = {"fear_greed": [(D0, 50.0), (D0 + DAY, 20.0), (D0 + 2 * DAY, 70.0), (D0 + 3 * DAY, 30.0)]}
    # 第 i 根用的是當天 00:00 公布的值（第 i 根收盤前）
    assert s.targets(candles) == ["flat", "long", "flat", "flat", "flat"]
    s.ext = {}  # 沒有資料：條件不成立，不會亂進場
    assert set(s.targets(candles)) == {"flat"}
    assert build_strategy("ema_cross").__class__.__name__ == "EmaCross"  # 其他策略不受影響


async def test_sentiment_api_and_backtest_attach(env, monkeypatch):  # noqa: F811
    from app.api import sentiment as sentiment_api
    from app.api import strategies as strategies_api
    from app.api.deps import get_executor, require_user
    from app.db import get_session

    ex, sf, _ = env
    svc = Sentiment(sf, fake_api([]))
    await svc.refresh_all(["BTCUSDT"])

    async def session():
        async with sf() as s:
            yield s

    async def fake_fetch(adapter, symbol, interval, bars):
        return day_candles([100.0] * bars, start=D0 - 200 * DAY)

    monkeypatch.setattr(strategies_api, "fetch_candles", fake_fetch)
    app = FastAPI()
    app.include_router(sentiment_api.router)
    app.include_router(strategies_api.router)
    app.state.sentiment = svc
    app.dependency_overrides.update({require_user: lambda: "admin", get_executor: lambda: ex, get_session: session})
    c = TestClient(app)

    d = c.get("/api/sentiment").json()
    assert d["fear_greed"]["value"] == 22 and d["fear_greed"]["prev"] == 21
    btc = next(x for x in d["symbols"] if x["symbol"] == "BTCUSDT")
    assert btc["long_short"]["value"] == pytest.approx(3.9) and btc["long_short"]["prev"] == pytest.approx(3.8)
    assert btc["funding"]["rate"] is not None and d["recorded_days"] == 30

    body = {"strategy": "rules", "symbol": "BTCUSDT", "market": "futures", "interval": "1d", "bars": 100,
            "params": fg_rules(25), "size_mode": "fixed", "size_value": 100, "leverage": 1}
    r = c.post("/api/strategies/backtest", content=json.dumps(body), headers={"Content-Type": "application/json"})
    assert r.status_code == 200, r.text  # 資料由 app.state.sentiment 自動載入
    # 沒有 external_needs 的內建策略也要能回測（不能因為檢查外部資料而出錯）
    plain = {**body, "strategy": "ma_trend", "params": {"length": 20}}
    assert c.post("/api/strategies/backtest", json=plain).status_code == 200
    sweep = {**plain, "x": {"param": "length", "values": [10, 20]}}
    assert c.post("/api/strategies/sweep", json=sweep).status_code == 200

    # 合約回測自動扣歷史資金費率；現貨不扣
    hold = {**body, "strategy": "ma_trend", "params": {"length": 2}, "interval": "1h", "bars": 300}
    fut = c.post("/api/strategies/backtest", json=hold).json()["stats"]
    spot = c.post("/api/strategies/backtest", json={**hold, "market": "spot"}).json()["stats"]
    assert fut["funding_included"] is True and spot["funding_included"] is False and spot["funding"] == 0
    cell = c.post("/api/strategies/sweep", json={**hold, "x": {"param": "length", "values": [2]}}).json()["cells"][0][0]
    assert cell["recent"]["funding_included"] is True
    await svc.close()
