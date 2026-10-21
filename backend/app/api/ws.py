import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .deps import verify_token

router = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str | None = None):
    """前端的即時通道。

    伺服器 → 前端：系統事件、{"type":"prices"} 持倉即時價格、{"type":"kline"} 圖表 K 線
    前端 → 伺服器：{"op":"chart","symbol":"BTCUSDT","market":"futures","interval":"15m"} 訂閱圖表；
                   {"op":"chart","symbol":null} 取消；其他文字（ping）忽略
    """
    if not verify_token(token):
        await ws.close(code=4401)
        return
    await ws.accept()
    bus = ws.app.state.executor.bus
    hub = ws.app.state.hub
    bus.clients.add(ws)
    await ws.send_text(json.dumps({"type": "stream_status", "data": hub.status, "log": None}))
    try:
        while True:
            text = await ws.receive_text()
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict) or msg.get("op") != "chart":
                continue
            if not msg.get("symbol"):
                await hub.unwatch_chart(ws)
                continue
            try:
                symbol = await hub.watch_chart(ws, msg["symbol"], msg.get("market", "spot"), msg.get("interval", "1h"))
                reply = {"type": "chart_ok", "data": {**msg, "resolved": symbol}, "log": None}
            except Exception as e:
                reply = {"type": "chart_error", "data": {**msg, "error": str(e)}, "log": None}
            await ws.send_text(json.dumps(reply))
    except WebSocketDisconnect:
        pass
    finally:
        bus.clients.discard(ws)
        await hub.unwatch_chart(ws)
