#!/usr/bin/env bash
# 在 VM 上安裝每日資料庫備份排程（只要做一次；之後部署更新 backup.sh 不用重裝）：
#   sudo ~/tradebot/deploy/install-backup.sh
#
# 事前準備（在有 GCP 權限的電腦上做一次）：
#   1. 建立 bucket（禁止公開、30 天後自動刪除舊備份）
#   2. 只給 VM 的 service account「建立物件」權限（roles/storage.objectCreator）
#   3. ~/tradebot/.env 加上 BACKUP_BUCKET=<bucket 名稱>
# 詳見 README 的「資料庫備份」。
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
	echo "請用 sudo 執行" >&2
	exit 1
fi
script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/backup.sh"
chmod +x "$script"

cat >/etc/systemd/system/tradebot-backup.service <<UNIT
[Unit]
Description=TradeBot 資料庫備份（pg_dump 上傳 Cloud Storage）
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
Environment=PATH=/snap/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=$script
UNIT

cat >/etc/systemd/system/tradebot-backup.timer <<'UNIT'
[Unit]
Description=每天台灣時間 03:30 備份 TradeBot 資料庫

[Timer]
# 03:30 台灣時間 = 19:30 UTC；同一台 VM 上其他專案的備份在 03:00，錯開避免同時跑
OnCalendar=*-*-* 19:30:00 UTC
# VM 剛好關機錯過的話，開機後補跑一次
Persistent=true
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now tradebot-backup.timer
systemctl list-timers tradebot-backup.timer --no-pager
echo "已安裝。立刻手動備份一次：sudo systemctl start tradebot-backup.service"
echo "查看紀錄：journalctl -u tradebot-backup.service -n 20 --no-pager"
