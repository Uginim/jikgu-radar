# 상품 25개를 직접 검사하고 당시 결과와 비교하기

**처음에는 ③ Stimerex Hardcore를 열어 보세요.** NIH에 보관된 라벨을 볼 수 있고 GPU 없이 같은 상품의 검사 기록도 재생할 수 있습니다. 앱은 차단 원료가 있는 상품 17개 중 13개에서 정답 원료를 하나 이상 찾았습니다. 놓친 4개는 모두 “확인 필요”로 남겨 잘못 통과시킨 상품은 0/17이었습니다. 차단 원료가 없는 상품 7개에서 잘못 차단한 건은 0/7입니다. 나머지 한 상품은 원료를 차단할지 정할 근거가 부족해 두 지표에서 모두 뺐습니다.

## 돌려 보는 방법

1. **새 사진을 검사하려면** [확장 설치와 실모델 연결](../README.md#심사위원이-직접-따라-하기)을 먼저 마칩니다. 아래에서 같은 이름·용량의 원출처를 엽니다. NIH 링크는 미국 국립보건원 DSLD가 보관한 해당 시점의 **제품 라벨 PDF**이지 현재 판매 페이지가 아닙니다.
2. 사용 권한이 있는 라벨에서 성분표와 `Other Ingredients`가 보이는 페이지를 **PNG 또는 JPEG**로 준비합니다. PDF 자체가 아니라 이 이미지를 확장 패널의 **사진 넣기 → 뒷면 라벨 → 이 사진 검사**에 넣습니다. ‘원료 일치’와 ‘전체 표 확인’을 따로 보세요. 글로 된 성분표가 있는 iHerb 제품은 상품 탭에서 **페이지 검사**를 선택할 수 있습니다. 403 등으로 접근이 막히면 우회하지 않습니다.
3. **GPU 없이 확인하려면** 저장소 루트에서 `./judge.sh`(창 없이 결과만 보면 `./judge.sh check`)를 실행합니다. 이 표와 겹치는 재생 사례는 **③ Stimerex와 ⑰ Nitrotest 두 건뿐**입니다. 세 번째 재생 사례인 Iron Rapi-Melt는 이 25개 밖입니다. 재생은 0430 촬영 당시의 모델·OCR 응답을 0505 코드로 다시 처리하며, 임의 링크나 새 사진을 인식하지 않습니다.

**표 읽는 법.** 결과는 2026-11-20 05:04–05:12 KST에 `integrated-0505`가 보관된 입력으로 수행한 실검사입니다. ‘사진만’은 검사에 본문 글을 주지 않았다는 뜻이지 원래 사이트에 글이 없다는 뜻은 아닙니다. 원출처 링크는 같은 날 05:43 KST에 비로그인 GET으로 각 1회 별도 확인했습니다. NIH 20개는 PDF 본문을 열었고 평가 때 사용한 PDF와 SHA-256도 일치했습니다. iHerb 5개는 모두 HTTP 403이라 지금 상품 내용은 확인하지 못했습니다. 보관 라벨·페이지를 이 저장소에 넣지 않았습니다. 현재 배합·지역·페이지 상태, 사진 범위·해상도 및 추론 시점에 따라 새 검사의 결과가 달라질 수 있습니다.

## 1. 정답 라벨에 지정 원료가 있는 17개

| 번호 | 상품 이름 | 원출처·접근 확인 | 정답: 금지 성분 유무와 식약처 지정명 | 당시 입력 | 0505 상품 결과 | 미검출 이유·해석 | 실검사일(KST) |
|---:|---|---|---|---|---|---|---|
| 1 | Rage XXX Strawberry Kiwi | [PDF 200](https://api.ods.od.nih.gov/dsld/s3/pdf/218367.pdf) | 있음: 테오필린; 1,3-디메틸아밀아민, 1,3-디메틸부틸아민, 1,5-디메틸헥실아민(옥토드린); 요힘빈 | 사진만 | 금지 성분 찾음 | — | 2026-11-20 |
| 2 | 2-Aminoisoheptane | [PDF 200](https://api.ods.od.nih.gov/dsld/s3/pdf/172744.pdf) | 있음: 1,3-디메틸아밀아민, 1,3-디메틸부틸아민, 1,5-디메틸헥실아민(옥토드린) | 사진만 | 확인 필요 | 대조한 줄은 Aconitum Kusnezoffii 한 줄뿐이어서 정답 원료가 빠졌다. | 2026-11-20 |
| 3 | Stimerex Hardcore | [PDF 200](https://api.ods.od.nih.gov/dsld/s3/pdf/48084.pdf) | 있음: 1,3-디메틸아밀아민, 1,3-디메틸부틸아민, 1,5-디메틸헥실아민(옥토드린); 메칠시네프린; 페닐에틸아민; 요힘빈; 테오브로민 | 사진만 | 금지 성분 찾음 | 정답 5개 중 4개를 찾았고 메칠시네프린은 놓쳤다. | 2026-11-20 |
| 4 | Yohimbe | [PDF 200](https://api.ods.od.nih.gov/dsld/s3/pdf/207910.pdf) | 있음: 요힘빈 | 사진만 | 금지 성분 찾음 | — | 2026-11-20 |
| 5 | DMAE (Dimethylaminoethanol) 250 mg | [PDF 200](https://api.ods.od.nih.gov/dsld/s3/pdf/245739.pdf) | 있음: 디아놀 | 사진만 | 금지 성분 찾음 | — | 2026-11-20 |
| 6 | Horny Goat Weed Extract 750 mg | [PDF 200](https://api.ods.od.nih.gov/dsld/s3/pdf/314391.pdf) | 있음: 이카린 | 사진만 | 금지 성분 찾음 | — | 2026-11-20 |
| 7 | Organic Smooth Move Senna Peppermint | [PDF 200](https://api.ods.od.nih.gov/dsld/s3/pdf/307008.pdf) | 있음: 센노사이드 | 사진만 | 금지 성분 찾음 | — | 2026-11-20 |
| 8 | Kapikachhu Powder | [PDF 200](https://api.ods.od.nih.gov/dsld/s3/pdf/247724.pdf) | 있음: 엘-도파 | 사진만 | 확인 필요 | 사진 재판독과 영역 요청 뒤에도 대조할 성분 줄이 없어 엘-도파를 찾지 못했다. | 2026-11-20 |
| 9 | Bacopa | [PDF 200](https://api.ods.od.nih.gov/dsld/s3/pdf/220336.pdf) | 있음: 바코파 | 사진만 | 확인 필요 | 사진을 다시 읽고 영역을 요청했지만 바코파를 대조할 성분 줄을 확보하지 못했다. | 2026-11-20 |
| 10 | Kava Kava | [PDF 200](https://api.ods.od.nih.gov/dsld/s3/pdf/220726.pdf) | 있음: 카바카바(뿌리, 잎, 줄기) | 사진만 | 금지 성분 찾음 | — | 2026-11-20 |
| 11 | Goldenseal | [PDF 200](https://api.ods.od.nih.gov/dsld/s3/pdf/235708.pdf) | 있음: 골든실 루트(뿌리) | 사진만 | 확인 필요 | 유기 알코올 한 줄만 대조했고 골든실 뿌리 줄은 대조하지 못했다. | 2026-11-20 |
| 12 | Lobelia Herb | [PDF 200](https://api.ods.od.nih.gov/dsld/s3/pdf/16843.pdf) | 있음: 로벨리아 | 사진만 | 금지 성분 찾음 | — | 2026-11-20 |
| 13 | Buchu | [PDF 200](https://api.ods.od.nih.gov/dsld/s3/pdf/50118.pdf) | 있음: 부추 | 사진만 | 금지 성분 찾음 | — | 2026-11-20 |
| 14 | Black Cohosh Extract 500 mg | [PDF 200](https://api.ods.od.nih.gov/dsld/s3/pdf/326711.pdf) | 있음: 블랙코호시 | 사진만 | 금지 성분 찾음 | — | 2026-11-20 |
| 15 | Echinacea Complex | [PDF 200](https://api.ods.od.nih.gov/dsld/s3/pdf/76351.pdf) | 있음: 에키네시아속 | 라벨 글과 사진 | 금지 성분 찾음 | PDF에서 얻은 글도 입력했다. 사진만 준 실험이 아니다. | 2026-11-20 |
| 16 | Organic Gymnema sylvestre Powder | [PDF 200](https://api.ods.od.nih.gov/dsld/s3/pdf/336713.pdf) | 있음: 당살초 | 사진만 | 금지 성분 찾음 | — | 2026-11-20 |
| 17 | MuscleMeds, Nitrotest™, Androgenic Pre-Workout Amplifier, Blue Raspberry, 16.72 oz (474 g) | [상품 페이지 403](https://kr.iherb.com/pr/musclemeds-nitrotest-androgenic-pre-workout-amplifier-blue-raspberry-16-72-oz-474-g/120411) | 있음: 요힘빈; 디아놀 | 페이지 글 | 금지 성분 찾음 | 정답 2개 모두 일치. 추가 표시 2개는 정답 적중에 더하지 않았다. | 2026-11-20 |

## 2. 나머지 8개: 지정 원료 없는 7개 + 별도 검토 1개

18–24번만 음성 분모입니다. 25번은 금지 성분이 없다고 단정한 제품이 아닙니다.

| 번호 | 상품 이름 | 원출처·접근 확인 | 정답: 금지 성분 유무와 식약처 지정명 | 당시 입력 | 0505 상품 결과 | 미검출 이유·해석 | 실검사일(KST) |
|---:|---|---|---|---|---|---|---|
| 18 | Gold C Vitamin C 1000 mg | [PDF 200](https://api.ods.od.nih.gov/dsld/s3/pdf/273686.pdf) | 없음(정답 기준) | 사진만 | 확인 필요 | — | 2026-11-20 |
| 19 | Magnesium Oxide Pure Powder | [PDF 200](https://api.ods.od.nih.gov/dsld/s3/pdf/72119.pdf) | 없음(정답 기준) | 사진만 | 확인 필요 | — | 2026-11-20 |
| 20 | Calcium Citrate Pure Powder | [PDF 200](https://api.ods.od.nih.gov/dsld/s3/pdf/313646.pdf) | 없음(정답 기준) | 사진만 | 확인 필요 | — | 2026-11-20 |
| 21 | Vitamin C Pure Powder | [PDF 200](https://api.ods.od.nih.gov/dsld/s3/pdf/270873.pdf) | 없음(정답 기준) | 사진만 | 확인 필요 | — | 2026-11-20 |
| 22 | Frontier Co-op, Organic China Green Tea, 16 oz (453 g) | [상품 페이지 403](https://kr.iherb.com/pr/frontier-co-op-organic-china-green-tea-16-oz-453-g/31204) | 없음(정답 기준) | 페이지 글(완전한 성분표 없음) | 확인 필요 | — | 2026-11-20 |
| 23 | NOW Foods, Garcinia Extract, 120 Tablets | [상품 페이지 403](https://kr.iherb.com/pr/now-foods-garcinia-extract-120-tablets/55359) | 없음(정답 기준) | 페이지 글 | 읽은 성분에 일치 없음 | — | 2026-11-20 |
| 24 | California Gold Nutrition, Gold C®, USP Grade Vitamin C, 1,000 mg, 60 Veggie Capsules | [상품 페이지 403](https://kr.iherb.com/pr/california-gold-nutrition-gold-c-usp-grade-vitamin-c-1-000-mg-60-veggie-capsules/61864) | 없음(정답 기준) | 페이지 글 | 읽은 성분에 일치 없음 | — | 2026-11-20 |
| 25 | NOW Foods, Glucosamine & Chondroitin, 120 Capsules | [상품 페이지 403](https://kr.iherb.com/pr/now-foods-glucosamine-chondroitin-120-capsules/102343) | 미확정(조건부 원료) | 페이지 글 | 확인 필요 | 소 연골 유래 원료의 원산지·월령 근거가 없어 별도 검토. | 2026-11-20 |

## 결과를 해석할 때

‘찾음’은 기대한 원료 ID 중 **하나 이상**을 찾았다는 뜻입니다. ‘읽은 성분에 일치 없음’은 제품 안전이나 통관 승인이 아닙니다. 25개 중 제공 자료의 범위를 확인하고 정답으로 끝낸 사례는 8개, 세부 항목까지 모두 해결한 사례는 7개였습니다. 이 표는 개발 중 반복해서 본 제품의 기록으로, 새로운 제품의 정확도를 추정하는 시험이 아닙니다.

[0505 판본과 25건 대조](logs/comparison-0505.json) · [25개 평가 기록](logs/evaluation-25.json) · [기준 방법별 상품 대조](agent-vs-no-agent-0500.md) · [식약처 원료명과 ID 사본](../data/mfds/snapshot/registry_20261119.json)
