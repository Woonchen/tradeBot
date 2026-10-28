"""成交量分佈（Volume Profile）：上一個交易時段（UTC 日／週一起算的週）的 POC、VAH、VAL。

演算法跟前端 K 線圖的 VP（frontend/src/components/volumeProfile.ts）一致：
- 時段內最高到最低價均分成 rows 個價格桶，每根 K 棒的成交量按 [低, 高] 跟各桶重疊的長度比例分配
  （高低相同的 K 棒整根算進所在的桶）
- POC = 量最大那個桶的中間價；價值區從 POC 往上下擴張，每次加入相鄰兩桶中量較大的一邊（平手取上方），
  直到累積量達 value_area %；VAH / VAL 是價值區的上下緣
"""

from .base import Candle

DAY = 86400
WEEK = 7 * DAY
_WEEK_SHIFT = 4 * DAY  # 1970-01-01 是週四，位移 4 天讓週一 00:00 UTC 成為週的起點


def period_start(t: int, period: str) -> int:
    if period == "day":
        return t // DAY * DAY
    return (t - _WEEK_SHIFT) // WEEK * WEEK + _WEEK_SHIFT


def profile(candles: list[Candle], rows: int, value_area: float) -> tuple[float, float, float] | None:
    """回傳 (POC, VAH, VAL)；沒有波動或沒有量時回傳 None。value_area 是比例（0.7 = 70%）。"""
    if not candles:
        return None
    lo = min(c.low for c in candles)
    hi = max(c.high for c in candles)
    if not hi > lo:
        return None
    step = (hi - lo) / rows
    vol = [0.0] * rows
    bin_of = lambda p: min(rows - 1, max(0, int((p - lo) / step)))  # noqa: E731
    for c in candles:
        if c.volume <= 0:
            continue
        rng = c.high - c.low
        if rng <= 0:
            vol[bin_of(c.close)] += c.volume
            continue
        for i in range(bin_of(c.low), bin_of(c.high) + 1):
            overlap = min(c.high, lo + (i + 1) * step) - max(c.low, lo + i * step)
            if overlap > 0:
                vol[i] += c.volume * overlap / rng
    total = sum(vol)
    if total <= 0:
        return None
    poc = max(range(rows), key=lambda i: (vol[i], -i))  # 同量取最低的桶（跟前端一樣取第一個）
    acc, dn, up = vol[poc], poc, poc
    while acc < total * value_area and (dn > 0 or up < rows - 1):
        up_vol = vol[up + 1] if up < rows - 1 else -1.0
        dn_vol = vol[dn - 1] if dn > 0 else -1.0
        if up_vol >= dn_vol:
            up += 1
            acc += vol[up]
        else:
            dn -= 1
            acc += vol[dn]
    return lo + (poc + 0.5) * step, lo + (up + 1) * step, lo + dn * step


def previous_period_levels(candles: list[Candle], period: str, rows: int, value_area: float) -> list[tuple | None]:
    """每根 K 棒可用的「上一個時段」POC/VAH/VAL（只用已經結束的時段，沒有未來資料）。

    上一個時段的 K 棒沒有從時段開頭就有（資料不夠長）時回傳 None，避免拿半個時段的分佈當真。
    """
    out: list[tuple | None] = [None] * len(candles)
    if not candles:
        return out
    first = candles[0].time
    starts = [period_start(c.time, period) for c in candles]
    cache: dict[int, tuple | None] = {}
    # 每個時段的 K 棒範圍
    ranges: dict[int, tuple[int, int]] = {}
    for i, s in enumerate(starts):
        a, _ = ranges.get(s, (i, i))
        ranges[s] = (a, i + 1)
    length = DAY if period == "day" else WEEK
    for i, s in enumerate(starts):
        prev = s - length
        if prev not in cache:
            r = ranges.get(prev)
            # 上一個時段完整：資料在它開始時（或更早）就有了
            cache[prev] = profile(candles[r[0]:r[1]], rows, value_area) if r and first <= prev else None
        out[i] = cache[prev]
    return out
