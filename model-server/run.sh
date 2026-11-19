#!/usr/bin/env bash
# 한 줄 실행 (Brev GPU 와 Mac 백업 공통):   ./infra/run.sh
#   1) 파이썬 환경(.venv, 버전 고정)  2) 웨이트 확인/다운로드  3) 판정 서버(죽으면 3초 뒤 자동 재시작)
#   4) /health 대기  5) 공개 URL(cloudflared quick tunnel)  6) 로그를 따라 보여 줌. Ctrl-C 로 모두 종료
# 옵션(.env 또는 환경 변수): ZORO_PYTHON=기존 파이썬(설치 생략), ZORO_TUNNEL=quick|none, ZORO_FAKE=1(모델 없이 배관만)
# Brev 에서는 SSH 가 끊겨도 살아 있게 tmux 안에서 실행:  tmux new -s zoro './infra/run.sh'
set -euo pipefail
cd "$(dirname "$0")"
# .env 는 이미 설정된 환경 변수를 덮어쓰지 않는다(명령줄 값이 우선)
load_env() { [ -f .env ] || return 0; while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in ''|\#*) continue ;; esac; k="${line%%=*}"; [ -z "${!k+x}" ] && export "$line"; done < .env; return 0; }
load_env
mkdir -p logs bin
PORT="${ZORO_PORT:-30001}"
log() { echo "[run $(date +%H:%M:%S)] $*"; }

# ---- 1. 파이썬 ----
PY="${ZORO_PYTHON:-}"
if [ -z "$PY" ]; then
  PY=.venv/bin/python
  if [ ! -x "$PY" ]; then
    command -v uv >/dev/null || { curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; }
    uv venv -p 3.12 .venv
  fi
  if [ "${ZORO_FAKE:-0}" != "1" ] && ! "$PY" -c "import torch, transformers, PIL, huggingface_hub" 2>/dev/null; then
    log "installing requirements (torch 2.8.0, transformers 5.12.1) ..."
    # 드라이버가 CUDA 12.8 보다 낮으면: ZORO_TORCH_INDEX=https://download.pytorch.org/whl/cu126
    uv pip install --python "$PY" ${ZORO_TORCH_INDEX:+--index-url "$ZORO_TORCH_INDEX" --extra-index-url https://pypi.org/simple} -r requirements.txt
  fi
fi
if [ "${ZORO_FAKE:-0}" != "1" ]; then
  "$PY" - <<'PYEOF'
import platform, torch, transformers
cuda = torch.cuda.is_available()
mps = getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available()
print(f"[run] python {platform.python_version()} torch {torch.__version__} transformers {transformers.__version__} "
      f"cuda={cuda} {torch.cuda.get_device_name(0) if cuda else ''} mps={mps}")
if not (cuda or mps):
    print("[run] WARNING: GPU 없음 -> CPU 로 돌면 판정 1건에 수십 초. 데모에 쓰지 말 것")
PYEOF
  # ---- 2. 웨이트 ----
  HF_BIN="$(dirname "$PY")/hf" ./fetch_weights.sh
fi

# ---- 3. 서버 (자동 재시작) ----
PIDS=()
# 종료: 재시작 루프를 먼저 죽이고(그래야 다시 안 띄운다) 그다음 서버·터널을 죽인다
cleanup() { trap - EXIT INT TERM; log "stopping"
  for p in "${PIDS[@]}"; do kill -9 "$p" 2>/dev/null || true; done
  pkill -f "$PWD/server.py" 2>/dev/null || true
  pkill -f "tunnel --no-autoupdate --url http://127.0.0.1:$PORT" 2>/dev/null || true
  pkill -f "tail -n 0 -F logs/server.log" 2>/dev/null || true; exit 0; }
trap cleanup EXIT INT TERM
( while true; do "$PY" -u "$PWD/server.py" >> logs/server.log 2>&1 || true
    echo "[run $(date +%H:%M:%S)] server exited, restarting in 3s" >> logs/server.log; sleep 3; done ) &
PIDS+=($!)
log "server starting on :$PORT (log: infra/logs/server.log)"
# Mac 백업: 덮개를 닫거나 자리를 비워도 잠들지 않게
if [ "$(uname -s)" = Darwin ]; then caffeinate -dimsu -w $$ & PIDS+=($!); fi

# ---- 4. 헬스 대기 (로드 Mac 15초, GPU 첫 실행은 더 걸릴 수 있음) ----
for i in $(seq 1 300); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then break; fi
  sleep 2
  [ "$i" = 300 ] && { log "server not healthy after 600s"; tail -30 logs/server.log; exit 1; }
done
log "healthy: $(curl -fsS http://127.0.0.1:$PORT/health)"

# ---- 5. 공개 URL ----
if [ "${ZORO_TUNNEL:-quick}" = "quick" ]; then
  CF="$(command -v cloudflared || true)"
  if [ -z "$CF" ]; then
    case "$(uname -s)-$(uname -m)" in
      Darwin-*) brew install cloudflared && CF="$(command -v cloudflared)" ;;
      Linux-x86_64) curl -fsSL -o bin/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 && chmod +x bin/cloudflared && CF=bin/cloudflared ;;
      Linux-aarch64) curl -fsSL -o bin/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 && chmod +x bin/cloudflared && CF=bin/cloudflared ;;
    esac
  fi
  log "$("$CF" --version)"
  : > logs/tunnel.log
  # 재시작하면 URL 이 바뀐다 -> logs/public_url.txt 를 다시 읽어 앱 설정을 갱신할 것
  ( while true; do "$CF" tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" >> logs/tunnel.log 2>&1 || true
      echo "[run $(date +%H:%M:%S)] tunnel exited, restarting in 3s (URL WILL CHANGE)" >> logs/tunnel.log; sleep 3; done ) &
  PIDS+=($!)
  for i in $(seq 1 60); do
    URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' logs/tunnel.log | tail -1 || true)"
    [ -n "$URL" ] && break; sleep 1
  done
  if [ -n "${URL:-}" ]; then
    echo "$URL" > logs/public_url.txt
    for i in $(seq 1 20); do curl -fsS "$URL/health" >/dev/null 2>&1 && break; sleep 2; done
    log "PUBLIC URL: $URL   (health: $(curl -fsS "$URL/health" 2>/dev/null || echo 'not yet'))"
  else
    log "tunnel URL not found, see infra/logs/tunnel.log"
  fi
fi

log "ready. smoke test:  $PY infra/smoke.py http://127.0.0.1:$PORT"
tail -n 0 -F logs/server.log
