from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_session
from ..engine.executor import Executor
from ..engine.risk import load_bot_settings
from ..models import EquitySnapshot
from .deps import get_executor, require_user

router = APIRouter(prefix="/api/bot", tags=["bot"], dependencies=[Depends(require_user)])


class BotSettingsUpdate(BaseModel):
    enabled: bool | None = None
    symbols: list[str] | None = None
    size_mode: Literal["fixed", "percent", "equity"] | None = None
    size_value: float | None = Field(None, gt=0)
    leverage: int | None = Field(None, ge=1, le=125)
    margin_mode: Literal["isolated", "cross"] | None = None
    max_open_positions: int | None = Field(None, ge=1)
    max_daily_loss_usdt: float | None = Field(None, gt=0)
    stop_loss_pct: float | None = Field(None, ge=0, lt=100)
    take_profit_pct: float | None = Field(None, ge=0)
    max_drawdown_pct: float | None = Field(None, ge=0, lt=100)  # 0 = 不啟用回撤熔斷
    breaker_fallback_leverage: int | None = Field(None, ge=0, le=125)  # 0 = 熔斷就停用 Bot


def _out(cfg, snap: EquitySnapshot | None = None) -> dict:
    return {
        **cfg.to_dict(),
        "execution_mode": settings.execution_mode,
        "testnet": settings.testnet,
        "drawdown": drawdown_status(cfg, snap),
    }


def drawdown_status(cfg, snap: EquitySnapshot | None) -> list[dict]:
    """各市場目前離權益最高點多遠，給設定頁顯示。"""
    current = {"spot": snap.spot_usdt, "futures": snap.futures_usdt} if snap else {}
    out = []
    for market, peak in sorted((cfg.equity_peaks or {}).items()):
        now = current.get(market)
        out.append({
            "market": market, "peak": peak["value"], "peak_at": peak["ts"], "equity": now,
            "drawdown_pct": (1 - now / peak["value"]) * 100 if now is not None and peak["value"] > 0 else None,
        })
    return out


async def _latest_snapshot(s: AsyncSession) -> EquitySnapshot | None:
    return await s.scalar(select(EquitySnapshot).order_by(EquitySnapshot.ts.desc()).limit(1))


@router.get("")
async def get_bot(s: AsyncSession = Depends(get_session)):
    cfg = await load_bot_settings(s)
    await s.commit()
    return _out(cfg, await _latest_snapshot(s))


@router.put("")
async def update_bot(body: BotSettingsUpdate, s: AsyncSession = Depends(get_session), ex: Executor = Depends(get_executor)):
    cfg = await load_bot_settings(s)
    changes = body.model_dump(exclude_none=True)
    if changes.get("enabled") and not cfg.enabled:
        # 重新啟用：權益最高點從現在重新記錄，否則被熔斷停用後一打開又會立刻觸發
        cfg.equity_peaks = {}
    for k, v in changes.items():
        setattr(cfg, k, v)
    await s.commit()
    await ex.bus.emit("bot", _out(cfg), category="system", message=f"Bot 設定已更新：{changes}")
    return _out(cfg, await _latest_snapshot(s))


@router.post("/close-all")
async def close_all(s: AsyncSession = Depends(get_session), ex: Executor = Depends(get_executor)):
    """緊急全部平倉，並停用 Bot。"""
    cfg = await load_bot_settings(s)
    ids = await ex.close_all(s, cfg, "手動觸發緊急全部平倉（{n} 個部位），Bot 已停用")
    return {"signal_ids": ids}
