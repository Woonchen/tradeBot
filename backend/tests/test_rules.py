import math

import pytest

from app.strategies import Candle, build_strategy
from app.strategies.indicators import ema
from app.strategies.rules import compute

V = lambda v: {"kind": "value", "value": v}  # noqa: E731
CLOSE = {"kind": "price", "field": "close"}


def cs(closes, highs=None, lows=None):
    highs = highs or closes
    lows = lows or closes
    return [Candle(i * 3600, c, h, l, c, 1.0) for i, (c, h, l) in enumerate(zip(closes, highs, lows))]


def rules(long_entry=(), long_exit=(), short_entry=(), short_exit=(), sl=0, tp=0, logic="and"):
    g = lambda conds: {"logic": logic, "conditions": list(conds)}  # noqa: E731
    return {"rules": {"long_entry": g(long_entry), "long_exit": g(long_exit), "short_entry": g(short_entry),
                      "short_exit": g(short_exit), "stop_loss_pct": sl, "take_profit_pct": tp}}


def cond(left, cmp, right):
    return {"left": left, "cmp": cmp, "right": right}


def test_compute_channel_excludes_current_bar():
    c = cs([1, 5, 2, 3, 9], highs=[1, 5, 2, 3, 9], lows=[1, 5, 2, 3, 9])
    assert compute({"kind": "highest", "length": 2}, c, {}) == [None, None, 5, 5, 3]
    assert compute({"kind": "lowest", "length": 2}, c, {}) == [None, None, 1, 2, 2]


def test_compute_bollinger_and_macd():
    closes = [float(x) for x in [2, 4, 4, 4, 5, 5, 7, 9]]
    c = cs(closes)
    mid = compute({"kind": "bb_middle", "length": 8, "mult": 2}, c, {})
    up = compute({"kind": "bb_upper", "length": 8, "mult": 2}, c, {})
    assert mid[-1] == pytest.approx(5) and up[-1] == pytest.approx(5 + 2 * 2)  # 母體標準差 = 2
    closes = [100 + math.sin(i / 5) * 10 for i in range(80)]
    c = cs(closes)
    line = compute({"kind": "macd", "fast": 12, "slow": 26, "signal": 9}, c, {})
    f, s = ema(closes, 12), ema(closes, 26)
    assert line[40] == pytest.approx(f[40] - s[40])
    hist = compute({"kind": "macd_hist", "fast": 12, "slow": 26, "signal": 9}, c, {})
    sig = compute({"kind": "macd_signal", "fast": 12, "slow": 26, "signal": 9}, c, {})
    assert line[24] is None and line[25] is not None  # 慢線 26 → 第 25 根開始有值
    assert sig[32] is None and sig[33] is not None  # 訊號線再晚 9 - 1 根 and hist[50] == pytest.approx(line[50] - sig[50])


def test_and_or_entry_exit():
    c = cs([10, 20, 30, 40, 50, 40, 30])
    s = build_strategy("rules", rules(long_entry=[cond(CLOSE, ">=", V(30))], long_exit=[cond(CLOSE, "<", V(35))]))
    assert s.targets(c) == ["flat", "flat", "long", "long", "long", "long", "flat"]
    both = rules(long_entry=[cond(CLOSE, ">", V(25)), cond(CLOSE, "<", V(45))])
    assert build_strategy("rules", both).targets(c)[2] == "long"
    either = rules(long_entry=[cond(CLOSE, ">", V(45)), cond(CLOSE, "<", V(15))], logic="or")
    assert build_strategy("rules", either).targets(c)[:2] == ["long", "long"]


def test_crosses_and_reversal():
    c = cs([10, 20, 10, 20, 10])
    s = build_strategy("rules", rules(
        long_entry=[cond(CLOSE, "crosses_above", V(15))],
        short_entry=[cond(CLOSE, "crosses_below", V(15))],
    ))
    assert s.targets(c) == ["flat", "long", "short", "long", "short"]


def test_stop_loss_and_take_profit():
    up = rules(long_entry=[cond(CLOSE, "crosses_above", V(100))], sl=5, tp=10)
    s = build_strategy("rules", up)
    assert s.targets(cs([99, 101, 97, 95.9]))[-1] == "flat"  # 101 × 0.95 = 95.95
    assert s.targets(cs([99, 101, 105, 111.2])) == ["flat", "long", "long", "flat"]


def test_rules_are_causal():
    closes = [100 + math.sin(i / 4) * 10 + i * 0.05 for i in range(400)]
    s = build_strategy("rules")  # 預設範例：RSI + EMA200
    assert s.targets(cs(closes))[:300] == s.targets(cs(closes[:300]))


