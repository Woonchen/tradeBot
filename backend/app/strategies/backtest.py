"""用同一份策略程式回測。

- 第 i 根收盤時策略判斷改變 → 第 i+1 根「開盤價」成交（避免用收盤價成交的偷看）
- 每次成交扣 taker 手續費；不模擬滑價和強平
- 合約有提供 funding（Binance 的歷史資金費率）時，照實際持倉扣收資金費：每次結算時
  部位價值 × 當次費率，費率為正時多單付、空單收（負的反過來）。結算先於下單：剛好在結算時刻
  開倉不用付、剛好在結算時刻平倉要付。結算時間是 K 棒開盤就用開盤價，落在 K 棒中間（例如日 K）
  用收盤價近似
- 停損停利跟機器人一樣以開倉價的百分比算出觸發價，用每根 K 棒的最高／最低價判斷：
  觸及就以觸發價出場（開盤就跳空越過時以開盤價）；同一根同時碰到兩邊時當作先停損（保守估計）。
  停損停利出場後，策略判斷沒變就不會再進場，要等判斷改變（跟實際執行一樣）
  實際交易時合約用標記價格觸發，這裡用成交價的 K 棒近似
- 下單大小規則跟機器人一樣：固定保證金或可用資金 %，合約乘上槓桿
- warmup > 0 時，前 warmup 根只用來計算指標，不交易也不計入結果；回測起點就像那一刻啟用策略：
  當下的判斷只記下來，要等判斷「改變」才進場（跟實際啟用一樣，不會一開始就追進既有的部位）；
  follow_initial=True 時（對應策略的「啟用時立即依目前判斷進場」）起點就照當下判斷進場
"""

from dataclasses import dataclass, field

from ..engine.sizing import protection_prices
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
    stop_loss_pct: float = 0,
    take_profit_pct: float = 0,
    follow_initial: bool = False,
    funding: list[tuple[int, float]] | None = None,
) -> BacktestResult:
    """funding：[(結算時間秒, 每 8 小時費率 %)]，依時間排序；只用在合約。"""
    if fee_rate is None:
        fee_rate = 0.001 if market == "spot" else 0.0004
    lev = max(leverage, 1) if market == "futures" else 1
    targets = [for_market(t, market) for t in strategy.targets(candles)]
    if warmup > 0:
        warmup = min(warmup, len(candles) - 1)
        observed = "flat" if follow_initial else targets[warmup - 1]  # 啟用那一刻的判斷
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
    stop = tp = None
    blocked = None  # 被停損停利出場時的策略判斷；判斷沒變之前不再進場
    trade_funding = 0.0  # 這筆交易付掉的資金費（收到為負）
    total_funding = 0.0
    rates = funding if market == "futures" and funding else []
    fi = 0  # 下一個還沒處理的結算
    while fi < len(rates) and candles and rates[fi][0] < candles[0].time:
        fi += 1
    sec = candles[1].time - candles[0].time if len(candles) > 1 else 0
    res = BacktestResult()

    def settle(until: int, price: float, inclusive: bool):
        """處理時間 <= until（或 < until）的結算；持倉中就付／收資金費。"""
        nonlocal fi, cash, trade_funding, total_funding
        while fi < len(rates) and (rates[fi][0] <= until if inclusive else rates[fi][0] < until):
            if side != "flat":
                paid = qty * price * rates[fi][1] / 100 * (1 if side == "long" else -1)
                cash -= paid
                trade_funding += paid
                total_funding += paid
            fi += 1

    def close_at(price: float, t: int, reason: str):
        nonlocal cash, side, qty, entry, trade_funding
        d = 1 if side == "long" else -1
        pnl = (price - entry) * qty * d
        fee = price * qty * fee_rate
        cash += pnl - fee
        res.trades.append({
            "side": side, "entry_time": entry_time, "exit_time": t, "entry": entry, "exit": price, "qty": qty,
            "pnl": pnl, "fees": entry_fee + fee, "funding": trade_funding,
            "net": pnl - entry_fee - fee - trade_funding, "exit_reason": reason,
        })
        side, qty, entry, trade_funding = "flat", 0.0, 0.0, 0.0

    for i, c in enumerate(candles):
        settle(c.time, c.open, inclusive=True)  # 開盤那一刻的結算先於這根開盤的下單
        want = targets[i - 1] if i > 0 else side  # 前一根收盤的判斷，在這一根開盤執行
        if blocked is not None and want != blocked:
            blocked = None
        if blocked is not None:
            want = side  # 停損停利後判斷還沒變：維持空手
        if want != side:
            if side != "flat":
                close_at(c.open, c.time, "signal")
            if want != "flat":
                margin = size_value if size_mode == "fixed" else cash * size_value / 100
                margin = min(margin, cash)
                if margin > 0:
                    side, qty, entry, entry_time = want, margin * lev / c.open, c.open, c.time
                    entry_fee = entry * qty * fee_rate
                    cash -= entry_fee
                    stop, tp = protection_prices(side, entry, stop_loss_pct, take_profit_pct)
        if side != "flat" and (stop or tp):
            long = side == "long"
            worst, best = (c.low, c.high) if long else (c.high, c.low)
            if stop and (worst <= stop if long else worst >= stop):
                # 開盤就已經越過觸發價（跳空）時，只能用開盤價出場
                held, reason = side, "stop"
                close_at(min(c.open, stop) if long else max(c.open, stop), c.time, reason)
                blocked = held
            elif tp and (best >= tp if long else best <= tp):
                held, reason = side, "take_profit"
                close_at(max(c.open, tp) if long else min(c.open, tp), c.time, reason)
                blocked = held
        settle(c.time + sec, c.close, inclusive=False)  # K 棒中間的結算（下一根開盤那次留給下一根）
        unreal = (c.close - entry) * qty * (1 if side == "long" else -1) if side != "flat" else 0.0
        res.equity.append({"time": c.time, "value": cash + unreal})

    if side != "flat":  # 最後一根收盤時強制平倉，方便統計
        close_at(candles[-1].close, candles[-1].time, "end")
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
        "funding": total_funding,  # 正 = 付出、負 = 收到；沒有提供資金費率時是 0
        "funding_included": bool(rates),
        "buy_hold_return": candles[-1].close / candles[0].open - 1 if candles else 0,
        "bars": len(candles),
        "from": candles[0].time if candles else None,
        "to": candles[-1].time if candles else None,
    }
    return res
