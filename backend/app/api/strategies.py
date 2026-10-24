import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..engine.executor import Executor
from ..engine.risk import ORDER_FIELDS, load_bot_settings
from ..exchange.base import UnknownSymbol, normalize_ticker
from ..engine import shadow
from ..engine.performance import OwnerStats, RoundTrip, attribute, names_from_signals, owner_label
from ..models import Position, ShadowTrade, Signal, StrategyConfig, Trade, utcnow
from ..strategies import INTERVAL_SECONDS, REGISTRY, build_strategy
from ..strategies.backtest import run_backtest
from ..strategies.history import fetch_candles
from .deps import get_executor, require_user

router = APIRouter(prefix="/api/strategies", tags=["strategies"], dependencies=[Depends(require_user)])


class StrategyBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    strategy: str
    symbol: str = Field(min_length=2, max_length=40)
    market: Literal["spot", "futures"]
    interval: str
    params: dict = {}
    enabled: bool = False
    enter_on_enable: bool = False  # 啟用後第一次判斷就照目前判斷進場
    mode: Literal["live", "shadow"] = "live"  # shadow：影子模式，只記錄虛擬交易、不下單
    # 這個策略自己的下單規則；None = 沿用「設定」頁（限制跟設定頁一樣）
    size_mode: Literal["fixed", "percent", "equity"] | None = None
    size_value: float | None = Field(None, gt=0)
    leverage: int | None = Field(None, ge=1, le=125)
    stop_loss_pct: float | None = Field(None, ge=0, lt=100)
    take_profit_pct: float | None = Field(None, ge=0, le=1000)


class BacktestBody(BaseModel):
    strategy: str
    symbol: str
    market: Literal["spot", "futures"]
    interval: str
    params: dict = {}
    bars: int = Field(1000, ge=50, le=20000)
    enter_on_enable: bool = False  # 回測起點就照當下判斷進場（對應策略的同名設定）
    compare_earlier: bool = False  # 同時回測緊接在前、一樣長的期間（驗證策略不是只適合最近的行情）
    size_mode: Literal["fixed", "percent", "equity"] | None = None
    size_value: float | None = Field(None, gt=0)
    leverage: int | None = Field(None, ge=1, le=125)
    start_equity: float = Field(10_000, gt=0)
    # 沒填就用「設定」頁的全域停損停利（實際下單也會套用）；0 = 不設
    stop_loss_pct: float | None = Field(None, ge=0, lt=100)
    take_profit_pct: float | None = Field(None, ge=0, le=1000)


async def _validate(body: StrategyBody | BacktestBody, ex: Executor) -> str:
    if body.interval not in INTERVAL_SECONDS:
        raise HTTPException(422, f"週期必須是 {list(INTERVAL_SECONDS)} 之一")
    try:
        build_strategy(body.strategy, body.params)
    except ValueError as e:
        raise HTTPException(422, str(e))
    try:
        return await ex.market_data[body.market].resolve_symbol(body.symbol)
    except UnknownSymbol:
        raise HTTPException(422, f"找不到交易對 {body.symbol}（{body.market}）")


async def _check_one_per_symbol(s: AsyncSession, body: StrategyBody, config_id: int | None = None):
    """同一個交易對（同一個市場）同時只能啟用一個策略。

    機器人每個交易對只有一個部位，兩個策略會共用它、互相平倉，回測和實際結果都會失真。
    """
    if not body.enabled or body.mode != "live":  # 影子模式不下單，不會搶部位
        return
    want = normalize_ticker(body.symbol)
    q = select(StrategyConfig).where(
        StrategyConfig.enabled.is_(True), StrategyConfig.market == body.market, StrategyConfig.mode == "live",
    )
    for other in (await s.scalars(q)).all():
        if other.id != config_id and normalize_ticker(other.symbol) == want:
            raise HTTPException(
                409,
                f"「{other.name}」已經在 {want}（{'合約' if body.market == 'futures' else '現貨'}）上啟用。"
                "同一個交易對同時只能啟用一個策略，否則它們會共用同一個部位、互相平倉；請先停用它",
            )


