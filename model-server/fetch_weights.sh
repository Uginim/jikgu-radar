#!/usr/bin/env bash
# 웨이트를 고정 리비전으로 준비한다. 이미 있으면 건너뛴다.
#   HF 에서: HF_TOKEN 필요(gated). Brev 데이터센터 회선이라 9.1GB 가 몇 분이면 끝난다(미실측).
#   Mac 에서 올리기(HF 가 막힐 때): rsync -av --progress <Mac 웨이트 경로>/ <brev 인스턴스>:~/zoro-infra/infra/weights/zoro1/
set -euo pipefail
cd "$(dirname "$0")"
# .env 는 이미 설정된 환경 변수를 덮어쓰지 않는다(명령줄 값이 우선)
load_env() { [ -f .env ] || return 0; while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in ''|\#*) continue ;; esac; k="${line%%=*}"; [ -z "${!k+x}" ] && export "$line"; done < .env; return 0; }
load_env
DIR="${ZORO_MODEL_DIR:-$PWD/weights/zoro1}"
REV="${ZORO_REVISION:-bf1b76b9b4df03b05fcbb73444e1cd4db0f566da}"
# OCR(GravityOCR-KO, MIT, 2.2GB, 공개 저장소라 토큰 불필요) — ZORO_OCR=1 일 때만
if [ "${ZORO_OCR:-0}" = "1" ]; then
  ODIR="${ZORO_OCR_DIR:-$PWD/weights/gravityocr-ko}"
  if [ ! -f "$ODIR/model.safetensors" ]; then
    echo "[weights] downloading trillionlabs/GravityOCR-KO@${ZORO_OCR_REVISION:-6c749d4} -> $ODIR"
    "${HF_BIN:-.venv/bin/hf}" download trillionlabs/GravityOCR-KO --revision "${ZORO_OCR_REVISION:-6c749d417879037da8a1f6af4cf9aaa7632308bc}" --local-dir "$ODIR"
  fi
  [ -f "$ODIR/model.safetensors" ] && echo "[weights] ocr ok: $ODIR"
fi
shards() { ls "$DIR"/model-000*.safetensors 2>/dev/null | wc -l | tr -d ' '; }
if [ -f "$DIR/decision_head.pt" ] && [ -f "$DIR/zoro_decide.py" ] && [ "$(shards)" = "17" ]; then
  echo "[weights] ok: $DIR (17 shards)"; exit 0
fi
[ -n "${HF_TOKEN:-}" ] || { echo "[weights] HF_TOKEN 이 없습니다 (.env). 또는 Mac 에서 rsync 로 올리세요"; exit 1; }
mkdir -p "$DIR"
echo "[weights] downloading trillionlabs/Zoro-1@$REV -> $DIR"
for i in 1 2 3; do
  if "${HF_BIN:-.venv/bin/hf}" download trillionlabs/Zoro-1 --revision "$REV" --local-dir "$DIR"; then break; fi
  echo "[weights] retry $i"; sleep 5
done
[ "$(shards)" = "17" ] && [ -f "$DIR/decision_head.pt" ] || { echo "[weights] incomplete download"; exit 1; }
echo "[weights] ok"
