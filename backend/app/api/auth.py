import hmac
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..config import settings
from . import totp
from .deps import client_ip, create_token

router = APIRouter(prefix="/api/auth", tags=["auth"])

MAX_FAILURES = 5  # 同一個 IP 在 WINDOW 秒內最多失敗幾次
WINDOW = 15 * 60
_failures: dict[str, list[float]] = {}  # IP → 最近失敗的時間；重啟後歸零
_last_totp_step = -1  # 最近一次成功登入用掉的驗證碼時間步；同一組碼不能再用（防止被偷看後重放）


class LoginBody(BaseModel):
    username: str = Field(max_length=128)
    password: str = Field(max_length=256)
    code: str = Field("", max_length=16)  # 兩步驟驗證碼；沒啟用時忽略


@router.get("/config")
async def auth_config():
    """登入頁要不要顯示驗證碼欄位。"""
    return {"totp": bool(settings.dashboard_totp_secret)}


def _recent(ip: str, now: float) -> list[float]:
    recent = [t for t in _failures.get(ip, []) if now - t < WINDOW]
    if recent:
        _failures[ip] = recent
    else:
        _failures.pop(ip, None)
    return recent


@router.post("/login")
async def login(body: LoginBody, request: Request):
    ip, now = client_ip(request), time.time()
    if len(_failures) > 10_000:  # 大量不同 IP 來試時，避免記錄無限長大
        for k in list(_failures):
            _recent(k, now)
    recent = _recent(ip, now)
    if len(recent) >= MAX_FAILURES:  # 鎖定期間連正確密碼也不接受，避免被拿來試出密碼
        wait = int(recent[0] + WINDOW - now) // 60 + 1
        raise HTTPException(429, f"登入失敗次數過多，請 {wait} 分鐘後再試")

    global _last_totp_step
    ok_user = hmac.compare_digest(body.username, settings.dashboard_user)
    ok_pass = hmac.compare_digest(body.password, settings.dashboard_password)
    step = None
    if settings.dashboard_totp_secret:  # 不管密碼對不對都算一次，不從回應時間透露密碼是否正確
        step = totp.match(settings.dashboard_totp_secret, body.code, now)
    ok_code = not settings.dashboard_totp_secret or (step is not None and step > _last_totp_step)
    if not (ok_user and ok_pass and ok_code):
        _failures[ip] = [*recent, now]
        left = MAX_FAILURES - len(_failures[ip])
        what = "帳號、密碼或驗證碼錯誤" if settings.dashboard_totp_secret else "帳號或密碼錯誤"
        raise HTTPException(401, f"{what}（再錯 {left} 次將暫停登入 15 分鐘）" if left else what)
    if step is not None:
        _last_totp_step = step
    _failures.pop(ip, None)
    return {"token": create_token(body.username)}
