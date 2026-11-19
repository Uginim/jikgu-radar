#!/usr/bin/env bash
# Judge only: verify submission/release.json, run its frozen code, replay the filmed requests.
set -euo pipefail
cd "$(dirname "$0")"
mode=${1:-replay}
case "$mode" in replay|check|live) ;; *) printf '사용법: ./judge.sh [replay|check|live]\n' >&2; exit 2 ;; esac
if [[ "$mode" == live && -z "${ZORO_URL:-}" ]]; then printf '실제 GPU 주소가 필요합니다: ZORO_URL을 먼저 설정하세요.\n' >&2; exit 2; fi
venv=.judge-cache/venv
mkdir -p .judge-cache
if command -v uv >/dev/null 2>&1; then
    [[ -x "$venv/bin/python" ]] || uv venv --python 3.12 "$venv"
    uv pip install --python "$venv/bin/python" -r requirements.txt
else
    python=${JUDGE_PYTHON:-python3}
    "$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else "Python 3.12 이상 또는 uv가 필요합니다.")'
    [[ -x "$venv/bin/python" ]] || "$python" -m venv "$venv"
    "$venv/bin/python" -m pip install --disable-pip-version-check -r requirements.txt
fi
args=("$mode"); [[ -z "${JUDGE_PORT:-}" ]] || args+=(--port "$JUDGE_PORT")
exec "$venv/bin/python" -B submission/tools/judge.py "${args[@]}"