def _runner(request: Request):
    return request.app.state.strategy_runner


@router.get("/catalog")
async def catalog():
    return [cls.describe() for cls in REGISTRY.values()]


async def _shadow_stats(s: AsyncSession, ex: Executor) -> dict[int, OwnerStats]:
    """影子策略的虛擬交易績效；還沒平倉的用目前價格算未實現損益。"""
    out: dict[int, OwnerStats] = {}
    prices: dict[tuple[str, str], float | None] = {}
    for t in (await s.scalars(select(ShadowTrade).order_by(ShadowTrade.entry_time, ShadowTrade.id))).all():
        st = out.setdefault(t.strategy_id, OwnerStats())
        # 資金費算進已實現（跟回測的淨損益一致）
        trip = RoundTrip(t.symbol, t.market, t.side, t.entry_time.isoformat(), qty=t.qty, fees=t.entry_fee + t.exit_fee,
                         realized=-(t.funding or 0))
        if t.exit_time:
            trip.closed_at, trip.realized = t.exit_time.isoformat(), t.pnl - (t.funding or 0)
            st.trips.append(trip)
        else:
            key = (t.market, t.symbol)
            if key not in prices:
                try:
                    prices[key] = await ex.market_data[t.market].fetch_price(t.symbol)
                except Exception:  # 拿不到價格就不顯示未實現損益
                    prices[key] = None
            price = prices[key]
            trip.unrealized = None if price is None else (price - t.entry_price) * t.qty * (1 if t.side == "long" else -1)
            st.open.append(trip)
    return out


@router.get("/performance")
async def performance(s: AsyncSession = Depends(get_session), ex: Executor = Depends(get_executor)):
    """每個策略的實際成交績效（部位由誰開倉就算誰的，見 engine/performance.py）。"""
    trades = (await s.scalars(select(Trade).order_by(Trade.executed_at, Trade.id))).all()
    sig_ids = {t.signal_id for t in trades if t.signal_id}
    signals = {x.id: x for x in (await s.scalars(select(Signal).where(Signal.id.in_(sig_ids)))).all()} if sig_ids else {}
    pos_ids = {t.position_id for t in trades if t.position_id}
    positions = {p.id: p for p in (await s.scalars(select(Position).where(Position.id.in_(pos_ids)))).all()} if pos_ids else {}
    stats = attribute(list(trades), signals, positions)
    unrealized = {(p.symbol, p.market): p.unrealized_pnl
                  for p in (await s.scalars(select(Position).where(Position.closed_at.is_(None)))).all()}

    def summary(owner) -> dict:
        out = stats.pop(owner, OwnerStats()).summary()  # 還沒有成交的策略也列出來
        for o in out["open"]:
            o["unrealized"] = unrealized.get((o["symbol"], o["market"]))
        return out

    shadows = await _shadow_stats(s, ex)
    configs = (await s.scalars(select(StrategyConfig).order_by(StrategyConfig.id))).all()
    result = [
        {"config": c.to_dict(), **(shadows.get(c.id, OwnerStats()).summary() if c.mode == "shadow" else summary(("strategy", c.id)))}
        for c in configs
    ]
    old_names = names_from_signals(signals)
    others = [{"label": owner_label(owner, {}, old_names), **summary(owner)} for owner in sorted(stats, key=str)]
    return {"strategies": result, "others": others}


@router.get("")
async def list_configs(s: AsyncSession = Depends(get_session)):
    return [c.to_dict() for c in (await s.scalars(select(StrategyConfig).order_by(StrategyConfig.id))).all()]


@router.post("")
async def create_config(body: StrategyBody, s: AsyncSession = Depends(get_session), ex: Executor = Depends(get_executor)):
    await _validate(body, ex)
    await _check_one_per_symbol(s, body)
    cfg = StrategyConfig(**body.model_dump())
    cfg.symbol = cfg.symbol.upper()
    if cfg.enabled:
        cfg.enabled_at = utcnow()
    s.add(cfg)
    await s.commit()
    await ex.bus.emit("strategy", cfg.to_dict(), category="system",
                      message=f"新增策略「{cfg.name}」（{cfg.strategy} {cfg.symbol} {cfg.market} {cfg.interval}）"
                      + ("，已啟用" if cfg.enabled else ""))
    return cfg.to_dict()


