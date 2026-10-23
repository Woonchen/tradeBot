from .base import Candle, Param, Strategy, Target, plot, register
from .indicators import rsi


@register
class RsiReversion(Strategy):
    key = "rsi_reversion"
    label = "RSI 超買超賣反轉"
    description = (
        "RSI 由下往上穿過超賣線做多，RSI 回到出場線以上就平倉；"
        "RSI 由上往下穿過超買線做空，回到出場線以下就平倉。適合盤整行情。"
    )
    params = [
        Param("length", "RSI 長度", "int", 14, min=2, max=100),
        Param("oversold", "超賣線", "float", 30, min=1, max=50),
        Param("overbought", "超買線", "float", 70, min=50, max=99),
        Param("exit_level", "出場線", "float", 50, min=1, max=99),
        Param("allow_short", "允許做空", "bool", True, help="現貨一律不會做空"),
    ]

    def validate(self):
        if not self.p["oversold"] < self.p["exit_level"] < self.p["overbought"]:
            raise ValueError("必須滿足：超賣線 < 出場線 < 超買線")

    @property
    def warmup(self) -> int:
        return max(self.p["length"] * 20, 300)

    def plots(self, candles: list[Candle]) -> list[dict]:
        levels = [self.p["oversold"], self.p["exit_level"], self.p["overbought"]]
        return [plot(f"RSI{self.p['length']}", rsi([c.close for c in candles], self.p["length"]), "sub", "RSI", levels)]

    def targets(self, candles: list[Candle]) -> list[Target]:
        r = rsi([c.close for c in candles], self.p["length"])
        lo, hi, mid = self.p["oversold"], self.p["overbought"], self.p["exit_level"]
        out: list[Target] = []
        pos: Target = "flat"
        for i in range(len(candles)):
            cur, prev = r[i], r[i - 1] if i else None
            if cur is not None and prev is not None:
                if pos == "long" and cur >= mid:
                    pos = "flat"
                elif pos == "short" and cur <= mid:
                    pos = "flat"
                if pos == "flat":
                    if prev <= lo < cur:
                        pos = "long"
                    elif self.p["allow_short"] and prev >= hi > cur:
                        pos = "short"
            out.append(pos)
        return out
