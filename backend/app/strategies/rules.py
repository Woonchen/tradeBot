"""條件組合器：在網頁上用「指標 比較 指標/數值」組出策略，不用寫程式。

規則以 JSON 儲存在策略的 params["rules"]，這裡負責驗證與計算。伺服器只計算白名單內的指標，
不會執行任何使用者提供的程式碼。
"""

import math
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .base import Candle, Param, Strategy, Target, plot, register
from .external import EXTERNAL_KINDS, LAG, MAX_AGE, align
from .indicators import atr, crossover, crossunder, ema, kd, rsi, sma

Source = Literal["open", "high", "low", "close", "volume"]


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Value(_Op):
    kind: Literal["value"]
    value: float


class Price(_Op):
    kind: Literal["price"]
    field: Source = "close"


class MovingAverage(_Op):
    kind: Literal["sma", "ema"]
    length: int = Field(20, ge=1, le=500)
    source: Source = "close"


class Rsi(_Op):
    kind: Literal["rsi"]
    length: int = Field(14, ge=2, le=200)


class Atr(_Op):
    kind: Literal["atr"]
    length: int = Field(14, ge=1, le=200)


class Channel(_Op):
    """前 N 根（不含當根）的最高價 / 最低價。"""

    kind: Literal["highest", "lowest"]
    length: int = Field(20, ge=1, le=500)


class Bollinger(_Op):
    kind: Literal["bb_upper", "bb_middle", "bb_lower"]
    length: int = Field(20, ge=2, le=500)
    mult: float = Field(2.0, gt=0, le=10)


class Kd(_Op):
    """KD 隨機指標的 K 值或 D 值（0～100）。"""

    kind: Literal["kd_k", "kd_d"]
    length: int = Field(9, ge=2, le=200)


class External(_Op):
    """市場情緒：恐懼貪婪指數（0～100）、資金費率（每 8 小時 %，0.01 = 0.01%）、散戶多空比（帳戶數，多 ÷ 空）。"""

    kind: Literal["fear_greed", "funding_rate", "long_short"]


class Macd(_Op):
    kind: Literal["macd", "macd_signal", "macd_hist"]
    fast: int = Field(12, ge=1, le=200)
    slow: int = Field(26, ge=2, le=400)
    signal: int = Field(9, ge=1, le=200)

    @model_validator(mode="after")
    def _check(self):
        if self.fast >= self.slow:
            raise ValueError("MACD 快線長度必須小於慢線長度")
        return self


Operand = Annotated[
    Union[Value, Price, MovingAverage, Rsi, Atr, Channel, Bollinger, Macd, Kd, External], Field(discriminator="kind")
]
Comparator = Literal[">", "<", ">=", "<=", "crosses_above", "crosses_below"]


class Condition(_Op):
    left: Operand
    cmp: Comparator
    right: Operand


class Group(_Op):
    logic: Literal["and", "or"] = "and"
    conditions: list[Condition] = Field(default_factory=list, max_length=10)


class RuleSet(_Op):
    long_entry: Group = Field(default_factory=Group)
    long_exit: Group = Field(default_factory=Group)
    short_entry: Group = Field(default_factory=Group)
    short_exit: Group = Field(default_factory=Group)
    stop_loss_pct: float = Field(0, ge=0, lt=100)
    take_profit_pct: float = Field(0, ge=0, le=1000)

    @model_validator(mode="after")
    def _need_entry(self):
        if not self.long_entry.conditions and not self.short_entry.conditions:
            raise ValueError("至少要有一個做多或做空的進場條件")
        return self


def parse_rules(value) -> dict:
    try:
        return RuleSet.model_validate(value).model_dump()
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(x) for x in first["loc"])
        msg = first["msg"].removeprefix("Value error, ")
        raise ValueError(f"條件設定錯誤{f'（{where}）' if where else ''}：{msg}") from None


DEFAULT_RULES = {
    "long_entry": {"logic": "and", "conditions": [
        {"left": {"kind": "rsi", "length": 14}, "cmp": "crosses_above", "right": {"kind": "value", "value": 30}},
        {"left": {"kind": "price", "field": "close"}, "cmp": ">", "right": {"kind": "ema", "length": 200, "source": "close"}},
    ]},
    "long_exit": {"logic": "or", "conditions": [
        {"left": {"kind": "rsi", "length": 14}, "cmp": ">", "right": {"kind": "value", "value": 60}},
    ]},
    "short_entry": {"logic": "and", "conditions": []},
    "short_exit": {"logic": "and", "conditions": []},
    "stop_loss_pct": 3,
    "take_profit_pct": 0,
}


