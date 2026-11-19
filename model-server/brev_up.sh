#!/usr/bin/env bash
# Mac 에서 실행: 정지된 Brev 인스턴스를 켜고, 최신 infra 코드를 올리고, 서버 + 공개 주소(cloudflared)를 띄운다.
#   ./infra/brev_up.sh [인스턴스=zoro1]
# 끝나면 공개 주소를 출력하고 infra/logs-brev/public_url.txt 에 저장한다. 시작 시각은 비용 장부에 적을 것.
set -euo pipefail
cd "$(dirname "$0")"
INST="${1:-zoro1}"
echo "[up $(date +%H:%M:%S)] brev start $INST"
brev start "$INST" >/dev/null 2>&1 || true
for i in $(seq 1 90); do
  L="$(brev ls 2>/dev/null | grep -w "$INST" || true)"
  echo "$L" | grep -q "RUNNING" && echo "$L" | grep -q " READY" && ! echo "$L" | grep -q "NOT READY" && break
  [ $((i % 6)) = 0 ] && echo "[up $(date +%H:%M:%S)] $(echo "$L" | tr -s ' ')"
  sleep 10
done
brev refresh >/dev/null 2>&1 || true
echo "[up $(date +%H:%M:%S)] ready, copying code"
SSH="ssh -o ConnectTimeout=20 -o StrictHostKeyChecking=no $INST"
for i in 1 2 3 4 5 6; do $SSH true 2>/dev/null && break; sleep 10; done
$SSH "mkdir -p ~/infra" 2>/dev/null || true
scp -q -o StrictHostKeyChecking=no s2_bench.py server.py safe_decider.py client.py smoke.py bench.py batch.py pair_bench.py ocr_compare.py \
  paddle_ocr.py run.sh fetch_weights.sh requirements.txt .env "$INST:/home/ubuntu/infra/"
$SSH 'cd ~/infra && tmux kill-session -t zoro 2>/dev/null; pkill -f server.py; pkill -f "cloudflared tunnel"; rm -f logs/public_url.txt
      ZORO_TUNNEL=quick tmux new -d -s zoro "./run.sh 2>&1 | tee -a run.out"' 2>/dev/null
for i in $(seq 1 60); do
  U="$($SSH 'cat ~/infra/logs/public_url.txt 2>/dev/null' 2>/dev/null || true)"
  H="$($SSH 'curl -s -m 3 localhost:30001/health' 2>/dev/null || true)"
  echo "$H" | grep -q '"status": "ok"' && [ -n "$U" ] && break
  sleep 10
done
mkdir -p logs-brev; echo "$U" > logs-brev/public_url.txt
echo "[up $(date +%H:%M:%S)] health: $H"
echo "[up $(date +%H:%M:%S)] PUBLIC URL: $U"
curl -s -m 15 "$U/health" >/dev/null && echo "[up] public health ok" || echo "[up] public health FAILED (터널 확인: ssh $INST 'tail ~/infra/logs/tunnel.log')"
