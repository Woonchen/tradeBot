"""策略用到的外部資料（K 線以外）：恐懼貪婪指數、資金費率、散戶多空比。

資料由呼叫端（策略執行、回測）事先載入到 strategy.ext，計算時對齊到每根 K 棒。
"""

from .base import Candle

# 規則裡用得到的外部資料種類
EXTERNAL_KINDS = ("fear_greed", "funding_rate", "long_short")

# 散戶多空比每小時只存一筆（Binance API 時間 :55 分那筆，那一刻發布）。
# 截止時間 = 收盤前 4 分鐘：4h K 收盤時用的是收盤前 5 分鐘的值，實際執行時一定已經拿得到。
# 超過 2 小時沒有新資料就當作缺值，條件不成立。
LAG = {"long_short": 240}
MAX_AGE = {"long_short": 7200}


def align(points: list[tuple[int, float]], candles: list[Candle], lag: int = 0, max_age: int | None = None) -> list[float | None]:
    """每根 K 棒取「收盤之前」最後一筆資料（時間嚴格早於收盤 − lag 秒）。

    收盤當下才公布的資料不能用，否則回測會偷看到未來：例如日 K 在 00:00 UTC 收盤，
    同一刻公布的恐懼貪婪指數算是下一根的資料。points 要依時間排序（秒）。
    max_age：最後一筆比截止時間舊超過這麼多秒就當作缺值（None）。
    """
    if not candles:
        return []
    sec = candles[1].time - candles[0].time if len(candles) > 1 else 0
    out: list[float | None] = []
    j = -1
    for c in candles:
        cutoff = c.time + sec - lag
        while j + 1 < len(points) and points[j + 1][0] < cutoff:
            j += 1
        if j < 0 or (max_age is not None and cutoff - points[j][0] > max_age):
            out.append(None)
        else:
            out.append(points[j][1])
    return out