# ---------------------------------------------------------------------------- 計算
def _rolling_std(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(n - 1, len(values)):
        w = values[i - n + 1:i + 1]
        m = sum(w) / n
        out[i] = math.sqrt(sum((x - m) ** 2 for x in w) / n)  # 母體標準差（同 TradingView）
    return out


def _ema_of_partial(values: list[float | None], n: int) -> list[float | None]:
    start = next((i for i, v in enumerate(values) if v is not None), len(values))
    tail = ema([v for v in values[start:]], n)  # type: ignore[misc]
    return [None] * start + tail


def compute(op: dict, candles: list[Candle], cache: dict) -> list[float | None]:
    key = repr(sorted(op.items()))
    if key in cache:
        return cache[key]
    n = len(candles)
    k = op["kind"]
    col = lambda f: [getattr(c, f) for c in candles]  # noqa: E731
    if k == "value":
        out = [op["value"]] * n
    elif k == "price":
        out = col(op["field"])
    elif k == "sma":
        out = sma(col(op["source"]), op["length"])
    elif k == "ema":
        out = ema(col(op["source"]), op["length"])
    elif k == "rsi":
        out = rsi(col("close"), op["length"])
    elif k == "atr":
        out = atr(col("high"), col("low"), col("close"), op["length"])
    elif k in ("highest", "lowest"):
        L, src = op["length"], col("high" if k == "highest" else "low")
        f = max if k == "highest" else min
        out = [f(src[i - L:i]) if i >= L else None for i in range(n)]
    elif k.startswith("bb_"):
        closes = col("close")
        mid = sma(closes, op["length"])
        if k == "bb_middle":
            out = mid
        else:
            sd = _rolling_std(closes, op["length"])
            sign = 1 if k == "bb_upper" else -1
            out = [None if m is None or s is None else m + sign * op["mult"] * s for m, s in zip(mid, sd)]
    elif k in EXTERNAL_KINDS:
        # 由策略執行／回測事先載入（strategy.ext）；沒有資料時整段都是 None，條件不成立
        out = align(cache.get("__ext__", {}).get(k, []), candles, LAG.get(k, 0), MAX_AGE.get(k))
    elif k in ("kd_k", "kd_d"):
        kk, dd = kd(col("high"), col("low"), col("close"), op["length"])
        out = kk if k == "kd_k" else dd
    elif k.startswith("macd"):
        closes = col("close")
        f, s = ema(closes, op["fast"]), ema(closes, op["slow"])
        line = [None if a is None or b is None else a - b for a, b in zip(f, s)]
        sig = _ema_of_partial(line, op["signal"])
        if k == "macd":
            out = line
        elif k == "macd_signal":
            out = sig
        else:
            out = [None if a is None or b is None else a - b for a, b in zip(line, sig)]
    else:  # pragma: no cover — 驗證已擋掉
        raise ValueError(k)
    cache[key] = out
    return out


def _check(cond: dict, i: int, candles: list[Candle], cache: dict) -> bool:
    a = compute(cond["left"], candles, cache)
    b = compute(cond["right"], candles, cache)
    cmp = cond["cmp"]
    if cmp == "crosses_above":
        return crossover(a, b, i)
    if cmp == "crosses_below":
        return crossunder(a, b, i)
    x, y = a[i], b[i]
    if x is None or y is None:
        return False
    return {">": x > y, "<": x < y, ">=": x >= y, "<=": x <= y}[cmp]


def _group(g: dict, i: int, candles: list[Candle], cache: dict) -> bool:
    conds = g["conditions"]
    if not conds:
        return False
    hits = (_check(c, i, candles, cache) for c in conds)
    return all(hits) if g["logic"] == "and" else any(hits)


def _max_length(op: dict) -> int:
    k = op["kind"]
    if k.startswith("macd"):
        return op["slow"] + op["signal"]
    return int(op.get("length", 1))


# 回測圖：價格尺度的指標疊在 K 線上，其他畫在副圖（同一類同一格）
_ON_PRICE = {"sma", "ema", "bb_upper", "bb_middle", "bb_lower", "highest", "lowest"}
_SUB_GROUP = {
    "rsi": "RSI", "kd_k": "KD", "kd_d": "KD", "macd": "MACD", "macd_signal": "MACD", "macd_hist": "MACD",
    "atr": "ATR", "fear_greed": "恐懼貪婪指數", "funding_rate": "資金費率 %", "long_short": "散戶多空比",
}
_ZH = {"bb_upper": "布林上軌", "bb_middle": "布林中軌", "bb_lower": "布林下軌", "macd": "MACD", "macd_signal": "MACD 訊號線",
       "macd_hist": "MACD 柱", "fear_greed": "恐懼貪婪指數", "funding_rate": "資金費率", "long_short": "散戶多空比"}


def _name(op: dict) -> str:
    k = op["kind"]
    if k in ("sma", "ema"):
        return f"{k.upper()}{op['length']}" + (f"（{op['source']}）" if op.get("source", "close") != "close" else "")
    if k in ("rsi", "atr"):
        return f"{k.upper()}{op['length']}"
    if k in ("highest", "lowest"):
        return f"前 {op['length']} 根{'最高' if k == 'highest' else '最低'}價"
    if k.startswith("bb_"):
        return f"{_ZH[k]}（{op['length']}, {op['mult']}）"
    if k in ("kd_k", "kd_d"):
        return f"{'K' if k == 'kd_k' else 'D'}{op['length']}"
    if k.startswith("macd"):
        return f"{_ZH[k]}（{op['fast']}, {op['slow']}, {op['signal']}）"
    return _ZH.get(k, k)


_PRICE_ZH = {"close": "收盤", "open": "開盤", "high": "最高", "low": "最低"}


def _label(op: dict) -> str:
    if op["kind"] == "value":
        return f"{op['value']:g}"
    if op["kind"] == "price":
        return _PRICE_ZH.get(op["field"], op["field"])
    return _name(op)


def _is_price(op: dict) -> bool:
    return op["kind"] == "price" or op["kind"] in _ON_PRICE


@register
class RuleStrategy(Strategy):
    key = "rules"
    label = "條件組合器（自訂規則）"
    description = (
        "用指標與條件自己組合進出場規則。持倉中出現反方向的進場條件會直接反手；"
        "停損停利以收盤價判斷。現貨不會做空。"
    )
    params = [Param("rules", "進出場條件", "rules", DEFAULT_RULES)]

    @property
    def warmup(self) -> int:
        r = self.p["rules"]
        ops = [c[side] for g in ("long_entry", "long_exit", "short_entry", "short_exit")
               for c in r[g]["conditions"] for side in ("left", "right")]
        longest = max((_max_length(o) for o in ops), default=1)
        return max(300, min(1500, longest * 10))

    def external_needs(self) -> set[str]:
        """規則用到哪些 K 線以外的資料（恐懼貪婪、資金費率、散戶多空比），呼叫端據此事先載入。"""
        return {op["kind"] for op in self._operands() if op["kind"] in EXTERNAL_KINDS}

    def _operands(self) -> list[dict]:
        r = self.p["rules"]
        return [c[side] for g in ("long_entry", "long_exit", "short_entry", "short_exit")
                for c in r[g]["conditions"] for side in ("left", "right")]

    def plots(self, candles: list[Candle]) -> list[dict]:
        """規則裡用到的每個指標各一條線；跟數值比較的條件（例如 RSI < 30）畫成副圖的水平線。"""
        r = self.p["rules"]
        cache: dict = {"__ext__": getattr(self, "ext", {})}
        ops: dict[str, dict] = {}
        levels: dict[str, set[float]] = {}
        for g in ("long_entry", "long_exit", "short_entry", "short_exit"):
            for c in r[g]["conditions"]:
                for op, other in ((c["left"], c["right"]), (c["right"], c["left"])):
                    if op["kind"] in ("value", "price"):
                        continue
                    ops.setdefault(repr(sorted(op.items())), op)
                    if other["kind"] == "value" and op["kind"] in _SUB_GROUP:
                        levels.setdefault(_SUB_GROUP[op["kind"]], set()).add(other["value"])
        out = []
        for op in ops.values():
            values = compute(op, candles, cache)
            if op["kind"] in _ON_PRICE:
                out.append(plot(_name(op), values))
            else:
                group = _SUB_GROUP[op["kind"]]
                out.append(plot(_name(op), values, "sub", group, sorted(levels.get(group, []))))
        return out

    def explain(self, candles: list[Candle]) -> dict:
        """最後一根 K 棒每個條件的數值與是否成立，給「策略狀態」顯示「為什麼還沒進場／出場」。"""
        r = self.p["rules"]
        cache: dict = {"__ext__": getattr(self, "ext", {})}
        i = len(candles) - 1
        groups = {}
        for g in ("long_entry", "long_exit", "short_entry", "short_exit"):
            conds = r[g]["conditions"]
            if not conds:
                continue
            rows = []
            for c in conds:
                a, b = compute(c["left"], candles, cache), compute(c["right"], candles, cache)
                rows.append({
                    "left": _label(c["left"]), "cmp": c["cmp"], "right": _label(c["right"]),
                    "left_value": a[i], "right_value": b[i], "ok": _check(c, i, candles, cache),
                    # 兩邊都是價格尺度（例如 收盤 vs SMA）時，前端顯示還差幾 %
                    "price_scale": all(_is_price(op) for op in (c["left"], c["right"])),
                })
            groups[g] = {"logic": r[g]["logic"], "met": _group(r[g], i, candles, cache), "conditions": rows}
        return {"bar_time": candles[i].time if candles else None, "groups": groups}

    def targets(self, candles: list[Candle]) -> list[Target]:
        r = self.p["rules"]
        cache: dict = {"__ext__": getattr(self, "ext", {})}
        sl, tp = r["stop_loss_pct"], r["take_profit_pct"]
        out: list[Target] = []
        pos: Target = "flat"
        entry = 0.0
        for i, c in enumerate(candles):
            exited = None  # 這根剛出場的方向：同一根不再同方向進場，否則出場條件形同無效
            if pos != "flat":
                d = 1 if pos == "long" else -1
                change = (c.close / entry - 1) * 100 * d
                if (sl and change <= -sl) or (tp and change >= tp) or _group(r[f"{pos}_exit"], i, candles, cache):
                    exited, pos = pos, "flat"
            if pos != "long" and exited != "long" and _group(r["long_entry"], i, candles, cache):
                pos, entry = "long", c.close
            elif pos != "short" and exited != "short" and _group(r["short_entry"], i, candles, cache):
                pos, entry = "short", c.close
            out.append(pos)
        return out
