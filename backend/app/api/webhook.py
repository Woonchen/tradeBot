import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from ..config import settings
from ..engine.executor import Executor
from .deps import client_ip, get_executor

router = APIRouter(tags=["webhook"])


class WebhookPayload(BaseModel):
    # 長度上限對應資料庫欄位（models.Signal），超過時回 422 而不是寫入失敗
    secret: str = Field(max_length=256)
    symbol: str = Field(max_length=40)  # {{ticker}}，例如 BTCUSDT 或 BTCUSDT.P
    action: str = Field(max_length=10)  # {{strategy.order.action}}（buy/sell）或明確的 long/short/close
    market: Literal["spot", "futures"] = "spot"
    position: str | None = Field(None, max_length=10)  # {{strategy.market_position}}：long/short/flat
    strategy: str = Field("tradingview", max_length=64)
    price: float | None = None
    time: str | None = Field(None, max_length=64)  # {{timenow}}
    id: str | None = Field(None, max_length=128)  # {{strategy.order.id}}


def resolve_target(action: str, position: str | None) -> str:
    """把 TradingView 的 action/market_position 轉成「目標部位」long / short / flat。"""
    a = action.strip().lower()
    p = (position or "").strip().lower()
    if a in ("long", "short"):
        return a
    if a in ("close", "exit", "flat"):
        return "flat"
    if a in ("buy", "sell"):
        if p in ("long", "short", "flat"):
            return p
        return "long" if a == "buy" else "flat"
    raise ValueError(f"未知的 action：{action}")


def dedupe_key(p: WebhookPayload, body: bytes) -> str:
    if p.time or p.id:
        raw = "|".join(str(x) for x in (p.strategy, p.symbol, p.market, p.action, p.position, p.id, p.time))
    else:  # 沒有時間戳時，同一分鐘內內容完全相同視為重複
        raw = body.decode(errors="ignore") + datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    return hashlib.sha256(raw.encode()).hexdigest()


@router.post("/webhook/tradingview")
async def tradingview_webhook(request: Request, executor: Executor = Depends(get_executor)):
    allow = {ip.strip() for ip in settings.webhook_ip_allowlist.split(",") if ip.strip()}
    if allow and client_ip(request) not in allow:
        raise HTTPException(403, "IP not allowed")

    body = await request.body()
    try:
        payload = WebhookPayload.model_validate(json.loads(body))
    except json.JSONDecodeError:
        raise HTTPException(422, "payload 不是有效的 JSON")
    except ValidationError as e:
        # 只回欄位名稱，方便設定 TradingView 時除錯，不回傳內部的驗證細節與輸入內容
        fields = sorted({".".join(str(x) for x in err["loc"]) or "body" for err in e.errors()})
        raise HTTPException(422, f"payload 欄位不正確：{', '.join(fields)}")
    if not hmac.compare_digest(payload.secret, settings.webhook_secret):
        raise HTTPException(401, "invalid secret")
    try:
        target = resolve_target(payload.action, payload.position)
    except ValueError as e:
        raise HTTPException(422, str(e))

    raw = payload.model_dump(exclude={"secret"})
    sig, created = await executor.submit(
        source="tradingview", ticker=payload.symbol, market=payload.market, action=payload.action.lower(),
        target=target, strategy=payload.strategy, price=payload.price, raw=raw,
        dedupe_key=dedupe_key(payload, body),
    )
    # 立刻回應（TradingView 只等約 3 秒），下單在背景佇列執行
    return {"status": "queued" if created else "duplicate", "signal_id": sig.id}
