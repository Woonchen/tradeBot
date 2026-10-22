#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["qrcode[pil]>=7.4,<9"]
# ///
"""設定儀表板的兩步驟驗證（在你自己的電腦上執行，需要已登入 gcloud）。

    uv run deploy/setup-2fa.py             # 產生新金鑰、寫進 VM 的 .env、重啟後端
    uv run deploy/setup-2fa.py --disable   # 關閉兩步驟驗證（例如手機遺失，之後再重新設定）

金鑰不會印在終端機上：QR code 和手動輸入用的金鑰存成桌面上的一張圖片，用驗證器 App 掃完請刪除。
重新設定或關閉後，所有已登入的裝置都要重新登入。
可用環境變數覆寫：VM、ZONE、REMOTE_DIR、EDGE_NETWORK（同 deploy/gcp-deploy.sh）
"""

import argparse
import base64
import os
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

VM = os.environ.get("VM", "zushum-prod")
ZONE = os.environ.get("ZONE", "asia-east1-c")
REMOTE_DIR = os.environ.get("REMOTE_DIR", "tradebot")
EDGE_NETWORK = os.environ.get("EDGE_NETWORK", "zushun_default")

# 從 stdin 讀金鑰（不放在指令列，免得出現在 VM 的 process list 或 shell 歷史），空字串 = 關閉
REMOTE = f"""set -euo pipefail
cd ~/{REMOTE_DIR}
IFS= read -r secret || true
cp -p .env ".env.bak.$(date +%Y%m%d%H%M%S)"
umask 077
grep -v '^DASHBOARD_TOTP_SECRET=' .env > .env.tmp || true
if [ -n "$secret" ]; then printf 'DASHBOARD_TOTP_SECRET=%s\\n' "$secret" >> .env.tmp; fi
mv .env.tmp .env
EDGE_NETWORK={EDGE_NETWORK} docker compose -p tradebot -f docker-compose.yml -f docker-compose.proxy.yml up -d backend >/dev/null 2>&1
for i in $(seq 30); do
  sleep 1
  if docker logs --since 40s tradebot-backend-1 2>&1 | grep -q 'Application startup complete'; then echo started; exit 0; fi
done
echo '後端沒有在 30 秒內啟動，請檢查：docker logs tradebot-backend-1' >&2
exit 1
"""


def push(secret: str):
    cmd = ["gcloud", "compute", "ssh", VM, "--zone", ZONE, "--tunnel-through-iap", "--command", REMOTE]
    r = subprocess.run(cmd, input=secret + "\n", text=True, capture_output=True)
    if r.returncode != 0 or "started" not in r.stdout:
        sys.exit(f"✕ 更新 VM 失敗：\n{r.stderr.strip()[-800:]}")


def save_qr(uri: str, secret: str, path: Path):
    import qrcode
    from PIL import Image, ImageDraw, ImageFont

    qr = qrcode.make(uri, box_size=8, border=3).get_image().convert("RGB")
    try:
        font = ImageFont.load_default(size=22)
    except TypeError:  # Pillow < 10.1 沒有 size 參數
        font = ImageFont.load_default()
    grouped = " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))
    lines = ["TradeBot 2FA", "Manual entry key:", grouped, "Delete this image after scanning."]
    img = Image.new("RGB", (max(qr.width, 640), qr.height + 40 * len(lines) + 20), "white")
    img.paste(qr, ((img.width - qr.width) // 2, 0))
    draw = ImageDraw.Draw(img)
    for i, text in enumerate(lines):
        draw.text((20, qr.height + 10 + 40 * i), text, fill="black", font=font)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # 只有自己讀得到
    with os.fdopen(fd, "wb") as f:
        img.save(f, format="PNG")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--disable", action="store_true", help="關閉兩步驟驗證")
    ap.add_argument("--user", default="admin", help="顯示在驗證器 App 裡的帳號名稱（預設 admin）")
    ap.add_argument("--out", type=Path, default=Path.home() / "Desktop" / "tradebot-2fa.png")
    args = ap.parse_args()

    if args.disable:
        push("")
        print("✓ 已關閉兩步驟驗證，後端已重啟；所有裝置需要重新登入（只要帳號密碼）")
        return

    secret = base64.b32encode(secrets.token_bytes(20)).decode()  # 160 bits，驗證器 App 的標準長度
    uri = f"otpauth://totp/{quote('TradeBot:' + args.user)}?secret={secret}&issuer=TradeBot&digits=6&period=30"
    save_qr(uri, secret, args.out)  # 先存圖：VM 更新失敗也不會留下一個沒人有 QR code 的金鑰
    push(secret)
    print(f"✓ 已啟用兩步驟驗證，後端已重啟\n  QR code：{args.out}\n"
          "  用驗證器 App 掃描後請刪除這張圖片；所有裝置需要重新登入（帳號 + 密碼 + 6 位數驗證碼）")


if __name__ == "__main__":
    main()
