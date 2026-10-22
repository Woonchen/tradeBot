FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app
EXPOSE 8000
# 只信任內網的反向代理（Caddy、共用機器上的既有代理）傳來的 X-Forwarded-For；
# 設成 * 的話 uvicorn 會取最左邊、可被偽造的 IP 當來源，IP 白名單就形同無效
CMD ["uv", "run", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"]