def test_validation_messages():
    with pytest.raises(ValueError, match="至少要有一個"):
        build_strategy("rules", rules())
    with pytest.raises(ValueError, match="MACD"):
        build_strategy("rules", rules(long_entry=[cond({"kind": "macd", "fast": 30, "slow": 20, "signal": 9}, ">", V(0))]))
    with pytest.raises(ValueError):
        build_strategy("rules", rules(long_entry=[cond({"kind": "eval", "code": "1"}, ">", V(0))]))
    with pytest.raises(ValueError):
        build_strategy("rules", rules(long_entry=[cond(CLOSE, ">", V(1))] * 11))


def test_kd_matches_hand_calculation_and_drives_rules():
    from app.strategies.indicators import kd

    highs, lows, closes = [10, 11, 12, 13], [8, 9, 10, 9], [9, 10, 11, 9]
    k, d = kd(highs, lows, closes, 3)  # 跟前端 K 線圖用同一組手算數字核對
    assert k[:2] == [None, None] and d[:2] == [None, None]
    assert k[2] == pytest.approx(58.3333, abs=1e-3) and d[2] == pytest.approx(52.7778, abs=1e-3)
    assert k[3] == pytest.approx(38.8889, abs=1e-3) and d[3] == pytest.approx(48.1481, abs=1e-3)

    # 條件組合器：K 向上穿越 D 進場、向下穿越出場
    K, D = {"kind": "kd_k", "length": 9}, {"kind": "kd_d", "length": 9}
    rules = {"long_entry": {"logic": "and", "conditions": [{"left": K, "cmp": "crosses_above", "right": D}]},
             "long_exit": {"logic": "or", "conditions": [{"left": K, "cmp": "crosses_below", "right": D}]}}
    closes = [100 - i for i in range(20)] + [80 + 2 * i for i in range(15)] + [110 - 3 * i for i in range(15)]
    c = [Candle(i, x, x + 1, x - 1, x, 1) for i, x in enumerate(closes)]
    t = build_strategy("rules", {"rules": rules}).targets(c)
    assert "long" in t[20:35] and t[-1] == "flat"


def test_volume_profile_matches_frontend_numbers():
    from app.strategies.volume_profile import profile

    # 跟前端 volumeProfile.ts 驗證時用的同一組 K 棒：POC 105.5、價值區 104–106
    c = [Candle(1, 100, 110, 100, 105, 10), Candle(2, 105, 106, 104, 105, 100),
         Candle(3, 105, 105, 105, 105, 5), Candle(4, 101, 102, 100, 101, 20)]
    poc, vah, val = profile(c, 10, 0.7)
    assert (poc, vah, val) == pytest.approx((105.5, 106, 104))
    assert profile([Candle(1, 1, 1, 1, 1, 1)], 10, 0.7) is None  # 沒有波動


def test_previous_week_vp_is_causal_and_needs_a_full_week():
    from app.strategies.volume_profile import WEEK, period_start

    monday = period_start(10 * WEEK, "week")
    # 資料從上上週五開始：上上週不完整，所以上週的 K 棒沒有 VP；這週一起才有（用完整的上週）
    start = monday - WEEK - 3 * 86400
    px = lambda i: 100 + math.sin(i / 7) * 5  # noqa: E731
    candles = [Candle(start + i * 14400, px(i), px(i) + 1, px(i) - 1, px(i), 1 + i % 5) for i in range(42 * 3)]
    op = {"kind": "vp_poc", "period": "week", "rows": 30, "value_area": 70}
    poc = compute(op, candles, {})
    this_week = next(i for i, c in enumerate(candles) if c.time >= monday)
    assert all(v is None for v in poc[:this_week])
    assert poc[this_week] is not None and len(set(poc[this_week:this_week + 42])) == 1  # 整週用同一個值
    assert poc[this_week + 42] != poc[this_week]  # 下週換成這週的分佈
    # 只用已經結束的時段：後面的 K 棒不影響前面的值
    assert compute(op, candles[:this_week + 5], {}) == poc[:this_week + 5]


def test_vp_operand_in_rules():
    VAH = {"kind": "vp_vah", "period": "day", "rows": 20, "value_area": 70}
    day = 86400
    # 第一天在 100 附近盤整，第二天往上突破
    closes = [100 + (i % 3) for i in range(6)] + [101, 103, 106, 108, 110, 112]
    c = [Candle(day + i * 14400, x, x + 0.5, x - 0.5, x, 10) for i, x in enumerate(closes)]
    s = build_strategy("rules", rules(long_entry=[cond(CLOSE, ">", VAH)]))
    t = s.targets(c)
    assert t[:6] == ["flat"] * 6 and t[-1] == "long"
    plots = s.plots(c)
    assert plots[0]["name"].startswith("前日 VAH") and plots[0]["pane"] == "main"
    with pytest.raises(ValueError):
        build_strategy("rules", rules(long_entry=[cond(CLOSE, ">", {**VAH, "value_area": 99})]))
