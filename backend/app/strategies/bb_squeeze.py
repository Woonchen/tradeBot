from .base import Candle, Param, Strategy, Target, register
from .indicators import crossover, crossunder, sma
from .rules import _rolling_std


@register
class BollingerSqueeze(Strategy):
    key = "bb_squeeze"
    label = "布林通道盤整突破（只做多）"
    description = (
        "通道寬度連續一段時間低於平均（盤整）後，通道開始擴張且收盤價上穿上軌時做多；"
        "通道縮小且收盤價下穿中軌時出場。只做多，不做空。"
    )
    params = [
        Param("length", "布林通道長度", "int", 20, min=5, max=200),
        Param("mult", "標準差倍數", "float", 2.0, min=0.5, max=5, step=0.1),
        Param("bw_avg_len", "通道寬度平均長度", "int", 100, min=10, max=500,
              help="寬度低於最近這麼多根的平均寬度，視為「收窄」"),
        Param("squeeze_bars", "盤整至少幾根", "int", 20, min=3, max=300,
              help="連續收窄這麼多根才算盤整（15 分 K 的 20 根 = 5 小時）"),
        Param("breakout_window", "盤整結束後幾根內突破有效", "int", 10, min=0, max=100,
              help="突破常發生在寬度剛開始放大時；超過這麼多根才突破就不算這次盤整的突破"),
        Param("exit_on_cross", "出場要剛好下穿中軌", "bool", True,
              help="開啟：只有「收盤價下穿中軌」那一根同時通道縮小才出場，錯過就要等下一次下穿；"
                   "關閉：通道縮小時收盤價在中軌下方就出場"),
    ]

    @property
    def warmup(self) -> int:
        return max(300, (self.p["length"] + self.p["bw_avg_len"] + self.p["squeeze_bars"]) * 3)

    def targets(self, candles: list[Candle]) -> list[Target]:
        p = self.p
        closes = [c.close for c in candles]
        mid = sma(closes, p["length"])
        sd = _rolling_std(closes, p["length"])
        upper = [None if m is None else m + p["mult"] * s for m, s in zip(mid, sd)]
        # 通道寬度用中軌的比例表示，不同價位的幣才能用同一組參數
        width = [None if m is None or not m else (2 * p["mult"] * s) / m for m, s in zip(mid, sd)]
        avg = _sma_partial(width, p["bw_avg_len"])

        out: list[Target] = []
        pos: Target = "flat"
        run = 0  # 目前連續收窄幾根
        last_squeeze = None  # 最近一次「夠長的盤整」最後一根的位置
        for i in range(len(candles)):
            w, a = width[i], avg[i]
            prev_w = width[i - 1] if i > 0 else None
            expanding = w is not None and prev_w is not None and w > prev_w
            contracting = w is not None and prev_w is not None and w < prev_w
            # 只看到前一根為止的盤整：突破那根的寬度常常還在平均以下，不能讓它自己算成「還在盤整」
            after_squeeze = last_squeeze is not None and i - 1 - last_squeeze <= p["breakout_window"]
            if w is not None and a is not None and w < a:
                run += 1
                if run >= p["squeeze_bars"]:
                    last_squeeze = i
            else:
                run = 0

            if pos == "flat":
                if after_squeeze and expanding and crossover(closes, upper, i):
                    pos = "long"
            else:
                below = crossunder(closes, mid, i) if p["exit_on_cross"] else (mid[i] is not None and closes[i] < mid[i])
                if contracting and below:
                    pos = "flat"
            out.append(pos)
        return out


def _sma_partial(values: list[float | None], n: int) -> list[float | None]:
    """前段是 None 的序列取移動平均（寬度在布林通道算出來之前是 None）。"""
    start = next((i for i, v in enumerate(values) if v is not None), len(values))
    return [None] * start + sma([v for v in values[start:]], n)  # type: ignore[misc]
