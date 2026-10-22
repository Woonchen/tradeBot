#!/usr/bin/env bash
# 每日資料庫備份：pg_dump → 確認備份檔讀得回來 → 上傳 Cloud Storage → 結果寫進儀表板的操作軌跡。
#
# 由 systemd timer 每天執行（安裝：sudo ~/tradebot/deploy/install-backup.sh），也可以手動跑：
#   sudo systemctl start tradebot-backup.service
#
# bucket 名稱放在 ~/tradebot/.env 的 BACKUP_BUCKET。VM 對這個 bucket 只有「建立」權限，
# 讀不到也刪不掉既有備份，所以就算 VM 被入侵，備份也不會被清掉；舊備份由 bucket 的生命週期規則刪除。
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
db=tradebot-postgres-1

# 把結果寫進 activity_logs，儀表板「操作軌跡」看得到；資料庫掛了就算了，不影響結束碼
activity() { # activity <level> <message>
	printf "INSERT INTO activity_logs (ts, level, category, message) VALUES (now(), :'lvl', 'system', :'msg');\n" |
		docker exec -i "$db" psql -q -U tradebot -d tradebot -v ON_ERROR_STOP=1 -v lvl="$1" -v msg="$2" >/dev/null 2>&1 || true
}

on_exit() {
	local status=$?
	rm -f "${dump:-}"
	if [[ $status -ne 0 ]]; then
		echo "備份失敗（結束碼 ${status}）" >&2
		activity error "資料庫備份失敗（結束碼 ${status}），請到 VM 執行 journalctl -u tradebot-backup.service 查看原因"
	fi
}
trap on_exit EXIT

bucket="$(sed -n 's/^BACKUP_BUCKET=//p' "$root/.env" | tail -n 1)"
if [[ -z "$bucket" ]]; then
	echo "錯誤：$root/.env 沒有設定 BACKUP_BUCKET" >&2
	exit 1
fi

# 備份檔含完整交易紀錄，只有 root 讀得到
umask 077
dump="$(mktemp /var/tmp/tradebot-db.XXXXXX.dump)"
# -Fc：PostgreSQL 自訂格式，有壓縮，還原時可以只挑部分資料表
docker exec "$db" pg_dump -U tradebot -d tradebot -Fc >"$dump"

# 上傳前先確認 pg_restore 讀得懂，避免存了一堆壞掉的備份還不知道
# 清單裡每張表會有「TABLE」（結構）和「TABLE DATA」（資料）兩行，只算結構那行
tables="$(docker exec -i "$db" pg_restore --list <"$dump" | grep ' TABLE ' | grep -vc ' TABLE DATA ' || true)"
if [[ ! -s "$dump" || "$tables" -lt 1 ]]; then
	echo "錯誤：備份檔是空的或讀不出資料表（$(stat -c %s "$dump") bytes）" >&2
	exit 1
fi

# 直接呼叫 Cloud Storage API 上傳，不用 gcloud storage cp：gcloud 上傳前會先讀取目標物件，
# 需要讀取權限，但 VM 刻意只有「建立」權限。ifGenerationMatch=0 表示只能新增、不會覆蓋既有備份。
token="$(curl -fsS -H "Metadata-Flavor: Google" \
	http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token |
	python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])')"
name="$(TZ=Asia/Taipei date +%Y/%m/tradebot-%Y%m%d-%H%M%S).dump"
encoded="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$name")"
curl -fsS -o /dev/null -X POST \
	-H "Authorization: Bearer $token" \
	-H "Content-Type: application/octet-stream" \
	--data-binary "@$dump" \
	"https://storage.googleapis.com/upload/storage/v1/b/$bucket/o?uploadType=media&ifGenerationMatch=0&name=$encoded"

size="$(stat -c %s "$dump")"
echo "備份完成：gs://$bucket/$name（$size bytes，$tables 張資料表）"
activity info "資料庫備份完成：gs://$bucket/$name（$((size / 1024)) KB，$tables 張資料表）"
