import time

from ..exchange.base import ExchangeAdapter
from .base import INTERVAL_SECONDS, Candle


async def fetch_candles(adapter: ExchangeAdapter, symbol: str, interval: str, bars: int, closed_only: bool = True) -> list[Candle]:
    """抓最近 bars 根 K 棒（自動分頁），預設去掉還沒收盤的最後一根。"""
    sec = INTERVAL_SECONDS[interval]
    now = int(time.time())
    since = (now // sec - bars - 1) * sec
    out: dict[int, Candle] = {}
    page = 1000
    while since < now:
        rows = await adapter.fetch_ohlcv(symbol, interval, page, since=since * 1000)
        if not rows:
            break
        for r in rows:
            t = int(r[0]) // 1000
            out[t] = Candle(t, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
        last = int(rows[-1][0]) // 1000
        if last + sec <= since or len(rows) < 2:
            break
        since = last + sec
    candles = [out[t] for t in sorted(out)]
    if closed_only:
        candles = [c for c in candles if c.time + sec <= now]
    return candles[-bars:]
