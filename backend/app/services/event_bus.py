import asyncio
import json
import logging

from fastapi import WebSocket
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..models import ActivityLog
from ..notify.telegram import send_telegram

log = logging.getLogger(__name__)


class EventBus:
    """所有「機器人做了什麼」都經過這裡：寫入操作軌跡 + 即時推播到前端 + 選擇性通知。"""

    def __init__(self, session_factory: async_sessionmaker):
        self.session_factory = session_factory
        self.clients: set[WebSocket] = set()
        self._tasks: set[asyncio.Task] = set()
        # 其他元件想知道事件時註冊在這裡（async fn(type, data)），例如 MarketHub 要知道持倉變動
        self.listeners: list = []

    async def emit(
        self,
        type: str,
        data: dict | None = None,
        *,
        message: str | None = None,
        level: str = "info",
        category: str = "system",
        signal_id: int | None = None,
        order_id: int | None = None,
        notify: bool = False,
    ):
        log_entry = None
        if message:
            # 用獨立 session，就算呼叫端的交易 rollback，操作軌跡也會留下來
            async with self.session_factory() as s:
                entry = ActivityLog(
                    level=level, category=category, message=message, signal_id=signal_id, order_id=order_id
                )
                s.add(entry)
                await s.commit()
                log_entry = entry.to_dict()
            getattr(log, "warning" if level == "warn" else level)("[%s] %s", category, message)
        await self.broadcast({"type": type, "data": data, "log": log_entry})
        for fn in self.listeners:
            try:
                await fn(type, data)
            except Exception:
                log.exception("event listener failed")
        if message and (notify or level == "error"):
            task = asyncio.create_task(send_telegram(f"[{level.upper()}] {message}"))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def broadcast(self, payload: dict):
        text = json.dumps(payload, default=str)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)
