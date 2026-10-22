from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import strategies as api
from app.api.deps import get_executor, require_user
from app.db import get_session
from app.engine.performance import attribute
from app.models import Position, Signal, StrategyConfig, Trade

from .test_executor import env  # noqa: F401  (pytest fixture)

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def sig(id, source, strategy_id=None, name=""):
    raw = {"strategy_id": strategy_id} if strategy_id is not None else {}
    return Signal(id=id, source=source, strategy=name, ticker="BTCUSDT", market="futures", action="x", target="x",
                  dedupe_key=str(id), raw_payload=raw)


def trade(n, intent, qty, signal_id=None, pnl=0.0, fee=1.0, symbol="BTC/USDT:USDT"):
    return Trade(symbol=symbol, market="futures", side="buy", intent=intent, price=100, qty=qty, fee_usdt=fee,
                 realized_pnl=pnl, signal_id=signal_id, executed_at=T0 + timedelta(minutes=n))


def test_position_belongs_to_whoever_opened_it():
    signals = {1: sig(1, "strategy", 7), 2: sig(2, "protection"), 3: sig(3, "strategy", 7),
               4: sig(4, "strategy", 7), 5: sig(5, "tradingview", name="my alert"), 6: sig(6, "strategy", 9)}
    trades = [
        trade(0, "open_long", 1, 1),              # 策略 7 開多
        trade(1, "close_long", 1, 2, pnl=10),     # 停損停利平倉 → 仍算策略 7
        trade(2, "open_long", 2, 3),              # 策略 7 再開多
        trade(3, "close_long", 1, None, pnl=-3),  # 交易所上平掉一半（沒有訊號）
        trade(4, "close_long", 1, 4, pnl=-2),     # 策略 7 平掉剩下的 → 一筆完整交易
        trade(5, "open_short", 1, 5, symbol="ETH/USDT:USDT"),  # TradingView 開空，還沒平
        trade(6, "open_long", 1, 6, symbol="SOL/USDT:USDT"),   # 策略 9 開多，還沒平
    ]
    out = attribute(trades, signals)
    s7 = out[("strategy", 7)].summary()
    assert s7["trades"] == 2 and s7["wins"] == 1
    assert s7["net"] == pytest.approx((10 - 2) + (-5 - 3))  # 每筆含開平倉手續費
    assert s7["open"] == []
    assert out[("tradingview", "my alert")].summary()["open"][0]["side"] == "short"
    assert out[("strategy", 9)].summary()["open"][0]["symbol"] == "SOL/USDT:USDT"


def test_reversal_splits_into_two_trades():
    signals = {1: sig(1, "strategy", 1), 2: sig(2, "strategy", 1)}
    trades = [trade(0, "open_long", 1, 1), trade(1, "close_long", 1, 2, pnl=5), trade(1, "open_short", 1, 2),
              trade(2, "close_short", 1, None, pnl=-1)]
    s = attribute(trades, signals)[("strategy", 1)].summary()
    assert s["trades"] == 2 and s["profit_factor"] == pytest.approx((5 - 2) / (1 + 2))


@pytest.fixture
async def client(env):  # noqa: F811
    ex, sf, ad = env

    async def session():
        async with sf() as s:
            yield s

    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides.update({require_user: lambda: "admin", get_executor: lambda: ex, get_session: session})
    app.state.strategy_runner = type("R", (), {"reset": lambda self, i: None})()
    return TestClient(app), sf


def body(name, symbol="BTCUSDT", market="futures", enabled=True):
    return {"name": name, "strategy": "ema_cross", "symbol": symbol, "market": market, "interval": "15m",
            "params": {}, "enabled": enabled}


async def test_only_one_enabled_strategy_per_symbol(client):
    c, _ = client
    assert c.post("/api/strategies", json=body("a")).status_code == 200
    r = c.post("/api/strategies", json=body("b", symbol="btcusdt.p"))  # 同一個交易對的不同寫法也算
    assert r.status_code == 409 and "「a」" in r.json()["detail"]
    assert c.post("/api/strategies", json=body("b", enabled=False)).status_code == 200  # 停用狀態可以存
    assert c.put("/api/strategies/2", json=body("b")).status_code == 409  # 啟用就擋
    assert c.post("/api/strategies", json=body("c", market="spot")).status_code == 200  # 現貨是另一個部位
    assert c.put("/api/strategies/1", json=body("a2")).status_code == 200  # 自己改名不會跟自己衝突
    assert c.put("/api/strategies/1", json=body("a2", enabled=False)).status_code == 200
    assert c.put("/api/strategies/2", json=body("b")).status_code == 200  # 原本的停用後就可以


async def test_performance_endpoint(client):
    c, sf = client
    async with sf() as s:
        s.add(StrategyConfig(id=1, name="renamed", strategy="ema_cross", symbol="BTCUSDT", market="futures",
                             interval="15m", params={}, enabled=True))
        s.add(StrategyConfig(id=2, name="idle", strategy="ema_cross", symbol="ETHUSDT", market="futures",
                             interval="15m", params={}, enabled=True))
        s.add_all([sig(1, "strategy", 1, "old name"), sig(2, "strategy", 1), sig(3, "manual")])
        s.add_all([trade(0, "open_long", 1, 1), trade(1, "close_long", 1, 2, pnl=4),
                   trade(2, "open_long", 1, 1), trade(3, "open_short", 2, 3, symbol="X/USDT:USDT")])
        s.add(Position(symbol="BTC/USDT:USDT", market="futures", side="long", qty=1, entry_price=100,
                       unrealized_pnl=2.5, realized_pnl=0, leverage=1, native_protection=False, opened_at=T0))
        await s.commit()
    d = c.get("/api/strategies/performance").json()
    by_id = {x["config"]["id"]: x for x in d["strategies"]}
    assert by_id[1]["trades"] == 1 and by_id[1]["net"] == pytest.approx(2)  # 用 id 歸屬，改名不影響
    assert by_id[1]["open"][0]["unrealized"] == 2.5
    assert by_id[2]["trades"] == 0 and by_id[2]["open"] == []  # 還沒成交的策略也列出來
    assert [o["label"] for o in d["others"]] == ["手動開倉"]
