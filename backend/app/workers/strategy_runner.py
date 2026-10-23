"""自動執行策略：每根 K 棒收盤後計算一次，策略判斷改變時送出訊號（跟 TradingView 警示同一條路）。"""

import asyncio
import logging
import time

from sqlalchemy import select

from ..engine import shadow
from ..engine.executor import Executor, get_open_position
from ..engine.risk import load_bot_settings
from ..exchange.base import UnknownSymbol
from ..models import ShadowTrade, StrategyConfig, utcnow
from ..strategies import INTERVAL_SECONDS, build_strategy, for_market
from ..strategies.history import fetch_candles

log = logging.getLogger(__name__)
SIDE_ZH = {"long": "多", "short": "空"}

CHECK_EVERY = 5  # 秒
CLOSE_DELAY = 3  # K 棒收盤後等幾秒再抓，讓交易所資料就緒
RETRY_AFTER_ERROR = 30


class StrategyRunner:
    def __init__(self, executor: Executor, sentiment=None):
        self.ex = executor
        self.sentiment = sentiment  # 規則用到恐懼貪婪、資金費率時，由它載入資料
        self.bus = executor.bus
        self._next_check: dict[int, float] = {}

    def reset(self, config_id: int):
        """設定改變後立刻重新評估。"""
        self._next_check.pop(config_id, None)

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("strategy runner tick failed")
            await asyncio.sleep(CHECK_EVERY)

    async def tick(self):
        async with self.ex.sf() as s:
            configs = (await s.scalars(select(StrategyConfig).where(StrategyConfig.enabled.is_(True)))).all()
        now = time.time()
        for cfg in configs:
            if now < self._next_check.get(cfg.id, 0):
                continue
            await self.evaluate(cfg.id)

    async def evaluate(self, config_id: int):
        async with self.ex.sf() as s:
            cfg = await s.get(StrategyConfig, config_id)
            if not cfg or not cfg.enabled:
                return
            sec = INTERVAL_SECONDS[cfg.interval]
            try:
                strat = build_strategy(cfg.strategy, cfg.params)
                adapter = self.ex.market_data[cfg.market]  # 判斷用真實市場行情；下單仍走帳戶（executor）
                symbol = await adapter.resolve_symbol(cfg.symbol)
                candles = await fetch_candles(adapter, symbol, cfg.interval, strat.warmup)
                if self.sentiment:
                    await self.sentiment.attach(strat, cfg.symbol)
                if len(candles) < 2:
                    raise RuntimeError("K 線資料不足")
                bar = candles[-1]
                cfg.last_run_at, cfg.last_error = utcnow(), None
                # 下一次檢查：下一根 K 棒收盤後
                self._next_check[cfg.id] = bar.time + 2 * sec + CLOSE_DELAY
                if bar.time == cfg.last_bar_time:
                    await s.commit()
                    return

                target = for_market(strat.targets(candles)[-1], cfg.market)
                prev = cfg.last_target
                cfg.last_target, cfg.last_bar_time = target, bar.time
                await s.commit()
            except (ValueError, UnknownSymbol, RuntimeError) as e:
                cfg.last_error = str(e) or type(e).__name__
                await s.commit()
                self._next_check[config_id] = time.time() + RETRY_AFTER_ERROR
                await self.bus.emit(
                    "strategy", cfg.to_dict(), level="error", category="system",
                    message=f"策略「{cfg.name}」執行失敗：{cfg.last_error}",
                )
                return
            except Exception as e:  # 網路/交易所暫時錯誤：記錄後稍後重試
                log.warning("strategy %s evaluate failed: %r", config_id, e)
                cfg.last_error = repr(e)
                await s.commit()
                self._next_check[config_id] = time.time() + RETRY_AFTER_ERROR
                return

        if cfg.mode == "shadow":
            await self._shadow(cfg, symbol, candles, target, prev, sec)
            return

        if prev is None:
            # 啟用後第一次判斷：預設只記下來；設定了 enter_on_enable 就照目前判斷進場
            # （判斷是空手時不動作，不去平掉不是這個策略開的部位）
            enter = cfg.enter_on_enable and target != "flat"
            if enter:
                async with self.ex.sf() as s:
                    pos = await get_open_position(s, symbol, cfg.market)
                enter = pos is None or pos.side != target
            await self.bus.emit(
                "strategy", cfg.to_dict(), category="signal",
                message=f"策略「{cfg.name}」開始運作，目前判斷：{target}。"
                + ("依設定立即進場" if enter else "等判斷改變時才會下單"),
            )
            if not enter:
                return
        else:
            await self.bus.emit("strategy", cfg.to_dict())
        if target != prev:
            if target == "flat":
                # 策略啟用時不會跟著進場，之後判斷轉回空手時帳上可能根本沒有部位，送出去也只會被拒絕
                async with self.ex.sf() as s:
                    pos = await get_open_position(s, symbol, cfg.market)
                if pos is None:
                    await self.bus.emit(
                        "strategy", cfg.to_dict(), category="signal",
                        message=f"策略「{cfg.name}」判斷轉為空手（原本 {prev}），目前沒有部位，略過不送訊號",
                    )
                    return
            await self.ex.submit(
                source="strategy", strategy=cfg.name, ticker=cfg.symbol, market=cfg.market,
                action=target, target=target, price=bar.close,
                raw={"strategy_id": cfg.id, "strategy": cfg.strategy, "interval": cfg.interval,
                     "bar_time": bar.time, "from": prev, "to": target, "params": cfg.params},
                dedupe_key=f"strategy:{cfg.id}:{bar.time}:{target}",
            )

    async def _shadow(self, cfg: StrategyConfig, symbol: str, candles, target: str, prev: str | None, sec: int):
        """影子模式：判斷流程跟實際下單一樣，只是把交易記成虛擬交易（見 engine/shadow.py）。"""
        bar = candles[-1]
        first = prev is None
        # 跟實際下單一樣：啟用後第一次只記下判斷（除非設定了立即進場），之後判斷改變才動作
        act = (cfg.enter_on_enable and target != "flat") if first else target != prev
        done: list[str] = []
        async with self.ex.sf() as s:
            bot = await load_bot_settings(s)
            t = await s.scalar(select(ShadowTrade).where(
                ShadowTrade.strategy_id == cfg.id, ShadowTrade.exit_time.is_(None),
            ))
            if t and (hit := shadow.protection_exit(t, candles, sec)):
                price, when, reason = hit
                shadow.close(t, price, when, reason)
                done.append(f"{'停損' if reason == 'stop' else '停利'}出場 @ {price:g}，淨損益 {shadow.net(t):+.2f} USDT")
                t = None
            if act:
                if t and t.side != target:
                    shadow.close(t, bar.close, bar.time + sec, "signal")
                    done.append(f"平{SIDE_ZH[t.side]} @ {bar.close:g}，淨損益 {shadow.net(t):+.2f} USDT")
                    t = None
                if target != "flat" and t is None:
                    new = await shadow.open_trade(s, cfg, bot, symbol, target, bar, sec)
                    if new:
                        done.append(f"開{SIDE_ZH[target]} {new.qty:.6g} @ {bar.close:g}")
            await s.commit()

        head = f"影子策略「{cfg.name}」"
        if first:
            await self.bus.emit(
                "strategy", cfg.to_dict(), category="signal",
                message=f"{head}開始運作（只記錄、不下單），目前判斷：{target}。"
                + ("依設定立即記錄進場" if act else "等判斷改變時才記錄進場"),
            )
        else:
            await self.bus.emit("strategy", cfg.to_dict())
        for m in done:
            await self.bus.emit("strategy", cfg.to_dict(), category="signal", message=f"{head}（虛擬）{m}")
