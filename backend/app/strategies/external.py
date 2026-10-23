"""策略用到的外部資料（K 線以外）：恐懼貪婪指數、資金費率。

資料由呼叫端（策略執行、回測）事先載入到 strategy.ext，計算時對齊到每根 K 棒。
"""

from .base import Candle

# 規則裡用得到的外部資料種類
EXTERNAL_KINDS = ("fear_greed", "funding_rate")


def align(points: list[tuple[int, float]], candles: list[Candle]) -> list[float | None]:
    """每根 K 棒取「收盤之前」最後一筆資料（時間嚴格早於收盤）。

    收盤當下才公布的資料不能用，否則回測會偷看到未來：例如日 K 在 00:00 UTC 收盤，
    同一刻公布的恐懼貪婪指數算是下一根的資料。points 要依時間排序（秒）。
    """
    if not candles:
        return []
    sec = candles[1].time - candles[0].time if len(candles) > 1 else 0
    out: list[float | None] = []
    j = -1
    for c in candles:
        close = c.time + sec
        while j + 1 < len(points) and points[j + 1][0] < close:
            j += 1
        out.append(points[j][1] if j >= 0 else None)
    return out
