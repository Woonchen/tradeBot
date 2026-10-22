"""TOTP 兩步驟驗證（RFC 6238）。

跟 Google Authenticator、1Password 等驗證器 App 相容：HMAC-SHA1、6 位數、每 30 秒換一次。
"""

import base64
import hashlib
import hmac
import struct
import time

STEP = 30
DIGITS = 6


def decode_secret(secret: str) -> bytes:
    """Base32 金鑰（驗證器 App 顯示的那串）→ bytes；格式不對時丟 ValueError。"""
    s = secret.strip().replace(" ", "").upper()
    try:
        key = base64.b32decode(s + "=" * (-len(s) % 8))
    except ValueError as e:  # binascii.Error 也是 ValueError
        raise ValueError("DASHBOARD_TOTP_SECRET 不是有效的 Base32 金鑰") from e
    if len(key) < 10:
        raise ValueError("DASHBOARD_TOTP_SECRET 太短，至少要 80 bits（16 個 Base32 字元）")
    return key


def code_at(key: bytes, counter: int, digits: int = DIGITS, digest=hashlib.sha1) -> str:
    mac = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    offset = mac[-1] & 0x0F
    n = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(n % 10**digits).zfill(digits)


def match(secret: str, code: str, now: float | None = None, window: int = 1) -> int | None:
    """驗證碼正確時回傳它所屬的時間步（用來防止同一組碼被重複使用），否則 None。

    允許前後各 window 步（±30 秒）的時鐘誤差。
    """
    code = code.strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    key = decode_secret(secret)
    current = int((time.time() if now is None else now) // STEP)
    found = None
    for counter in range(current - window, current + window + 1):  # 全部比完，不提早結束，避免時間差洩漏資訊
        if hmac.compare_digest(code_at(key, counter), code):
            found = counter
    return found
