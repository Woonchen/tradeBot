import uuid
from collections import defaultdict

import ccxt.async_support as ccxt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..models import Position, Trade
from .base import AssetBalance, ExchangeAdapter, OrderResult, Wallet


class PaperAdapter(ExchangeAdapter):
    """本地模擬撮合：用 Binance 正式環境的即時價格成交，不需要 API key。

    錢包餘額不另外存，而是由 trades / positions 表推算，重啟後也不會遺失。
    """

    def __init__(self, ex: ccxt.Exchange, market: str, start_usdt: float, session_factory: async_sessionmaker):
        super().__init__(ex, market)
        self.start_usdt = start_usdt
        self.session_factory = session_factory
        self._orders: dict[str, OrderResult] = {}

    async def create_market_order(self, symbol, side, qty, client_order_id, reduce_only=False):
        await self.ensure_loaded()
        price = await self.fetch_price(symbol)
        fee_rate = 0.001 if self.market == "spot" else 0.0004
        result = OrderResult(
            exchange_order_id=f"paper-{uuid.uuid4().hex[:12]}",
            status="filled",
            filled_qty=qty,
            avg_price=price,
            fee=qty * price * fee_rate,
            fee_asset="USDT",
        )
        self._orders[result.exchange_order_id] = result
        return result

    async def fetch_order(self, symbol, exchange_order_id):
        return self._orders[exchange_order_id]

    async def fetch_wallet(self) -> Wallet:
        async with self.session_factory() as s:
            trades = (await s.scalars(select(Trade).where(Trade.market == self.market))).all()
            open_pos = (
                await s.scalars(
                    select(Position).where(Position.market == self.market, Position.closed_at.is_(None))
                )
            ).all()

        if self.market == "spot":
            amounts: dict[str, float] = defaultdict(float)
            amounts["USDT"] = self.start_usdt
            for t in trades:
                base = t.symbol.split("/")[0]
                sign = 1 if t.side == "buy" else -1
                amounts[base] += sign * t.qty
                amounts["USDT"] -= sign * t.qty * t.price + t.fee_usdt
            amounts = {a: v for a, v in amounts.items() if abs(v) > 1e-12}
            values = await self.usdt_values(amounts)
            assets = [AssetBalance(a, v, 0.0, values.get(a, 0.0)) for a, v in amounts.items()]
            return Wallet(assets=assets, equity_usdt=sum(values.values()), free_usdt=amounts.get("USDT", 0.0))

        wallet = self.start_usdt + sum(t.realized_pnl - t.fee_usdt for t in trades)
        prices = await self.fetch_prices([p.symbol for p in open_pos])
        unrealized = 0.0
        used_margin = 0.0
        for p in open_pos:
            mark = prices.get(p.symbol, p.entry_price)
            direction = 1 if p.side == "long" else -1
            unrealized += (mark - p.entry_price) * p.qty * direction
            used_margin += p.entry_price * p.qty / max(p.leverage, 1)
        equity = wallet + unrealized
        return Wallet(
            assets=[AssetBalance("USDT", wallet - used_margin, used_margin, equity)],
            equity_usdt=equity,
            free_usdt=wallet - used_margin + min(unrealized, 0),
        )
