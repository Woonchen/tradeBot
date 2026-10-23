"""把實際成交歸到「開倉的那個來源」，算出每個策略的實際績效。

一個部位（positions 表的一列）從開倉到平倉算一筆完整交易（round trip），成交用 position_id 對應到部位。
平倉可能來自策略、停損停利、手動、或交易所上直接平掉（沒有對應的訊號），所以不看平倉是誰做的：
部位是哪個策略開的，整筆交易的損益與手續費就算給那個策略。

部位是否平完以部位表為準，不用成交數量相減：現貨買入的手續費從幣扣、殘量直接結案時，
成交數量的加總會對不起來。沒有 position_id 的舊成交才退回用數量判斷。

策略用 id 辨識（訊號的 raw_payload 裡有 strategy_id），改名不會讓紀錄分家。
"""

from dataclasses import dataclass, field

from ..models import Position, Signal, Trade

# 歸屬的 key：("strategy", 策略 id) / ("tradingview", 警示名稱) / ("manual", "") / ("other", "")
Owner = tuple[str, int | str]


def owner_of(sig: Signal | None) -> Owner:
    if sig is None:
        return ("other", "")
    if sig.source == "strategy" and isinstance((sig.raw_payload or {}).get("strategy_id"), int):
        return ("strategy", sig.raw_payload["strategy_id"])
    if sig.source == "tradingview":
        return ("tradingview", sig.strategy or "tradingview")
    if sig.source == "manual":
        return ("manual", "")
    return ("other", "")


OTHER_LABELS = {"tradingview": "TradingView", "manual": "手動開倉", "other": "其他（交易所上直接操作等）"}


def names_from_signals(signals: dict[int, Signal]) -> dict[int, str]:
    """策略 id → 下單當時的策略名稱（策略刪掉後還能顯示它叫什麼）。"""
    return {
        sig.raw_payload["strategy_id"]: sig.strategy
        for sig in signals.values()
        if owner_of(sig)[0] == "strategy" and sig.strategy
    }


def owner_label(owner: Owner, strategy_names: dict[int, str], signal_names: dict[int, str] | None = None) -> str:
    """strategy_names：目前存在的策略；signal_names：訊號上記錄的名稱，給已刪除的策略用。"""
    kind, name = owner
    if kind == "strategy":
        if name in strategy_names:
            return strategy_names[name]  # type: ignore[index]
        old = (signal_names or {}).get(name)  # type: ignore[arg-type]
        return f"已刪除：{old}" if old else f"已刪除的策略 #{name}"
    if kind == "tradingview":
        return f"TradingView：{name}"
    return OTHER_LABELS[kind]


@dataclass
class RoundTrip:
    symbol: str
    market: str
    side: str
    opened_at: str
    closed_at: str | None = None
    qty: float = 0.0
    realized: float = 0.0
    fees: float = 0.0

    @property
    def net(self) -> float:
        return self.realized - self.fees


@dataclass
class OwnerStats:
    trips: list[RoundTrip] = field(default_factory=list)
    open: list[RoundTrip] = field(default_factory=list)  # 還沒平完的部位

    def summary(self) -> dict:
        nets = [t.net for t in self.trips]
        wins = [n for n in nets if n > 0]
        losses = [-n for n in nets if n <= 0]
        return {
            "trades": len(nets),
            "wins": len(wins),
            "win_rate": len(wins) / len(nets) if nets else None,
            "net": sum(nets),
            "fees": sum(t.fees for t in self.trips),
            "profit_factor": sum(wins) / sum(losses) if sum(losses) > 0 else None,
            "first_trade": self.trips[0].opened_at if self.trips else (self.open[0].opened_at if self.open else None),
            "last_trade": self.trips[-1].closed_at if self.trips else None,
            "open": [{"symbol": t.symbol, "market": t.market, "side": t.side, "qty": t.qty, "opened_at": t.opened_at,
                      "realized": t.realized, "fees": t.fees} for t in self.open],
        }


def _iso(dt) -> str:
    return dt.isoformat() if dt else ""


def attribute(
    trades: list[Trade], signals: dict[int, Signal], positions: dict[int, Position] | None = None,
) -> dict[Owner, OwnerStats]:
    """trades 必須依成交時間排序；positions 是 id → Position（成交的 position_id 對得到的才用部位表）。"""
    out: dict[Owner, OwnerStats] = {}
    positions = positions or {}
    by_pos: dict[int, tuple[Owner, RoundTrip]] = {}
    legacy: list[Trade] = []
    for t in trades:
        p = positions.get(t.position_id) if t.position_id else None
        if p is None:
            legacy.append(t)
            continue
        if p.id not in by_pos:  # 部位的第一筆成交就是開倉，用它的訊號決定歸屬
            owner = owner_of(signals.get(t.signal_id) if t.signal_id else None)
            by_pos[p.id] = (owner, RoundTrip(p.symbol, p.market, p.side, _iso(p.opened_at)))
        _, trip = by_pos[p.id]
        trip.realized += t.realized_pnl
        trip.fees += t.fee_usdt
    for pid, (owner, trip) in by_pos.items():
        p = positions[pid]
        if p.closed_at:
            trip.closed_at = _iso(p.closed_at)
            out.setdefault(owner, OwnerStats()).trips.append(trip)
        else:
            trip.qty = p.qty
            out.setdefault(owner, OwnerStats()).open.append(trip)
    for st in out.values():
        st.trips.sort(key=lambda x: x.closed_at or "")
    _attribute_by_qty(legacy, signals, out)
    return out


def _attribute_by_qty(trades: list[Trade], signals: dict[int, Signal], out: dict[Owner, OwnerStats]) -> None:
    """沒有 position_id 的舊成交：用成交數量相減判斷部位是否平完。"""
    current: dict[tuple[str, str], tuple[Owner, RoundTrip]] = {}  # (symbol, market) → 進行中的部位
    for t in trades:
        key = (t.symbol, t.market)
        when = t.executed_at.isoformat() if t.executed_at else ""
        if t.intent.startswith("open"):
            if key not in current:
                owner = owner_of(signals.get(t.signal_id) if t.signal_id else None)
                current[key] = (owner, RoundTrip(t.symbol, t.market, t.intent.removeprefix("open_"), when))
            _, trip = current[key]
            trip.qty += t.qty
            trip.fees += t.fee_usdt
        else:
            if key not in current:  # 開倉發生在有紀錄之前（理論上不會）：歸到「其他」
                current[key] = (("other", ""), RoundTrip(t.symbol, t.market, t.intent.removeprefix("close_"), when))
            owner, trip = current[key]
            trip.qty -= t.qty
            trip.realized += t.realized_pnl
            trip.fees += t.fee_usdt
            if trip.qty <= 1e-9 * max(1.0, t.qty):  # 全部平掉：一筆完整交易結束
                trip.qty, trip.closed_at = 0.0, when
                out.setdefault(owner, OwnerStats()).trips.append(trip)
                del current[key]
    for owner, trip in current.values():
        out.setdefault(owner, OwnerStats()).open.append(trip)
