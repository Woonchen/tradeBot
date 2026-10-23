from .base import Candle, Param, Strategy, Target, register
from .indicators import ema, sma


@register
class MaTrend(Strategy):
    key = "ma_trend"
    label = "均線趨勢（收盤價在均線上方做多）"
    description = (
        "收盤價在均線上方就持有多單，跌到均線下方就出場（允許做空時改為做空）。"
        "最簡單的趨勢跟隨，日線週期最常用；均線長度可以用參數掃描檢查穩健性。"
    )
    params = [
        Param("length", "均線長度", "int", 50, min=2, max=500),
        Param("use_ema", "使用 EMA", "bool", False, help="否 = SMA（簡單移動平均）"),
        Param("allow_short", "允許做空", "bool", False, help="現貨一律不會做空"),
    ]

    @property
    def warmup(self) -> int:
        return max(300, self.p["length"] * (5 if self.p["use_ema"] else 2))

    def targets(self, candles: list[Candle]) -> list[Target]:
        closes = [c.close for c in candles]
        ma = (ema if self.p["use_ema"] else sma)(closes, self.p["length"])
        out: list[Target] = []
        pos: Target = "flat"
        for c, m in zip(closes, ma):
            if m is not None:
                if c > m:
                    pos = "long"
                elif c < m:
                    pos = "short" if self.p["allow_short"] else "flat"
                # 剛好等於均線：維持原本的部位
            out.append(pos)
        return out
