import asyncio
import json
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .deps import decode_token

router = APIRouter()

AUTH_TIMEOUT = 10  # 秒；連上後要在這段時間內送出登入訊息
UNAUTHORIZED = 4401  # 前端收到這個關閉碼會登出、不再重連


async def _authenticate(ws: WebSocket) -> dict | None:
    """第一則訊息必須是 {"op":"auth","token":"..."}。

    token 不放在網址（?token=）裡，因為網址會被反向代理和 uvicorn 的存取 log 記下來。
    """
    try:
        msg = json.loads(await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT))
    except (asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
        return None
    if not isinstance(msg, dict) or msg.get("op") != "auth":
        return None
    return decode_token(msg.get("token"))


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    """前端的即時通道。

    前端 → 伺服器（第一則）：{"op":"auth","token":"..."}；token 到期時伺服器以 4401 關閉連線
    伺服器 → 前端：系統事件、{"type":"prices"} 持倉即時價格、{"type":"kline"} 圖表 K 線
    前端 → 伺服器：{"op":"chart","symbol":"BTCUSDT","market":"futures","interval":"15m"} 訂閱圖表；
                   {"op":"chart","symbol":null} 取消；其他文字（ping）忽略
    """
    await ws.accept()
    claims = await _authenticate(ws)
    if not claims:
        await ws.close(code=UNAUTHORIZED)
        return
    expires = claims["exp"]
    bus = ws.app.state.executor.bus
    hub = ws.app.state.hub
    bus.clients.add(ws)
    await ws.send_text(json.dumps({"type": "stream_status", "data": hub.status, "log": None}))
    try:
        while True:
            try:
                text = await asyncio.wait_for(ws.receive_text(), max(expires - time.time(), 0))
            except asyncio.TimeoutError:  # token 過期：不能讓已開著的連線一直收到資料
                await ws.close(code=UNAUTHORIZED)
                break
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
