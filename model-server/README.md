# 모델 서버 (GPU에서 Zoro-1과 GravityOCR-KO를 띄우는 코드)

이 폴더는 팀이 직접 만든 모델 서버와, GPU 장비에서 그 서버를 켜는 스크립트입니다. 직구레이더의 앱 서버(`nextask/`)는 이 서버에 HTTP로 요청을 보냅니다. 모델 가중치와 키는 이 저장소에 없습니다.

## 서버가 받는 요청 (`server.py`)

| 주소 | 하는 일 |
|---|---|
| `GET /health` | 서버가 살아 있는지, 모델을 불러왔는지 알려 줍니다. |
| `POST /v1/systemone` | 상태와 질문, 보기를 받아 Zoro-1이 고른 답과 보기별 확률을 돌려줍니다. |
| `POST /v1/decide` | 같은 판단을 다른 요청 형식으로 받습니다. |
| `POST /v1/generate` | Zoro-1이 문장을 씁니다. |
| `POST /v1/ocr` | GravityOCR-KO가 사진의 글자를 읽고 판독 확률을 함께 돌려줍니다. |

서버는 파이썬 표준 라이브러리의 HTTP 서버로 만들었습니다. 모델은 한 번만 불러오고 요청을 하나씩 차례로 처리합니다. 요청마다 입력과 출력을 `logs/` 아래 JSONL 파일에 남깁니다.

## 켜는 순서

1. `.env.example`을 `.env`로 복사하고 값을 채웁니다. Zoro-1은 접근 승인이 필요한 모델이라, Hugging Face에서 승인을 받은 계정의 읽기 토큰이 있어야 가중치를 받을 수 있습니다. `.env`는 저장소에 올리지 마세요.
2. `./run.sh`를 실행합니다. 파이썬 환경을 만들고, 가중치를 확인하거나 내려받고, 서버를 켠 뒤 접속 주소를 출력합니다.
3. 출력된 주소를 `ZORO_URL`로, 키를 정했다면 `ZORO_API_KEY`로 설정하고 저장소 맨 위 폴더에서 `JUDGE_PORT=8000 ./judge.sh live`를 실행합니다. 자세한 연결 방법은 [`submission/infra.md`](../submission/infra.md)에 있습니다.
4. `python smoke.py <주소>`로 서버가 응답하는지 확인할 수 있습니다.

## 파일

- `server.py`: 모델 서버. `safe_decider.py`: 서버가 모델을 불러올 때 쓰는 코드. `client.py`: 서버를 부르는 예시 클라이언트.
- `run.sh`: 한 번에 켜는 스크립트. `fetch_weights.sh`: 가중치 받기. `requirements.txt`: 필요한 패키지.
- `brev_up.sh`, `deploy_replica.sh`, `replica_boot.sh`, `stop_replicas.sh`: Brev에서 빌린 GPU 장비를 준비하고, 서버를 올리고, 끄는 스크립트.
- `smoke.py`, `batch.py`, `bench.py`, `pair_bench.py`, `s2_bench.py`, `ocr_compare.py`, `paddle_ocr.py`, `fleet_monitor.py`: 동작 확인, 속도 측정, OCR 비교, 장비 상태 감시에 쓴 스크립트.

## 우리가 쓴 장비와 한계

- Brev에서 빌린 NVIDIA L4와 L40S에서 실행했습니다.
- 이 스크립트들은 우리 환경에서 쓴 그대로입니다. 다른 계정이나 다른 장비에서 처음부터 끝까지 다시 세워 본 적은 없습니다.
- 이 폴더는 제출 마감 시각(2026-11-20 09:00)에 맞춰 마지막으로 추가했습니다. 앱 코드(`nextask/`)는 바꾸지 않았습니다.
