from dataclasses import dataclass

EPS = 1e-12


@dataclass(frozen=True)
class LedgerState:
    side: str | None  # long / short / None = 空手
    qty: float = 0.0
    entry: float = 0.0


def apply_fill(state: LedgerState, side: str, qty: float, price: float) -> tuple[LedgerState, float]:
    """把一筆成交套用到部位上，回傳 (新部位, 已實現損益)。平均成本法。"""
    fill_dir = 1 if side == "buy" else -1
    pos_dir = {"long": 1, "short": -1}.get(state.side or "", 0)

    if pos_dir == 0 or pos_dir == fill_dir:  # 開倉 / 加倉
        new_qty = state.qty + qty
        entry = (state.entry * state.qty + price * qty) / new_qty
        return LedgerState("long" if fill_dir > 0 else "short", new_qty, entry), 0.0

    closed = min(qty, state.qty)  # 減倉 / 平倉
    realized = (price - state.entry) * closed * pos_dir
    remaining = state.qty - closed
    leftover = qty - closed
    if leftover > EPS:  # 反手
        return LedgerState("long" if fill_dir > 0 else "short", leftover, price), realized
    if remaining <= EPS:
        return LedgerState(None), realized
    return LedgerState(state.side, remaining, state.entry), realized
