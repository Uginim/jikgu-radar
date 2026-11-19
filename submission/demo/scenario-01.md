# 01 · 미국 운동 보충제의 글 성분표에서 차단 근거 찾기

> “MuscleMeds, Nitrotest™, Androgenic Pre-Workout Amplifier, Blue Raspberry, 16.72 oz (474 g)” — 실제 검사에 들어간 상품명

[iHerb Nitrotest](https://kr.iherb.com/pr/musclemeds-nitrotest-androgenic-pre-workout-amplifier-blue-raspberry-16-72-oz-474-g/120411) 저장 페이지에는 성분표가 글로 남아 있었다. 판매자에게 사진이나 답을 다시 요청하지 않고 그 글부터 대조했다. **0430 사건 소요 시간: 12.775초.**

| 0430 결과 화면 | 읽은 성분 | 목록에 없음 | 차단 근거가 나온 성분 | 남은 확인 |
|---|---:|---:|---:|---:|
| 등록 보류 권고 | 21 | 17 | 4 | 0 |

**Zoro-1이 판단한 것 / 코드가 한 것.** “성분 이름을 읽을 수 있는가?”에 모델은 `읽을 만함` 0.9428을 골랐고, 분류와 규칙만으로 끝나지 않은 성분 여섯도 판단했다([18행](../logs/scenario-01.jsonl#L18), [30행](../logs/scenario-01.jsonl#L30), [72–119행](../logs/scenario-01.jsonl#L72-L119)). 다음 단계를 정한 것은 코드다. 글이 충분해 사진 읽기를 건너뛰고 공식 이름·이명을 대조한 뒤, 마지막에 자료 범위를 따로 확인했다.

**원본에 남은 결과.** 시트룰린·디아놀·엘도파·요힘빈 네 근거가 확인됐다([39–53행](../logs/scenario-01.jsonl#L39-L53)). ‘등록 보류’는 판매자에게 낸 권고이며 실제 마켓에서 판매를 정지시키지 않았다([124–127행](../logs/scenario-01.jsonl#L124-L127)).

**[다음: 사진 성분표 읽기](scenario-02.md)** · [이 장면의 실행 로그](../logs/scenario-01.jsonl)
