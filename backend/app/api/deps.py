import hashlib
import hmac
import ipaddress
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import settings
from ..engine.executor import Executor

bearer = HTTPBearer(auto_error=False)


def _password_version() -> str:
    """目前密碼的指紋。放進 token，改了 DASHBOARD_PASSWORD 之後舊 token 就自動失效。"""
    return hmac.new(settings.jwt_secret.encode(), settings.dashboard_password.encode(), hashlib.sha256).hexdigest()[:16]


def create_token(username: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=settings.jwt_ttl_hours)
    return jwt.encode({"sub": username, "exp": exp, "pv": _password_version()}, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str | None) -> dict | None:
    """驗證通過時回傳 token 內容（含 sub、exp），否則 None。"""
    if not token:
        return None
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"], options={"require": ["exp", "sub"]})
    except jwt.PyJWTError:
        return None
    if not hmac.compare_digest(str(claims.get("pv", "")), _password_version()):
        return None
    return claims


def verify_token(token: str | None) -> str | None:
    claims = decode_token(token)
    return claims["sub"] if claims else None


def require_user(cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
    user = verify_token(cred.credentials if cred else None)
    if not user:
        raise HTTPException(401, "未登入或登入已過期")
    return user


def _is_proxy(ip: str) -> bool:
    """內網／本機位址視為我們自己的反向代理（Caddy、共用機器上的既有代理）。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback


def client_ip(request: Request) -> str:
    """請求的真實來源 IP。

    X-Forwarded-For 最左邊的值是發送端自己填的，可以偽造；每一層反向代理只會往右邊追加，
    所以從右往左找第一個不是內網的位址，才是最外層代理實際看到的連線來源。
    """
    peer = request.client.host if request.client else ""
    hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    chain = [*hops, peer] if peer else hops
    for ip in reversed(chain):
        if not _is_proxy(ip):
            return ip
    return chain[0] if chain else ""  # 全部都是內網：本機開發或內網直連


def get_executor(request: Request) -> Executor:
    return request.app.state.executor
