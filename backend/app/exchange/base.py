from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import ccxt.async_support as ccxt

STABLES = {"USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI"}


class UnknownSymbol(Exception):
    pass


@dataclass
class OrderResult:
    exchange_order_id: str
    status: str  # filled / open / canceled / rejected
    filled_qty: float
    avg_price: float | None
    fee: float = 0.0
    fee_asset: str = "USDT"
    raw: dict = field(default_factory=dict)


@dataclass
class AssetBalance:
    asset: str
    free: float
    locked: float
    usdt_value: float


@dataclass
class Wallet:
    assets: list[AssetBalance]
    equity_usdt: float  # 現貨：資產總市值；合約：保證金餘額（含未實現損益）
    free_usdt: float  # 可用來開新倉的 USDT


@dataclass
class ExchangePosition:
    symbol: str
    side: str
    qty: float
    entry_price: float
    mark_price: float | None
    unrealized_pnl: float
    leverage: int | None
    liquidation_price: float | None


@dataclass
class ExitInfo:
    """部位在交易所被平掉時，從訂單紀錄查到的平倉原因（見 engine/sizing.py 的 EXIT_REASONS）與實際成交均價。"""
    reason: str
    price: float | None


def normalize_ticker(raw: str) -> str:
    """TradingView ticker → Binance market id。

    BINANCE:BTCUSDT.P → BTCUSDT、BTCUSDT → BTCUSDT、btc-usdt → BTCUSDT
    """
    t = raw.strip().upper()
    if ":" in t:
        t = t.split(":", 1)[1]
    if t.endswith(".P"):
        t = t[:-2]
    return t.replace("-", "").replace("_", "")


class ExchangeAdapter(ABC):
    """一個 adapter 對應一個市場（spot 或 futures）。行情/市場資訊都透過 ccxt 實例取得。"""

    market: str

    def __init__(self, ex: ccxt.Exchange, market: str):
        self.ex = ex
        self.market = market
        self._loaded = False

    async def ensure_loaded(self):
        if not self._loaded:
            await self.ex.load_markets()
            self._loaded = True

    def _is_our_market(self, m: dict) -> bool:
        if self.market == "spot":
            return bool(m.get("spot"))
        return bool(m.get("swap") and m.get("linear"))

    async def resolve_symbol(self, raw: str) -> str:
        await self.ensure_loaded()
        if "/" in raw:
            candidates = [raw, f"{raw}:{raw.split('/')[1].split(':')[0]}", raw.split(":")[0]]
            for c in candidates:
                m = self.ex.markets.get(c)
                if m and self._is_our_market(m):
                    return c
            raise UnknownSymbol(raw)
        mid = normalize_ticker(raw)
        for m in self.ex.markets_by_id.get(mid, []):
            if self._is_our_market(m):
                return m["symbol"]
        raise UnknownSymbol(raw)

    def amount_to_precision(self, symbol: str, qty: float) -> float:
        try:
            return float(self.ex.amount_to_precision(symbol, qty))
        except ccxt.BaseError:  # 低於最小下單數量時 ccxt 會丟錯
            return 0.0

    def min_notional(self, symbol: str) -> float:
        cost = (self.ex.markets[symbol].get("limits") or {}).get("cost") or {}
        return float(cost.get("min") or 5.0)

    def taker_fee(self, symbol: str) -> float:
        return float(self.ex.markets[symbol].get("taker") or 0.001)

    def base_asset(self, symbol: str) -> str:
        return self.ex.markets[symbol]["base"]

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int, since: int | None = None):
        await self.ensure_loaded()
        return await self.ex.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)

    async def fetch_price(self, symbol: str) -> float:
        await self.ensure_loaded()
        t = await self.ex.fetch_ticker(symbol)
        return float(t["last"])

    async def fetch_prices(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        await self.ensure_loaded()
        tickers = await self.ex.fetch_tickers(symbols)
        return {s: float(t["last"]) for s, t in tickers.items() if t.get("last")}

    async def usdt_values(self, amounts: dict[str, float]) -> dict[str, float]:
        """把各資產數量換算成 USDT。"""
        await self.ensure_loaded()
        pairs = [f"{a}/USDT" for a in amounts if a not in STABLES and f"{a}/USDT" in self.ex.markets]
        prices = await self.fetch_prices(pairs) if pairs else {}
        out = {}
        for a, amt in amounts.items():
            if a in STABLES:
                out[a] = amt
            else:
                out[a] = amt * prices.get(f"{a}/USDT", 0.0)
        return out

    # ---- 子類別實作 ----
    @abstractmethod
    async def create_market_order(
        self, symbol: str, side: str, qty: float, client_order_id: str, reduce_only: bool = False
    ) -> OrderResult: ...

    @abstractmethod
    async def fetch_order(self, symbol: str, exchange_order_id: str) -> OrderResult: ...

    @abstractmethod
    async def fetch_wallet(self) -> Wallet: ...

    async def fetch_positions(self) -> list[ExchangePosition] | None:
        """回傳交易所上的實際部位；None 代表此 adapter 不提供（以本地帳本為準）。"""
        return None

    async def prepare_futures(self, symbol: str, leverage: int, margin_mode: str):
        pass

    async def place_protection(self, symbol: str, side: str, qty: float, stop: float | None, tp: float | None) -> bool:
        """在交易所掛停損/停利單；回傳 False 表示交由軟體監控。"""
        return False

    async def cancel_protection(self, symbol: str):
        pass

    async def find_exit(self, symbol: str, side: str, since_ms: int) -> ExitInfo | None:
        """部位在交易所上不見了：查訂單紀錄是哪一張單把它平掉的。None = 查不到（呼叫端改用價格研判）。"""
        return None

    async def close(self):
        await self.ex.close()
