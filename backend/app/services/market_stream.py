"""Binance 公開 WebSocket 行情（免費、不需 API key）。

- 每個市場（spot / futures）一條連線，依需要動態 SUBSCRIBE / UNSUBSCRIBE，斷線自動重連
- K 線：轉發給正在看該圖表的前端
- 價格：持倉的即時價格（合約用標記價格），每個 tick 檢查軟體停損/停利，並每秒推播給前端
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from fastapi import WebSocket
from sqlalchemy import select
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from ..config import Settings
from ..engine.executor import Executor
from ..engine.sizing import protection_hit
from ..models import Position

log = logging.getLogger(__name__)

WS_URLS = {
    # 合約的 K 線 / 標記價格已搬到 /market/ws，舊的 /ws 只回 ack 不推資料
    "live": {"spot": "wss://stream.binance.com:9443/ws", "futures": "wss://fstream.binance.com/market/ws"},
    "demo": {"spot": "wss://demo-stream.binance.com/ws", "futures": "wss://fstream.binancefuture.com/ws"},
}
KLINE_INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"}
PROTECTION_RETRY_SECONDS = 60


class StreamConnection:
    """一條 Binance WebSocket 連線，維護目前要訂閱的 stream 集合。"""

    def __init__(self, name: str, url: str, on_event):
        self.name = name
        self.url = url
        self.on_event = on_event
        self.streams: set[str] = set()
        self.connected = False
        self._ws: ClientConnection | None = None
        self._lock = asyncio.Lock()
        self._msg_id = 0

    async def _send(self, method: str, params: list[str]):
        if not self._ws or not params:
            return
        # Binance 限制每條連線每秒傳入的訊息數，分批送
        for i in range(0, len(params), 100):
            self._msg_id += 1
            await self._ws.send(json.dumps({"method": method, "params": params[i:i + 100], "id": self._msg_id}))
            await asyncio.sleep(0.25)

    async def set_streams(self, wanted: set[str]):
        async with self._lock:
            add, remove = wanted - self.streams, self.streams - wanted
            self.streams = set(wanted)
            try:
                await self._send("UNSUBSCRIBE", sorted(remove))
                await self._send("SUBSCRIBE", sorted(add))
            except ConnectionClosed:
                pass  # 重連時會重新訂閱 self.streams

    async def run(self):
        backoff = 1
        while True:
            try:
                async with connect(self.url, open_timeout=10, ping_interval=20, max_queue=4096) as ws:
                    async with self._lock:
                        self._ws = ws
                        await self._send("SUBSCRIBE", sorted(self.streams))
                    self.connected = True
                    backoff = 1
                    log.info("market stream %s connected (%d streams)", self.name, len(self.streams))
                    async for raw in ws:
                        msg = json.loads(raw)
                        if "e" in msg:
                            try:
                                await self.on_event(self.name, msg)
                            except Exception:
                                log.exception("market stream handler failed")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 包含 Binance 每 24 小時主動斷線
                log.warning("market stream %s disconnected: %r", self.name, e)
            finally:
                self._ws = None
                self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


@dataclass
class PositionWatch:
    id: int
    symbol: str
    side: str
    stop: float | None
    tp: float | None
    native: bool


class MarketHub:
    def __init__(self, executor: Executor, cfg: Settings):
        self.ex = executor
        self.bus = executor.bus
        env = "demo" if cfg.execution_mode == "binance" and cfg.testnet else "live"
        # 持倉的即時價格（軟體停損）跟帳戶走：Demo 帳戶看 Demo 的價格
        self.conns = {m: StreamConnection(m, url, self._on_event) for m, url in WS_URLS[env].items()}
        # 圖表的 K 線一律用真實市場（Demo 的 K 線有假影線）：Demo 模式另開一組連線，其他模式共用上面那組
        self.separate_chart = env == "demo"
        self.chart_conns = (
            {m: StreamConnection(f"{m}-chart", url, self._on_chart_event) for m, url in WS_URLS["live"].items()}
            if self.separate_chart else {}
        )
        # (market, ccxt symbol) -> (價格, 收到的時間)
        self.prices: dict[tuple[str, str], tuple[float, float]] = {}
        # 前端 client -> (market, stream, ccxt symbol, interval)
        self.charts: dict[WebSocket, tuple[str, str, str, str]] = {}
        # (market, Binance id 例如 BTCUSDT) -> ccxt symbol
        self._ids: dict[tuple[str, str], str] = {}
        self._watches: dict[tuple[str, str], list[PositionWatch]] = {}
        self._triggered: dict[int, float] = {}  # position id -> 觸發時間
        self._dirty: dict[str, float] = {}
        self._refresh_pending = False
        self._tasks: set[asyncio.Task] = set()
        self.bus.listeners.append(self._on_bus_event)

    def start(self) -> list[asyncio.Task]:
        tasks = [asyncio.create_task(c.run()) for c in [*self.conns.values(), *self.chart_conns.values()]]
        tasks.append(asyncio.create_task(self._broadcast_prices()))
        tasks.append(asyncio.create_task(self.refresh_positions()))
        return tasks

    # ------------------------------------------------------------ 共用
    async def _market_id(self, market: str, symbol: str) -> str:
        adapter = self.ex.market_data[market]  # Binance 的 id 在 Demo 與真實市場相同
        await adapter.ensure_loaded()
        mid = adapter.ex.markets[symbol]["id"]
        self._ids[(market, mid)] = symbol
        return mid

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _sync_streams(self, market: str):
        charts = {stream for (m, stream, _, _) in self.charts.values() if m == market}
        prices = {
            f"{mid.lower()}@markPrice@1s" if market == "futures" else f"{mid.lower()}@miniTicker"
            for m, mid in self._watches if m == market
        }
        if self.separate_chart:
            await self.conns[market].set_streams(prices)
            await self.chart_conns[market].set_streams(charts)
        else:
            await self.conns[market].set_streams(charts | prices)

    def fresh_price(self, market: str, symbol: str, max_age: float = 5.0) -> float | None:
        hit = self.prices.get((market, symbol))
        return hit[0] if hit and time.monotonic() - hit[1] <= max_age else None

    @property
    def status(self) -> dict[str, bool]:
        return {m: c.connected and (not self.separate_chart or self.chart_conns[m].connected) for m, c in self.conns.items()}

    # ------------------------------------------------------------ K 線（前端圖表）
    async def watch_chart(self, client: WebSocket, symbol: str, market: str, interval: str) -> str:
        if market not in self.conns or interval not in KLINE_INTERVALS:
            raise ValueError("market 或 interval 不正確")
        resolved = await self.ex.market_data[market].resolve_symbol(symbol)
        mid = await self._market_id(market, resolved)
        old = self.charts.get(client)
        self.charts[client] = (market, f"{mid.lower()}@kline_{interval}", resolved, interval)
        await self._sync_streams(market)
        if old and old[0] != market:
            await self._sync_streams(old[0])
        return resolved

    async def unwatch_chart(self, client: WebSocket):
        old = self.charts.pop(client, None)
        if old:
            await self._sync_streams(old[0])

    # ------------------------------------------------------------ 持倉價格
    async def refresh_positions(self):
        """依目前持倉重新決定要訂閱哪些價格 stream。"""
        async with self.ex.sf() as s:
            rows = (await s.scalars(select(Position).where(Position.closed_at.is_(None)))).all()
        watches: dict[tuple[str, str], list[PositionWatch]] = {}
        for p in rows:
            mid = await self._market_id(p.market, p.symbol)
            watches.setdefault((p.market, mid), []).append(
                PositionWatch(p.id, p.symbol, p.side, p.stop_price, p.take_profit_price, p.native_protection)
            )
        self._watches = watches
        open_ids = {p.id for p in rows}
        self._triggered = {k: v for k, v in self._triggered.items() if k in open_ids}
        for market in self.conns:
            await self._sync_streams(market)

    async def _on_bus_event(self, type: str, data):
        # 成交或部位變動後，重新整理要監控的持倉（合併短時間內的多次事件）
        if type not in ("trade", "position_update") or self._refresh_pending:
            return
        self._refresh_pending = True

        async def later():
            await asyncio.sleep(0.3)
            self._refresh_pending = False
            await self.refresh_positions()

        self._spawn(later())

    def on_price(self, market: str, symbol: str, price: float):
        """收到新價格（WebSocket 或 REST 同步）：更新快取並檢查停損停利。"""
        self.prices[(market, symbol)] = (price, time.monotonic())
        mid = next((m for (mk, m), s in self._ids.items() if mk == market and s == symbol), None)
        watches = self._watches.get((market, mid), []) if mid else []
        if watches:
            self._dirty[f"{market}:{symbol}"] = price
        now = time.monotonic()
        for w in watches:
            if w.native or now - self._triggered.get(w.id, -1e9) < PROTECTION_RETRY_SECONDS:
                continue
            hit = protection_hit(w.side, price, w.stop, w.tp)
            if hit:
                self._triggered[w.id] = now
                self._spawn(self._trigger(w, market, price, hit))

    async def _trigger(self, w: PositionWatch, market: str, price: float, hit: str):
        await self.bus.emit(
            "log", level="warn", category="risk",
            message=f"{w.symbol} 即時價格 {price} 觸發{'停損' if hit == 'stop' else '停利'}，送出平倉",
        )
        await self.ex.submit(
            source="protection", ticker=w.symbol, market=market, action="close", target="flat",
            strategy=hit, raw={"position_id": w.id, "price": price},
        )

    async def _broadcast_prices(self):
        while True:
            await asyncio.sleep(1)
            if self._dirty:
                data, self._dirty = self._dirty, {}
                await self.bus.broadcast({"type": "prices", "data": data, "log": None})

    # ------------------------------------------------------------ Binance 事件
    async def _on_chart_event(self, name: str, msg: dict):
        """圖表專用連線（Demo 模式）：只轉發 K 線。"""
        if msg["e"] == "kline":
            await self._on_event(name.removesuffix("-chart"), msg)

    async def _on_event(self, market: str, msg: dict):
        e = msg["e"]
        if e == "kline":
            k = msg["k"]
            stream = f"{msg['s'].lower()}@kline_{k['i']}"
            targets = [c for c, sub in self.charts.items() if sub[0] == market and sub[1] == stream]
            if not targets:
                return
            symbol = self._ids.get((market, msg["s"]), msg["s"])
            payload = json.dumps({
                "type": "kline",
                "data": {
                    "market": market, "symbol": symbol, "interval": k["i"],
                    "candle": {
                        "time": k["t"] // 1000, "open": float(k["o"]), "high": float(k["h"]),
                        "low": float(k["l"]), "close": float(k["c"]), "volume": float(k["v"]),
                    },
                },
                "log": None,
            })
            for c in targets:
                try:
                    await c.send_text(payload)
                except Exception:
                    self.charts.pop(c, None)
        elif e in ("markPriceUpdate", "24hrMiniTicker"):
            symbol = self._ids.get((market, msg["s"]))
            if symbol:
                self.on_price(market, symbol, float(msg["p"] if e == "markPriceUpdate" else msg["c"]))
