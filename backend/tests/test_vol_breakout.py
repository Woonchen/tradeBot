import pytest

from app.strategies import Candle, build_strategy


def flat_then(spikes: dict[int, tuple[float, float]], n=320, base=100.0):
    """平穩的 K 棒（實體 0.2、量 10），spikes[i] = (這根漲多少, 量)；收盤之後維持在新價位。"""
    out, px = [], base
    for i in range(n):
        up, vol = spikes.get(i, (0.2, 10.0))
        o = px
        px = o + up
        out.append(Candle(i * 14400, o, max(o, px) + 0.1, min(o, px) - 0.1, px, vol))
        if i not in spikes:  # 平常是一根漲一根跌，價格不會一直漂
            px = o
    return out


P = {"body_atr": 2.0, "vol_mult": 2.0, "vol_len": 20, "atr_len": 14, "trend_len": 200, "hold_bars": 30, "trail_atr": 0}


def test_entry_needs_big_body_volume_and_trend():
    c = flat_then({300: (5.0, 50.0)})
    t = build_strategy("vol_breakout", P).targets(c)
    assert t[299] == "flat" and t[300] == "long"
    # 量不夠
    assert build_strategy("vol_breakout", P).targets(flat_then({300: (5.0, 15.0)}))[300] == "flat"
    # 實體不夠（ATR 大約 0.4）
    assert build_strategy("vol_breakout", P).targets(flat_then({300: (0.5, 50.0)}))[300] == "flat"
    # 在 SMA200 之下：先跌一大段，再出現同樣的大陽線
    down = flat_then({250: (-30.0, 10.0), 300: (5.0, 50.0)})  # SMA200 還有一大半是下跌前的價格
    assert build_strategy("vol_breakout", P).targets(down)[300] == "flat"
    assert build_strategy("vol_breakout", {**P, "trend_len": 0}).targets(down)[300] == "long"


def test_time_exit_after_hold_bars():
    c = flat_then({250: (5.0, 50.0)})
    t = build_strategy("vol_breakout", P).targets(c)
    assert t[250] == "long" and t[279] == "long" and t[280] == "flat"  # 進場後第 30 根收盤出場


def test_trailing_atr_exit():
    # 突破後先再漲，然後跌回去：最高收盤 − 3×ATR 被跌破就出場
    c = flat_then({250: (5.0, 50.0), 251: (3.0, 10.0), 260: (-4.0, 10.0)})
    t = build_strategy("vol_breakout", {**P, "hold_bars": 0, "trail_atr": 3}).targets(c)
    assert t[259] == "long" and t[260] == "flat"
    assert build_strategy("vol_breakout", {**P, "hold_bars": 0, "trail_atr": 20}).targets(c)[260] == "long"


def test_causal_and_explain():
    c = flat_then({250: (5.0, 50.0), 290: (6.0, 60.0)})
    s = build_strategy("vol_breakout", {**P, "trail_atr": 4})
    full = s.targets(c)
    assert all(s.targets(c[:k]) == full[:k] for k in (251, 270, 291, 310))
    ex = s.explain(c[:260])
    assert ex["groups"]["long_entry"]["met"] is False and len(ex["groups"]["long_exit"]["conditions"]) == 2
    assert ex["groups"]["long_exit"]["conditions"][0]["left_value"] == 9  # 第 250 根進場，到 259 持有 9 根


def test_needs_an_exit():
    with pytest.raises(ValueError, match="出場"):
        build_strategy("vol_breakout", {**P, "hold_bars": 0, "trail_atr": 0})
