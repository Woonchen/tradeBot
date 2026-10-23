"""策略框架。

寫一個新策略：
1. 在 app/strategies/ 新增一個 .py 檔
2. 繼承 Strategy，設定 key / label / description / params，實作 targets()
3. 加上 @register，並在 app/strategies/__init__.py 匯入那個檔案
儀表板的「策略」頁會自動出現新策略，參數表單也會依 params 自動產生。
"""

from dataclasses import asdict, dataclass
from typing import Any, ClassVar, Literal

INTERVAL_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200,
    "4h": 14400, "6h": 21600, "8h": 28800, "12h": 43200, "1d": 86400,
}

Target = Literal["long", "short", "flat"]


@dataclass(frozen=True)
class Candle:
    time: int  # 開盤時間（秒）
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Param:
    name: str
    label: str
    type: Literal["int", "float", "bool", "rules"]  # rules = 條件組合器的 JSON 規則
    default: Any
    min: float | None = None
    max: float | None = None
    step: float | None = None
    help: str = ""

    def cast(self, value: Any) -> Any:
        if self.type == "rules":
            from .rules import parse_rules

            return parse_rules(value)
        if self.type == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        v = int(value) if self.type == "int" else float(value)
        if self.min is not None and v < self.min:
            raise ValueError(f"{self.label} 不能小於 {self.min}")
        if self.max is not None and v > self.max:
            raise ValueError(f"{self.label} 不能大於 {self.max}")
        return v


class Strategy:
    key: ClassVar[str]
    label: ClassVar[str]
    description: ClassVar[str] = ""
    params: ClassVar[list[Param]] = []

    def __init__(self, params: dict | None = None):
        given = params or {}
        unknown = set(given) - {p.name for p in self.params}
        if unknown:
            raise ValueError(f"未知的參數：{', '.join(sorted(unknown))}")
        self.p: dict[str, Any] = {p.name: p.cast(given.get(p.name, p.default)) for p in self.params}
        self.validate()

    def validate(self):
        """需要檢查參數之間的關係時覆寫（例如快線要比慢線短）。"""

    @property
    def warmup(self) -> int:
        """指標需要多少根 K 棒才穩定。即時執行時每次會抓這麼多根來計算。"""
        return 300

    def targets(self, candles: list["Candle"]) -> list[Target]:
        """對每一根（已收盤的）K 棒回傳「收盤後應該持有的部位」。

        - 只能用到第 i 根（含）以前的資料決定第 i 個值
        - 回傳長度必須跟 candles 一樣
        - 機器人只在這個值「改變」時下單
        """
        raise NotImplementedError

    def plots(self, candles: list["Candle"]) -> list[dict]:
        """回測圖要畫的指標（用跟 targets 同一份計算，圖上的線就是策略實際看到的線）。

        每一項：{"name", "values"（跟 candles 對齊，None = 還沒有值）, "pane": "main" 疊在 K 線上 / "sub" 副圖,
        "group": 副圖名稱（同名的畫在同一格）, "levels": 副圖的水平參考線}。用 plot() 產生。
        """
        return []

    @classmethod
    def describe(cls) -> dict:
        return {
            "key": cls.key, "label": cls.label, "description": cls.description,
            "params": [asdict(p) for p in cls.params],
        }


REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    REGISTRY[cls.key] = cls
    return cls


def build_strategy(key: str, params: dict | None = None) -> Strategy:
    cls = REGISTRY.get(key)
    if cls is None:
        raise ValueError(f"找不到策略：{key}")
    return cls(params)


def plot(name: str, values: list, pane: str = "main", group: str | None = None, levels: list[float] | None = None) -> dict:
    return {"name": name, "values": values, "pane": pane, "group": group or name, "levels": levels or []}


def for_market(target: Target, market: str) -> Target:
    """現貨不能做空：空單訊號在現貨等於空手。"""
    return "flat" if market == "spot" and target == "short" else target
