from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import strategies as api
from app.api.deps import get_executor, require_user
from app.db import get_session
from app.engine.performance import attribute
from sqlalchemy import select

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
        s.add_all([sig(1, "strategy", 1, "old name"), sig(2, "strategy", 1), sig(3, "manual"),
                   sig(4, "strategy", 99, "刪掉的策略")])
        s.add_all([trade(0, "open_long", 1, 1), trade(1, "close_long", 1, 2, pnl=4),
                   trade(2, "open_long", 1, 1), trade(3, "open_short", 2, 3, symbol="X/USDT:USDT"),
                   trade(4, "open_long", 1, 4, symbol="Y/USDT:USDT")])
        s.add(Position(symbol="BTC/USDT:USDT", market="futures", side="long", qty=1, entry_price=100,
                       unrealized_pnl=2.5, realized_pnl=0, leverage=1, native_protection=False, opened_at=T0))
        await s.commit()
    d = c.get("/api/strategies/performance").json()
    by_id = {x["config"]["id"]: x for x in d["strategies"]}
    assert by_id[1]["trades"] == 1 and by_id[1]["net"] == pytest.approx(2)  # 用 id 歸屬，改名不影響
    assert by_id[1]["open"][0]["unrealized"] == 2.5
    assert by_id[2]["trades"] == 0 and by_id[2]["open"] == []  # 還沒成交的策略也列出來
    assert [o["label"] for o in d["others"]] == ["手動開倉", "已刪除：刪掉的策略"]  # 刪掉的策略用訊號上記錄的名稱


async def test_enabled_at_set_on_enable_and_on_logic_change(client):
    c, _ = client
    d = c.post("/api/strategies", json=body("a", enabled=False)).json()
    assert d["enabled_at"] is None
    d = c.put("/api/strategies/1", json=body("a")).json()  # 啟用
    first = d["enabled_at"]
    assert first is not None
    assert c.put("/api/strategies/1", json=body("renamed")).json()["enabled_at"] == first  # 只改名：不重算
    changed = {**body("renamed"), "interval": "1h"}
    assert c.put("/api/strategies/1", json=changed).json()["enabled_at"] > first  # 啟用中改了週期：重新開始觀察
    assert c.put("/api/strategies/1", json={**changed, "enabled": False}).json()["enabled_at"] is not None  # 停用後保留


async def test_turning_on_enter_on_enable_restarts_observation(client):
    c, sf = client
    c.post("/api/strategies", json=body("a"))
    async with sf() as s:
        cfg = await s.get(StrategyConfig, 1)
        cfg.last_target, cfg.last_bar_time = "long", 123  # 已經在觀察、記下判斷
        await s.commit()
    d = c.put("/api/strategies/1", json={**body("a"), "enter_on_enable": True}).json()
    assert d["enter_on_enable"] is True
    assert d["last_target"] is None  # 重新開始：下一次判斷就會照目前判斷進場
    d = c.put("/api/strategies/1", json={**body("a"), "enabled": False, "enter_on_enable": True}).json()
    assert d["enter_on_enable"] is True  # 停用時設定保留


async def test_dashboard_win_rate_counts_both_fees(client, env):  # noqa: F811
    from app.api import portfolio

    _, sf = client
    ex = env[0]

    async def session():
        async with sf() as s:
            yield s

    app = FastAPI()
    app.include_router(portfolio.router)
    app.dependency_overrides.update({require_user: lambda: "admin", get_executor: lambda: ex, get_session: session})
    async with sf() as s:
        s.add_all([
            trade(0, "open_long", 1, fee=1.0), trade(1, "close_long", 1, pnl=1.5, fee=1.0),  # 1.5 - 1 - 1 = -0.5：賠
            trade(2, "open_long", 1, fee=0.1), trade(3, "close_long", 1, pnl=5.0, fee=0.1),  # 賺
            trade(4, "open_long", 1, fee=0.1),                                              # 還沒平倉，不算
        ])
        await s.commit()
    d = TestClient(app).get("/api/summary").json()
    assert d["closed_trades"] == 2 and d["win_rate"] == pytest.approx(0.5)  # 舊算法只扣平倉手續費，會算成 2 勝


# ------------------------------------------------------------------ 成交 → 部位
def test_spot_fee_in_coin_still_closes_the_trade():
    """現貨買入手續費從幣扣：買 0.00612、賣 0.00611388，數量對不起來，但部位已關就是一筆完整交易。"""
    pos = Position(id=1, symbol="BTC/USDT", market="spot", side="long", qty=0.00611388, entry_price=81671.81,
                   realized_pnl=-3.0, unrealized_pnl=0, leverage=1, native_protection=False,
                   opened_at=T0, closed_at=T0 + timedelta(hours=15))
    t_open = trade(0, "open_long", 0.00612, 1, fee=0.4998, symbol="BTC/USDT")
    t_close = trade(900, "close_long", 0.00611388, None, pnl=-3.0, fee=0.48, symbol="BTC/USDT")
    t_open.market = t_close.market = "spot"
    t_open.position_id = t_close.position_id = 1
    s = attribute([t_open, t_close], {1: sig(1, "strategy", 6)}, {1: pos})[("strategy", 6)].summary()
    assert s["trades"] == 1 and s["open"] == []
    assert s["net"] == pytest.approx(-3.0 - 0.4998 - 0.48)
    # 舊算法（不看部位）會因為差 0.00000612 而永遠算成持倉中
    legacy = attribute([t_open, t_close], {1: sig(1, "strategy", 6)})[("strategy", 6)].summary()
    assert legacy["trades"] == 0 and len(legacy["open"]) == 1


