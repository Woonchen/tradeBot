from sqlalchemy.ext.asyncio import async_sessionmaker

from ..config import Settings
from .base import ExchangeAdapter
from .binance import BinanceAdapter, build_binance_exchanges, build_public_exchanges
from .paper import PaperAdapter


def build_market_data(cfg: Settings, adapters: dict[str, ExchangeAdapter]) -> dict[str, ExchangeAdapter]:
    """分析用的行情（K 線、價格）：回測、策略判斷、影子模式、K 線圖都用這組。

    Demo 帳戶的合約 K 線收盤價跟真實市場幾乎一樣，但最高／最低價常有不存在的極端影線
    （800 天裡振幅 > 15% 的 K 棒 Demo 有 60 根、真實市場只有 4 根），會讓停損停利和用到高低價的
    指標（KD、ATR、前 N 根最高最低價）失真。所以 Demo 模式下行情改用 Binance 真實市場的公開資料，
    Demo 帳戶只負責下單、餘額與持倉價格。Paper 本來就用公開資料、正式帳戶本來就是真實市場，沿用即可。
    """
    if cfg.execution_mode == "binance" and cfg.testnet:
        spot, fut = build_public_exchanges()
        return {"spot": BinanceAdapter(spot, "spot"), "futures": BinanceAdapter(fut, "futures")}
    return adapters


def build_adapters(cfg: Settings, session_factory: async_sessionmaker) -> dict[str, ExchangeAdapter]:
    if cfg.execution_mode == "paper":
        spot, fut = build_public_exchanges()
        return {
            "spot": PaperAdapter(spot, "spot", cfg.paper_start_usdt, session_factory),
            "futures": PaperAdapter(fut, "futures", cfg.paper_start_usdt, session_factory),
        }
    spot, fut = build_binance_exchanges(cfg.binance_api_key, cfg.binance_api_secret, demo=cfg.testnet)
    return {"spot": BinanceAdapter(spot, "spot"), "futures": BinanceAdapter(fut, "futures")}
