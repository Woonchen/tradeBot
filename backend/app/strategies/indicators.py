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


def crossover(a: list[float | None], b: list[float | None], i: int) -> bool:
    """第 i 根 a 由下往上穿過 b。"""
    return i > 0 and None not in (a[i], b[i], a[i - 1], b[i - 1]) and a[i - 1] <= b[i - 1] and a[i] > b[i]


def crossunder(a: list[float | None], b: list[float | None], i: int) -> bool:
    return i > 0 and None not in (a[i], b[i], a[i - 1], b[i - 1]) and a[i - 1] >= b[i - 1] and a[i] < b[i]
