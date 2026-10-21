from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..engine.executor import Executor
from ..engine.risk import daily_net_pnl, utc_day_start
from ..models import BalanceSnapshot, EquitySnapshot, Position, Trade
from .deps import get_executor, require_user

router = APIRouter(prefix="/api", tags=["portfolio"], dependencies=[Depends(require_user)])


def max_drawdown(values: list[float]) -> float:
    """最大回撤（0~1）。"""
    peak, mdd = float("-inf"), 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


@router.get("/positions")
async def positions(status: str = "open", limit: int = Query(200, le=2000), s: AsyncSession = Depends(get_session)):
    q = select(Position).order_by(Position.id.desc()).limit(limit)
    q = q.where(Position.closed_at.is_(None) if status == "open" else Position.closed_at.is_not(None))
    return [p.to_dict() for p in (await s.scalars(q)).all()]


@router.post("/positions/{position_id}/close")
async def close_position(position_id: int, s: AsyncSession = Depends(get_session), ex: Executor = Depends(get_executor)):
    p = await s.get(Position, position_id)
    if not p or p.closed_at:
        raise HTTPException(404, "部位不存在或已平倉")
    sig, _ = await ex.submit(source="manual", ticker=p.symbol, market=p.market, action="close", target="flat")
    return {"signal_id": sig.id}


@router.get("/balances")
async def balances(s: AsyncSession = Depends(get_session)):
    latest = await s.scalar(select(func.max(BalanceSnapshot.ts)))
    if latest is None:
        return []
    rows = (await s.scalars(
        select(BalanceSnapshot).where(BalanceSnapshot.ts == latest).order_by(BalanceSnapshot.usdt_value.desc())
    )).all()
    return [r.to_dict() for r in rows]


@router.get("/equity")
async def equity(days: int = Query(30, le=3650), s: AsyncSession = Depends(get_session)):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await s.scalars(
        select(EquitySnapshot).where(EquitySnapshot.ts >= since).order_by(EquitySnapshot.ts)
    )).all()
    return [r.to_dict() for r in rows]


@router.get("/summary")
async def summary(s: AsyncSession = Depends(get_session)):
    snaps = (await s.scalars(select(EquitySnapshot).order_by(EquitySnapshot.ts))).all()
    first_today = await s.scalar(
        select(EquitySnapshot).where(EquitySnapshot.ts >= utc_day_start()).order_by(EquitySnapshot.ts).limit(1)
    )
    closes = (await s.scalars(select(Trade).where(Trade.intent.like("close%")))).all()
    wins = sum(1 for t in closes if t.realized_pnl - t.fee_usdt > 0)
    total_net = await s.scalar(select(func.coalesce(func.sum(Trade.realized_pnl - Trade.fee_usdt), 0.0)))
    open_pos = (await s.scalars(select(Position).where(Position.closed_at.is_(None)))).all()
    eq = snaps[-1].total_usdt if snaps else None
    return {
        "equity": eq,
        "equity_change_today": (eq - first_today.total_usdt) if (eq is not None and first_today) else 0.0,
        "realized_today": await daily_net_pnl(s),
        "realized_total": float(total_net),
        "unrealized": sum(p.unrealized_pnl or 0 for p in open_pos),
        "open_positions": len(open_pos),
        "closed_trades": len(closes),
        "win_rate": wins / len(closes) if closes else None,
        "max_drawdown": max_drawdown([x.total_usdt for x in snaps]),
        "start_equity": snaps[0].total_usdt if snaps else None,
    }
