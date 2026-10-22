"""把實際成交歸到「開倉的那個來源」，算出每個策略的實際績效。

一個部位從開倉到全部平掉算一筆完整交易（round trip）。平倉可能來自策略、停損停利、手動、
或交易所上直接平掉（沒有對應的訊號），所以不看平倉是誰做的：部位是哪個策略開的，
整筆交易的損益與手續費就算給那個策略。

策略用 id 辨識（訊號的 raw_payload 裡有 strategy_id），改名不會讓紀錄分家。
"""

from dataclasses import dataclass, field

from ..models import Signal, Trade

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


def attribute(trades: list[Trade], signals: dict[int, Signal]) -> dict[Owner, OwnerStats]:
    """trades 必須依成交時間排序。"""
    out: dict[Owner, OwnerStats] = {}
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
    return out
