"""自動執行策略：每根 K 棒收盤後計算一次，策略判斷改變時送出訊號（跟 TradingView 警示同一條路）。"""

import asyncio
import logging
import time

from sqlalchemy import select

from ..engine import shadow
from ..engine.executor import Executor, get_open_position
from ..engine.performance import owner_of
from ..engine.risk import load_bot_settings
from ..exchange.base import UnknownSymbol
from ..models import ShadowTrade, Signal, StrategyConfig, Trade, utcnow
from ..strategies import INTERVAL_SECONDS, build_strategy, for_market
from ..strategies.history import fetch_candles
from ..strategies.indicators import atr

log = logging.getLogger(__name__)
SIDE_ZH = {"long": "多", "short": "空"}

CHECK_EVERY = 5  # 秒
CLOSE_DELAY = 3  # K 棒收盤後等幾秒再抓，讓交易所資料就緒
RETRY_AFTER_ERROR = 30
RECONCILE_EVERY = 60  # 秒：多久比對一次策略判斷和實際持倉
MAX_RETRIES = 3


class StrategyRunner:
    def __init__(self, executor: Executor, sentiment=None):
        self.ex = executor
        self.sentiment = sentiment  # 規則用到恐懼貪婪、資金費率時，由它載入資料
        self.bus = executor.bus
        self._next_check: dict[int, float] = {}
        self._next_reconcile: dict[int, float] = {}
        self._retries: dict[tuple, int] = {}  # 同一件事補送了幾次（重啟後歸零）
        self.checks: dict[int, dict] = {}  # 每個策略最後一根 K 棒各條件的狀態（給「策略狀態」顯示）

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
            if now >= self._next_check.get(cfg.id, 0):
                await self.evaluate(cfg.id)
            if now >= self._next_reconcile.get(cfg.id, 0):
                self._next_reconcile[cfg.id] = now + RECONCILE_EVERY
                try:
                    await self.reconcile(cfg.id)
                except Exception:
                    log.exception("strategy %s reconcile failed", cfg.id)

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
                if hasattr(strat, "explain"):
                    self.checks[cfg.id] = strat.explain(candles)
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
            if target == prev:
                await self._maybe_add(cfg, symbol, candles, target)
                return
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

    async def _maybe_add(self, cfg: StrategyConfig, symbol: str, candles, target: str):
        """加碼：判斷沒變、這個策略的部位往有利方向比最後一次成交多走 pyramid_atr 個 ATR，就加一份。"""
        if cfg.pyramid_atr is None or target == "flat":
            return
        async with self.ex.sf() as s:
            pos = await get_open_position(s, symbol, cfg.market)
            if pos is None or pos.side != target or (pos.adds or 0) >= (cfg.pyramid_max or 0):
                return
            if owner_of(await opening_signal(s, pos.id)) != ("strategy", cfg.id):
                return
            ref = pos.last_fill_price or pos.entry_price
            adds = pos.adds or 0
        need = pyramid_due(candles, target, ref, cfg.pyramid_atr)
        if need is None:
            return
        bar = candles[-1]
        await self.bus.emit(
            "strategy", cfg.to_dict(), category="signal",
            message=f"策略「{cfg.name}」收盤 {bar.close:g} 比上次成交 {ref:g} 多走 {need:.1f} 個 ATR，加碼第 {adds + 1} 次",
        )
        await self.ex.submit(
            source="strategy", strategy=cfg.name, ticker=cfg.symbol, market=cfg.market, action="add", target=target,
            price=bar.close,
            raw={"strategy_id": cfg.id, "strategy": cfg.strategy, "interval": cfg.interval, "bar_time": bar.time,
                 "from": target, "to": target, "add": adds + 1},
            dedupe_key=f"strategy:{cfg.id}:{bar.time}:add",
        )

    async def _shadow(self, cfg: StrategyConfig, symbol: str, candles, target: str, prev: str | None, sec: int):
        """影子模式：判斷流程跟實際下單一樣，只是把交易記成虛擬交易（見 engine/shadow.py）。"""
        bar = candles[-1]
        first = prev is None
        # 跟實際下單一樣：啟用後第一次只記下判斷（除非設定了立即進場），之後判斷改變才動作
        act = (cfg.enter_on_enable and target != "flat") if first else target != prev
        done: list[str] = []
        rates = await self.sentiment.funding_series(cfg.symbol) if cfg.market == "futures" and self.sentiment else []
        async with self.ex.sf() as s:
            bot = await load_bot_settings(s)
            t = await s.scalar(select(ShadowTrade).where(
                ShadowTrade.strategy_id == cfg.id, ShadowTrade.exit_time.is_(None),
            ))
            if t and (hit := shadow.protection_exit(t, candles, sec)):
                price, when, reason = hit
                shadow.apply_funding(t, rates, candles, sec, until=when - sec)  # 觸發那根開盤之後就沒有持倉了
                shadow.close(t, price, when, reason)
                done.append(f"{'停損' if reason == 'stop' else '停利'}出場 @ {price:g}，淨損益 {shadow.net(t):+.2f} USDT")
                t = None
            if t:  # 結算到現在（這根收盤 = 下一根開盤，結算先於下單）
                shadow.apply_funding(t, rates, candles, sec, until=bar.time + sec)
            if act:
                if t and t.side != target:
                    shadow.close(t, bar.close, bar.time + sec, "signal")
                    done.append(f"平{SIDE_ZH[t.side]} @ {bar.close:g}，淨損益 {shadow.net(t):+.2f} USDT")
                    t = None
                if target != "flat" and t is None:
                    new = await shadow.open_trade(s, cfg, bot, symbol, target, bar, sec)
                    if new:
                        done.append(f"開{SIDE_ZH[target]} {new.qty:.6g} @ {bar.close:g}")
            elif (t and not first and cfg.pyramid_atr is not None and t.side == target and t.adds < (cfg.pyramid_max or 0)
                  and pyramid_due(candles, target, t.last_fill_price or t.entry_price, cfg.pyramid_atr) is not None):
                added = await shadow.add_to(s, cfg, bot, t, bar)
                if added:
                    done.append(f"加碼第 {t.adds} 次 {added:.6g} @ {bar.close:g}，平均成本 {t.entry_price:g}")
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

    # ------------------------------------------------------------ 判斷和持倉不一致時補救
    async def reconcile(self, config_id: int):
        """策略只在判斷「改變」時送一次訊號；那次沒成功時，在這裡補救：

        - 判斷是空手，帳上卻還有這個策略開的部位（平倉失敗、或當時被擋）：補送平倉。
        - 最近一次訊號因為系統錯誤（failed，例如網路、交易所暫時錯誤）沒開成：重試。
          被風控規則拒絕的（rejected，例如持倉上限、Bot 停用）不重試；停損停利出場後也不會重新進場。
        每件事最多補 MAX_RETRIES 次，之後只記錄錯誤，等人處理。
        """
        async with self.ex.sf() as s:
            cfg = await s.get(StrategyConfig, config_id)
            if not cfg or not cfg.enabled or cfg.mode != "live" or cfg.last_target is None:
                return
            adapter = self.ex.adapters.get(cfg.market)
            if adapter is None:
                return
            symbol = await adapter.resolve_symbol(cfg.symbol)
            last = await last_signal(s, cfg.id)
            if last is not None and last.status == "accepted":  # 還在處理中
                return
            pos = await get_open_position(s, symbol, cfg.market)
            target = cfg.last_target

            if target == "flat":
                if pos is None or owner_of(await opening_signal(s, pos.id)) != ("strategy", cfg.id):
                    return
                key, why = (cfg.id, "close", pos.id), f"判斷是空手，但帳上還有它開的 {pos.symbol} {pos.side} 部位"
                raw = {"strategy_id": cfg.id, "strategy": cfg.strategy, "interval": cfg.interval,
                       "bar_time": cfg.last_bar_time, "from": pos.side, "to": "flat", "reconcile": True}
            else:
                if last is None or last.status != "failed" or last.target != target:
                    return
                if pos is not None and pos.side == target:
                    return
                if pos is None and await holding_on_exchange(adapter, symbol):
                    # 上次可能其實有成交（例如逾時），只是帳本沒記到：不重試，避免開出第二個部位
                    key = (cfg.id, "held", last.id)
                    if key not in self._retries:
                        self._retries[key] = 1
                        await self.bus.emit(
                            "strategy", cfg.to_dict(), level="error", category="system", notify=True,
                            message=f"策略「{cfg.name}」上次開倉沒有成功，但交易所上已經有 {symbol} 的部位，"
                            "可能其實有成交。為避免重複開倉不自動重試，請到交易所確認",
                        )
                    return
                raw = dict(last.raw_payload or {})
                key, why = (cfg.id, "open", raw.get("bar_time"), target), f"上次{SIDE_ZH[target]}單沒有成功（{last.reject_reason}）"

        n = self._retries.get(key, 0) + 1
        self._retries[key] = n
        if n > MAX_RETRIES:
            if n == MAX_RETRIES + 1:
                await self.bus.emit(
                    "strategy", cfg.to_dict(), level="error", category="system", notify=True,
                    message=f"策略「{cfg.name}」{why}，已補送 {MAX_RETRIES} 次仍未成功，不再自動補送，請手動處理",
                )
            return
        await self.bus.emit(
            "strategy", cfg.to_dict(), level="warn", category="signal",
            message=f"策略「{cfg.name}」{why}，補送{'平倉' if target == 'flat' else '開倉'}（第 {n} 次）",
        )
        await self.ex.submit(
            source="strategy", strategy=cfg.name, ticker=cfg.symbol, market=cfg.market, action=target, target=target,
            raw=raw | {"retry": n}, dedupe_key=f"strategy:{cfg.id}:{'/'.join(map(str, key[1:]))}:retry{n}",
        )


