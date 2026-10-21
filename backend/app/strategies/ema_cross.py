from .base import Candle, Param, Strategy, Target, register
from .indicators import atr, crossover, crossunder, ema


@register
class EmaCross(Strategy):
    key = "ema_cross"
    label = "EMA 均線交叉 + ATR 停損"
    description = (
        "快線向上穿過慢線做多、向下穿過做空（現貨則賣出）。"
        "進場後若收盤價跌破（空單為漲破）進場價 ± ATR × 倍數就出場，等下一次交叉再進場。"
    )
    params = [
        Param("fast", "快線 EMA 長度", "int", 12, min=2, max=200),
        Param("slow", "慢線 EMA 長度", "int", 26, min=3, max=400),
        Param("atr_len", "ATR 長度", "int", 14, min=2, max=100),
        Param("atr_mult", "ATR 停損倍數", "float", 2.0, min=0, max=20, step=0.1, help="0 = 不使用 ATR 停損"),
        Param("allow_short", "允許做空", "bool", True, help="現貨一律不會做空"),
    ]

    def validate(self):
        if self.p["fast"] >= self.p["slow"]:
            raise ValueError("快線長度必須小於慢線長度")

    @property
    def warmup(self) -> int:
        return max(self.p["slow"] * 10, 300)

    def targets(self, candles: list[Candle]) -> list[Target]:
        closes = [c.close for c in candles]
        fast, slow = ema(closes, self.p["fast"]), ema(closes, self.p["slow"])
        a = atr([c.high for c in candles], [c.low for c in candles], closes, self.p["atr_len"])
        mult = self.p["atr_mult"]

        out: list[Target] = []
        pos: Target = "flat"
        stop: float | None = None
        for i, c in enumerate(candles):
            if crossover(fast, slow, i):
                pos = "long"
                stop = c.close - a[i] * mult if mult > 0 and a[i] else None
            elif crossunder(fast, slow, i):
                pos = "short" if self.p["allow_short"] else "flat"
                stop = c.close + a[i] * mult if mult > 0 and a[i] and pos == "short" else None
            elif stop is not None and ((pos == "long" and c.close <= stop) or (pos == "short" and c.close >= stop)):
                pos, stop = "flat", None
            out.append(pos)
        return out