@router.put("/{config_id}")
async def update_config(
    config_id: int, body: StrategyBody, request: Request,
    s: AsyncSession = Depends(get_session), ex: Executor = Depends(get_executor),
):
    cfg = await s.get(StrategyConfig, config_id)
    if not cfg:
        raise HTTPException(404)
    await _validate(body, ex)
    await _check_one_per_symbol(s, body, config_id)
    # 只改名稱、開關或下單金額不算改了判斷邏輯，不必重新開始觀察
    changed_logic = any(
        getattr(cfg, k) != v for k, v in body.model_dump(exclude={"name", "enabled", *ORDER_FIELDS}).items()
    )
    turned_on = body.enabled and not cfg.enabled
    changes = describe_changes(cfg, body)
    for k, v in body.model_dump().items():
        setattr(cfg, k, v)
    cfg.symbol = cfg.symbol.upper()
    if changed_logic or turned_on:
        # 參數改了或重新啟用：重新觀察，等「之後」判斷改變才下單，不會立刻進場
        cfg.last_target = cfg.last_bar_time = None
        if cfg.enabled:  # 從現在開始觀察，「實際績效」的回測同期間從這裡算起
            cfg.enabled_at = utcnow()
    cfg.last_error = None
    await s.commit()
    _runner(request).reset(cfg.id)
    await ex.bus.emit("strategy", cfg.to_dict(), category="system",
                      message=f"策略「{cfg.name}」已更新" + ("（啟用中）" if cfg.enabled else "（已停用）")
                      + ("：" + "、".join(changes) if changes else "：沒有變更"))
    return cfg.to_dict()


# 操作紀錄裡顯示的欄位名稱，順序就是紀錄的順序
CHANGE_LABELS = {
    "name": "名稱", "enabled": "開關", "mode": "模式", "strategy": "策略", "symbol": "交易對", "market": "市場",
    "interval": "週期", "params": "參數", "enter_on_enable": "啟用時立即進場", "size_mode": "金額方式",
    "size_value": "金額", "leverage": "槓桿", "stop_loss_pct": "停損", "take_profit_pct": "停利",
}
_WORDS = {
    "market": {"spot": "現貨", "futures": "合約"}, "mode": {"live": "實際下單", "shadow": "影子模式"},
    "size_mode": {"fixed": "固定金額", "percent": "可用資金 %", "equity": "總權益 %"},
    "enabled": {True: "啟用", False: "停用"}, "enter_on_enable": {True: "開", False: "關"},
}


def _show(field: str, v) -> str:
    if v is None:
        return "照設定頁"
    if field in _WORDS:
        return _WORDS[field].get(v, str(v))
    if isinstance(v, float):
        v = f"{v:g}"
    return {"leverage": f"{v} 倍", "stop_loss_pct": f"{v}%", "take_profit_pct": f"{v}%"}.get(field, str(v))


def describe_changes(cfg: StrategyConfig, body: StrategyBody) -> list[str]:
    """這次修改了哪些欄位，例如「市場 現貨 → 合約」；參數太長，只寫「參數已修改」。"""
    new = body.model_dump() | {"symbol": body.symbol.upper()}
    out = []
    for field, label in CHANGE_LABELS.items():
        old, val = getattr(cfg, field), new[field]
        if old == val:
            continue
        out.append(f"{label}已修改" if field == "params" else f"{label} {_show(field, old)} → {_show(field, val)}")
    return out


@router.delete("/{config_id}")
async def delete_config(config_id: int, s: AsyncSession = Depends(get_session), ex: Executor = Depends(get_executor)):
    cfg = await s.get(StrategyConfig, config_id)
    if not cfg:
        raise HTTPException(404)
    await s.execute(delete(ShadowTrade).where(ShadowTrade.strategy_id == config_id))
    await s.delete(cfg)
    await s.commit()
    await ex.bus.emit("strategy", None, category="system", message=f"刪除策略「{cfg.name}」（既有持倉不會自動平倉）")
    return {"ok": True}


