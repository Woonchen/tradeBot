from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_session
from ..engine.executor import Executor
from ..engine.risk import load_bot_settings
from ..models import Position
from .deps import get_executor, require_user

router = APIRouter(prefix="/api/bot", tags=["bot"], dependencies=[Depends(require_user)])


class BotSettingsUpdate(BaseModel):
    enabled: bool | None = None
    symbols: list[str] | None = None
    size_mode: Literal["fixed", "percent"] | None = None
    size_value: float | None = Field(None, gt=0)
    leverage: int | None = Field(None, ge=1, le=125)
    margin_mode: Literal["isolated", "cross"] | None = None
    max_open_positions: int | None = Field(None, ge=1)
    max_daily_loss_usdt: float | None = Field(None, gt=0)
    stop_loss_pct: float | None = Field(None, ge=0, lt=100)
    take_profit_pct: float | None = Field(None, ge=0)


def _out(cfg) -> dict:
    return {
        **cfg.to_dict(),
        "execution_mode": settings.execution_mode,
        "testnet": settings.testnet,
    }


@router.get("")
async def get_bot(s: AsyncSession = Depends(get_session)):
    cfg = await load_bot_settings(s)
    await s.commit()
    return _out(cfg)


@router.put("")
async def update_bot(body: BotSettingsUpdate, s: AsyncSession = Depends(get_session), ex: Executor = Depends(get_executor)):
    cfg = await load_bot_settings(s)
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(cfg, k, v)
    await s.commit()
    await ex.bus.emit("bot", _out(cfg), category="system", message=f"Bot 設定已更新：{changes}")
    return _out(cfg)


@router.post("/close-all")
async def close_all(s: AsyncSession = Depends(get_session), ex: Executor = Depends(get_executor)):
    """緊急全部平倉，並停用 Bot。"""
    cfg = await load_bot_settings(s)
    cfg.enabled = False
    await s.commit()
    positions = (await s.scalars(select(Position).where(Position.closed_at.is_(None)))).all()
    await ex.bus.emit(
        "bot", _out(cfg), level="warn", category="risk", notify=True,
        message=f"手動觸發緊急全部平倉（{len(positions)} 個部位），Bot 已停用",
    )
    ids = []
    for p in positions:
        sig, _ = await ex.submit(source="manual", ticker=p.symbol, market=p.market, action="close", target="flat")
        ids.append(sig.id)
    return {"signal_ids": ids}
