"""System1(즉시 판정) vs System2(생각 모드 재판정) 지연·확률 비교. 서버가 ZORO_ALLOW_REASONING=1 이어야 한다.

    .venv/bin/python infra/s2_bench.py https://xxxx.trycloudflare.com     # 결과: infra/logs/s2-bench-<시각>.json

같은 질문을 /v1/decide 로 reasoning=False(System1) 와 reasoning=True(System2) 로 각각 보내
지연(왕복·서버 계산)과 확률 변화를 기록한다. 질문은 앱 시나리오(성분 같은지, 차단 성분, 같은 상품)에서 골랐다.
"""
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import ZoroClient  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

CASES = [
    ("same_ingredient_easy", {
        "state": "Label ingredient: 'Ascorbic Acid'. Korean food-safety list entry: '비타민C (L-아스코르브산)'.",
        "questions": {"same": {"type": "noul", "instructions": "Do these two names refer to the same ingredient?"}}}),
    ("same_ingredient_hard", {
        "state": "Label ingredient: 'Rauwolscine (alpha-yohimbine)'. MFDS banned ingredient: '요힘빈 (Yohimbine)'.",
        "questions": {"same": {"type": "noul", "instructions": "Do these two names refer to the same ingredient "
                                                                "(same chemical substance)?"}}}),
    ("blocked_choice", {
        "state": "Supplement label lists: Caffeine anhydrous 300mg, DMHA (2-aminoisoheptane) 150mg, Beta-alanine 3.2g.",
        "questions": {"verdict": {"type": "choice", "instructions": "Does this product contain an ingredient that "
                                  "Korea's MFDS blocks for overseas direct purchase?",
                                  "criteria": {"blocked": "contains a blocked ingredient",
                                               "clear": "no blocked ingredient",
                                               "unsure": "cannot tell from the label"}}}}),
]


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ZORO_URL", "http://127.0.0.1:30001")
    reps = int(os.environ.get("S2_REPS", "2"))
    z = ZoroClient(urls=url, timeout=300, retries=1)
    report = {"url": url, "at": datetime.now().isoformat(timespec="seconds"), "health": z.health(), "cases": []}
    for name, req in CASES:
        for mode in (False, True):
            rtt, comp, out = [], [], None
            for _ in range(reps):
                t = time.perf_counter()
                out = z.call("/v1/decide", {"request": req, "reasoning": mode})
                rtt.append(round((time.perf_counter() - t) * 1000))
                comp.append(out.get("timing_ms"))
            row = {"case": name, "mode": "system2" if mode else "system1", "rtt_ms": rtt, "server_ms": comp,
                   "answers": out.get("answers"), "extra": {k: v for k, v in out.items()
                                                            if k not in ("answers", "timing_ms", "attempts")}}
            report["cases"].append(row)
            print(f"{name:22s} {row['mode']}  rtt {rtt}  server {comp}  "
                  f"{json.dumps(out.get('answers'), ensure_ascii=False)[:200]}")
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    p = os.path.join(HERE, "logs", f"s2-bench-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    json.dump(report, open(p, "w"), ensure_ascii=False, indent=1)
    print("saved", p)


if __name__ == "__main__":
    main()
