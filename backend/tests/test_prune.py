"""舊的餘額快照每小時只留一筆，最近 7 天完整保留。"""

from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import func, select

from app.models import BalanceSnapshot, EquitySnapshot, utcnow
from app.workers.sync import SyncWorkers

from .test_executor import env  # noqa: F401  (pytest fixture)


async def test_prune_keeps_recent_and_one_per_hour(env):  # noqa: F811
    ex, sf, _ = env
    now = utcnow().replace(minute=30, second=0, microsecond=0)
    old = now - timedelta(days=10)
    async with sf() as s:
        for base in (old, old + timedelta(hours=1), now - timedelta(days=1)):
            for m in range(0, 60, 5):  # 每 5 分鐘一筆，一小時 12 筆
                ts = base.replace(minute=0) + timedelta(minutes=m)
                s.add(EquitySnapshot(ts=ts, spot_usdt=1, futures_usdt=m, total_usdt=1 + m))
                s.add(BalanceSnapshot(ts=ts, market="spot", asset="USDT", free=1, locked=0, usdt_value=1))
                s.add(BalanceSnapshot(ts=ts, market="futures", asset="USDT", free=m, locked=0, usdt_value=m))
        await s.commit()
    w = SyncWorkers(ex, SimpleNamespace())
    assert await w.prune_snapshots() == 2 * 11 * 3  # 兩個舊的小時各刪 11 次快照（權益 1 筆 + 資產 2 筆）
    async with sf() as s:
        eq = (await s.scalars(select(EquitySnapshot).order_by(EquitySnapshot.ts))).all()
        assert len(eq) == 2 + 12  # 舊的每小時 1 筆，昨天的 12 筆全留
        assert [e.futures_usdt for e in eq[:2]] == [0, 0]  # 留的是該小時最早那筆
        assert await s.scalar(select(func.count()).select_from(BalanceSnapshot)) == (2 + 12) * 2
    assert await w.prune_snapshots() == 0  # 再跑一次不會多刪
