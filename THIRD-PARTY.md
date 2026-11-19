# 외부 모델·글꼴·데이터의 출처와 이용 조건

직구레이더 자체 코드의 [LICENSE](LICENSE)는 **Apache License, Version 2.0**이다. 아래 모델·데이터·제품 라벨·글꼴의 권리를 이 라이선스로 다시 허락하지 않는다. 2026-11-20 KST 기준의 공개 메타데이터 및 실제 제출 파일을 확인했다.

## 모델과 글꼴

| 이름·출처 | 확인한 라이선스·접근 조건 | 제출 파일에 포함하는 것 |
|---|---|---|
| Trillion Labs [GravityOCR-KO 모델 카드](https://huggingface.co/trillionlabs/GravityOCR-KO) · [메타데이터](https://huggingface.co/api/models/trillionlabs/GravityOCR-KO) | 카드 `mit`, `private: false`, `gated: false`. GravityOCR를 한국어에 맞게 미세조정한 모델 | **가중치 없음**. 실제 검사에서 사진 글자 읽기(OCR)를 이 모델로 구현했다. 심사 재생은 기록한 응답만 쓴다. |
| Trillion Labs [Zoro-1 모델 카드](https://huggingface.co/trillionlabs/Zoro-1) · [메타데이터](https://huggingface.co/api/models/trillionlabs/Zoro-1) | 카드 `apache-2.0`, `private: false`, **`gated: auto`**. 카드 공개와 가중치 접근은 다르다. 접근 조건을 확인하고 필요한 동의를 해야 한다. | **가중치 없음**. 자료 판독 가능성·일부 성분 판단 및 문의 초안에 사용. |
| Kil Hyung-jin [Pretendard](https://github.com/orioncactus/pretendard) | SIL Open Font License 1.1. 저작권 표시와 예약 글꼴 이름 `Pretendard`를 유지 | 확장에 Regular·SemiBold·Bold WOFF2 3종과 [원 저작권·OFL 전문](submission/runtime/extension/assets/fonts/OFL.txt) 포함. |

모델 운영자가 원격 추론 환경을 준비할 때 원 제공처에서 가중치를 받는다. 심사자의 `./judge.sh`는 어느 모델의 가중치도 받지 않는다. `./judge.sh live`도 이미 운영 중인 서버에 연결할 뿐 가중치를 배포하지 않는다.

## 직접 의존성

다음은 [requirements.txt](requirements.txt)에 고정한 직접 의존성이다. 해당 버전의 배포 메타데이터(`License-Expression`)와 패키지에 포함된 LICENSE의 존재를 확인했다. 이 저장소에는 패키지 본체나 가상환경을 넣지 않고 설치할 때 받는다.

| 이름·확인한 버전의 배포처 | 라이선스 |
|---|---|
| [FastAPI 0.121.1](https://pypi.org/project/fastapi/0.121.1/) | MIT |
| [Uvicorn 0.38.0](https://pypi.org/project/uvicorn/0.38.0/) | BSD-3-Clause |
| [python-multipart 0.0.20](https://pypi.org/project/python-multipart/0.0.20/) | Apache-2.0 |
| [Starlette 0.49.3](https://pypi.org/project/starlette/0.49.3/) | BSD-3-Clause |
| [Pydantic 2.14.3](https://pypi.org/project/pydantic/2.14.3/) | MIT |
| [Pillow 13.0.0](https://pypi.org/project/pillow/13.0.0/) | MIT-CMU |

**Node:** 제출 확장과 심사용 실행기에는 `package.json`이나 `node_modules`가 없고 Node 설치도 필요 없다. 별도 촬영·검증용 `package.json`의 직접 의존성은 [Microsoft Playwright](https://github.com/microsoft/playwright) `1.66.0-alpha-2026-11-18` 하나이며 실제 패키지의 `license`와 LICENSE는 Apache-2.0이다. 브라우저·Playwright·영상 편집기 실행 파일은 미포함이다. 확인한 직접 의존성에서 GPL 계열은 발견되지 않았다. 전이 의존성 전체의 감사 결과라는 뜻은 아니다.

## 공공데이터와 제품 화면

| 자료·출처 | 공식 이용 조건과 포함 범위 |
|---|---|
| 식품의약품안전처 [국내 반입차단 대상 원료·성분(15132686)](https://www.data.go.kr/data/15132686/openapi.do) · [식품안전나라 공개 목록](https://www.foodsafetykorea.go.kr/portal/fooddanger/foodDirectImportBlockRawIrdnt.do) | 공공데이터포털 해당 자료의 명시: **이용허락범위 제한 없음**, 무료. [지정 목록 사본](submission/runtime/data/mfds/snapshot/registry_20261119.json) 포함. 지정 316건(전체 조건 318건). 고정 사본은 현재 목록을 보장하지 않는다. |
| 식품의약품안전처·식품안전나라 [해외직구 위해식품 차단정보 I2715](https://www.data.go.kr/data/15057831/openapi.do) | 해당 자료의 이용허락범위는 **제한 없음**, 무료. [제품 데이터 CSV 사본](submission/runtime/data/mfds/I2715-20261119.csv) 포함. 사진 URL 열이 있어도 원본 사진 파일은 배포하지 않는다. 제품 차단 이력과 원료 지정 목록은 서로 다른 자료다. |
| NIH/NLM [PubChem](https://pubchem.ncbi.nlm.nih.gov/) | [일부 화합물 동의어 캐시](submission/runtime/data/mfds/pubchem-synonyms.json) 및 출처를 기록한 참고 지식 포함. 개별 기탁자가 붙인 이름까지 일괄 재허락하는 조건은 미확인. 공식 차단 근거를 대신하지 않는다. |
| NIH Office of Dietary Supplements [Dietary Supplement Label Database](https://dsld.od.nih.gov/) · [Stimerex 라벨 원출처](https://api.ods.od.nih.gov/dsld/s3/pdf/48084.pdf) | 원본 PDF·사진 묶음 미포함. 제품명의 권리, 제조사의 라벨 그림·상표까지 NIH가 자유 재배포하도록 허락했다는 근거는 미확인. 원본이 필요하면 원출처의 이용 조건을 확인해 직접 받는다. |
| iHerb 상품 페이지, Blackmores 제조사 페이지 | 저장한 원본 HTML·상품 사진·브라우저 프로필 미포함. 권리자에게 별도 허락받은 자료로 표시하지 않는다. [장면별 제품 원출처](submission/demo/README.md)에서 직접 확인한다. |

촬영 영상과 필요한 정지 화면에는 상품 페이지·라벨 일부가 실제 검사 과정으로 보인다. 대조한 성분 줄·OCR 응답은 근거를 따라가는 데 필요한 발췌다. 화면 포함이 원래 자료 전체나 상표의 별도 이용 허락이라는 뜻은 아니다. 심사용 재생은 사진 지문·크기와 식별용 그림만으로 대응하는 원래 응답을 찾는다. 법적 판매·통관 승인이나 기관·제조사의 보증을 뜻하지 않는다.
