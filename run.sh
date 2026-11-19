#!/usr/bin/env bash
# 한 줄 실행: ./run.sh            → 직구레이더 API http://127.0.0.1:8000 (.env 적용, 없으면 mock)
#             ./run.sh scenarios  → 시나리오 실행 로그 생성(사용자 장부는 변경하지 않음)
# 화면은 크롬 확장(extension/): chrome://extensions → 개발자 모드 → '압축해제된 확장 프로그램 로드' → extension 폴더
#   → 아이허브·아마존 상품 페이지에서 툴바 아이콘 → 사이드 패널. 패널의 서버 칸 기본값 http://127.0.0.1:8000
# 원격 판정 서버를 쓰려면 .env 에 ZORO_MODE=remote, ZORO_URL=<판정 서버 URL>
set -euo pipefail
cd "$(dirname "$0")"
case "${1:-web}" in
  -h|--help)
    printf '%s\n' '직구레이더: ./run.sh [web|scenarios [입력.json] [-o 로그.md]]' \
      '.env.example을 .env로 복사해 모드·서버·키 설정. 기본은 GPU 없는 mock.' \
      '확장: chrome://extensions → 개발자 모드 → extension/ 압축해제 로드.' \
      'remote/record는 GPU 서버가 필요. HTTP 성공만으로 S2 완료로 간주하지 않음.'
    exit 0 ;;
  web|scenarios) ;;
  *) printf '알 수 없는 명령: %s\n' "$1" >&2; exit 2 ;;
esac
[ -f .env ] && { set -a; . ./.env; set +a; }
if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null; then uv venv -q --python 3.12 .venv; else python3 -m venv .venv; fi
fi
if command -v uv >/dev/null; then uv pip install -q --python .venv/bin/python -r requirements.txt
else .venv/bin/python -m pip install -q -r requirements.txt; fi
[ -d data/samples/dsld ] || .venv/bin/python scripts/make_samples.py
if [ "${1:-web}" = "scenarios" ]; then shift; exec .venv/bin/python scripts/run_scenarios.py "$@"; fi
exec .venv/bin/python -m uvicorn nextask.core.web.app:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
