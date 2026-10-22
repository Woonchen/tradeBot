#!/usr/bin/env bash
# 把目前的工作目錄部署到 GCP VM，並接在 VM 上既有的反向代理後面。
# 會打包 git 有追蹤或可追蹤的檔案（含尚未 commit 的修改），.gitignore 排除的（.env、node_modules…）不會上傳。
#
#   ./deploy/gcp-deploy.sh
#
# VM 上的 ~/tradebot/.env 需要先準備好（不會被覆蓋）。
# 可用環境變數覆寫：VM、ZONE、REMOTE_DIR、EDGE_NETWORK
set -euo pipefail

VM="${VM:-zushum-prod}"
ZONE="${ZONE:-asia-east1-c}"
REMOTE_DIR="${REMOTE_DIR:-tradebot}"
EDGE_NETWORK="${EDGE_NETWORK:-zushun_default}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SSH=(gcloud compute ssh "$VM" --zone "$ZONE" --tunnel-through-iap --command)

if [[ -n "$(git -C "$ROOT" status --porcelain)" ]]; then
  echo "! 注意：包含尚未 commit 的修改（HEAD $(git -C "$ROOT" rev-parse --short HEAD) + 工作目錄變更）"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# macOS 的 tar 預設會把檔案屬性一起打包：COPYFILE_DISABLE 不產生 ._ 開頭的附帶檔，--no-xattrs 不寫延伸屬性
(cd "$ROOT" && git ls-files -z --cached --others --exclude-standard |
  COPYFILE_DISABLE=1 tar --no-xattrs --null -T - -czf "$TMP/tradebot.tar.gz")

echo "▶ 上傳到 $VM …"
gcloud compute scp --zone "$ZONE" --tunnel-through-iap "$TMP/tradebot.tar.gz" "$VM:/tmp/tradebot.tar.gz"

echo "▶ 在 $VM 上建置並啟動 …"
"${SSH[@]}" "set -e
  mkdir -p ~/$REMOTE_DIR
  tar -xzf /tmp/tradebot.tar.gz -C ~/$REMOTE_DIR && rm /tmp/tradebot.tar.gz
  cd ~/$REMOTE_DIR
  test -f .env || { echo '✕ 缺少 ~/$REMOTE_DIR/.env，請先建立'; exit 1; }
  EDGE_NETWORK=$EDGE_NETWORK docker compose -p tradebot -f docker-compose.yml -f docker-compose.proxy.yml up -d --build --remove-orphans
  docker compose -p tradebot ps"
echo "✓ 部署完成"
