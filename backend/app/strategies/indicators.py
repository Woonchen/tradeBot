"""常用技術指標。全部是「因果」的：第 i 個值只用到第 i 根（含）以前的資料，回測不會偷看未來。

資料不足時回傳 None。
"""


def sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(values: list[float], n: int) -> list[float | None]:
    """指數移動平均，以前 n 根的 SMA 當起點（跟 TradingView ta.ema 一樣）。"""
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rma(values: list[float], n: int) -> list[float | None]:
    """Wilder 平滑（RSI、ATR 用的移動平均）。"""
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = (prev * (n - 1) + values[i]) / n
        out[i] = prev
    return out


def rsi(closes: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [0.0] + [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [0.0] + [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_g = rma(gains[1:], n)
    avg_l = rma(losses[1:], n)
    for i in range(len(avg_g)):
        g, l = avg_g[i], avg_l[i]
        if g is None or l is None:
            continue
        out[i + 1] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def atr(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> list[float | None]:
    tr = [highs[0] - lows[0]] + [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    return rma(tr, n)


def kd(highs: list[float], lows: list[float], closes: list[float], n: int = 9) -> tuple[list[float | None], list[float | None]]:
    """KD 隨機指標（台灣看盤軟體常用的算法，跟前端 K 線圖一致）。

    RSV = (收盤 − n 根最低) ÷ (n 根最高 − n 根最低) × 100；K = ⅔·前K + ⅓·RSV；D = ⅔·前D + ⅓·K；起始都是 50。
    n 根內完全沒有波動時 RSV 當作 50。
    """
    k: list[float | None] = [None] * len(closes)
    d: list[float | None] = [None] * len(closes)
    pk = pd = 50.0
    for i in range(n - 1, len(closes)):
        hi, lo = max(highs[i - n + 1:i + 1]), min(lows[i - n + 1:i + 1])
        rsv = 50.0 if hi == lo else (closes[i] - lo) / (hi - lo) * 100
        pk = pk * 2 / 3 + rsv / 3
        pd = pd * 2 / 3 + pk / 3
        k[i], d[i] = pk, pd
    return k, d


def crossover(a: list[float | None], b: list[float | None], i: int) -> bool:
    """第 i 根 a 由下往上穿過 b。"""
    return i > 0 and None not in (a[i], b[i], a[i - 1], b[i - 1]) and a[i - 1] <= b[i - 1] and a[i] > b[i]


def crossunder(a: list[float | None], b: list[float | None], i: int) -> bool:
    return i > 0 and None not in (a[i], b[i], a[i - 1], b[i - 1]) and a[i - 1] >= b[i - 1] and a[i] < b[i]
