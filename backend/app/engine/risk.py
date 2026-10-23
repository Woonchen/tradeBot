from datetime import datetime, time, timezone
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import BotSettings, Position, Trade


class Reject(Exception):
    """訊號被風控或規則拒絕（不是系統錯誤）。"""


async def load_bot_settings(s: AsyncSession) -> BotSettings:
    cfg = await s.get(BotSettings, 1)
    if cfg is None:
        cfg = BotSettings(id=1)
        s.add(cfg)
        await s.flush()
    return cfg


def utc_day_start() -> datetime:
    return datetime.combine(datetime.now(timezone.utc).date(), time.min, tzinfo=timezone.utc)


async def daily_net_pnl(s: AsyncSession) -> float:
    """今天（UTC）已實現損益扣掉手續費。"""
    q = select(func.coalesce(func.sum(Trade.realized_pnl - Trade.fee_usdt), 0.0)).where(
        Trade.executed_at >= utc_day_start()
    )
    return float(await s.scalar(q))


async def open_position_count(s: AsyncSession) -> int:
    return int(await s.scalar(select(func.count()).select_from(Position).where(Position.closed_at.is_(None))))


async def check_open_risk(s: AsyncSession, cfg: BotSettings, closing_one: bool):
    pnl = await daily_net_pnl(s)
    if pnl <= -cfg.max_daily_loss_usdt:
        raise Reject(f"今日已虧損 {pnl:.2f} USDT，達到每日虧損上限 {cfg.max_daily_loss_usdt}")
    count = await open_position_count(s) - (1 if closing_one else 0)
    if count >= cfg.max_open_positions:
        raise Reject(f"持倉數 {count} 已達上限 {cfg.max_open_positions}")


ORDER_FIELDS = ("size_mode", "size_value", "leverage", "stop_loss_pct", "take_profit_pct")


def order_rules(bot: BotSettings, strategy=None) -> SimpleNamespace:
    """這筆下單用的規則：策略有自訂的欄位用策略的，沒填的沿用「設定」頁。

    保證金模式是帳戶層級的設定，一律用設定頁；持倉上限、每日虧損上限也是（在 check_open_risk）。
    """
    vals = {f: getattr(bot, f) for f in ORDER_FIELDS}
    for f in ORDER_FIELDS:
        v = getattr(strategy, f, None) if strategy is not None else None
        if v is not None:
            vals[f] = v
    return SimpleNamespace(**vals, margin_mode=bot.margin_mode)
