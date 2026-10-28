from .base import Candle, Param, Strategy, Target, plot, register
from .indicators import atr, sma


@register
class VolumeBreakout(Strategy):
    key = "vol_breakout"
    label = "動能突破：大陽線＋爆量（只做多）"
    description = (
        "一根 K 棒的實體（收盤 − 開盤）大於前一根 ATR 的幾倍、成交量大於前 N 根平均的幾倍，"
        "而且收盤在長期均線之上時做多；持有固定根數，或從持倉期間最高收盤回落幾倍 ATR 就出場（先到先出）。"
        "研究：4h 週期在 9 個主流幣上，突破後 5 天平均有超額報酬；只做多，做空沒有優勢。"
    )
    params = [
        Param("body_atr", "實體至少幾倍 ATR", "float", 2.0, min=0.5, max=10, step=0.1,
              help="收盤 − 開盤 > 這個倍數 × 前一根的 ATR（用前一根，避免這根自己把 ATR 撐大）"),
        Param("vol_mult", "成交量至少幾倍平均", "float", 2.0, min=1, max=20, step=0.1,
              help="這根的成交量 > 這個倍數 × 前 N 根（不含這根）的平均成交量"),
        Param("vol_len", "平均成交量長度", "int", 20, min=2, max=200),
        Param("atr_len", "ATR 長度", "int", 14, min=2, max=200),
        Param("trend_len", "趨勢過濾 SMA 長度", "int", 200, min=0, max=1000,
              help="收盤價高於這條 SMA 才進場；0 = 不過濾。研究裡沒有這個過濾時結果差很多"),
        Param("hold_bars", "持有幾根後出場", "int", 30, min=0, max=1000,
              help="進場後第幾根收盤時出場（4h 的 30 根 = 5 天）；0 = 不用時間出場"),
        Param("trail_atr", "追蹤停損（幾倍 ATR）", "float", 0.0, min=0, max=50, step=0.5,
              help="收盤價比持倉期間最高收盤低這麼多倍 ATR 就出場；0 = 不用。可以跟時間出場一起用，先到先出"),
    ]

    def validate(self):
        if not self.p["hold_bars"] and not self.p["trail_atr"]:
            raise ValueError("至少要設定一種出場方式：持有根數或追蹤停損")

    @property
    def warmup(self) -> int:
        p = self.p
        return max(300, p["trend_len"] + 100, p["vol_len"] + p["atr_len"] * 10)

    def _series(self, candles: list[Candle]) -> dict:
        p = self.p
        closes = [c.close for c in candles]
        a = atr([c.high for c in candles], [c.low for c in candles], closes, p["atr_len"])
        vols = [c.volume for c in candles]
        vma = sma(vols, p["vol_len"])
        n = len(candles)
        return {
            "closes": closes,
            "atr": a,
            "prev_atr": [None] + a[:-1],
            "prev_vma": [None] + vma[:-1],  # 前 N 根（不含這根）的平均量
            "trend": sma(closes, p["trend_len"]) if p["trend_len"] else [None] * n,
        }

    # 1：大陽線（收盤 − 開盤）；-1：大陰線（開盤 − 收盤），見 VolumeDip
    direction = 1

    def _entry_checks(self, c: Candle, x: dict, i: int) -> list[tuple[str, float | None, str, float | None, bool]]:
        p = self.p
        body = (c.close - c.open) * self.direction
        pa, pv, tr = x["prev_atr"][i], x["prev_vma"][i], x["trend"][i]
        need_body = None if pa is None else p["body_atr"] * pa
        need_vol = None if pv is None else p["vol_mult"] * pv
        rows = [
            ("K 棒實體（收盤 − 開盤）" if self.direction == 1 else "K 棒實體（開盤 − 收盤）", body, f"{p['body_atr']:g} × 前一根 ATR{p['atr_len']}", need_body,
             need_body is not None and body > need_body),
            ("成交量", c.volume, f"{p['vol_mult']:g} × 前 {p['vol_len']} 根平均量", need_vol,
             need_vol is not None and c.volume > need_vol),
        ]
        if p["trend_len"]:
            rows.append(("收盤", c.close, f"SMA{p['trend_len']}", tr, tr is not None and c.close > tr))
        return rows

    def _run(self, candles: list[Candle]):
        """逐根跑狀態機，回傳每根的部位、以及最後一根的持倉資訊（給 explain）。"""
        p = self.p
        x = self._series(candles)
        out: list[Target] = []
        inpos, age, peak = False, 0, 0.0
        for i, c in enumerate(candles):
            if inpos:
                age += 1
                peak = max(peak, c.close)
                a = x["atr"][i]
                timed = p["hold_bars"] and age >= p["hold_bars"]
                trailed = p["trail_atr"] and a is not None and c.close < peak - p["trail_atr"] * a
                if timed or trailed:
                    inpos = False
            # 出場那根如果又出現突破，直接重新進場（跟研究的回測一樣）
            if not inpos and all(ok for *_, ok in self._entry_checks(c, x, i)):
                inpos, age, peak = True, 0, c.close
            out.append("long" if inpos else "flat")
        return out, x, (inpos, age, peak)

    def targets(self, candles: list[Candle]) -> list[Target]:
        return self._run(candles)[0]

    def plots(self, candles: list[Candle]) -> list[dict]:
        x = self._series(candles)
        out = []
        if self.p["trend_len"]:
            out.append(plot(f"SMA{self.p['trend_len']}（趨勢過濾）", x["trend"]))
        out.append(plot(f"ATR{self.p['atr_len']}", x["atr"], "sub", "ATR"))
        return out

    def explain(self, candles: list[Candle]) -> dict:
        """最後一根 K 棒的進場條件，以及持倉中的出場條件（給「策略狀態」顯示）。"""
        if not candles:
            return {"bar_time": None, "groups": {}}
        p = self.p
        _, x, (inpos, age, peak) = self._run(candles)
        i = len(candles) - 1
        c = candles[i]
        row = lambda left, lv, right, rv, ok, price=False: {  # noqa: E731
            "left": left, "cmp": ">", "right": right, "left_value": lv, "right_value": rv, "ok": ok, "price_scale": price}
        entry = [row(*r, price=r[0] == "收盤") for r in self._entry_checks(c, x, i)]
        groups = {"long_entry": {"logic": "and", "met": all(r["ok"] for r in entry), "conditions": entry}}
        if inpos:
            exits = []
            if p["hold_bars"]:
                exits.append({"left": "已持有根數", "cmp": ">=", "right": f"{p['hold_bars']} 根", "left_value": age,
                              "right_value": p["hold_bars"], "ok": age >= p["hold_bars"], "price_scale": False})
            a = x["atr"][i]
            if p["trail_atr"] and a is not None:
                stop = peak - p["trail_atr"] * a
                exits.append({"left": "收盤", "cmp": "<", "right": f"最高收盤 − {p['trail_atr']:g} × ATR", "left_value": c.close,
                              "right_value": stop, "ok": c.close < stop, "price_scale": True})
            groups["long_exit"] = {"logic": "or", "met": any(r["ok"] for r in exits), "conditions": exits}
        return {"bar_time": c.time, "groups": groups}