async def last_signal(s, strategy_id: int) -> Signal | None:
    """這個策略最近送出的訊號。"""
    recent = (await s.scalars(
        select(Signal).where(Signal.source == "strategy").order_by(Signal.id.desc()).limit(200)
    )).all()
    return next((x for x in recent if (x.raw_payload or {}).get("strategy_id") == strategy_id), None)


async def opening_signal(s, position_id: int) -> Signal | None:
    """開這個部位的那筆成交，是哪個訊號送的。"""
    t = await s.scalar(
        select(Trade).where(Trade.position_id == position_id).order_by(Trade.executed_at, Trade.id).limit(1)
    )
    return await s.get(Signal, t.signal_id) if t and t.signal_id else None


async def holding_on_exchange(adapter, symbol: str) -> bool:
    """交易所上這個交易對是否已有部位（合約看持倉；現貨看有沒有超過最小下單金額的幣）。"""
    remote = await adapter.fetch_positions()
    if remote is not None:
        return any(p.symbol == symbol and p.qty > 0 for p in remote)
    wallet = await adapter.fetch_wallet()
    base = adapter.base_asset(symbol)
    return any(a.asset == base and a.usdt_value >= adapter.min_notional(symbol) for a in wallet.assets)


def pyramid_due(candles, side: str, ref: float, k: float) -> float | None:
    """最後一根收盤比 ref 往有利方向多走了幾個 ATR(14)；達到 k 才回傳，否則 None。"""
    a = atr([c.high for c in candles], [c.low for c in candles], [c.close for c in candles], 14)[-1]
    if not a:
        return None
    moved = (candles[-1].close - ref) * (1 if side == "long" else -1) / a
    return moved if moved >= k else None
