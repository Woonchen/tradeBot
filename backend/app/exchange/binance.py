import logging

import ccxt.async_support as ccxt

from .base import AssetBalance, ExchangeAdapter, ExchangePosition, ExitInfo, OrderResult, Wallet

log = logging.getLogger(__name__)

# Binance 的 allOrders 一次只能查 7 天內
_MAX_WINDOW_MS = 7 * 24 * 3600 * 1000 - 60_000
# 條件單（algo order）的種類 → 平倉原因
_ALGO_REASON = {"STOP_MARKET": "stop", "STOP": "stop", "TRAILING_STOP_MARKET": "stop",
                "TAKE_PROFIT_MARKET": "take_profit", "TAKE_PROFIT": "take_profit"}

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


    async def find_exit(self, symbol, side, since_ms):
        """找出把部位平掉的那張單：開倉後、反方向、最後一張有成交的單。
        - 是我們掛的停損停利條件單觸發出來的（條件單的 actualOrderId 指向它）→ stop / take_profit
        - clientOrderId 是 autoclose-（強平）或 adl_autoclose（自動減倉）→ liquidation
        - 其他 → external（在交易所上手動平的）"""
        if self.market != "futures":
            return None
        since_ms = max(since_ms, self.ex.milliseconds() - _MAX_WINDOW_MS)
        close_side = "sell" if side == "long" else "buy"
        try:
            orders = await self.ex.fetch_orders(symbol, since_ms, 100)
            filled = [o for o in orders if o.get("side") == close_side and float(o.get("filled") or 0) > 0]
            if not filled:
                return None
            last = max(filled, key=lambda o: o.get("lastTradeTimestamp") or o.get("timestamp") or 0)
            price = float(last["average"]) if last.get("average") else None
            info = last.get("info") or {}
            client_id = str(last.get("clientOrderId") or info.get("clientOrderId") or "")
            if client_id.startswith(("autoclose", "adl_autoclose")) or "LIQUIDATION" in (info.get("type"), info.get("origType")):
                return ExitInfo("liquidation", price)
            algos = await self.ex.fetch_orders(symbol, since_ms, 100, {"trigger": True})
            for a in algos:
                ai = a.get("info") or {}
                if str(ai.get("actualOrderId") or "") == str(last["id"]):
                    return ExitInfo(_ALGO_REASON.get(ai.get("orderType"), "external"), price)
            return ExitInfo("external", price)
        except ccxt.BaseError as e:
            log.warning("find_exit %s failed, fall back to price inference: %s", symbol, e)
            return None


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
