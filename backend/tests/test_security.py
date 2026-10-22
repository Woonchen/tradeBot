import asyncio
import time
import json
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api import auth
from app.api.deps import client_ip
from app.config import Settings, settings


def req(xff: str | None, peer: str = "172.18.0.5") -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


@pytest.mark.parametrize(
    "xff,peer,expected",
    [
        (None, "52.89.214.238", "52.89.214.238"),                      # 直連，沒有代理
        ("52.89.214.238", "172.18.0.5", "52.89.214.238"),              # 經過一層內網代理
        ("52.89.214.238, 172.18.0.3", "172.18.0.5", "52.89.214.238"),  # 兩層代理
        ("52.89.214.238, 1.2.3.4", "172.18.0.5", "1.2.3.4"),           # 偽造最左邊的 TradingView IP
        ("garbage, 1.2.3.4", "172.18.0.5", "1.2.3.4"),
        ("192.168.1.10", "127.0.0.1", "192.168.1.10"),                 # 全部是內網：本機開發
    ],
)
def test_client_ip_ignores_spoofed_left_entries(xff, peer, expected):
    assert client_ip(req(xff, peer)) == expected


def dockerfile_trusted_proxies() -> str:
    cmd = re.search(r"^CMD (\[.*\])", (Path(__file__).parents[1] / "Dockerfile").read_text(), re.M).group(1)
    args = json.loads(cmd)
    return args[args.index("--forwarded-allow-ips") + 1]


@pytest.mark.parametrize(
    "xff,expected",
    [
        ("52.89.214.238", "52.89.214.238"),
        ("52.89.214.238, 1.2.3.4", "1.2.3.4"),            # 偽造最左邊的 TradingView IP
        ("52.89.214.238, 1.2.3.4, 172.18.0.3", "1.2.3.4"),  # 再經過一層內網代理
    ],
)
def test_client_ip_through_uvicorn_proxy_headers(xff, expected):
    """經過正式環境同樣設定的 uvicorn --proxy-headers，仍然拿到真實 IP，不會被偽造的 X-Forwarded-For 騙過。"""
    seen = {}

    async def app(scope, receive, send):
        seen["ip"] = client_ip(Request(scope))

    mw = ProxyHeadersMiddleware(app, trusted_hosts=dockerfile_trusted_proxies())
    scope = {"type": "http", "headers": [(b"x-forwarded-for", xff.encode())], "client": ("172.18.0.5", 1234)}
    asyncio.run(mw(scope, None, None))
    assert seen["ip"] == expected


def test_insecure_secrets():
    weak = Settings(_env_file=None, webhook_secret="", dashboard_password="change-me", jwt_secret="x" * 32)
    assert weak.insecure_secrets() == ["WEBHOOK_SECRET", "DASHBOARD_PASSWORD"]
    ok = Settings(_env_file=None, webhook_secret="a" * 32, dashboard_password="b" * 16, jwt_secret="c" * 32)
    assert ok.insecure_secrets() == []


def test_login_locks_after_repeated_failures(monkeypatch):
    monkeypatch.setattr(settings, "dashboard_user", "admin")
    monkeypatch.setattr(settings, "dashboard_password", "right-password")
    monkeypatch.setattr(auth, "_failures", {})
    app = FastAPI()
    app.include_router(auth.router)
    c = TestClient(app, client=("172.18.0.5", 50000))  # 模擬在內網 Caddy 後面

    def login(pw, ip="1.2.3.4"):
        return c.post("/api/auth/login", json={"username": "admin", "password": pw}, headers={"x-forwarded-for": ip})

    for _ in range(auth.MAX_FAILURES):
        assert login("wrong").status_code == 401
    r = login("right-password")
    assert r.status_code == 429 and "分鐘後再試" in r.json()["detail"]  # 鎖定中連正確密碼也擋
    assert login("right-password", ip="5.6.7.8").status_code == 200   # 其他 IP 不受影響

    # 時間過了鎖定期就能再登入，成功後清掉失敗紀錄
    auth._failures["1.2.3.4"] = [t - auth.WINDOW for t in auth._failures["1.2.3.4"]]
    assert login("right-password").status_code == 200
    assert "1.2.3.4" not in auth._failures


# ------------------------------------------------------------------ token 與 WebSocket
def ws_app():
    from types import SimpleNamespace

    from app.api import ws

    class Hub:
        status = {"spot": True}

        async def unwatch_chart(self, client):
            pass

    app = FastAPI()
    app.include_router(ws.router)
    app.state.executor = SimpleNamespace(bus=SimpleNamespace(clients=set()))
    app.state.hub = Hub()
    return app


def test_token_invalid_after_password_change(monkeypatch):
    from app.api.deps import create_token, verify_token

    monkeypatch.setattr(settings, "dashboard_password", "old-password")
    token = create_token("admin")
    assert verify_token(token) == "admin"
    monkeypatch.setattr(settings, "dashboard_password", "new-password")
    assert verify_token(token) is None


