import pytest

from app.api.portfolio import max_drawdown
from app.api.webhook import resolve_target
from app.engine.ledger import LedgerState, apply_fill
from app.engine.sizing import compute_order_size, protection_hit, protection_prices
from app.exchange.base import normalize_ticker


@pytest.mark.parametrize(
    "raw,expected",
    [("BTCUSDT", "BTCUSDT"), ("BTCUSDT.P", "BTCUSDT"), ("BINANCE:ETHUSDT.P", "ETHUSDT"), ("sol-usdt", "SOLUSDT")],
)
def test_normalize_ticker(raw, expected):
    assert normalize_ticker(raw) == expected


@pytest.mark.parametrize(
    "action,position,target",
    [
        ("buy", "long", "long"),
        ("sell", "flat", "flat"),
        ("sell", "short", "short"),
        ("buy", "flat", "flat"),  # 空單回補
        ("buy", None, "long"),
        ("sell", None, "flat"),
        ("SHORT", None, "short"),
        ("close", "long", "flat"),
    ],
)
def test_resolve_target(action, position, target):
    assert resolve_target(action, position) == target


def test_resolve_target_unknown():
    with pytest.raises(ValueError):
        resolve_target("hodl", None)


def test_ledger_open_add_close():
    s, r = apply_fill(LedgerState(None), "buy", 1, 100)
    assert (s.side, s.qty, s.entry, r) == ("long", 1, 100, 0)
    s, r = apply_fill(s, "buy", 1, 200)
    assert (s.qty, s.entry) == (2, 150)
    s, r = apply_fill(s, "sell", 0.5, 170)
    assert (s.side, s.qty, s.entry) == ("long", 1.5, 150) and r == pytest.approx(10)
    s, r = apply_fill(s, "sell", 1.5, 140)
    assert s.side is None and r == pytest.approx(-15)


def test_ledger_short_and_flip():
    s, _ = apply_fill(LedgerState(None), "sell", 2, 100)
    assert s.side == "short"
    s, r = apply_fill(s, "buy", 3, 90)
    assert r == pytest.approx(20)
    assert (s.side, s.qty, s.entry) == ("long", 1, 90)


def test_sizing():
    assert compute_order_size("fixed", 100, 1, 1000, 50) == (100, 2)
    assert compute_order_size("fixed", 100, 5, 1000, 50) == (100, 10)
    assert compute_order_size("percent", 10, 2, 1000, 50) == (100, 4)


def test_protection():
    stop, tp = protection_prices("long", 100, 2, 5)
    assert stop == pytest.approx(98) and tp == pytest.approx(105)
    stop, tp = protection_prices("short", 100, 2, 0)
    assert stop == pytest.approx(102) and tp is None
    assert protection_hit("long", 97, 98, 105) == "stop"
    assert protection_hit("long", 106, 98, 105) == "take_profit"
    assert protection_hit("short", 103, 102, None) == "stop"
    assert protection_hit("long", 100, 98, 105) is None


def test_max_drawdown():
    assert max_drawdown([100, 120, 90, 130, 117]) == pytest.approx(0.25)
    assert max_drawdown([]) == 0
