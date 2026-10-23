import asyncio
import hashlib
import logging
import uuid

import ccxt.async_support as ccxt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..exchange.base import STABLES, ExchangeAdapter, OrderResult, UnknownSymbol, normalize_ticker
from ..models import Order, Position, Signal, Trade, utcnow
from ..services.event_bus import EventBus
from .ledger import LedgerState, apply_fill
from .risk import Reject, check_open_risk, daily_net_pnl, load_bot_settings
from .sizing import compute_order_size, protection_prices

log = logging.getLogger(__name__)

SIDE_ZH = {"buy": "買入", "sell": "賣出"}
SOURCE_ZH = {"tradingview": "TradingView", "manual": "手動", "protection": "停損停利", "strategy": "策略"}
INTENT_ZH = {"open_long": "開多", "close_long": "平多", "open_short": "開空", "close_short": "平空"}


async def get_open_position(s: AsyncSession, symbol: str, market: str) -> Position | None:
    return await s.scalar(
        select(Position).where(Position.symbol == symbol, Position.market == market, Position.closed_at.is_(None))
    )


class Executor:
    """從 queue 取出訊號，依序執行：風控 → 計算數量 → 下單 → 記錄成交 → 更新部位帳本。

    只有一個 worker 依序處理，避免同一個交易對的訊號互相競爭。
    """

    def __init__(self, adapters: dict[str, ExchangeAdapter], bus: EventBus, session_factory: async_sessionmaker):
        self.adapters = adapters
        self.bus = bus
        self.sf = session_factory
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        # 處理訊號（下單 → 寫入成交/帳本）期間持有；同步程序也要拿這把鎖才能比對交易所部位，
        # 否則會在「交易所已成交、帳本還沒寫入」的空檔誤判部位不一致
        self.ledger_lock = asyncio.Lock()

    # ------------------------------------------------------------------ 進件
    async def submit(
        self,
        *,
        source: str,
        ticker: str,
        market: str,
        action: str,
        target: str,
        strategy: str = "",
        price: float | None = None,
        raw: dict | None = None,
        dedupe_key: str | None = None,
    ) -> tuple[Signal, bool]:
        """建立訊號並排入佇列。回傳 (signal, 是否為新訊號)。"""
        key = dedupe_key or hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        async with self.sf() as s:
            existing = await s.scalar(select(Signal).where(Signal.dedupe_key == key))
            if existing:
                return existing, False
            sig = Signal(
                source=source, strategy=strategy, ticker=ticker, market=market, action=action,
                target=target, price=price, raw_payload=raw or {}, dedupe_key=key,
            )
            s.add(sig)
            await s.commit()
        await self.bus.emit(
            "signal", sig.to_dict(), category="signal", signal_id=sig.id,
            message=f"收到{SOURCE_ZH.get(source, source)}訊號 #{sig.id}："
            f"{ticker} {market} {action} → 目標 {target}",
        )
        await self.queue.put(sig.id)
        return sig, True

    async def requeue_pending(self):
        async with self.sf() as s:
            ids = (await s.scalars(select(Signal.id).where(Signal.status == "accepted").order_by(Signal.id))).all()
        for sid in ids:
            await self.queue.put(sid)

    async def run(self):
        while True:
            sid = await self.queue.get()
            try:
                await self.process(sid)
            except Exception:
                log.exception("signal %s crashed", sid)
            finally:
                self.queue.task_done()

    # ------------------------------------------------------------------ 處理
    async def process(self, sid: int):
        async with self.ledger_lock:
            await self._process(sid)

    async def _process(self, sid: int):
        async with self.sf() as s:
            sig = await s.get(Signal, sid)
            if not sig or sig.status != "accepted":
                return
            try:
                await self._handle(s, sig)
                sig.status = "executed"
                await s.commit()
                await self.bus.emit("signal", sig.to_dict())
            except Reject as e:
                sig.status, sig.reject_reason = "rejected", str(e)
                await s.commit()
                await self.bus.emit(
                    "signal", sig.to_dict(), level="warn", category="risk", signal_id=sig.id,
                    message=f"訊號 #{sig.id} 未執行：{e}",
                )
            except Exception as e:
                log.exception("signal %s failed", sid)
                await s.rollback()
                sig = await s.get(Signal, sid)
                sig.status, sig.reject_reason = "failed", repr(e)
                await s.commit()
                await self.bus.emit(
                    "signal", sig.to_dict(), level="error", category="system", signal_id=sig.id,
                    message=f"訊號 #{sig.id} 執行失敗：{e!r}",
                )

    async def _handle(self, s: AsyncSession, sig: Signal):
        adapter = self.adapters.get(sig.market)
        if adapter is None:
            raise Reject(f"不支援的市場：{sig.market}")
        cfg = await load_bot_settings(s)

        if sig.source in ("tradingview", "strategy"):  # 自動訊號才受 Bot 開關與白名單限制
            if not cfg.enabled:
                raise Reject("Bot 已停用")
            allowed = {normalize_ticker(x) for x in cfg.symbols or []}
            if allowed and normalize_ticker(sig.ticker) not in allowed:
                raise Reject(f"{sig.ticker} 不在允許交易清單內")

        try:
            symbol = await adapter.resolve_symbol(sig.ticker)
        except UnknownSymbol:
            raise Reject(f"找不到交易對 {sig.ticker}（{sig.market}）")
        sig.symbol = symbol

        if sig.market == "spot" and sig.target == "short":
            raise Reject("現貨無法做空")

        pos = await get_open_position(s, symbol, sig.market)
        current = pos.side if pos else "flat"
        if current == sig.target:
            raise Reject(f"目前部位已經是 {current}，不需動作")

        opening = sig.target != "flat"
        if opening:
            # 先檢查開倉風控，避免「平倉成功但反手被擋」的半套狀態
            await check_open_risk(s, cfg, closing_one=pos is not None)

        if pos:
            await self._close_position(s, sig, adapter, pos)
            if await self._kill_switch(s, cfg):
                if opening:
                    raise Reject("已平倉，但觸發每日虧損上限，不再反手開倉")
                return
        if opening:
            await self._open_position(s, sig, adapter, cfg, symbol)

    async def _kill_switch(self, s: AsyncSession, cfg) -> bool:
        pnl = await daily_net_pnl(s)
        if pnl > -cfg.max_daily_loss_usdt:
            return False
        if cfg.enabled:
            cfg.enabled = False
            await s.commit()
            await self.bus.emit(
                "bot", cfg.to_dict(), level="error", category="risk",
                message=f"今日淨損益 {pnl:.2f} USDT 達到上限，Bot 已自動停用（Kill switch）",
            )
        return True

    async def _close_position(self, s: AsyncSession, sig: Signal, adapter: ExchangeAdapter, pos: Position):
        qty = pos.qty
        if adapter.market == "spot":
            wallet = await adapter.fetch_wallet()
            base = adapter.base_asset(pos.symbol)
            free = next((a.free for a in wallet.assets if a.asset == base), 0.0)
            qty = min(qty, free)
        qty = adapter.amount_to_precision(pos.symbol, qty)
        if qty <= 0:
            pos.closed_at = utcnow()
            await s.commit()
            await self.bus.emit(
                "position_update", pos.to_dict(), level="warn", category="order", signal_id=sig.id,
                message=f"{pos.symbol} 剩餘數量低於最小下單單位，直接標記為已平倉",
            )
            return
        await adapter.cancel_protection(pos.symbol)
        await self._place(
            s, sig, adapter, pos.symbol, "sell" if pos.side == "long" else "buy", qty,
            intent=f"close_{pos.side}", reduce_only=adapter.market == "futures", leverage=pos.leverage,
        )

    async def _open_position(self, s: AsyncSession, sig: Signal, adapter: ExchangeAdapter, cfg, symbol: str):
        lev = cfg.leverage if adapter.market == "futures" else 1
        price = await adapter.fetch_price(symbol)
        wallet = await adapter.fetch_wallet()
        margin, raw_qty = compute_order_size(cfg.size_mode, cfg.size_value, lev, wallet.free_usdt, price)
        if margin > wallet.free_usdt:
            raise Reject(f"可用餘額不足：需要 {margin:.2f} USDT，可用 {wallet.free_usdt:.2f}")
        qty = adapter.amount_to_precision(symbol, raw_qty)
        if qty * price < adapter.min_notional(symbol):
            raise Reject(f"下單金額 {qty * price:.2f} 低於交易所最小值 {adapter.min_notional(symbol)}")
        if adapter.market == "futures":
            await adapter.prepare_futures(symbol, lev, cfg.margin_mode)

        side = "buy" if sig.target == "long" else "sell"
        filled = await self._place(s, sig, adapter, symbol, side, qty, intent=f"open_{sig.target}", leverage=lev)
        if not filled:
            return

        pos = await get_open_position(s, symbol, adapter.market)
        stop, tp = protection_prices(pos.side, pos.entry_price, cfg.stop_loss_pct, cfg.take_profit_pct)
        if stop or tp:
            pos.stop_price, pos.take_profit_price = stop, tp
            pos.native_protection = await adapter.place_protection(symbol, pos.side, pos.qty, stop, tp)
            await s.commit()
            where = "交易所" if pos.native_protection else "軟體監控"
            await self.bus.emit(
                "position_update", pos.to_dict(), category="order", signal_id=sig.id,
                message=f"{symbol} 設定保護（{where}）：停損 {stop and round(stop, 6)} / 停利 {tp and round(tp, 6)}",
            )

    # ------------------------------------------------------------------ 下單與成交
    async def _place(
        self, s: AsyncSession, sig: Signal, adapter: ExchangeAdapter, symbol: str, side: str, qty: float,
        *, intent: str, reduce_only: bool = False, leverage: int | None = None,
    ) -> Trade | None:
        order = Order(
            signal_id=sig.id, client_order_id=f"tb{sig.id}x{uuid.uuid4().hex[:16]}", symbol=symbol,
            market=adapter.market, side=side, qty=qty, reduce_only=reduce_only, intent=intent, leverage=leverage,
        )
        s.add(order)
        await s.commit()
        await self.bus.emit(
            "order_update", order.to_dict(), category="order", signal_id=sig.id, order_id=order.id,
            message=f"送出市價單 #{order.id}：{INTENT_ZH[intent]} {SIDE_ZH[side]} {qty} {symbol}",
        )
        try:
            res = await adapter.create_market_order(symbol, side, qty, order.client_order_id, reduce_only)
        except ccxt.BaseError as e:
            order.status, order.error = "rejected", str(e)
            await s.commit()
            await self.bus.emit(
                "order_update", order.to_dict(), level="error", category="order", signal_id=sig.id,
                order_id=order.id, message=f"訂單 #{order.id} 被交易所拒絕：{e}",
            )
            raise Reject(f"交易所拒絕下單：{e}")

        order.exchange_order_id = res.exchange_order_id
        res = await self._wait_fill(adapter, symbol, res)
        return await self.record_fill(s, adapter, order, res)

    async def _wait_fill(self, adapter: ExchangeAdapter, symbol: str, res: OrderResult) -> OrderResult:
        for _ in range(5):
            if res.status != "open" and res.avg_price:
                return res
            await asyncio.sleep(1)
            res = await adapter.fetch_order(symbol, res.exchange_order_id)
        return res

    async def record_fill(self, s: AsyncSession, adapter: ExchangeAdapter, order: Order, res: OrderResult) -> Trade | None:
        """把訂單結果寫進 DB；有成交就建立 Trade 並更新部位帳本。order_sync worker 也會呼叫。"""
        order.status, order.filled_qty, order.avg_price = res.status, res.filled_qty, res.avg_price
        if res.filled_qty <= 0 or not res.avg_price:
            await s.commit()
            await self.bus.emit(
                "order_update", order.to_dict(), level="warn", category="order", signal_id=order.signal_id,
                order_id=order.id, message=f"訂單 #{order.id} 狀態 {res.status}，尚未成交，交由同步程序追蹤",
            )
            return None

        price, qty = res.avg_price, res.filled_qty
        base = adapter.base_asset(order.symbol)
        if res.fee_asset in STABLES:
            fee_usdt = res.fee
        elif res.fee_asset == base:
            fee_usdt = res.fee * price
        else:  # 例如用 BNB 抵扣手續費：用 taker 費率估算
            fee_usdt = qty * price * adapter.taker_fee(order.symbol)
        ledger_qty = qty
        if order.market == "spot" and order.side == "buy" and res.fee_asset == base:
            ledger_qty = qty - res.fee  # 現貨買入時手續費從收到的幣扣

        pos = await get_open_position(s, order.symbol, order.market)
        state = LedgerState(pos.side, pos.qty, pos.entry_price) if pos else LedgerState(None)
        new, realized = apply_fill(state, order.side, ledger_qty, price)

        trade = Trade(
            order_id=order.id, signal_id=order.signal_id, symbol=order.symbol, market=order.market,
            side=order.side, intent=order.intent, price=price, qty=qty, fee=res.fee, fee_asset=res.fee_asset,
            fee_usdt=fee_usdt, realized_pnl=realized, executed_at=utcnow(),
        )
        s.add(trade)

        is_dust = new.side is not None and new.qty * price < adapter.min_notional(order.symbol) and order.intent.startswith("close")
        closed = None
        if pos and (new.side != pos.side or is_dust):
            pos.qty = new.qty if is_dust else pos.qty
            pos.realized_pnl += realized
            pos.unrealized_pnl = 0
            pos.closed_at = utcnow()
            closed, pos = pos, None
        if new.side and not is_dust:
            if pos is None:
                pos = Position(symbol=order.symbol, market=order.market, leverage=order.leverage or 1)
                s.add(pos)
            pos.side, pos.qty, pos.entry_price, pos.mark_price = new.side, new.qty, new.entry, price
            pos.realized_pnl = (pos.realized_pnl or 0) + realized
        await s.flush()  # 新部位要先寫入才有 id
        owner = closed or pos  # 平倉（含一次成交就反手）算給被平掉的部位
        trade.position_id = owner.id if owner else None
        await s.commit()

        pnl = f"，已實現損益 {realized:+.4f} USDT" if order.intent.startswith("close") else ""
        await self.bus.emit(
            "trade", trade.to_dict(), category="trade", signal_id=order.signal_id, order_id=order.id, notify=True,
            message=f"成交：{INTENT_ZH[order.intent]} {SIDE_ZH[order.side]} {qty} {order.symbol} @ {price}{pnl}",
        )
        await self.bus.emit("position_update", {"symbol": order.symbol, "market": order.market})
        return trade
