"""背景同步：餘額快照、部位標記價格/軟體停損、未成交訂單追蹤。"""

import asyncio
import logging

from datetime import timedelta, timezone

from sqlalchemy import delete, select

from ..engine.executor import Executor
from ..engine.ledger import LedgerState, apply_fill
from ..engine.risk import load_bot_settings, update_peaks
from ..engine.sizing import external_exit_reason
from ..models import BalanceSnapshot, EquitySnapshot, Order, Position, StrategyConfig, Trade, utcnow
from ..services.market_stream import MarketHub

log = logging.getLogger(__name__)

KEEP_FULL_DAYS = 7  # 最近幾天的餘額快照全部保留（每分鐘一筆）；更早的每小時只留一筆
PRUNE_EVERY = 6 * 3600

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
        """回撤熔斷：權益從最高點回撤超過上限，就全部平倉並停用 Bot（設了 breaker_fallback_leverage 時改成降槓桿繼續，只降一次）。

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
        head = f"觸發回撤熔斷：{detail}，超過上限 {cfg.max_drawdown_pct:g}%。已送出全部平倉（{{n}} 個部位）"
        lowered = await self._lower_leverage(s, cfg)
        if lowered:
            # 降槓桿繼續：權益最高點從現在重算；策略的判斷保留，要等判斷改變才重新進場（不會立刻追回去）
            cfg.equity_peaks = {}
            await self.ex.close_all(
                s, cfg, f"{head}；槓桿已從 {lowered} 降為 {cfg.breaker_fallback_leverage} 倍，Bot 繼續運作。"
                "下次再觸發熔斷就會停用 Bot", level="error", disable=False, reason="risk",
            )
        else:
            await self.ex.close_all(s, cfg, f"{head}，Bot 已停用", level="error", reason="risk")

    async def _lower_leverage(self, s, cfg) -> str | None:
        """熔斷後降槓桿：把槓桿高於 breaker_fallback_leverage 的合約策略（含設定頁）降下來。
        回傳被降下來的槓桿（例如「5」或「3～5」）；沒設定、或都已經不高於目標時回傳 None（照一般熔斷停用 Bot）。"""
        target = cfg.breaker_fallback_leverage or 0
        if target <= 0:
            return None
        strategies = (await s.scalars(select(StrategyConfig).where(
            StrategyConfig.enabled.is_(True), StrategyConfig.mode == "live", StrategyConfig.market == "futures",
        ))).all()
        before = {lev for lev in {st.leverage or cfg.leverage for st in strategies} | {cfg.leverage} if lev > target}
        if not before:
            return None
        for st in strategies:
            if st.leverage is not None and st.leverage > target:
                st.leverage = target
        cfg.leverage = min(cfg.leverage, target)  # 沒自訂槓桿的策略跟著設定頁一起降
        lo, hi = min(before), max(before)
        return f"{hi}" if lo == hi else f"{lo}～{hi}"

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
                            await self._external_close(s, adapter, p, prices.get(p.symbol, p.mark_price or p.entry_price))
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

    async def _external_close(self, s, adapter, p: Position, mark: float):
        """交易所上的部位不見了（原生停損/停利、強平、或在交易所手動平倉）。
        先查交易所的訂單紀錄找出是哪張單平掉的（原因和實際成交價）；查不到才用標記價格研判。"""
        opened = p.opened_at if p.opened_at.tzinfo else p.opened_at.replace(tzinfo=timezone.utc)
        found = await adapter.find_exit(p.symbol, p.side, int(opened.timestamp() * 1000))
        # 停損觸發後另一張停利單（或反過來）Binance 不會自動撤掉，留著會在下次同幣開倉時把新部位平掉
        await adapter.cancel_protection(p.symbol)
        price = found.price if found and found.price else mark
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
        if found:
            p.exit_reason = found.reason
            why = {"stop": "停損單成交", "take_profit": "停利單成交", "liquidation": "強平",
                   "external": "在交易所手動平倉"}.get(found.reason, found.reason)
        else:
            p.exit_reason = external_exit_reason(p.side, price, p.stop_price, p.take_profit_price, p.liquidation_price)
            why = {"stop": "研判為停損", "take_profit": "研判為停利", "liquidation": "研判為強平"}.get(
                p.exit_reason, "停損/停利/強平/手動")
        await s.flush()
        how = f"成交均價 {price}" if found and found.price else f"以標記價格 {price} 估算"
        await self.bus.emit(
            "trade", None, level="warn", category="trade", notify=True,
            message=f"{p.symbol} 部位已在交易所被平倉（{why}），{how}，損益 {realized:+.4f}",
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

    async def prune_snapshots(self) -> int:
        """舊的餘額快照每小時只留一筆（該小時最早的那次快照，資產明細跟權益一起留），回傳刪了幾筆。

        權益曲線、總覽的最大回撤只需要大概的形狀；最近 KEEP_FULL_DAYS 天保留完整的每分鐘資料。
        """
        cutoff = utcnow() - timedelta(days=KEEP_FULL_DAYS)
        removed = 0
        async with self.sf() as s:
            for model in (EquitySnapshot, BalanceSnapshot):
                rows = (await s.execute(select(model.id, model.ts).where(model.ts < cutoff))).all()
                first: dict = {}  # 每個小時最早的快照時間
                for _, ts in rows:
                    hour = ts.replace(minute=0, second=0, microsecond=0)
                    if hour not in first or ts < first[hour]:
                        first[hour] = ts
                drop = [i for i, ts in rows if ts != first[ts.replace(minute=0, second=0, microsecond=0)]]
                for k in range(0, len(drop), 1000):
                    await s.execute(delete(model).where(model.id.in_(drop[k:k + 1000])))
                removed += len(drop)
            await s.commit()
        if removed:
            log.info("pruned %d old balance/equity snapshots", removed)
        return removed

    def start(self, cfg) -> list[asyncio.Task]:
        return [
            asyncio.create_task(every(PRUNE_EVERY, self.prune_snapshots, "prune")),
            asyncio.create_task(every(cfg.balance_sync_seconds, self.snapshot_balances, "balance")),
            asyncio.create_task(every(cfg.position_sync_seconds, self.sync_positions, "position")),
            asyncio.create_task(every(cfg.order_sync_seconds, self.sync_orders, "order")),
        ]
