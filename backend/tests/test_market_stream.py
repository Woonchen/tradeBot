"""MarketHub：即時價格觸發停損、K 線轉發、訂閱清單隨持倉/圖表變化。"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import Settings
from app.models import BotSettings, Signal
from app.services.market_stream import MarketHub

from .test_executor import env, run  # noqa: F401  (pytest fixture)

MARKETS = {
    "BTC/USDT": {"id": "BTCUSDT"},
    "BTC/USDT:USDT": {"id": "BTCUSDT"},
}


class FakeClient:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))


class RecordingConn:
    def __init__(self):
        self.streams = set()
        self.connected = True

    async def set_streams(self, wanted):
        self.streams = set(wanted)


@pytest.fixture
async def hub(env):  # noqa: F811
    ex, sf, adapters = env
    for a in adapters.values():
        a.ex = SimpleNamespace(markets=MARKETS)
        a._loaded = True
    h = MarketHub(ex, Settings(execution_mode="paper"))
    h.conns = {"spot": RecordingConn(), "futures": RecordingConn()}
    return h, ex, sf, adapters


async def settle(ex):
    await asyncio.sleep(0.5)  # 等 hub 的背景任務（debounce refresh / trigger）
    while not ex.queue.empty():
        await ex.process(await ex.queue.get())


async def test_stop_loss_triggers_on_tick(hub):
    h, ex, sf, ad = hub
    async with sf() as s:
        cfg = await s.get(BotSettings, 1)
        cfg.stop_loss_pct, cfg.take_profit_pct = 2, 5
        await s.commit()
    await run(ex, ticker="BTCUSDT.P", market="futures", action="buy", target="long")
    await settle(ex)
    assert h.conns["futures"].streams == {"btcusdt@markPrice@1s"}

    h.on_price("futures", "BTC/USDT:USDT", 99.0)  # 停損在 98，還沒到
    await settle(ex)
    ad["futures"].price = 97.5
    h.on_price("futures", "BTC/USDT:USDT", 97.5)
    h.on_price("futures", "BTC/USDT:USDT", 97.4)  # 同一個部位只觸發一次
    await settle(ex)

    async with sf() as s:
        sigs = (await s.scalars(select(Signal).where(Signal.source == "protection"))).all()
    assert [(x.strategy, x.status) for x in sigs] == [("stop", "executed")]
    assert ad["futures"].orders[-1] == ("sell", 2.0, True)
    await settle(ex)
    assert h.conns["futures"].streams == set()  # 平倉後不再訂閱價格
    assert h._dirty  # 有價格要推播給前端


async def test_kline_forwarding(hub):
    h, *_ = hub
    a, b = FakeClient(), FakeClient()
    assert await h.watch_chart(a, "BTCUSDT", "spot", "1m") == "BTC/USDT"
    await h.watch_chart(b, "BTCUSDT", "futures", "15m")
    assert h.conns["spot"].streams == {"btcusdt@kline_1m"}
    assert h.conns["futures"].streams == {"btcusdt@kline_15m"}

    k = {"t": 1_700_000_040_000, "i": "1m", "o": "1", "h": "3", "l": "0.5", "c": "2", "v": "10"}
    await h._on_event("spot", {"e": "kline", "s": "BTCUSDT", "k": k})
    assert len(a.sent) == 1 and b.sent == []
    msg = a.sent[0]
    assert msg["type"] == "kline" and msg["data"]["symbol"] == "BTC/USDT"
    assert msg["data"]["candle"] == {"time": 1_700_000_040, "open": 1, "high": 3, "low": 0.5, "close": 2, "volume": 10}

    await h.unwatch_chart(a)
    assert h.conns["spot"].streams == set()
    with pytest.raises(ValueError):
        await h.watch_chart(a, "BTCUSDT", "spot", "7m")


async def test_demo_mode_charts_use_real_market_stream(env):  # noqa: F811
    """Demo 模式：圖表 K 線走真實市場的連線，持倉價格（停損用）仍走 Demo 的連線。"""
    ex, sf, adapters = env
    for a in adapters.values():
        a.ex = SimpleNamespace(markets=MARKETS)
        a._loaded = True
    h = MarketHub(ex, Settings(execution_mode="binance", testnet=True))
    assert h.separate_chart and h.chart_conns["futures"].url.startswith("wss://fstream.binance.com")
    h.conns = {"spot": RecordingConn(), "futures": RecordingConn()}
    h.chart_conns = {"spot": RecordingConn(), "futures": RecordingConn()}
    await h.watch_chart(FakeClient(), "BTCUSDT", "futures", "15m")
    h._watches[("futures", "BTCUSDT")] = []
    await h._sync_streams("futures")
    assert h.chart_conns["futures"].streams == {"btcusdt@kline_15m"}
    assert h.conns["futures"].streams == {"btcusdt@markPrice@1s"}

    # 圖表連線收到的 K 線照樣轉發；收到價格事件則忽略（價格只認 Demo 的）
    client = FakeClient()
    await h.watch_chart(client, "BTCUSDT", "spot", "1m")
    k = {"t": 1_700_000_040_000, "i": "1m", "o": "1", "h": "3", "l": "0.5", "c": "2", "v": "10"}
    await h._on_chart_event("spot-chart", {"e": "kline", "s": "BTCUSDT", "k": k})
    assert len(client.sent) == 1
    await h._on_chart_event("futures-chart", {"e": "markPriceUpdate", "s": "BTCUSDT", "p": "1"})
    assert h.prices == {}


async def test_market_data_source_by_mode():
    from app.exchange.factory import build_market_data

    own = {"spot": object(), "futures": object()}
    assert build_market_data(Settings(execution_mode="paper"), own) is own  # Paper 本來就是公開資料
    assert build_market_data(Settings(execution_mode="binance", testnet=False), own) is own  # 正式帳戶 = 真實市場
    demo = build_market_data(Settings(execution_mode="binance", testnet=True), own)
    assert demo is not own and demo["futures"].ex.urls["api"]["fapiPublic"].startswith("https://fapi.binance.com")
    for a in demo.values():
        await a.close()
