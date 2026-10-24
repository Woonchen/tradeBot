"""投資組合回測：多個策略共用同一個帳戶一起跑。

做法：
1. 每個策略先各自用 run_backtest 回測（同一套進出場、停損停利、手續費、資金費規則），
   部位大小固定為「每筆 1 USDT 部位價值」，得到每筆交易「每 1 USDT 部位」的淨損益。
2. 再照時間順序重播所有交易，部位大小改用策略自己的下單規則、以「當下的共用帳戶」計算：
   - fixed：固定保證金；percent：可用餘額的 %；equity：總權益的 %（跟實際下單一樣）
   - 可用餘額 = 總權益 − 持倉中的保證金。不夠時：equity 用剩下的 99% 進場，其他方式跳過這筆
   - 持倉數達到「設定」頁的上限時跳過
   - 有設回撤熔斷時：權益從最高點回撤超過上限，全部平倉並停止交易（跟實際一樣）
   損益跟部位價值成正比，所以每筆的淨損益 = 每 1 USDT 的淨損益 × 這筆的部位價值。
3. 每根 K 棒收盤（收盤之後才用那個價格）標記未實現損益，算出整個帳戶的權益曲線。

加碼：每次加碼是一筆獨立的「份」，部位大小 = 首筆的規則 × pyramid_frac、以加碼當下的共用帳戶計算；
可用餘額不夠時（equity 以外的方式）跳過這次加碼。每份的損益照它自己的成交價、分攤到的手續費與資金費計算。

簡化：持倉期間的資金費在平倉時一次入帳（權益曲線的標記不含尚未入帳的資金費）；不模擬強平；
同一個時間點依序：更新剛收盤的價格 → 出場 → 進場 → 加碼 → 標記權益。
"""

from dataclasses import dataclass, field

from .base import Candle


@dataclass
class Leg:
    """一個策略：下單規則、K 棒（只含回測期間）、每 1 USDT 部位價值的交易。"""
    id: int
    name: str
    symbol: str
    market: str
    size_mode: str
    size_value: float
    leverage: int
    candles: list[Candle]
    trades: list[dict]  # run_backtest 的交易（部位價值 1 USDT）
    pyramid_frac: float = 0.5


@dataclass
class _Open:
    leg: int
    trade: dict
    parts: list[tuple[float, float, float]]  # 每一份：(部位價值, 成交價, 每 1 USDT 平倉時入帳的損益)
    margin: float
    fees: float = 0.0  # 已經扣掉的進場手續費（算進這個策略的淨損益）

    @property
    def notional(self) -> float:
        return sum(p[0] for p in self.parts)


def _unit_net(tr: dict, f: dict) -> float:
    """這一份每 1 USDT 部位、平倉時入帳的損益：價差 − 分攤的出場手續費 − 分攤的資金費（進場手續費在成交時就扣）。"""
    d = 1 if tr["side"] == "long" else -1
    exit_fee = tr.get("exit_fee", tr["fees"] / 2) * f["qty"] / tr["qty"]
    return (f["qty"] * (tr["exit"] - f["price"]) * d - exit_fee - f["funding"]) / (f["qty"] * f["price"])


def _unit_fee(f: dict) -> float:
    return f["fee"] / (f["qty"] * f["price"])


def _fills(tr: dict) -> list[dict]:
    """沒有 fills 的舊格式：當成一次成交。"""
    return tr.get("fills") or [{"time": tr["entry_time"], "price": tr["entry"], "qty": tr["qty"],
                                "fee": tr["fees"] - tr.get("exit_fee", tr["fees"] / 2), "funding": tr["funding"]}]


@dataclass
class PortfolioResult:
    stats: dict = field(default_factory=dict)
    equity: list[dict] = field(default_factory=list)
    legs: list[dict] = field(default_factory=list)


