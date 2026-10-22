"""用同一份策略程式回測。

- 第 i 根收盤時策略判斷改變 → 第 i+1 根「開盤價」成交（避免用收盤價成交的偷看）
- 每次成交扣 taker 手續費；不模擬滑價、資金費率和強平
- 下單大小規則跟機器人一樣：固定保證金或可用資金 %，合約乘上槓桿
- warmup > 0 時，前 warmup 根只用來計算指標，不交易也不計入結果；回測起點就像那一刻啟用策略：
  當下的判斷只記下來，要等判斷「改變」才進場（跟實際啟用一樣，不會一開始就追進既有的部位）
"""

from dataclasses import dataclass, field

from .base import Candle, Strategy, for_market


@dataclass
class BacktestResult:
    trades: list[dict] = field(default_factory=list)
    equity: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def run_backtest(
    strategy: Strategy,
    candles: list[Candle],
    *,
    market: str,
    size_mode: str = "fixed",
    size_value: float = 100,
    leverage: int = 1,
    fee_rate: float | None = None,
    start_equity: float = 10_000,
    warmup: int = 0,
) -> BacktestResult:
    if fee_rate is None:
        fee_rate = 0.001 if market == "spot" else 0.0004
    lev = max(leverage, 1) if market == "futures" else 1
    targets = [for_market(t, market) for t in strategy.targets(candles)]
    if warmup > 0:
        warmup = min(warmup, len(candles) - 1)
        observed = targets[warmup - 1]  # 啟用那一刻的判斷
        window, changed = [], False
        for t in targets[warmup:]:
            changed = changed or t != observed
            window.append(t if changed else "flat")
        candles, targets = candles[warmup:], window

    cash = start_equity  # 已實現的資金（含手續費）
    side: str = "flat"
    qty = entry = 0.0
    entry_time = 0
    entry_fee = 0.0
    res = BacktestResult()

    def close_at(price: float, t: int):
        nonlocal cash, side, qty, entry
        d = 1 if side == "long" else -1
        pnl = (price - entry) * qty * d
        fee = price * qty * fee_rate
        cash += pnl - fee
        res.trades.append({
            "side": side, "entry_time": entry_time, "exit_time": t, "entry": entry, "exit": price, "qty": qty,
            "pnl": pnl, "fees": entry_fee + fee, "net": pnl - entry_fee - fee,
        })
        side, qty, entry = "flat", 0.0, 0.0

    for i, c in enumerate(candles):
        if i > 0 and targets[i - 1] != side:  # 前一根收盤的判斷，在這一根開盤執行
            want = targets[i - 1]
            if side != "flat":
                close_at(c.open, c.time)
            if want != "flat":
                margin = size_value if size_mode == "fixed" else cash * size_value / 100
                margin = min(margin, cash)
                if margin > 0:
                    side, qty, entry, entry_time = want, margin * lev / c.open, c.open, c.time
                    entry_fee = entry * qty * fee_rate
                    cash -= entry_fee
        unreal = (c.close - entry) * qty * (1 if side == "long" else -1) if side != "flat" else 0.0
        res.equity.append({"time": c.time, "value": cash + unreal})

    if side != "flat":  # 最後一根收盤時強制平倉，方便統計
        close_at(candles[-1].close, candles[-1].time)
        res.equity[-1]["value"] = cash

    final = res.equity[-1]["value"] if res.equity else start_equity
    peak, mdd = float("-inf"), 0.0
    for p in res.equity:
        peak = max(peak, p["value"])
        mdd = max(mdd, (peak - p["value"]) / peak if peak > 0 else 0)
    wins = [t for t in res.trades if t["net"] > 0]
    gross_win = sum(t["net"] for t in wins)
    gross_loss = -sum(t["net"] for t in res.trades if t["net"] <= 0)
    res.stats = {
        "start_equity": start_equity,
        "final_equity": final,
        "total_return": final / start_equity - 1,
        "max_drawdown": mdd,
        "trades": len(res.trades),
        "win_rate": len(wins) / len(res.trades) if res.trades else None,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "fees": sum(t["fees"] for t in res.trades),
        "buy_hold_return": candles[-1].close / candles[0].open - 1 if candles else 0,
        "bars": len(candles),
        "from": candles[0].time if candles else None,
        "to": candles[-1].time if candles else None,
    }
    return res
