def compute_order_size(
    size_mode: str, size_value: float, leverage: int, free_usdt: float, price: float, equity_usdt: float | None = None,
) -> tuple[float, float]:
    """回傳 (使用的保證金 USDT, 下單數量)。現貨 leverage 傳 1。

    fixed：固定 size_value USDT；percent：可用餘額的 %；equity：帳戶總權益的 %（沒給權益就用可用餘額）。
    多個策略同時持倉時用 equity：每個策略分到的一樣多，不會因為先進場的用掉可用餘額，後面的越分越少。
    """
    if size_mode == "fixed":
        margin = size_value
    else:
        base = equity_usdt if size_mode == "equity" and equity_usdt is not None else free_usdt
        margin = base * size_value / 100
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
