"""背景同步：餘額快照、部位標記價格/軟體停損、未成交訂單追蹤。"""

import asyncio
import logging

from sqlalchemy import select

from ..engine.executor import Executor
from ..engine.ledger import LedgerState, apply_fill
from ..engine.sizing import protection_hit
from ..models import BalanceSnapshot, EquitySnapshot, Order, Position, Trade, utcnow

log = logging.getLogger(__name__)


async def every(seconds: int, fn, name: str):
    while True:
        try:
            await fn()
        except Exception:
            log.exception("worker %s failed", name)
        await asyncio.sleep(seconds)


class SyncWorkers:
    def __init__(self, executor: Executor):
        self.ex = executor
        self.sf = executor.sf
        self.bus = executor.bus
        self._triggered: set[int] = set()  # 已送出軟體停損的 position id
        self._warned: set[str] = set()

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
        await self.bus.emit("balance_update", snap.to_dict())

    # ------------------------------------------------------------ 部位
    async def sync_positions(self):
        changed = False
        async with self.sf() as s:
            for market, adapter in self.ex.adapters.items():
                ledger = (await s.scalars(
                    select(Position).where(Position.market == market, Position.closed_at.is_(None))
                )).all()
                remote = await adapter.fetch_positions()
                prices = await adapter.fetch_prices([p.symbol for p in ledger])

                if remote is not None:
                    by_symbol = {r.symbol: r for r in remote}
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

                for p in ledger:
                    if p.closed_at or p.native_protection or p.id in self._triggered or not p.mark_price:
                        continue
                    hit = protection_hit(p.side, p.mark_price, p.stop_price, p.take_profit_price)
                    if hit:
                        self._triggered.add(p.id)
                        await self.bus.emit(
                            "log", level="warn", category="risk",
                            message=f"{p.symbol} 標記價格 {p.mark_price} 觸發{'停損' if hit == 'stop' else '停利'}，送出平倉",
                        )
                        await self.ex.submit(
                            source="protection", ticker=p.symbol, market=market, action="close", target="flat",
                            strategy=hit, raw={"position_id": p.id, "mark_price": p.mark_price},
                        )
        if changed:
            await self.bus.emit("position_update", None)

    async def _external_close(self, s, p: Position, price: float):
        """交易所上的部位不見了（原生停損/停利、強平、或在交易所手動平倉）。"""
        _, realized = apply_fill(
            LedgerState(p.side, p.qty, p.entry_price), "sell" if p.side == "long" else "buy", p.qty, price
        )
        s.add(Trade(
            symbol=p.symbol, market=p.market, side="sell" if p.side == "long" else "buy", intent=f"close_{p.side}",
            price=price, qty=p.qty, realized_pnl=realized, executed_at=utcnow(),
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
