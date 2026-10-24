"""Bot 停用時仍可平倉；策略下單沒成功時的補救（補送平倉、重試開倉、不重複開倉）。"""

import ccxt.async_support as ccxt
from sqlalchemy import select

from app.models import ActivityLog, BotSettings, Order, Position, Signal, StrategyConfig
from app.workers.strategy_runner import MAX_RETRIES, StrategyRunner

from .test_executor import FakeAdapter, env, run  # noqa: F401  (pytest fixture)


async def n_signals(sf):
    async with sf() as s:
        return len((await s.scalars(select(Signal))).all())


async def drain(ex):
    while not ex.queue.empty():
        await ex.process(ex.queue.get_nowait())


async def strategy_signal(ex, target, cid=1):
    return await run(ex, source="strategy", ticker="BTCUSDT", market="futures", action=target, target=target,
                     raw={"strategy_id": cid, "bar_time": 1000})


async def setup(sf, last_target=None):
    async with sf() as s:
        s.add(StrategyConfig(name="t", strategy="ma_trend", symbol="BTCUSDT", market="futures", interval="4h",
                             params={}, enabled=True, last_target=last_target, last_bar_time=1000))
        await s.commit()


async def set_bot(sf, **kw):
    async with sf() as s:
        bot = await s.get(BotSettings, 1)
        for k, v in kw.items():
            setattr(bot, k, v)
        await s.commit()


async def open_position(sf):
    async with sf() as s:
        return await s.scalar(select(Position).where(Position.closed_at.is_(None)))


async def logs(sf, like):
    async with sf() as s:
        return (await s.scalars(select(ActivityLog.message).where(ActivityLog.message.like(like)))).all()


async def test_closing_allowed_when_bot_disabled_or_symbol_not_allowed(env):  # noqa: F811
    ex, sf, ad = env
    assert (await strategy_signal(ex, "long")).status == "executed"
    await set_bot(sf, enabled=False)
    sig = await strategy_signal(ex, "flat")  # Bot 停用：平倉照樣執行
    assert sig.status == "executed" and await open_position(sf) is None
    sig = await strategy_signal(ex, "long")  # 開倉照樣擋
    assert sig.status == "rejected" and sig.reject_reason == "Bot 已停用"

    await set_bot(sf, enabled=True)
    await strategy_signal(ex, "long")
    await set_bot(sf, enabled=False)
    sig = await strategy_signal(ex, "short")  # 反手：只平倉
    assert sig.status == "rejected" and "已平倉" in sig.reject_reason and await open_position(sf) is None

    await set_bot(sf, enabled=True)
    await strategy_signal(ex, "long")
    await set_bot(sf, symbols=["ETHUSDT"])  # 從允許清單拿掉後，還是可以平
    assert (await strategy_signal(ex, "flat")).status == "executed"
    assert (await strategy_signal(ex, "long")).status == "rejected"


async def test_reconcile_closes_leftover_position(env):  # noqa: F811
    ex, sf, ad = env
    await setup(sf, last_target="flat")
    await strategy_signal(ex, "long")  # 策略開的部位
    await set_bot(sf, enabled=False)
    runner = StrategyRunner(ex)
    await runner.reconcile(1)  # 判斷是空手但還有部位 → 補送平倉（Bot 停用也能平）
    await drain(ex)
    assert await open_position(sf) is None
    assert await logs(sf, "%補送平倉（第 1 次）%")
    n = await n_signals(sf)
    await runner.reconcile(1)
    assert await n_signals(sf) == n  # 已經一致：不再動作

    # 別人開的部位（手動）不動
    await set_bot(sf, enabled=True)
    await run(ex, source="manual", ticker="BTCUSDT", market="futures", action="long", target="long")
    n = await n_signals(sf)
    await runner.reconcile(1)
    assert await n_signals(sf) == n and await open_position(sf) is not None


class Flaky(FakeAdapter):
    """前 fails 次送單都連線逾時；filled=True 表示其實有成交（交易所上有部位，回應卻逾時）。"""

    def __init__(self, *a, fails=1, filled=False, **kw):
        super().__init__(*a, **kw)
        self.fails, self.filled = fails, filled

    async def create_market_order(self, symbol, side, qty, cid, reduce_only=False):
        if self.fails > 0:
            self.fails -= 1
            if self.filled:
                await super().create_market_order(symbol, side, qty, cid, reduce_only)
            raise ccxt.RequestTimeout("timed out")
        return await super().create_market_order(symbol, side, qty, cid, reduce_only)


async def test_reconcile_retries_failed_open(env):  # noqa: F811
    ex, sf, ad = env
    ad["futures"] = Flaky("futures", fails=1)
    await setup(sf, last_target="long")
    sig = await strategy_signal(ex, "long")
    assert sig.status == "failed"
    async with sf() as s:
        assert (await s.scalar(select(Order))).status == "unknown"
    runner = StrategyRunner(ex)
    await runner.reconcile(1)
    await drain(ex)
    pos = await open_position(sf)
    assert pos is not None and pos.side == "long"
    async with sf() as s:
        sigs = (await s.scalars(select(Signal).order_by(Signal.id))).all()
    assert [x.status for x in sigs] == ["failed", "executed"] and sigs[1].raw_payload["retry"] == 1
    await runner.reconcile(1)
    assert await n_signals(sf) == 2


async def test_reconcile_gives_up_after_max_retries(env):  # noqa: F811
    ex, sf, ad = env
    ad["futures"] = Flaky("futures", fails=99)
    await setup(sf, last_target="long")
    await strategy_signal(ex, "long")
    runner = StrategyRunner(ex)
    for _ in range(MAX_RETRIES + 3):
        await runner.reconcile(1)
        await drain(ex)
    async with sf() as s:
        assert len((await s.scalars(select(Signal))).all()) == 1 + MAX_RETRIES
    assert len(await logs(sf, "%不再自動補送%")) == 1


async def test_reconcile_does_not_double_open(env):  # noqa: F811
    ex, sf, ad = env
    ad["futures"] = Flaky("futures", fails=1, filled=True)  # 其實成交了，只是回應逾時

    async def positions():
        from app.exchange.base import ExchangePosition
        f = ad["futures"]
        return [ExchangePosition("BTC/USDT:USDT", "long", f.base, 100, 100, 0, 2, None)] if f.base > 0 else []

    ad["futures"].fetch_positions = positions
    await setup(sf, last_target="long")
    assert (await strategy_signal(ex, "long")).status == "failed"
    runner = StrategyRunner(ex)
    await runner.reconcile(1)
    await runner.reconcile(1)
    assert await n_signals(sf) == 1 and len(ad["futures"].orders) == 1  # 沒有再送單
    assert len(await logs(sf, "%可能其實有成交%")) == 1


async def test_rejected_open_is_not_retried(env):  # noqa: F811
    ex, sf, ad = env
    await setup(sf, last_target="long")
    await set_bot(sf, max_open_positions=1)
    await run(ex, source="manual", ticker="BTCUSDT", market="spot", action="long", target="long")
    assert (await strategy_signal(ex, "long")).status == "rejected"  # 持倉上限：風控拒絕
    await StrategyRunner(ex).reconcile(1)
    assert await n_signals(sf) == 2