def test_ws_requires_auth_message():
    from starlette.websockets import WebSocketDisconnect

    from app.api.deps import create_token

    c = TestClient(ws_app())
    with c.websocket_connect("/ws") as ws:
        ws.send_text('{"op":"auth","token":"' + create_token("admin") + '"}')
        assert ws.receive_json()["type"] == "stream_status"

    for first in ['{"op":"auth","token":"bad"}', '{"op":"chart","symbol":"BTCUSDT"}', "ping"]:
        with c.websocket_connect("/ws") as ws:
            ws.send_text(first)
            with pytest.raises(WebSocketDisconnect) as e:
                ws.receive_json()
            assert e.value.code == 4401


def test_ws_closes_when_token_expires(monkeypatch):
    import jwt
    from starlette.websockets import WebSocketDisconnect

    from app.api import deps

    real_encode = jwt.encode

    def short_lived(claims, *a, **k):  # 1 秒後過期
        import time

        return real_encode({**claims, "exp": int(time.time()) + 1}, *a, **k)

    monkeypatch.setattr(deps.jwt, "encode", short_lived)
    with TestClient(ws_app()).websocket_connect("/ws") as ws:
        ws.send_text('{"op":"auth","token":"' + deps.create_token("admin") + '"}')
        ws.receive_json()
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 4401


# ------------------------------------------------------------------ webhook 輸入檢查
def test_webhook_rejects_oversized_fields_without_echoing_input(monkeypatch):
    from app.api import webhook

    monkeypatch.setattr(settings, "webhook_ip_allowlist", "")
    app = FastAPI()
    app.include_router(webhook.router)
    app.state.executor = None  # 驗證失敗就回應，不會用到
    c = TestClient(app)

    r = c.post("/webhook/tradingview", content=b"not json")
    assert r.status_code == 422 and r.json()["detail"] == "payload 不是有效的 JSON"

    long = "X" * 500
    r = c.post("/webhook/tradingview", json={"secret": "s", "symbol": "BTCUSDT", "action": "buy", "strategy": long})
    assert r.status_code == 422
    assert r.json()["detail"] == "payload 欄位不正確：strategy"
    assert long not in r.text  # 不把輸入內容回傳

    r = c.post("/webhook/tradingview", json={"secret": "s"})
    assert r.json()["detail"] == "payload 欄位不正確：action, symbol"


async def test_webhook_client_disconnect_is_quiet(monkeypatch):
    """讀 body 時連線被切斷（例如 Caddy 擋下超大請求）：直接結束，不丟例外。"""
    from app.api import webhook

    monkeypatch.setattr(settings, "webhook_ip_allowlist", "")

    async def receive():
        return {"type": "http.disconnect"}

    request = Request({"type": "http", "method": "POST", "headers": [], "client": ("172.18.0.5", 1)}, receive)
    resp = await webhook.tradingview_webhook(request, executor=None)
    assert resp.status_code == 400


# ------------------------------------------------------------------ 兩步驟驗證
@pytest.mark.parametrize(
    "t,expected",  # RFC 6238 附錄 B 的 SHA1 測試向量
    [(59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
     (1234567890, "89005924"), (2000000000, "69279037"), (20000000000, "65353130")],
)
def test_totp_rfc6238_vectors(t, expected):
    from app.api.totp import STEP, code_at

    assert code_at(b"12345678901234567890", t // STEP, digits=8) == expected


def test_totp_match_window_and_format():
    import base64

    from app.api.totp import STEP, code_at, decode_secret, match

    secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    key = decode_secret(secret)
    now = 1_700_000_000
    step = now // STEP
    assert match(secret, code_at(key, step), now) == step
    assert match(secret, code_at(key, step - 1), now) == step - 1  # 允許 30 秒時鐘誤差
    assert match(secret, code_at(key, step - 2), now) is None
    assert match(secret, "12 34 5", now) is None and match(secret, "abcdef", now) is None
    with pytest.raises(ValueError):
        decode_secret("not base32!")
    with pytest.raises(ValueError):
        decode_secret("AAAA")  # 太短


def test_login_requires_totp_code_once(monkeypatch):
    import base64

    from app.api.deps import verify_token
    from app.api.totp import STEP, code_at

    secret = base64.b32encode(b"tradebot-test-secret").decode()
    monkeypatch.setattr(settings, "dashboard_user", "admin")
    monkeypatch.setattr(settings, "dashboard_password", "right-password")
    monkeypatch.setattr(settings, "dashboard_totp_secret", "")
    monkeypatch.setattr(auth, "_failures", {})
    monkeypatch.setattr(auth, "_last_totp_step", -1)
    app = FastAPI()
    app.include_router(auth.router)
    c = TestClient(app, client=("172.18.0.5", 50000))

    def login(code="", ip="1.2.3.4"):
        body = {"username": "admin", "password": "right-password", "code": code}
        return c.post("/api/auth/login", json=body, headers={"x-forwarded-for": ip})

    old_token = login().json()["token"]  # 還沒啟用：只要密碼
    monkeypatch.setattr(settings, "dashboard_totp_secret", secret)
    assert c.get("/api/auth/config").json() == {"totp": True}
    assert verify_token(old_token) is None  # 啟用後舊 token 全部失效

    r = login()
    assert r.status_code == 401 and "驗證碼" in r.json()["detail"]
    code = code_at(base64.b32decode(secret), int(time.time()) // STEP)
    r = login(code, ip="5.6.7.8")
    assert r.status_code == 200 and verify_token(r.json()["token"]) == "admin"
    assert login(code, ip="9.9.9.9").status_code == 401  # 同一組碼不能再用