def _sizing(body: "BacktestBody", bot) -> dict:
    """回測的下單規則：沒填的用「設定」頁（實際下單也是用這些）。"""
    return {
        "market": body.market,
        "size_mode": body.size_mode or bot.size_mode,
        "size_value": body.size_value or bot.size_value,
        "leverage": body.leverage or bot.leverage,
        "start_equity": body.start_equity,
        "stop_loss_pct": bot.stop_loss_pct if body.stop_loss_pct is None else body.stop_loss_pct,
        "take_profit_pct": bot.take_profit_pct if body.take_profit_pct is None else body.take_profit_pct,
    }


async def _attach(request: Request, strategies: list, symbol: str) -> None:
    """規則用到恐懼貪婪、資金費率時，先把資料載入策略（同一份資料只抓一次）。"""
    sentiment = getattr(request.app.state, "sentiment", None)
    if sentiment is None:
        return
    # 只有條件組合器有 external_needs；其他內建策略不用外部資料
    first = next((st for st in strategies if hasattr(st, "external_needs") and st.external_needs()), None)
    if first is None:
        return
    await sentiment.attach(first, symbol)
    for st in strategies:
        st.ext = first.ext


async def _funding(request: Request, market: str, symbol: str) -> dict:
    """合約回測要扣的歷史資金費率；現貨或拿不到資料時不扣（結果會標示 funding_included）。"""
    sentiment = getattr(request.app.state, "sentiment", None)
    if market != "futures" or sentiment is None:
        return {}
    return {"funding": await sentiment.funding_series(symbol)}


@router.post("/backtest")
async def backtest(
    body: BacktestBody, request: Request, s: AsyncSession = Depends(get_session), ex: Executor = Depends(get_executor),
):
    symbol = await _validate(body, ex)
    bot = await load_bot_settings(s)
    strat = build_strategy(body.strategy, body.params)
    await _attach(request, [strat], body.symbol)
    n, warm = body.bars, strat.warmup  # 每段前面多抓 warm 根讓指標穩定，不計入結果
    candles = await fetch_candles(
        ex.market_data[body.market], symbol, body.interval, n * (2 if body.compare_earlier else 1) + warm,
    )
    recent = candles[-(n + warm):]
    recent_warm = max(0, len(recent) - n)  # 上市不久的幣，歷史不夠就少暖一點
    if len(recent) - recent_warm < 50:
        raise HTTPException(422, "歷史 K 線不足")
    sizing = _sizing(body, bot)
    follow = {"follow_initial": body.enter_on_enable, **await _funding(request, body.market, body.symbol)}
    res = run_backtest(strat, recent, warmup=recent_warm, **sizing, **follow)
    earlier = None
    if body.compare_earlier and len(candles) >= 2 * n + warm:
        earlier = run_backtest(strat, candles[-(2 * n + warm):-n], warmup=warm, **sizing, **follow).stats
    return {
        "symbol": symbol,
        "params": strat.p,
        "sizing": sizing,
        "warmup": recent_warm,
        "candles": [c.__dict__ for c in recent[recent_warm:]],
        # 策略用到的指標（跟判斷同一份計算），跟 candles 對齊
        "plots": [{**p, "values": p["values"][recent_warm:]} for p in strat.plots(recent)],
        "trades": res.trades,
        "equity": res.equity,
        "stats": res.stats,
        # 要求了但歷史不夠時是 None，前端會說明
        "earlier": earlier,
    }


# ---------------------------------------------------------------------------- 參數掃描
MAX_SWEEP_CELLS = 100
_sweep_lock = asyncio.Lock()  # 一次只跑一個掃描，避免同時吃滿 CPU


class SweepAxis(BaseModel):
    param: str
    values: list[float] = Field(min_length=1, max_length=25)


class SweepBody(BacktestBody):
    bars: int = Field(5000, ge=50, le=20000)
    compare_earlier: bool = True
    x: SweepAxis
    y: SweepAxis | None = None


