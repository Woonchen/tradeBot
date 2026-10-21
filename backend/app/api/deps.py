from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import settings
from ..engine.executor import Executor

bearer = HTTPBearer(auto_error=False)


def create_token(username: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=settings.jwt_ttl_hours)
    return jwt.encode({"sub": username, "exp": exp}, settings.jwt_secret, algorithm="HS256")


def verify_token(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])["sub"]
    except jwt.PyJWTError:
        return None


def require_user(cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
    user = verify_token(cred.credentials if cred else None)
    if not user:
        raise HTTPException(401, "未登入或登入已過期")
    return user


def get_executor(request: Request) -> Executor:
    return request.app.state.executor
