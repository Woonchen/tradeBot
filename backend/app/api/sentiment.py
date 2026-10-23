from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import FearGreed, FundingRate, MarketMetric
from ..services.sentiment import DAY, METRICS, tracked
from .deps import require_user

router = APIRouter(prefix="/api", tags=["sentiment"], dependencies=[Depends(require_user)])


def _at_or_before(rows: list, ts: int):
    """rows 依時間由新到舊排序；回傳時間 <= ts 的第一筆。"""
    return next((r for r in rows if r.ts <= ts), None)


@router.get("/sentiment")
async def sentiment(s: AsyncSession = Depends(get_session)):
    """儀表板的市場情緒：恐懼貪婪指數，以及每個追蹤中的幣的資金費率、多空比、未平倉量、主動買賣比。"""
    fng = (await s.scalars(select(FearGreed).order_by(FearGreed.ts.desc()).limit(8))).all()
    fear_greed = None
    if fng:
        now = fng[0]
        week = _at_or_before(fng, now.ts - 7 * DAY)
        fear_greed = {
            "value": now.value, "classification": now.classification, "ts": now.ts,
            "prev": fng[1].value if len(fng) > 1 else None, "week_ago": week.value if week else None,
        }

    symbols = []
    for ticker in await tracked(s):
        funding = (await s.scalars(
            select(FundingRate).where(FundingRate.symbol == ticker).order_by(FundingRate.ts.desc()).limit(21)
        )).all()
        row: dict = {"symbol": ticker, "funding": None}
        if funding:
            avg7 = sum(f.rate for f in funding) / len(funding)  # 最近 21 次 = 7 天
            row["funding"] = {"rate": funding[0].rate * 100, "ts": funding[0].ts, "avg7_annualized": avg7 * 3 * 365 * 100}
        for metric in METRICS:
            pts = (await s.scalars(
                select(MarketMetric).where(MarketMetric.symbol == ticker, MarketMetric.metric == metric)
                .order_by(MarketMetric.ts.desc()).limit(8)
            )).all()
            if not pts:
                row[metric] = None
                continue
            day, week = _at_or_before(pts, pts[0].ts - DAY), _at_or_before(pts, pts[0].ts - 7 * DAY)
            row[metric] = {"value": pts[0].value, "ts": pts[0].ts,
                           "prev": day.value if day else None, "week_ago": week.value if week else None}
        symbols.append(row)

    first, last = (await s.execute(select(func.min(MarketMetric.ts), func.max(MarketMetric.ts)))).one()
    return {
        "fear_greed": fear_greed,
        "symbols": symbols,
        # 系統自己記錄的合約數據累積了幾天（Binance 只保留 30 天，超過的部分只存在這裡）
        "recorded_days": (last - first) // DAY + 1 if first is not None else 0,
    }