def _brief(st: dict) -> dict:
    keys = ("total_return", "profit_factor", "max_drawdown", "trades", "win_rate", "buy_hold_return", "from", "to",
            "funding", "funding_included")
    return {k: st[k] for k in keys}


@router.post("/sweep")
async def sweep(
    body: SweepBody, request: Request, s: AsyncSession = Depends(get_session), ex: Executor = Depends(get_executor),
):
    """參數穩健性：一個或兩個數字參數的每個組合，在最近與更早兩段期間各回測一次。

    好的參數應該落在一片「附近也都不錯」的區域；只有單一數值特別好、旁邊都差，多半是運氣。
    """
    symbol = await _validate(body, ex)
    specs = {p.name: p for p in REGISTRY[body.strategy].params}
    axes = [a for a in (body.x, body.y) if a]
    for a in axes:
        spec = specs.get(a.param)
        if spec is None or spec.type not in ("int", "float"):
            raise HTTPException(422, f"「{a.param}」不是可以掃描的數字參數")
    if body.y and body.y.param == body.x.param:
        raise HTTPException(422, "兩個軸不能是同一個參數")
    ys = body.y.values if body.y else [None]
    if len(body.x.values) * len(ys) > MAX_SWEEP_CELLS:
        raise HTTPException(422, f"組合太多（最多 {MAX_SWEEP_CELLS} 個），請縮小範圍或加大間距")

    def cast(name: str, v: float):
        return int(round(v)) if specs[name].type == "int" else v

    # 先建出每一格的策略；參數彼此衝突（例如快線 ≥ 慢線）的格子標成錯誤
    grid: list[list[tuple]] = []
    for yv in ys:
        row = []
        for xv in body.x.values:
            params = {**body.params, body.x.param: cast(body.x.param, xv)}
            if body.y:
                params[body.y.param] = cast(body.y.param, yv)
            try:
                row.append((build_strategy(body.strategy, params), None))
            except ValueError as e:
                row.append((None, str(e)))
        grid.append(row)
    valid = [st for row in grid for st, _ in row if st]
    if not valid:
        raise HTTPException(422, "所有組合的參數都不合法")
    await _attach(request, valid, body.symbol)

    if _sweep_lock.locked():
        raise HTTPException(429, "已經有一個參數掃描在跑，請等它完成")
    async with _sweep_lock:
        bot = await load_bot_settings(s)
        sizing = _sizing(body, bot)
        follow = {"follow_initial": body.enter_on_enable, **await _funding(request, body.market, body.symbol)}
        n, warm = body.bars, max(st.warmup for st in valid)
        candles = await fetch_candles(ex.market_data[body.market], symbol, body.interval, n * (2 if body.compare_earlier else 1) + warm)
        recent = candles[-(n + warm):]
        recent_warm = max(0, len(recent) - n)
        if len(recent) - recent_warm < 50:
            raise HTTPException(422, "歷史 K 線不足")
        earlier = candles[-(2 * n + warm):-n] if body.compare_earlier and len(candles) >= 2 * n + warm else None

        def run_all():  # 在另一個執行緒跑，回測期間即時行情與策略執行不會卡住
            out = []
            for row in grid:
                cells = []
                for st, err in row:
                    if st is None:
                        cells.append({"error": err})
                        continue
                    r = run_backtest(st, recent, warmup=recent_warm, **sizing, **follow).stats
                    e = run_backtest(st, earlier, warmup=warm, **sizing, **follow).stats if earlier else None
                    cells.append({"recent": _brief(r), "earlier": _brief(e) if e else None})
                out.append(cells)
            return out

        cells = await asyncio.to_thread(run_all)
    return {
        "symbol": symbol,
        "x": {"param": body.x.param, "label": specs[body.x.param].label, "values": [cast(body.x.param, v) for v in body.x.values]},
        "y": ({"param": body.y.param, "label": specs[body.y.param].label, "values": [cast(body.y.param, v) for v in body.y.values]}
              if body.y else None),
        "cells": cells,
        "sizing": sizing,
        "bars": n,
        # 要求了但歷史不夠時，每格的 earlier 都是 None
        "earlier_available": earlier is not None,
    }
