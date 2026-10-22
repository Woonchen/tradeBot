# 基底映像固定到 digest，每次 build 都拿到同一份內容；要更新時查新的 digest 再改這裡：
#   docker buildx imagetools inspect python:3.12-slim
FROM python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2
COPY --from=ghcr.io/astral-sh/uv:0.5.11@sha256:0ac957607303916420297a4c9c213bb33fbd3c888f9cd7f4f7273596ebf42b85 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app
# 用一般使用者執行：程式被入侵時，攻擊者在容器裡不會是 root。程式碼與套件仍屬 root、這個使用者只能讀
RUN useradd --system --no-create-home --uid 10001 app
USER app
EXPOSE 8000
# 只信任內網的反向代理（Caddy、共用機器上的既有代理）傳來的 X-Forwarded-For；
# 設成 * 的話 uvicorn 會取最左邊、可被偽造的 IP 當來源，IP 白名單就形同無效
# 直接執行 venv 裡的 uvicorn，不經過 uv run（它會嘗試同步環境、寫入快取，一般使用者沒有權限）
CMD [".venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"]