@register
class VolumeDip(VolumeBreakout):
    key = "vol_dip"
    label = "急跌買進：大陰線＋爆量（只做多，短線反彈）"
    description = (
        "一根 K 棒的實體（開盤 − 收盤）大於前一根 ATR 的幾倍、成交量大於前 N 根平均的幾倍時買進，賭短線反彈；"
        "持有固定根數後出場。研究：15m、實體 > 3 倍 ATR、持有 2～4 小時，2020–2024 年小賺，"
        "但 2024-07 之後每年虧 20% 以上（反彈幅度跟手續費差不多）。只建議用影子模式觀察。"
    )
    direction = -1
    params = [
        Param("body_atr", "實體至少幾倍 ATR", "float", 3.0, min=0.5, max=10, step=0.1,
              help="開盤 − 收盤 > 這個倍數 × 前一根的 ATR"),
        Param("vol_mult", "成交量至少幾倍平均", "float", 2.0, min=1, max=20, step=0.1,
              help="這根的成交量 > 這個倍數 × 前 N 根（不含這根）的平均成交量"),
        Param("vol_len", "平均成交量長度", "int", 20, min=2, max=200),
        Param("atr_len", "ATR 長度", "int", 14, min=2, max=200),
        Param("trend_len", "趨勢過濾 SMA 長度", "int", 0, min=0, max=5000,
              help="收盤價高於這條 SMA 才進場；0 = 不過濾（研究裡不過濾比較好）"),
        Param("hold_bars", "持有幾根後出場", "int", 8, min=0, max=1000,
              help="進場後第幾根收盤時出場（15m 的 8 根 = 2 小時）；0 = 不用時間出場"),
        Param("trail_atr", "追蹤停損（幾倍 ATR）", "float", 0.0, min=0, max=50, step=0.5,
              help="收盤價比持倉期間最高收盤低這麼多倍 ATR 就出場；0 = 不用"),
    ]
