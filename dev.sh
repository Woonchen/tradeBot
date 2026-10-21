#!/usr/bin/env bash
# 一鍵啟動前後端（開發模式）。按 Ctrl+C 會同時關掉兩個。
#
#   ./dev.sh            啟動並自動打開瀏覽器
#   ./dev.sh --no-open  啟動但不打開瀏覽器
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
OPEN_BROWSER=1
[[ "${1:-}" == "--no-open" ]] && OPEN_BROWSER=0

c_blue=$'\033[34m'; c_green=$'\033[32m'; c_yellow=$'\033[33m'; c_red=$'\033[31m'; c_off=$'\033[0m'
info() { echo "${c_green}▶${c_off} $*"; }
warn() { echo "${c_yellow}!${c_off} $*"; }
die()  { echo "${c_red}✕${c_off} $*" >&2; exit 1; }

# ---- 檢查工具 ----
command -v uv  >/dev/null || die "找不到 uv，請先安裝：curl -LsSf https://astral.sh/uv/install.sh | sh"
command -v npm >/dev/null || die "找不到 npm，請先安裝 Node.js 20 以上版本"

# ---- .env ----
if [[ ! -f "$ROOT/.env" ]]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  warn "沒有 .env，已從 .env.example 複製一份（登入帳號 admin / 密碼 change-me，記得修改）"
fi

# ---- 檢查 port ----
for port in "$BACKEND_PORT" "$FRONTEND_PORT"; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    die "port $port 已被佔用：$(lsof -nP -iTCP:"$port" -sTCP:LISTEN | awk 'NR==2{print $1" (pid "$2")"}')，請先關掉它"
  fi
done

# ---- 安裝相依套件（只有缺少或有變更時才會真的安裝）----
info "檢查後端套件…"
(cd "$ROOT/backend" && uv sync --quiet)
if [[ ! -d "$ROOT/frontend/node_modules" || "$ROOT/frontend/package-lock.json" -nt "$ROOT/frontend/node_modules" ]]; then
  info "安裝前端套件…"
  (cd "$ROOT/frontend" && npm install --no-fund --no-audit)
fi

# ---- 啟動 ----
pids=()
cleanup() {
  trap - INT TERM EXIT
  echo
  info "關閉中…"
  # 每個服務都在自己的 process group（見 set -m），整組一起關，才不會留下 uvicorn worker 或 vite
  for pid in "${pids[@]}"; do kill -TERM -- "-$pid" 2>/dev/null || true; done
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    alive=0
    for pid in "${pids[@]}"; do kill -0 -- "-$pid" 2>/dev/null && alive=1; done
    [[ $alive == 0 ]] && break
    sleep 0.5
  done
  for pid in "${pids[@]}"; do kill -KILL -- "-$pid" 2>/dev/null || true; done
  info "已關閉"
}
trap cleanup EXIT
trap 'exit 0' INT TERM   # 收到 Ctrl+C 就結束，交給 EXIT trap 收尾

# 讓每個背景服務有自己的 process group，方便關閉時整組結束
set -m

# 在每一行輸出前加上 [backend] / [frontend] 前綴
prefix() { sed -u "s/^/$1 /"; }

info "啟動後端  http://localhost:$BACKEND_PORT"
(cd "$ROOT/backend" && exec uv run uvicorn app.main:app --reload --port "$BACKEND_PORT" </dev/null 2>&1 | prefix "${c_blue}[backend]${c_off}") &
pids+=($!)
disown  # 移出 bash 的 job 表，關閉時就不會印出 "Terminated" 訊息

info "啟動前端  http://localhost:$FRONTEND_PORT"
(cd "$ROOT/frontend" && BACKEND_URL="http://127.0.0.1:$BACKEND_PORT" exec npx vite --port "$FRONTEND_PORT" --strictPort </dev/null 2>&1 | prefix "${c_green}[frontend]${c_off}") &
pids+=($!)
disown

# 等後端可以連線
for _ in $(seq 1 60); do
  curl -sf "http://127.0.0.1:$BACKEND_PORT/health" >/dev/null 2>&1 && break
  sleep 0.5
done
if curl -sf "http://127.0.0.1:$BACKEND_PORT/health" >/dev/null 2>&1; then
  info "後端就緒 ✓   儀表板：http://localhost:$FRONTEND_PORT   （Ctrl+C 結束）"
  if [[ $OPEN_BROWSER == 1 ]] && command -v open >/dev/null; then
    open "http://localhost:$FRONTEND_PORT"
  fi
else
  warn "後端 30 秒內沒有回應，請看上面 [backend] 的錯誤訊息"
fi

# 任何一邊掛掉就一起結束
while true; do
  for pid in "${pids[@]}"; do
    if ! kill -0 -- "-$pid" 2>/dev/null; then
      warn "有一個服務停止了，一併關閉另一個"
      exit 1
    fi
  done
  sleep 1
done
