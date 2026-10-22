import hmac
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..config import settings
from .deps import client_ip, create_token

router = APIRouter(prefix="/api/auth", tags=["auth"])

MAX_FAILURES = 5  # 同一個 IP 在 WINDOW 秒內最多失敗幾次
WINDOW = 15 * 60
_failures: dict[str, list[float]] = {}  # IP → 最近失敗的時間；重啟後歸零


class LoginBody(BaseModel):
    username: str
    password: str


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

    ok_user = hmac.compare_digest(body.username, settings.dashboard_user)
    ok_pass = hmac.compare_digest(body.password, settings.dashboard_password)
    if not (ok_user and ok_pass):
        _failures[ip] = [*recent, now]
        left = MAX_FAILURES - len(_failures[ip])
        raise HTTPException(401, f"帳號或密碼錯誤（再錯 {left} 次將暫停登入 15 分鐘）" if left else "帳號或密碼錯誤")
    _failures.pop(ip, None)
    return {"token": create_token(body.username)}
