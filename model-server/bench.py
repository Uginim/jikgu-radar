"""상품 1건 검사 비용 측정 (경제 세션 요청 11/19 18:07). Brev 인스턴스에서 서버를 띄운 뒤 1회 실행.
    python3 bench.py http://127.0.0.1:30001 <크롭한 성분표 이미지>
측정(각 5회, 첫 1회는 예열로 버림):
  decide_1   : 같은 성분 판정 1문항(후보 5개 + 해당 없음)
  decide_8   : 같은 요청에 8문항(성분 8개) -> 서버가 한 번의 forward 배치로 처리
  decide_32  : 32문항
  generate   : 근거 카드 설명 1문단(최대 120 토큰)
  ocr        : 성분표 크롭 1장
결과: logs/bench-<시각>.json + 표. 시간당 판정 수 = 3600 / (문항당 지연)."""
import base64
import json
import os
import statistics
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import ZoroClient  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
INGR = ["Choline Bitartrate", "Caffeine Anhydrous", "4-Amino Methylpentane Citrate", "Tianeptine Sodium",
        "Phenyl Piracetam", "Nefiracetam", "L-Theanine", "Phenylethylamine HCl"]
CANDS = {"dmba": "1,3-Dimethylbutylamine (DMBA, AMP citrate) - blocked", "dmaa": "1,3-Dimethylamylamine (DMAA) - blocked",
         "tianeptine": "Tianeptine - blocked", "phenibut": "Phenibut - blocked", "yohimbine": "Yohimbine - blocked",
         "none": "None of the above: a different substance"}


def q(ing):
    return {"type": "choice", "instructions": f"Label ingredient: '{ing}'. Which blocked substance is it the same as?",
            "criteria": CANDS}


def req(n):
    ings = (INGR * 4)[:n]
    return {"state": {"product": "Pre-workout capsule (iHerb listing)", "label_ingredients": INGR},
            "questions": {f"i{k}": q(x) for k, x in enumerate(ings)}}


def timeit(fn, n=5):
    fn()   # 예열
    ts, last = [], None
    for _ in range(n):
        t = time.perf_counter()
        last = fn()
        ts.append((time.perf_counter() - t) * 1000)
    return {"ms_median": round(statistics.median(ts), 1), "ms_min": round(min(ts), 1), "ms_max": round(max(ts), 1),
            "server_ms": last.get("timing_ms")}, last


def main():
    url, img = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None
    z = ZoroClient(urls=url, timeout=300, retries=1)
    h = z.health()
    out = {"at": datetime.now().isoformat(timespec="seconds"), "health": h, "results": {}}
    for n in (1, 8, 32):
        r, last = timeit(lambda: z.decide(req(n)))
        r["questions"] = n
        r["ms_per_question"] = round(r["ms_median"] / n, 1)
        r["decisions_per_hour"] = int(3600 * 1000 / r["ms_per_question"])
        out["results"][f"decide_{n}"] = r
        out.setdefault("sample", {})[f"decide_{n}"] = {k: last["answers"][k] for k in list(last["answers"])[:3]}
        print(f"decide_{n}", r, flush=True)
    r, last = timeit(lambda: z.generate(
        prompt="In two sentences for an online seller, explain why the label ingredient '4-Amino Methylpentane "
               "Citrate' is the same as the blocked substance 1,3-dimethylbutylamine (DMBA).", max_new_tokens=120), n=3)
    r["tokens"] = last.get("tokens")
    r["ms_per_token"] = round(r["server_ms"] / max(1, last.get("tokens") or 1), 1)
    out["results"]["generate"], out.setdefault("sample", {})["generate"] = r, last.get("text")
    print("generate", r, flush=True)
    if img and h.get("ocr"):
        b64 = base64.b64encode(open(img, "rb").read()).decode()
        r, last = timeit(lambda: z.ocr(b64, max_new_tokens=1024), n=3)
        r["tokens"], r["lines"], r["mean_conf"] = last.get("tokens"), len(last.get("lines") or []), last.get("mean_conf")
        out["results"]["ocr"], out["sample"]["ocr"] = r, last.get("text")
        print("ocr", r, flush=True)
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    p = os.path.join(HERE, "logs", f"bench-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    json.dump(out, open(p, "w"), ensure_ascii=False, indent=1)
    print("saved", p)


if __name__ == "__main__":
    main()
