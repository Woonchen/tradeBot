from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..engine.executor import Executor
from ..engine.risk import load_bot_settings
from ..exchange.base import UnknownSymbol, normalize_ticker
from ..engine.performance import OwnerStats, attribute
from ..models import Position, Signal, StrategyConfig, Trade
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


class BacktestBody(BaseModel):
    strategy: str
    symbol: str
    market: Literal["spot", "futures"]
    interval: str
    params: dict = {}
    bars: int = Field(1000, ge=50, le=20000)
    compare_earlier: bool = False  # 同時回測緊接在前、一樣長的期間（驗證策略不是只適合最近的行情）
    size_mode: Literal["fixed", "percent"] | None = None
    size_value: float | None = Field(None, gt=0)
    leverage: int | None = Field(None, ge=1, le=125)
    start_equity: float = Field(10_000, gt=0)


async def _validate(body: StrategyBody | BacktestBody, ex: Executor) -> str:
    if body.interval not in INTERVAL_SECONDS:
        raise HTTPException(422, f"週期必須是 {list(INTERVAL_SECONDS)} 之一")
    try:
        build_strategy(body.strategy, body.params)
    except ValueError as e:
        raise HTTPException(422, str(e))
    try:
        return await ex.adapters[body.market].resolve_symbol(body.symbol)
    except UnknownSymbol:
        raise HTTPException(422, f"找不到交易對 {body.symbol}（{body.market}）")


async def _check_one_per_symbol(s: AsyncSession, body: StrategyBody, config_id: int | None = None):
    """同一個交易對（同一個市場）同時只能啟用一個策略。

    機器人每個交易對只有一個部位，兩個策略會共用它、互相平倉，回測和實際結果都會失真。
    """
    if not body.enabled:
        return
    want = normalize_ticker(body.symbol)
    q = select(StrategyConfig).where(StrategyConfig.enabled.is_(True), StrategyConfig.market == body.market)
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


@router.get("/performance")
async def performance(s: AsyncSession = Depends(get_session)):
    """每個策略的實際成交績效（部位由誰開倉就算誰的，見 engine/performance.py）。"""
    trades = (await s.scalars(select(Trade).order_by(Trade.executed_at, Trade.id))).all()
    sig_ids = {t.signal_id for t in trades if t.signal_id}
    signals = {x.id: x for x in (await s.scalars(select(Signal).where(Signal.id.in_(sig_ids)))).all()} if sig_ids else {}
    stats = attribute(list(trades), signals)
    unrealized = {(p.symbol, p.market): p.unrealized_pnl
                  for p in (await s.scalars(select(Position).where(Position.closed_at.is_(None)))).all()}

    def summary(owner) -> dict:
        out = stats.pop(owner, OwnerStats()).summary()  # 還沒有成交的策略也列出來
        for o in out["open"]:
            o["unrealized"] = unrealized.get((o["symbol"], o["market"]))
        return out

    configs = (await s.scalars(select(StrategyConfig).order_by(StrategyConfig.id))).all()
    result = [{"config": c.to_dict(), **summary(("strategy", c.id))} for c in configs]
    labels = {"tradingview": "TradingView", "manual": "手動開倉", "other": "其他（交易所上直接操作等）"}
    others = []
    for owner in sorted(stats, key=str):
        kind, name = owner
        label = f"已刪除的策略 #{name}" if kind == "strategy" else (f"TradingView：{name}" if kind == "tradingview" else labels[kind])
        others.append({"label": label, **summary(owner)})
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
    changed_logic = any(getattr(cfg, k) != v for k, v in body.model_dump(exclude={"name", "enabled"}).items())
    turned_on = body.enabled and not cfg.enabled
    for k, v in body.model_dump().items():
        setattr(cfg, k, v)
    cfg.symbol = cfg.symbol.upper()
    if changed_logic or turned_on:
        # 參數改了或重新啟用：重新觀察，等「之後」判斷改變才下單，不會立刻進場
        cfg.last_target = cfg.last_bar_time = None
    cfg.last_error = None
    await s.commit()
    _runner(request).reset(cfg.id)
    await ex.bus.emit("strategy", cfg.to_dict(), category="system",
                      message=f"策略「{cfg.name}」已更新" + ("（啟用中）" if cfg.enabled else "（已停用）"))
    return cfg.to_dict()


@router.delete("/{config_id}")
async def delete_config(config_id: int, s: AsyncSession = Depends(get_session), ex: Executor = Depends(get_executor)):
    cfg = await s.get(StrategyConfig, config_id)
    if not cfg:
        raise HTTPException(404)
    await s.delete(cfg)
    await s.commit()
    await ex.bus.emit("strategy", None, category="system", message=f"刪除策略「{cfg.name}」（既有持倉不會自動平倉）")
    return {"ok": True}


@router.post("/backtest")
async def backtest(body: BacktestBody, s: AsyncSession = Depends(get_session), ex: Executor = Depends(get_executor)):
    symbol = await _validate(body, ex)
    bot = await load_bot_settings(s)
    strat = build_strategy(body.strategy, body.params)
    n, warm = body.bars, strat.warmup  # 每段前面多抓 warm 根讓指標穩定，不計入結果
    candles = await fetch_candles(
        ex.adapters[body.market], symbol, body.interval, n * (2 if body.compare_earlier else 1) + warm,
    )
    recent = candles[-(n + warm):]
    recent_warm = max(0, len(recent) - n)  # 上市不久的幣，歷史不夠就少暖一點
    if len(recent) - recent_warm < 50:
        raise HTTPException(422, "歷史 K 線不足")
    sizing = {
        "market": body.market,
        "size_mode": body.size_mode or bot.size_mode,
        "size_value": body.size_value or bot.size_value,
        "leverage": body.leverage or bot.leverage,
        "start_equity": body.start_equity,
    }
    res = run_backtest(strat, recent, warmup=recent_warm, **sizing)
    earlier = None
    if body.compare_earlier and len(candles) >= 2 * n + warm:
        earlier = run_backtest(strat, candles[-(2 * n + warm):-n], warmup=warm, **sizing).stats
    return {
        "symbol": symbol,
        "params": strat.p,
        "sizing": sizing,
        "warmup": recent_warm,
        "candles": [c.__dict__ for c in recent[recent_warm:]],
        "trades": res.trades,
        "equity": res.equity,
        "stats": res.stats,
        # 要求了但歷史不夠時是 None，前端會說明
        "earlier": earlier,
    }
