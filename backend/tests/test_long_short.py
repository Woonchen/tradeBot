"""散戶多空比：每小時取樣、歷史補齊、對齊到 K 棒（不偷看未來）、條件組合器。"""

import io
import time
import zipfile
from datetime import date, datetime, timezone

import httpx

from app.models import MarketMetric
from app.services.sentiment import LS_METRIC, Sentiment, parse_vision_metrics
from app.strategies import Candle, build_strategy
from app.strategies.external import LAG, MAX_AGE, align
from sqlalchemy import select

from .test_executor import env  # noqa: F401  (pytest fixture)

H = 3600
T0 = 1_760_000_000 // (4 * H) * (4 * H)  # 某根 4h K 的開盤


def ts(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())


def vision_zip(day: str, values: dict[str, float]) -> bytes:
    """data.binance.vision 每日 metrics 檔：create_time → count_long_short_ratio。"""
    lines = ["create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,"
             "sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio"]
    lines += [f"{day} {hm}:00,BTCUSDT,1,1,1,1,{v},1" for hm, v in values.items()]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"BTCUSDT-metrics-{day}.csv", "\n".join(lines))
    return buf.getvalue()


def test_vision_file_converted_to_api_time_and_sampled_hourly():
    rows = parse_vision_metrics("BTCUSDT", vision_zip("2024-01-05", {"00:45": 1.0, "00:50": 2.0, "00:55": 3.0, "01:50": 4.0}))
    # 檔案時間 + 5 分鐘 = API 時間；只留 API 時間 :55 分那筆（檔案的 :50）
    assert sorted((r["ts"], r["value"]) for r in rows) == [(ts("2024-01-05 00:55:00"), 2.0), (ts("2024-01-05 01:55:00"), 4.0)]


def test_align_uses_value_five_minutes_before_close_and_drops_stale():
    candles = [Candle(T0 + i * 4 * H, 1, 1, 1, 1, 1) for i in range(3)]
    close0 = T0 + 4 * H
    pts = [(close0 - 3 * H - 300, 1.0), (close0 - 300, 2.0), (close0, 9.0)]  # 收盤那一刻的值不能用
    assert align(pts, candles, LAG["long_short"], MAX_AGE["long_short"]) == [2.0, None, None]  # 後兩根：最新一筆已超過 2 小時
    pts.append((close0 + 4 * H - 300, 5.0))
    assert align(sorted(pts), candles, LAG["long_short"], MAX_AGE["long_short"])[:2] == [2.0, 5.0]


def fake(calls: list, now: int):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        p = req.url.params
        if req.url.path == "/futures/data/globalLongShortAccountRatio":
            a, b = int(p["startTime"]) // 1000, int(p["endTime"]) // 1000
            pts = [t for t in range((a + 299) // 300 * 300, b + 1, 300) if t >= now - 30 * 86400][:int(p["limit"])]
            return httpx.Response(200, json=[{"timestamp": t * 1000, "longShortRatio": str(1 + (t // H) % 3)} for t in pts])
        if "data.binance.vision" in req.url.host:
            day = req.url.path.rsplit("-metrics-", 1)[1][:10]
            if day < "2023-11-03":  # 這個幣從 2023-11-03 才有資料
                return httpx.Response(404)
            if req.method == "HEAD":
                return httpx.Response(200)
            return httpx.Response(200, content=vision_zip(day, {f"{h:02d}:50": 2.5 for h in range(24)}))
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_refresh_and_backfill(env, monkeypatch):  # noqa: F811
    _, sf, _ = env
    now = ts("2024-01-10 12:03:00")
    monkeypatch.setattr(time, "time", lambda: now)
    calls: list = []
    svc = Sentiment(sf, fake(calls, now))
    series = await svc.long_short_series("BTCUSDT.P")
    assert series and all(t % H == 3300 for t, _ in series)  # 每小時 :55 一筆
    assert series[-1][0] == ts("2024-01-10 11:55:00")
    n_api = len(calls)
    await svc.long_short_series("BTCUSDT")  # 已經是最新：不再向外抓
    assert len(calls) == n_api

    monkeypatch.setattr("app.services.sentiment.LS_START", date(2023, 10, 1))
    days = await svc.backfill_long_short("BTCUSDT", until=date(2023, 11, 6))
    assert days == 3  # 11-03 ～ 11-05；更早的用二分搜尋跳過，不會一天一天去試
    assert sum(1 for m, _ in calls if m == "GET" and "metrics" in _) == 3
    assert await svc.backfill_long_short("BTCUSDT", until=date(2023, 11, 6)) == 0  # 補過的不重抓
    async with sf() as s:
        rows = (await s.scalars(select(MarketMetric).where(MarketMetric.metric == LS_METRIC, MarketMetric.ts < ts("2023-11-06 00:00:00")))).all()
    assert len(rows) == 3 * 24 and {r.value for r in rows} == {2.5}
    await svc.close()


def test_rule_blocks_entry_when_retail_crowded():
    close = {"kind": "price", "field": "close"}
    ls = {"kind": "long_short"}
    rules = {"rules": {
        "long_entry": {"logic": "and", "conditions": [
            {"left": close, "cmp": ">", "right": {"kind": "value", "value": 0}},
            {"left": ls, "cmp": "<=", "right": {"kind": "value", "value": 2.0}}]},
        "long_exit": {"logic": "or", "conditions": [{"left": ls, "cmp": ">", "right": {"kind": "value", "value": 2.0}}]},
    }}
    s = build_strategy("rules", rules)
    assert s.external_needs() == {"long_short"}
    candles = [Candle(T0 + i * 4 * H, 1, 1, 1, 1, 1) for i in range(4)]
    closes = [T0 + (i + 1) * 4 * H for i in range(4)]
    s.ext = {"long_short": [(closes[0] - 300, 3.0), (closes[1] - 300, 1.5), (closes[2] - 300, 2.5), (closes[3] - 300, 1.8)]}
    assert s.targets(candles) == ["flat", "long", "flat", "long"]
    s.ext = {}  # 沒資料：不進場
    assert set(s.targets(candles)) == {"flat"}


def test_explain_reports_each_condition_at_last_bar():
    close = {"kind": "price", "field": "close"}
    rules = {"rules": {
        "long_entry": {"logic": "and", "conditions": [
            {"left": close, "cmp": ">", "right": {"kind": "sma", "length": 2, "source": "close"}},
            {"left": {"kind": "long_short"}, "cmp": "<=", "right": {"kind": "value", "value": 2.0}}]},
        "long_exit": {"logic": "or", "conditions": [{"left": close, "cmp": "<", "right": {"kind": "sma", "length": 2, "source": "close"}}]},
    }}
    s = build_strategy("rules", rules)
    candles = [Candle(T0 + i * 4 * H, c, c, c, c, 1) for i, c in enumerate([10, 10, 13])]
    s.ext = {"long_short": [(T0 + 3 * 4 * H - 300, 3.27)]}
    e = s.explain(candles)
    assert e["bar_time"] == candles[-1].time and set(e["groups"]) == {"long_entry", "long_exit"}
    entry = e["groups"]["long_entry"]
    assert entry["met"] is False and entry["logic"] == "and"
    price, ls = entry["conditions"]
    assert (price["left"], price["right"], price["ok"], price["price_scale"]) == ("收盤", "SMA2", True, True)
    assert price["left_value"] == 13 and price["right_value"] == 11.5
    assert (ls["left"], ls["right"], ls["left_value"], ls["ok"], ls["price_scale"]) == ("散戶多空比", "2", 3.27, False, False)
