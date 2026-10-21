import logging

import ccxt.async_support as ccxt

from .base import AssetBalance, ExchangeAdapter, ExchangePosition, OrderResult, Wallet

log = logging.getLogger(__name__)

_STATUS = {"closed": "filled", "open": "open", "canceled": "canceled", "expired": "canceled", "rejected": "rejected"}


class BinanceAdapter(ExchangeAdapter):
    """真實下單到 Binance（TESTNET=true 時為 Demo Trading 帳戶）。"""

    def __init__(self, ex: ccxt.Exchange, market: str):
        super().__init__(ex, market)
        self._prepared: dict[str, tuple[int, str]] = {}

    def _to_result(self, o: dict, symbol: str) -> OrderResult:
        filled = float(o.get("filled") or 0)
        avg = o.get("average") or o.get("price")
        avg = float(avg) if avg else None
        fee, fee_asset = 0.0, "USDT"
        fees = o.get("fees") or ([o["fee"]] if o.get("fee") else [])
        fees = [f for f in fees if f and f.get("cost") is not None]
        if fees:
            fee_asset = fees[0]["currency"]
            fee = sum(float(f["cost"]) for f in fees if f["currency"] == fee_asset)
        elif filled and avg:
            # 合約的下單回應不含手續費，用 taker 費率估算
            fee = filled * avg * self.taker_fee(symbol)
        return OrderResult(
            exchange_order_id=str(o["id"]),
            status=_STATUS.get(o.get("status") or "open", "open"),
            filled_qty=filled,
            avg_price=avg,
            fee=fee,
            fee_asset=fee_asset,
            raw=o.get("info") or {},
        )

    async def create_market_order(self, symbol, side, qty, client_order_id, reduce_only=False):
        await self.ensure_loaded()
        params: dict = {"clientOrderId": client_order_id}
        if self.market == "futures":
            params["newOrderRespType"] = "RESULT"
            if reduce_only:
                params["reduceOnly"] = True
        o = await self.ex.create_order(symbol, "market", side, qty, None, params)
        return self._to_result(o, symbol)

    async def fetch_order(self, symbol, exchange_order_id):
        o = await self.ex.fetch_order(exchange_order_id, symbol)
        return self._to_result(o, symbol)

    async def fetch_wallet(self) -> Wallet:
        await self.ensure_loaded()
        bal = await self.ex.fetch_balance()
        amounts = {a: float(v) for a, v in (bal.get("total") or {}).items() if v and float(v) > 0}
        values = await self.usdt_values(amounts)
        assets = [
            AssetBalance(
                asset=a,
                free=float(bal.get("free", {}).get(a) or 0),
                locked=float(bal.get("used", {}).get(a) or 0),
                usdt_value=values.get(a, 0.0),
            )
            for a in amounts
        ]
        free_usdt = float(bal.get("free", {}).get("USDT") or 0)
        if self.market == "futures":
            info = bal.get("info") or {}
            equity = float(info.get("totalMarginBalance") or sum(values.values()))
            free_usdt = float(info.get("availableBalance") or free_usdt)
        else:
            equity = sum(values.values())
        return Wallet(assets=assets, equity_usdt=equity, free_usdt=free_usdt)

    async def fetch_positions(self):
        if self.market != "futures":
            return None
        await self.ensure_loaded()
        out = []
        for p in await self.ex.fetch_positions():
            qty = float(p.get("contracts") or 0)
            if qty == 0:
                continue
            out.append(
                ExchangePosition(
                    symbol=p["symbol"],
                    side=p["side"],
                    qty=qty,
                    entry_price=float(p.get("entryPrice") or 0),
                    mark_price=float(p["markPrice"]) if p.get("markPrice") else None,
                    unrealized_pnl=float(p.get("unrealizedPnl") or 0),
                    leverage=int(float(p["leverage"])) if p.get("leverage") else None,
                    liquidation_price=float(p["liquidationPrice"]) if p.get("liquidationPrice") else None,
                )
            )
        return out

    async def prepare_futures(self, symbol, leverage, margin_mode):
        if self.market != "futures" or self._prepared.get(symbol) == (leverage, margin_mode):
            return
        try:
            await self.ex.set_margin_mode(margin_mode, symbol)
        except ccxt.ExchangeError as e:
            # 已經是同一種模式，或者有持倉時不能改
            log.info("set_margin_mode %s: %s", symbol, e)
        await self.ex.set_leverage(leverage, symbol)
        self._prepared[symbol] = (leverage, margin_mode)

    async def place_protection(self, symbol, side, qty, stop, tp):
        if self.market != "futures":
            return False
        close_side = "sell" if side == "long" else "buy"
        try:
            if stop:
                await self.ex.create_order(
                    symbol, "market", close_side, qty, None,
                    {"stopLossPrice": stop, "reduceOnly": True, "workingType": "MARK_PRICE"},
                )
            if tp:
                await self.ex.create_order(
                    symbol, "market", close_side, qty, None,
                    {"takeProfitPrice": tp, "reduceOnly": True, "workingType": "MARK_PRICE"},
                )
            return True
        except ccxt.BaseError as e:
            log.warning("place_protection failed for %s, fallback to software stop: %s", symbol, e)
            return False

    async def cancel_protection(self, symbol):
        if self.market != "futures":
            return
        for params in ({}, {"trigger": True}):
            try:
                await self.ex.cancel_all_orders(symbol, params)
            except ccxt.BaseError as e:
                log.info("cancel_all_orders %s %s: %s", symbol, params, e)


def build_binance_exchanges(api_key: str, secret: str, demo: bool) -> tuple[ccxt.Exchange, ccxt.Exchange]:
    common = {"apiKey": api_key, "secret": secret, "enableRateLimit": True}
    spot = ccxt.binance({**common, "options": {"defaultType": "spot", "fetchMarkets": {"types": ["spot"]}}})
    fut = ccxt.binanceusdm(common)
    if demo:
        # Binance 舊的 futures testnet 已停用，改用 Demo Trading（demo.binance.com）
        spot.enable_demo_trading(True)
        fut.enable_demo_trading(True)
    return spot, fut


def build_public_exchanges() -> tuple[ccxt.Exchange, ccxt.Exchange]:
    spot = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot", "fetchMarkets": {"types": ["spot"]}}})
    fut = ccxt.binanceusdm({"enableRateLimit": True})
    return spot, fut
