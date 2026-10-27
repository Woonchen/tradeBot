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


# 平倉原因：stop / take_profit = 停損停利（軟體監控或交易所的原生單）；signal = 策略或 TradingView 的訊號；
# manual = 手動平倉；risk = 回撤熔斷全部平倉；liquidation = 強平；external = 在交易所被平掉、看不出原因
EXIT_REASONS = ("stop", "take_profit", "signal", "manual", "risk", "liquidation", "external")


def exit_reason_of(source: str, strategy: str, raw: dict | None) -> str:
    """從平倉訊號判斷原因（record_fill 平倉時用）。"""
    if source == "protection":
        return strategy if strategy in ("stop", "take_profit") else "stop"
    if source == "manual":
        return "risk" if (raw or {}).get("reason") == "risk" else "manual"
    return "signal"


def external_exit_reason(side: str, price: float, stop: float | None, tp: float | None,
                         liquidation: float | None, tol: float = 0.003) -> str:
    """部位在交易所上不見了，只拿得到同步當下的標記價格：碰到（或差 tol 以內）停損停利價就算那個，越過強平價算強平。
    觸發後價格可能已經彈回去一點，所以留一點容許誤差。"""
    d = 1 if side == "long" else -1
    if stop and (price - stop) * d <= price * tol:
        return "stop"
    if tp and (tp - price) * d <= price * tol:
        return "take_profit"
    if liquidation and (price - liquidation) * d <= price * tol:
        return "liquidation"
    return "external"


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
