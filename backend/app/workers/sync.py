"""背景同步：餘額快照、部位標記價格/軟體停損、未成交訂單追蹤。"""

import asyncio
import logging

from sqlalchemy import select

from ..engine.executor import Executor
from ..engine.ledger import LedgerState, apply_fill
from ..engine.risk import load_bot_settings, update_peaks
from ..models import BalanceSnapshot, EquitySnapshot, Order, Position, Trade, utcnow
from ..services.market_stream import MarketHub

log = logging.getLogger(__name__)

MARKET_ZH = {"spot": "現貨", "futures": "合約"}


async def every(seconds: int, fn, name: str):
    while True:
        try:
            await fn()
        except Exception:
            log.exception("worker %s failed", name)
        await asyncio.sleep(seconds)


class SyncWorkers:
    def __init__(self, executor: Executor, hub: MarketHub):
        self.ex = executor
        self.hub = hub
        self.sf = executor.sf
        self.bus = executor.bus
        self._warned: set[str] = set()
        self._breaches = 0  # 連續幾次快照超過回撤上限

    # ------------------------------------------------------------ 餘額
    async def snapshot_balances(self):
        equity = {}
        now = utcnow()
        async with self.sf() as s:
            for market, adapter in self.ex.adapters.items():
                wallet = await adapter.fetch_wallet()
                equity[market] = wallet.equity_usdt
                for a in wallet.assets:
                    if a.asset == "USDT" or a.usdt_value >= 1:
                        s.add(BalanceSnapshot(
                            ts=now, market=market, asset=a.asset, free=a.free, locked=a.locked, usdt_value=a.usdt_value,
                        ))
            snap = EquitySnapshot(
                ts=now, spot_usdt=equity.get("spot", 0.0), futures_usdt=equity.get("futures", 0.0),
                total_usdt=sum(equity.values()),
            )
            s.add(snap)
            await s.commit()
            await self._check_drawdown(s, equity, now)
        await self.bus.emit("balance_update", snap.to_dict())

    async def _check_drawdown(self, s, equity: dict[str, float], now):
        """回撤熔斷：權益從最高點回撤超過上限，就全部平倉並停用 Bot。

        要連續兩次快照都超過才觸發，避免交易所偶爾回傳一筆異常的餘額就誤平倉。
        """
        cfg = await load_bot_settings(s)
        breached = update_peaks(cfg, equity, now)
        await s.commit()
        self._breaches = self._breaches + 1 if breached and cfg.enabled else 0
        if self._breaches < 2:
            return
        self._breaches = 0
        detail = "、".join(f"{MARKET_ZH.get(m, m)}權益 {now_v:.2f} USDT，比最高點 {peak:.2f} 回撤 {(1 - now_v / peak) * 100:.1f}%"
                          for m, peak, now_v in breached)
        await self.ex.close_all(
            s, cfg, f"觸發回撤熔斷：{detail}，超過上限 {cfg.max_drawdown_pct:g}%。已送出全部平倉（{{n}} 個部位），Bot 已停用",
            level="error",
        )

    # ------------------------------------------------------------ 部位
    async def sync_positions(self):
        async with self.ex.ledger_lock:
            await self._sync_positions()

    async def _sync_positions(self):
        changed = False
        async with self.sf() as s:
            for market, adapter in self.ex.adapters.items():
                ledger = (await s.scalars(
                    select(Position).where(Position.market == market, Position.closed_at.is_(None))
                )).all()
                remote = await adapter.fetch_positions()
                # 優先用 WebSocket 的即時價格，沒有（或太舊）才用 REST 補
                prices = {p.symbol: px for p in ledger if (px := self.hub.fresh_price(market, p.symbol))}
                missing = [p.symbol for p in ledger if p.symbol not in prices]
                prices.update(await adapter.fetch_prices(missing))

                if remote is not None:
                    by_symbol = {r.symbol: r for r in remote}
                    self._warned -= {p.symbol for p in ledger}  # 已納入帳本的就不再算「外部部位」
                    for r in remote:
                        if r.symbol not in {p.symbol for p in ledger} and r.symbol not in self._warned:
                            self._warned.add(r.symbol)
                            await self.bus.emit(
                                "log", level="warn", category="system",
                                message=f"交易所上有 {r.symbol} {r.side} {r.qty} 的部位不是機器人開的，不會被管理",
                            )
                    for p in ledger:
                        r = by_symbol.get(p.symbol)
                        if r is None:
                            await self._external_close(s, p, prices.get(p.symbol, p.mark_price or p.entry_price))
                        else:
                            p.mark_price = r.mark_price or prices.get(p.symbol)
                            p.unrealized_pnl = r.unrealized_pnl
                            p.liquidation_price = r.liquidation_price
                            p.leverage = r.leverage or p.leverage
                        changed = True
                else:
                    for p in ledger:
                        mark = prices.get(p.symbol)
                        if mark:
                            p.mark_price = mark
                            p.unrealized_pnl = (mark - p.entry_price) * p.qty * (1 if p.side == "long" else -1)
                            changed = True
                await s.commit()

                # WebSocket 斷線時的備援：用這次同步到的價格也檢查一次停損停利
                for p in ledger:
                    if not p.closed_at and p.mark_price:
                        self.hub.on_price(market, p.symbol, p.mark_price)
        if changed:
            await self.bus.emit("position_update", None)

    async def _external_close(self, s, p: Position, price: float):
        """交易所上的部位不見了（原生停損/停利、強平、或在交易所手動平倉）。"""
        _, realized = apply_fill(
            LedgerState(p.side, p.qty, p.entry_price), "sell" if p.side == "long" else "buy", p.qty, price
        )
        s.add(Trade(
            symbol=p.symbol, market=p.market, side="sell" if p.side == "long" else "buy", intent=f"close_{p.side}",
            price=price, qty=p.qty, realized_pnl=realized, executed_at=utcnow(), position_id=p.id,
        ))
        p.realized_pnl += realized
        p.unrealized_pnl = 0
        p.closed_at = utcnow()
        await s.flush()
        await self.bus.emit(
            "trade", None, level="warn", category="trade", notify=True,
            message=f"{p.symbol} 部位已在交易所被平倉（停損/停利/強平/手動），以標記價格 {price} 估算損益 {realized:+.4f}",
        )

    # ------------------------------------------------------------ 訂單
    async def sync_orders(self):
        async with self.ex.ledger_lock:
            await self._sync_orders()

    async def _sync_orders(self):
        async with self.sf() as s:
            pending = (await s.scalars(select(Order).where(Order.status.in_(("new", "open"))))).all()
            for o in pending:
                if (utcnow() - o.created_at.replace(tzinfo=utcnow().tzinfo)).total_seconds() < 15:
                    continue
                adapter = self.ex.adapters[o.market]
                if not o.exchange_order_id:
                    o.status, o.error = "unknown", "送單途中中斷，請到交易所確認"
                    await s.commit()
                    await self.bus.emit(
                        "order_update", o.to_dict(), level="error", category="order", order_id=o.id,
                        signal_id=o.signal_id, message=f"訂單 #{o.id} 送單狀態不明，請到交易所確認",
                    )
                    continue
                res = await adapter.fetch_order(o.symbol, o.exchange_order_id)
                if res.status != "open":  # 只在訂單結束時記錄一次，避免部分成交重複入帳
                    await self.ex.record_fill(s, adapter, o, res)

    def start(self, cfg) -> list[asyncio.Task]:
        return [
            asyncio.create_task(every(cfg.balance_sync_seconds, self.snapshot_balances, "balance")),
            asyncio.create_task(every(cfg.position_sync_seconds, self.sync_positions, "position")),
            asyncio.create_task(every(cfg.order_sync_seconds, self.sync_orders, "order")),
        ]