def run_portfolio(legs: list[Leg], *, start_equity: float = 10_000, max_open: int | None = None,
                  max_drawdown_pct: float = 0) -> PortfolioResult:
    # 每根 K 棒的收盤時間 → 收盤價：只在收盤之後才用到這個價格（週期不同的策略混在一起也不會偷看）
    closes: dict[int, dict[int, float]] = {}
    for i, lg in enumerate(legs):
        sec = lg.candles[1].time - lg.candles[0].time if len(lg.candles) > 1 else 0
        closes[i] = {c.time + sec: c.close for c in lg.candles}
    entries: dict[int, list[tuple[int, dict]]] = {}
    adds: dict[int, list[tuple[int, dict, dict]]] = {}
    exits: dict[int, list[tuple[int, dict]]] = {}
    for i, lg in enumerate(legs):
        for t in lg.trades:
            fs = _fills(t)
            entries.setdefault(fs[0]["time"], []).append((i, t))
            for f in fs[1:]:
                adds.setdefault(f["time"], []).append((i, t, f))
            exits.setdefault(t["exit_time"], []).append((i, t))
    times = sorted({*entries, *adds, *exits, *(t for cl in closes.values() for t in cl)})

    cash = start_equity  # 已實現的權益
    open_: dict[int, _Open] = {}  # leg → 持倉
    last_close: dict[int, float] = {}
    peak, mdd, stopped_at = start_equity, 0.0, None
    skipped: dict[str, int] = {}
    per_leg = [{"trades": 0, "net": 0.0, "wins": 0} for _ in legs]
    exposure_sum, max_concurrent, n_marks = 0.0, 0, 0
    res = PortfolioResult()

    def unrealized(o: _Open) -> float:
        d = 1 if o.trade["side"] == "long" else -1
        px = last_close.get(o.leg)
        return sum(n * ((px if px is not None else p) / p - 1) * d for n, p, _ in o.parts)

    def equity() -> float:
        return cash + sum(unrealized(o) for o in open_.values())

    def size(lg: Leg, frac: float) -> float | None:
        """照下單規則、以當下的共用帳戶算保證金；不夠時 equity 用剩下的 99%，其他方式回傳 None（跳過）。"""
        eq = equity()
        free = eq - sum(o.margin for o in open_.values())
        if lg.size_mode == "fixed":
            margin = lg.size_value * frac
        elif lg.size_mode == "equity":
            margin = eq * lg.size_value / 100 * frac
        else:
            margin = free * lg.size_value / 100 * frac
        if lg.size_mode == "equity" and margin > free * 0.99:
            margin = free * 0.99
        return None if margin <= 0 or margin > free else margin

    def close(o: _Open, pnl: float):
        nonlocal cash
        cash += pnl
        net = pnl - o.fees
        per_leg[o.leg]["trades"] += 1
        per_leg[o.leg]["net"] += net
        per_leg[o.leg]["wins"] += net > 0
        del open_[o.leg]

    for t in times:
        # 0. 這一刻收盤的 K 棒：之後才用得到它的收盤價（出場、進場的部位大小、權益標記）
        for i in range(len(legs)):
            if t in closes[i]:
                last_close[i] = closes[i][t]
        if stopped_at is None:
            # 1. 出場（照實際成交價，含手續費、資金費）
            for i, tr in exits.get(t, []):
                o = open_.get(i)
                if o is not None and o.trade is tr:
                    close(o, sum(n * u for n, _, u in o.parts))
            # 2. 進場：用這一刻的共用帳戶計算部位大小
            for i, tr in entries.get(t, []):
                lg = legs[i]
                if i in open_:
                    continue
                if max_open is not None and len(open_) >= max_open:
                    skipped["持倉數已達上限"] = skipped.get("持倉數已達上限", 0) + 1
                    continue
                margin = size(lg, 1.0)
                if margin is None:
                    skipped["可用餘額不足"] = skipped.get("可用餘額不足", 0) + 1
                    continue
                f = _fills(tr)[0]
                lev = lg.leverage if lg.market == "futures" else 1
                open_[i] = _Open(i, tr, [(margin * lev, f["price"], _unit_net(tr, f))], margin)
                open_[i].fees = margin * lev * _unit_fee(f)
                cash -= open_[i].fees
                last_close[i] = f["price"]  # 剛進場：下一根收盤前，以成交價標記
            # 3. 加碼
            for i, tr, f in adds.get(t, []):
                o = open_.get(i)
                if o is None or o.trade is not tr:
                    continue
                lg = legs[i]
                margin = size(lg, lg.pyramid_frac)
                if margin is None:
                    skipped["加碼時可用餘額不足"] = skipped.get("加碼時可用餘額不足", 0) + 1
                    continue
                lev = lg.leverage if lg.market == "futures" else 1
                o.parts.append((margin * lev, f["price"], _unit_net(tr, f)))
                o.margin += margin
                o.fees += margin * lev * _unit_fee(f)
                cash -= margin * lev * _unit_fee(f)
        # 4. 標記未實現損益
        eq = equity()
        if open_:
            exposure_sum += sum(o.notional for o in open_.values()) / eq if eq > 0 else 0
        max_concurrent = max(max_concurrent, len(open_))
        n_marks += 1
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak if peak > 0 else 0)
        res.equity.append({"time": t, "value": eq})
        if stopped_at is None and max_drawdown_pct > 0 and eq <= peak * (1 - max_drawdown_pct / 100):
            for o in list(open_.values()):  # 熔斷：以收盤價全部平倉（手續費照 run_backtest 的出場費率估）
                fee = o.trade.get("exit_fee", o.trade["fees"] / 2) / (o.trade["qty"] * o.trade["exit"])
                close(o, unrealized(o) - o.notional * fee)
            stopped_at = t
            res.equity[-1]["value"] = cash  # 平倉後的權益（扣掉出場手續費）

    final = res.equity[-1]["value"] if res.equity else start_equity
    days = (times[-1] - times[0]) / 86400 if len(times) > 1 else 0
    ret = final / start_equity - 1
    res.stats = {
        "start_equity": start_equity, "final_equity": final, "total_return": ret,
        "cagr": (1 + ret) ** (365 / days) - 1 if days > 0 and ret > -1 else None,
        "max_drawdown": mdd,
        "trades": sum(x["trades"] for x in per_leg),
        "skipped": skipped,
        "max_concurrent": max_concurrent,
        # 平均部位價值 ÷ 權益（例如 0.5 = 平均有一半資金在市場裡，2 倍槓桿持滿約 2）
        "avg_exposure": exposure_sum / n_marks if n_marks else 0,
        "stopped_at": stopped_at,
        "from": times[0] if times else None, "to": res.equity[-1]["time"] if res.equity else None,
    }
    res.legs = [{
        "id": lg.id, "name": lg.name, "symbol": lg.symbol, "trades": per_leg[i]["trades"], "net": per_leg[i]["net"],
        "win_rate": per_leg[i]["wins"] / per_leg[i]["trades"] if per_leg[i]["trades"] else None,
    } for i, lg in enumerate(legs)]
    return res
