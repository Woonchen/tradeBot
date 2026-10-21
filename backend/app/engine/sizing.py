def compute_order_size(
    size_mode: str, size_value: float, leverage: int, free_usdt: float, price: float
) -> tuple[float, float]:
    """回傳 (使用的保證金 USDT, 下單數量)。現貨 leverage 傳 1。"""
    margin = size_value if size_mode == "fixed" else free_usdt * size_value / 100
    if price <= 0 or margin <= 0:
        return 0.0, 0.0
    return margin, margin * max(leverage, 1) / price


def protection_prices(side: str, entry: float, stop_pct: float, tp_pct: float) -> tuple[float | None, float | None]:
    d = 1 if side == "long" else -1
    stop = entry * (1 - d * stop_pct / 100) if stop_pct > 0 else None
    tp = entry * (1 + d * tp_pct / 100) if tp_pct > 0 else None
    return stop, tp


def protection_hit(side: str, mark: float, stop: float | None, tp: float | None) -> str | None:
    if side == "long":
        if stop and mark <= stop:
            return "stop"
        if tp and mark >= tp:
            return "take_profit"
    else:
        if stop and mark >= stop:
            return "stop"
        if tp and mark <= tp:
            return "take_profit"
    return None
