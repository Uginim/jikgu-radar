# 세 줄로 시작하기

1. 이 저장소를 내려받아 압축을 풀고 터미널에서 `README.md`가 있는 폴더로 이동한다. Python 3.12 이상과 `uv` 또는 `pip`가 필요하다. 처음 한 번 의존성을 설치할 때 인터넷이 필요하다.
2. `./judge.sh`를 실행한다. 세 상품을 새 사건으로 검사하고 결과 주소를 출력한다. GPU·키·판매자 계정은 필요 없다. 기존 `.env`는 읽지 않는다. 설치된 `chromium`이 있으면 전용 프로필로 확장과 결과를 연다. Chrome for Testing은 실행 파일 경로를 `JUDGE_CHROME_BIN`으로 지정할 수 있다.
3. 창이 자동으로 열리지 않으면 Chrome에서 `chrome://extensions` → 개발자 모드 → **압축해제된 확장 프로그램을 로드** → `submission/runtime/extension/`을 선택한다. 터미널의 로컬 주소에서 새 사건 로그 3개를 연다. 종료는 `Ctrl+C`(새 사건과 임시 장부도 삭제됨). 창 없이 결과만 검증하려면 `./judge.sh check`.

## 평소 쓰는 Chrome에 설치하기

1. 제출 폴더를 계속 보관할 위치에 풀고, 평소 Chrome 주소창에 `chrome://extensions`를 입력한 뒤 **개발자 모드**를 켠다.
2. **압축해제된 확장 프로그램을 로드**를 눌러 `manifest.json`이 들어 있는 `submission/runtime/extension/` 폴더를 선택한다. 퍼즐 아이콘에서 **직구레이더**를 고정한다.
3. 서버를 켜고 상품 탭에서 확장 아이콘을 누른다. **작업 설정 → 서버**에 터미널의 로컬 주소를 넣는다. `./judge.sh`는 촬영 사례 재생용이며 새 상품·사진은 [실제 GPU 연결](submission/infra.md)로 검사한다.

[영상·로그부터 보기](submission/README.md) · [실제 GPU 연결](submission/infra.md)
