"""影子模式：策略照常判斷，但只記錄虛擬交易，不送單、不佔用部位。

跟回測用同一套規則，結果才能拿來對照：
- 以產生判斷那根 K 棒的收盤價成交（實際市價單也差不多是這個價），手續費用 taker 費率估算
- 下單金額照「設定」頁的規則，套在每個策略自己的虛擬帳戶上（起始 SHADOW_START_EQUITY + 已實現淨損益）
- 停損停利用之後每根 K 棒的最高／最低價判斷；觸發後判斷沒變就不再進場（由呼叫端只在判斷改變時進場來保證）
- 合約照實際持倉扣收資金費（Binance 歷史費率）：結算先於下單，剛好在結算時刻開倉不付、平倉要付；
  結算時間是 K 棒開盤用開盤價，落在 K 棒中間用收盤價近似；被停損停利時只算到觸發那根的開盤
"""

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import SHADOW_START_EQUITY, BotSettings, ShadowTrade, StrategyConfig
from ..strategies.base import Candle
from .risk import order_rules
from .sizing import compute_order_size, protection_prices


def fee_rate(market: str) -> float:
    return 0.001 if market == "spot" else 0.0004  # 跟回測的預設一致


def _at(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


def close(t: ShadowTrade, price: float, when: int, reason: str) -> None:
    d = 1 if t.side == "long" else -1
    t.exit_price, t.exit_time, t.exit_reason = price, _at(when), reason
    t.pnl = (price - t.entry_price) * t.qty * d
    t.exit_fee = price * t.qty * fee_rate(t.market)


def net(t: ShadowTrade) -> float:
    return t.pnl - t.entry_fee - t.exit_fee - t.funding


def apply_funding(t: ShadowTrade, rates: list[tuple[int, float]], candles: list[Candle], sec: int, until: int) -> None:
    """把 (funding_until, until] 之間的結算算進這筆虛擬交易。rates：[(秒, 每 8 小時 %)]。"""
    if t.market != "futures" or not rates:
        return
    after = t.funding_until if t.funding_until is not None else t.entry_bar + sec  # 開倉那一刻的結算不算
    for ts, rate in rates:
        if ts <= after:
            continue
        if ts > until:
            break
        bar = next((c for c in candles if c.time <= ts < c.time + sec), None)
        price = (bar.open if bar.time == ts else bar.close) if bar else t.entry_price
        t.funding = (t.funding or 0) + t.qty * price * rate / 100 * (1 if t.side == "long" else -1)
        t.funding_until = ts


def protection_exit(t: ShadowTrade, candles: list[Candle], sec: int) -> tuple[float, int, str] | None:
    """開倉之後收盤的 K 棒裡，第一根觸發停損停利的（價格、時間、原因）。規則跟回測一樣。"""
    if not (t.stop_price or t.take_profit_price):
        return None
    long = t.side == "long"
    for c in candles:
        if c.time <= t.entry_bar:
            continue
        worst, best = (c.low, c.high) if long else (c.high, c.low)
        if t.stop_price and (worst <= t.stop_price if long else worst >= t.stop_price):
            return (min(c.open, t.stop_price) if long else max(c.open, t.stop_price)), c.time + sec, "stop"
        if t.take_profit_price and (best >= t.take_profit_price if long else best <= t.take_profit_price):
            return (max(c.open, t.take_profit_price) if long else min(c.open, t.take_profit_price)), c.time + sec, "take_profit"
    return None


async def virtual_equity(s: AsyncSession, strategy_id: int) -> float:
    q = select(func.coalesce(func.sum(ShadowTrade.pnl - ShadowTrade.entry_fee - ShadowTrade.exit_fee - ShadowTrade.funding), 0.0)).where(
        ShadowTrade.strategy_id == strategy_id, ShadowTrade.exit_time.is_not(None),
    )
    return SHADOW_START_EQUITY + float(await s.scalar(q))


async def open_trade(
    s: AsyncSession, cfg: StrategyConfig, bot: BotSettings, symbol: str, side: str, bar: Candle, sec: int,
) -> ShadowTrade | None:
    rules = order_rules(bot, cfg)  # 策略自訂的下單規則，沒填的沿用設定頁
    lev = rules.leverage if cfg.market == "futures" else 1
    _, qty = compute_order_size(rules.size_mode, rules.size_value, lev, await virtual_equity(s, cfg.id), bar.close)
    if qty <= 0:
        return None
    stop, tp = protection_prices(side, bar.close, rules.stop_loss_pct, rules.take_profit_pct)
    t = ShadowTrade(
        strategy_id=cfg.id, symbol=symbol, market=cfg.market, side=side, qty=qty, entry_price=bar.close,
        entry_bar=bar.time, entry_time=_at(bar.time + sec), entry_fee=bar.close * qty * fee_rate(cfg.market),
        stop_price=stop, take_profit_price=tp, funding=0.0,
    )
    s.add(t)
    return t
