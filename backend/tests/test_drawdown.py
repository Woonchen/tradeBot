"""總權益 % 下單、回撤熔斷。"""

from types import SimpleNamespace

from sqlalchemy import select

from app.engine.sizing import compute_order_size
from app.exchange.base import AssetBalance, Wallet
from app.models import ActivityLog, BotSettings, Position, Signal
from app.workers.sync import SyncWorkers

from .test_executor import FakeAdapter, env, run  # noqa: F401  (pytest fixture)


def test_equity_sizing_uses_total_equity():
    assert compute_order_size("equity", 10, 2, 1000, 50, equity_usdt=5000) == (500, 20)
    assert compute_order_size("percent", 10, 2, 1000, 50, equity_usdt=5000) == (100, 4)
    assert compute_order_size("equity", 10, 1, 1000, 50) == (100, 2)  # 沒給權益就用可用餘額


class Committed(FakeAdapter):
    """權益 10,000，但大部分已經是其他部位的保證金，可用只剩 3,000。"""

    async def fetch_wallet(self):
        return Wallet(assets=[AssetBalance("USDT", 3000, 7000, 10_000)], equity_usdt=10_000, free_usdt=3000)


async def test_equity_mode_enters_with_what_is_left(env):  # noqa: F811
    ex, sf, ad = env
    ad["futures"] = Committed("futures")
    ex.adapters = ad
    async with sf() as s:
        bot = await s.get(BotSettings, 1)
        bot.size_mode, bot.size_value, bot.leverage = "equity", 40, 1
        await s.commit()
        await run(ex, ticker="BTCUSDT", market="futures", action="buy", target="long")
        pos = await s.scalar(select(Position))
        assert pos is not None and pos.qty == 29.7  # 想用 4,000，可用只剩 3,000：用 99% → 2,970 ÷ 100

        # percent / fixed 不夠就照舊拒絕
        bot.size_mode, bot.size_value = "fixed", 5000
        await s.commit()
        await run(ex, ticker="BTCUSDT", market="futures", action="sell", target="flat")
        sig = await run(ex, ticker="BTCUSDT", market="futures", action="buy", target="long")
        assert sig.status == "rejected" and "可用餘額不足" in sig.reject_reason


async def test_drawdown_breaker_closes_all_and_disables(env):  # noqa: F811
    from app.api.bot import BotSettingsUpdate, get_bot, update_bot

    ex, sf, ad = env
    workers = SyncWorkers(ex, SimpleNamespace(fresh_price=lambda m, s: None, on_price=lambda *a: None))
    async with sf() as s:
        bot = await s.get(BotSettings, 1)
        bot.max_drawdown_pct = 20
        await s.commit()
    await run(ex, ticker="BTCUSDT", market="futures", action="buy", target="long")
    await workers.snapshot_balances()  # 合約權益 10,000 → 最高點

    ad["futures"].usdt -= 3000  # 合約權益掉到約 7,000，回撤 30%；現貨不變
    await workers.snapshot_balances()
    async with sf() as s:
        assert (await s.get(BotSettings, 1)).enabled  # 只有一次：先不動作
        assert (await get_bot(s))["drawdown"][0]["drawdown_pct"] > 29
    await workers.snapshot_balances()  # 連續第二次：觸發
    while not ex.queue.empty():
        await ex.process(ex.queue.get_nowait())
    async with sf() as s:
        bot = await s.get(BotSettings, 1)
        assert not bot.enabled
        assert await s.scalar(select(Position).where(Position.closed_at.is_(None))) is None
        closes = (await s.scalars(select(Signal).where(Signal.source == "manual"))).all()
        assert [c.status for c in closes] == ["executed"]
        log = await s.scalar(select(ActivityLog).where(ActivityLog.message.like("觸發回撤熔斷%")))
        assert "合約權益" in log.message and "1 個部位" in log.message
        assert bot.equity_peaks["spot"]["value"] == 10_000  # 現貨分開算，沒有回撤

        # 重新啟用：最高點從現在重算，不會一打開又觸發
        await update_bot(BotSettingsUpdate(enabled=True), s, ex)
        assert (await s.get(BotSettings, 1)).equity_peaks == {}
    await workers.snapshot_balances()
    await workers.snapshot_balances()
    async with sf() as s:
        assert (await s.get(BotSettings, 1)).enabled


async def test_breaker_lowers_leverage_once_then_disables(env):  # noqa: F811
    from app.models import StrategyConfig

    ex, sf, ad = env
    workers = SyncWorkers(ex, SimpleNamespace(fresh_price=lambda m, s: None, on_price=lambda *a: None))
    async with sf() as s:
        bot = await s.get(BotSettings, 1)
        bot.max_drawdown_pct, bot.breaker_fallback_leverage, bot.leverage = 20, 2, 5
        s.add(StrategyConfig(name="a", strategy="ma_trend", symbol="BTCUSDT", market="futures", interval="4h",
                             params={}, enabled=True, leverage=5))
        s.add(StrategyConfig(name="b", strategy="ma_trend", symbol="ETHUSDT", market="futures", interval="4h",
                             params={}, enabled=True, leverage=1))  # 本來就比較低：不動
        s.add(StrategyConfig(name="c", strategy="ma_trend", symbol="SOLUSDT", market="futures", interval="4h",
                             params={}, enabled=True))  # 沒自訂：用設定頁的槓桿
        await s.commit()

    async def crash():
        await run(ex, ticker="BTCUSDT", market="futures", action="buy", target="long")
        await workers.snapshot_balances()
        ad["futures"].usdt -= 3000
        await workers.snapshot_balances()
        await workers.snapshot_balances()
        while not ex.queue.empty():
            await ex.process(ex.queue.get_nowait())
        ad["futures"].usdt += 3000  # 讓下一輪從同樣的水位開始

    await crash()  # 第一次：降到 2 倍、平倉、Bot 繼續
    async with sf() as s:
        bot = await s.get(BotSettings, 1)
        levs = {c.name: c.leverage for c in (await s.scalars(select(StrategyConfig))).all()}
        assert bot.enabled and bot.leverage == 2 and levs == {"a": 2, "b": 1, "c": None}
        assert await s.scalar(select(Position).where(Position.closed_at.is_(None))) is None
        log = await s.scalar(select(ActivityLog).where(ActivityLog.message.like("%槓桿已從 5 降為 2 倍%")))
        assert log is not None and "Bot 繼續運作" in log.message

    await crash()  # 第二次：已經是 2 倍 → 照一般熔斷停用
    async with sf() as s:
        bot = await s.get(BotSettings, 1)
        assert not bot.enabled and bot.leverage == 2
        assert await s.scalar(select(ActivityLog).where(ActivityLog.message.like("%Bot 已停用%"))) is not None
