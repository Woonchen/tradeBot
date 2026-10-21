# 匯入策略檔案，@register 才會把它們加進 REGISTRY。新增策略時在這裡加一行。
from . import ema_cross, rsi_reversion, rules  # noqa: F401
from .base import INTERVAL_SECONDS, REGISTRY, Candle, Param, Strategy, build_strategy, for_market, register  # noqa: F401