async def test_executor_records_position_on_every_fill(env):  # noqa: F811
    from sqlalchemy import select

    from .test_executor import run

    ex, sf, ad = env
    await run(ex, ticker="BTCUSDT", market="futures", action="buy", target="long")
    await run(ex, ticker="BTCUSDT", market="futures", action="sell", target="short")  # 反手：平多 + 開空
    async with sf() as s:
        trades = (await s.scalars(select(Trade).order_by(Trade.id))).all()
        positions = {p.id: p for p in (await s.scalars(select(Position))).all()}
    assert [(t.intent, t.position_id) for t in trades] == [("open_long", 1), ("close_long", 1), ("open_short", 2)]
    st = attribute(list(trades), {}, positions)[("other", "")].summary()
    assert st["trades"] == 1 and len(st["open"]) == 1 and st["open"][0]["side"] == "short"


async def test_positions_api_shows_net_and_owner(client, env):  # noqa: F811
    from app.api import portfolio

    _, sf = client

    async def session():
        async with sf() as s:
            yield s

    app = FastAPI()
    app.include_router(portfolio.router)
    app.dependency_overrides.update({require_user: lambda: "admin", get_executor: lambda: env[0], get_session: session})
    async with sf() as s:
        s.add(StrategyConfig(id=6, name="BTC 日線 SMA50", strategy="rules", symbol="BTCUSDT", market="spot",
                             interval="1d", params={}, enabled=True))
        s.add(sig(1, "strategy", 6, "舊名稱"))
        s.add(Position(id=1, symbol="BTC/USDT:USDT", market="futures", side="long", qty=1, entry_price=100,
                       realized_pnl=4.0, unrealized_pnl=0, leverage=1, native_protection=False,
                       opened_at=T0, closed_at=T0 + timedelta(hours=1)))
        t1, t2 = trade(0, "open_long", 1, 1, fee=0.5), trade(60, "close_long", 1, None, pnl=4.0, fee=0.5)
        t1.position_id = t2.position_id = 1
        s.add_all([t1, t2])
        await s.commit()
    (p,) = TestClient(app).get("/api/positions?status=closed").json()
    assert p["owner"] == "BTC 日線 SMA50"  # 用策略 id 找目前的名稱
    assert p["fees"] == pytest.approx(1.0) and p["net"] == pytest.approx(3.0)


async def test_strategy_order_rules_override_settings_page(client, env):  # noqa: F811
    """策略自訂的下單金額、槓桿、停損用在實際下單；沒填的沿用設定頁。改金額不會重新開始觀察。"""
    from app.engine.risk import order_rules
    from app.models import BotSettings
    from .test_executor import run

    c, sf = client
    ex, _, ad = env
    body_ = {**body("custom"), "size_mode": "fixed", "size_value": 300, "leverage": 3, "stop_loss_pct": 4}
    d = c.post("/api/strategies", json=body_).json()
    assert (d["size_value"], d["leverage"], d["stop_loss_pct"], d["take_profit_pct"]) == (300, 3, 4, None)

    async with sf() as s:
        bot = await s.get(BotSettings, 1)
        cfg = await s.get(StrategyConfig, d["id"])
        r = order_rules(bot, cfg)
        assert (r.size_mode, r.size_value, r.leverage, r.stop_loss_pct) == ("fixed", 300, 3, 4)
        assert r.take_profit_pct == bot.take_profit_pct and r.margin_mode == bot.margin_mode  # 沒填的用設定頁
        cfg.last_target = "long"
        await s.commit()

    # 實際下單：策略訊號用策略的金額與槓桿（價格 100、保證金 300 × 3 倍 = 9 顆），停損 4%
    sig = await run(ex, source="strategy", strategy="custom", ticker="BTCUSDT", market="futures", action="long",
                    target="long", raw={"strategy_id": d["id"]})
    assert sig.status == "executed" and ad["futures"].orders[-1][1] == 9
    async with sf() as s:
        pos = (await s.scalars(select(Position).where(Position.closed_at.is_(None)))).one()
        assert pos.leverage == 3 and pos.stop_price == pytest.approx(96)

    # 只改金額：不重新開始觀察（判斷保留）
    d2 = c.put(f"/api/strategies/{d['id']}", json={**body_, "size_value": 500}).json()
    assert d2["size_value"] == 500 and d2["last_target"] == "long"
