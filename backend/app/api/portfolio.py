from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..engine.executor import Executor
from ..engine.performance import attribute, owner_label, owner_of
from ..engine.risk import daily_net_pnl, utc_day_start
from ..models import BalanceSnapshot, EquitySnapshot, Position, Signal, StrategyConfig, Trade
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
    rows = (await s.scalars(q)).all()
    # 每個部位的手續費（開平倉兩邊）與開倉來源
    ids = [p.id for p in rows]
    trades = (await s.scalars(
        select(Trade).where(Trade.position_id.in_(ids)).order_by(Trade.executed_at, Trade.id)
    )).all() if ids else []
    fees: dict[int, float] = {}
    opener: dict[int, Trade] = {}
    for t in trades:
        fees[t.position_id] = fees.get(t.position_id, 0.0) + t.fee_usdt
        opener.setdefault(t.position_id, t)
    sig_ids = {t.signal_id for t in opener.values() if t.signal_id}
    signals = {x.id: x for x in (await s.scalars(select(Signal).where(Signal.id.in_(sig_ids)))).all()} if sig_ids else {}
    names = {c.id: c.name for c in (await s.scalars(select(StrategyConfig))).all()}
    out = []
    for p in rows:
        t = opener.get(p.id)
        owner = owner_of(signals.get(t.signal_id) if t and t.signal_id else None)
        fee = fees.get(p.id, 0.0)
        out.append({**p.to_dict(), "fees": fee, "net": p.realized_pnl - fee, "owner": owner_label(owner, names) if t else None})
    return out


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
    # 勝率以「完整交易」計：開倉到全部平掉算一筆，淨損益扣開倉與平倉兩邊的手續費（跟策略頁的實際績效一致）
    trades = (await s.scalars(select(Trade).order_by(Trade.executed_at, Trade.id))).all()
    positions = {p.id: p for p in (await s.scalars(select(Position))).all()}
    trips = [t for st in attribute(list(trades), {}, positions).values() for t in st.trips]
    wins = sum(1 for t in trips if t.net > 0)
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
        "closed_trades": len(trips),
        "win_rate": wins / len(trips) if trips else None,
        "max_drawdown": max_drawdown([x.total_usdt for x in snaps]),
        "start_equity": snaps[0].total_usdt if snaps else None,
    }
