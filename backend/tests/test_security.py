import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

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
