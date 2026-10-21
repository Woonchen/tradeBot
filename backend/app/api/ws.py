from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .deps import verify_token

router = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str | None = None):
    if not verify_token(token):
        await ws.close(code=4401)
        return
    await ws.accept()
    bus = ws.app.state.executor.bus
    bus.clients.add(ws)
    try:
        while True:
            await ws.receive_text()  # 前端只送 ping，忽略
    except WebSocketDisconnect:
        pass
    finally:
        bus.clients.discard(ws)
