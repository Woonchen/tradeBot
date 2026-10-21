"""部位同步 vs 下單：交易所已成交、帳本還沒寫入的空檔，不能被誤判成外部平倉/外部部位。"""

import asyncio
from types import SimpleNamespace

from sqlalchemy import select

from app.exchange.base import ExchangePosition
from app.models import ActivityLog, Trade
from app.workers.sync import SyncWorkers

from .test_executor import FakeAdapter, env  # noqa: F401  (pytest fixture)


class SlowExchange(FakeAdapter):
    """交易所狀態立刻改變，但下單回應延遲 0.3 秒回來（模擬網路延遲）。"""

    async def create_market_order(self, *a, **kw):
        res = await super().create_market_order(*a, **kw)
        await asyncio.sleep(0.3)
        return res

    async def fetch_positions(self):
        if abs(self.base) < 1e-12:
            return []
        side = "long" if self.base > 0 else "short"
        return [ExchangePosition("BTC/USDT:USDT", side, abs(self.base), self.price, self.price, 0, 2, None)]


async def test_sync_waits_for_inflight_order(env):  # noqa: F811
    ex, sf, ad = env
    ad["futures"] = SlowExchange("futures")
    ex.adapters = ad
    hub = SimpleNamespace(fresh_price=lambda m, s: None, on_price=lambda *a: None)
    workers = SyncWorkers(ex, hub)

    async def signal_with_concurrent_sync(target):
        sig, _ = await ex.submit(source="tradingview", ticker="BTCUSDT.P", market="futures", action=target, target=target)
        task = asyncio.create_task(ex.process(sig.id))
        await asyncio.sleep(0.1)  # 單子已送到交易所，回應還沒回來
        await workers.sync_positions()
        await task

    await signal_with_concurrent_sync("long")
    await signal_with_concurrent_sync("flat")

    async with sf() as s:
        trades = (await s.scalars(select(Trade).order_by(Trade.id))).all()
        logs = [x.message for x in (await s.scalars(select(ActivityLog))).all()]
    assert [t.intent for t in trades] == ["open_long", "close_long"]
    assert all(t.order_id is not None for t in trades)  # 沒有被補記的「交易所平倉」
    assert not any("不是機器人開的" in m or "已在交易所被平倉" in m for m in logs)
