from sqlalchemy.ext.asyncio import async_sessionmaker

from ..config import Settings
from .base import ExchangeAdapter
from .binance import BinanceAdapter, build_binance_exchanges, build_public_exchanges
from .paper import PaperAdapter


def build_adapters(cfg: Settings, session_factory: async_sessionmaker) -> dict[str, ExchangeAdapter]:
    if cfg.execution_mode == "paper":
        spot, fut = build_public_exchanges()
        return {
            "spot": PaperAdapter(spot, "spot", cfg.paper_start_usdt, session_factory),
            "futures": PaperAdapter(fut, "futures", cfg.paper_start_usdt, session_factory),
        }
    spot, fut = build_binance_exchanges(cfg.binance_api_key, cfg.binance_api_secret, demo=cfg.testnet)
    return {"spot": BinanceAdapter(spot, "spot"), "futures": BinanceAdapter(fut, "futures")}
