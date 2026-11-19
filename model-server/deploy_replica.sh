#!/usr/bin/env bash
# Runs on the operator machine; only authorized replicas/replacement are accepted.
# Never creates/restarts the original server, runs a local model, or prints .env.
set -euo pipefail
cd "$(dirname "$0")"
INST="${1:?usage: deploy_replica.sh zoro3|zoro4|zoro5}"
case "$INST" in
  zoro3) ROLE=decision-vs-generation ;;
  zoro4) ROLE=agent-vs-fixed ;;
  zoro5) ROLE=l40s-role2 ;;
  *) echo 'Only zoro3, zoro4 and zoro5 are authorized replicas' >&2; exit 2 ;;
esac
DEADLINE=$(date -j -f '%Y-%m-%d %H:%M:%S %z' '2026-11-20 14:00:00 +0900' '+%s')
check_deadline() { [ "$(date '+%s')" -lt "$DEADLINE" ] || { echo 'Stop boundary reached; deployment aborted' >&2; exit 1; }; }
SSH=(ssh -T -o BatchMode=yes -o ConnectTimeout=10 "$INST")
echo "$INST: waiting for provisioned SSH (at most 90 attempts, bounded by stop time)"
CONNECTED=0
for attempt in $(seq 1 90); do
  check_deadline
  # VM assignment can finish after the first refresh; refresh missing SSH routes.
  if (( attempt % 3 == 1 )); then brev refresh >/dev/null; fi
  if "${SSH[@]}" true >/dev/null 2>&1; then CONNECTED=1; break; fi
  sleep 10
done
[ "$CONNECTED" = 1 ] || { echo "$INST: SSH not ready; no model launched" >&2; exit 1; }
# Install/confirm the stop guard before transferring credentials or starting setup.
"${SSH[@]}" 'set -e; if ! systemctl is-active --quiet zoro-replica-stop.timer; then sudo -n systemd-run --unit=zoro-replica-stop --on-calendar="2026-11-20 05:00:00 UTC" /usr/sbin/shutdown -h now; fi; systemctl list-timers zoro-replica-stop.timer --no-pager; mkdir -p ~/infra/logs'
scp -q server.py safe_decider.py run.sh fetch_weights.sh requirements.txt .env "$INST:/home/ubuntu/infra/"
"${SSH[@]}" "set -e; cd ~/infra; chmod 600 .env; chmod +x run.sh fetch_weights.sh; tmux new-session -d -s zoro 'cd /home/ubuntu/infra && export PATH=\"\$HOME/.local/bin:\$PATH\" && ZORO_SERVER_NAME=$INST-$ROLE ZORO_ALLOW_REASONING=1 ZORO_REASONING_MAX_TOKENS=1024 ZORO_OCR=1 ZORO_MODEL_DIR=/home/ubuntu/infra/weights/zoro1 ZORO_OCR_DIR=/home/ubuntu/infra/weights/gravityocr-ko bash run.sh >logs/bootstrap.log 2>&1'"
echo "$INST: installing pinned environment/checkpoint; waiting for health and tunnel"
for _ in $(seq 1 120); do
  check_deadline
  if "${SSH[@]}" 'python3 - <<'"'"'PY'"'"'
import json,pathlib,urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:30001/health", timeout=5) as r:
        h=json.load(r)
    p=pathlib.Path.home()/"infra/logs/public_url.txt"
    if h.get("status")!="ok" or not p.exists():
        raise RuntimeError("not ready")
    url=p.read_text().strip()
    with urllib.request.urlopen(url+"/health", timeout=10) as r:
        public=json.load(r)
    assert public.get("status")=="ok" and not public.get("fake")
    assert public.get("reasoning_contract")=="completion-checked-v1"
    print(json.dumps({"url":url,"health":public}))
except Exception:
    raise SystemExit(1)
PY' 2>/dev/null; then
    echo "$INST: service ready (inference smoke still required)"
    exit 0
  fi
  sleep 10
done
echo "$INST: setup readiness deadline exceeded; stop guard remains active" >&2
exit 1
