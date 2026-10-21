import logging

import httpx

from ..config import settings

log = logging.getLogger(__name__)


async def send_telegram(text: str):
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={"chat_id": settings.telegram_chat_id, "text": text})
    except httpx.HTTPError as e:
        log.warning("telegram send failed: %s", e)
