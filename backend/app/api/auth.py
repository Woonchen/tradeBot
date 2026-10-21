import hmac

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import settings
from .deps import create_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginBody):
    ok_user = hmac.compare_digest(body.username, settings.dashboard_user)
    ok_pass = hmac.compare_digest(body.password, settings.dashboard_password)
    if not (ok_user and ok_pass):
        raise HTTPException(401, "帳號或密碼錯誤")
    return {"token": create_token(body.username)}
