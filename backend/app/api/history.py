from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import ActivityLog, Order, Signal, Trade
from .deps import require_user

router = APIRouter(prefix="/api", tags=["history"], dependencies=[Depends(require_user)])


@router.get("/signals")
async def list_signals(
    limit: int = Query(100, le=1000), status: str | None = None, s: AsyncSession = Depends(get_session)
):
    q = select(Signal).order_by(Signal.id.desc()).limit(limit)
    if status:
        q = q.where(Signal.status == status)
    return [x.to_dict() for x in (await s.scalars(q)).all()]


@router.get("/signals/{signal_id}")
async def signal_trace(signal_id: int, s: AsyncSession = Depends(get_session)):
    """單一訊號的完整軌跡：訊號 → 訂單 → 成交 → 日誌。"""
    sig = await s.get(Signal, signal_id)
    if not sig:
        raise HTTPException(404)
    orders = (await s.scalars(select(Order).where(Order.signal_id == signal_id).order_by(Order.id))).all()
    trades = (await s.scalars(select(Trade).where(Trade.signal_id == signal_id).order_by(Trade.id))).all()
    logs = (await s.scalars(
        select(ActivityLog).where(ActivityLog.signal_id == signal_id).order_by(ActivityLog.id)
    )).all()
    return {
        "signal": sig.to_dict(),
        "orders": [o.to_dict() for o in orders],
        "trades": [t.to_dict() for t in trades],
        "logs": [x.to_dict() for x in logs],
    }


@router.get("/orders")
async def list_orders(limit: int = Query(100, le=1000), s: AsyncSession = Depends(get_session)):
    q = select(Order).order_by(Order.id.desc()).limit(limit)
    return [x.to_dict() for x in (await s.scalars(q)).all()]


@router.get("/trades")
async def list_trades(
    symbol: str | None = None,
    market: str | None = None,
    limit: int = Query(500, le=5000),
    s: AsyncSession = Depends(get_session),
):
    q = select(Trade).order_by(Trade.id.desc()).limit(limit)
    if symbol:
        q = q.where(Trade.symbol == symbol)
    if market:
        q = q.where(Trade.market == market)
    return [x.to_dict() for x in (await s.scalars(q)).all()]


@router.get("/activity")
async def activity(
    limit: int = Query(200, le=1000),
    level: str | None = None,
    category: str | None = None,
    before_id: int | None = None,
    s: AsyncSession = Depends(get_session),
):
    q = select(ActivityLog).order_by(ActivityLog.id.desc()).limit(limit)
    if level:
        q = q.where(ActivityLog.level == level)
    if category:
        q = q.where(ActivityLog.category == category)
    if before_id:
        q = q.where(ActivityLog.id < before_id)
    return [x.to_dict() for x in (await s.scalars(q)).all()]
