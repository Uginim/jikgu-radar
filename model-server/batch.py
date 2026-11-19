"""docs/zoro-requests.md 로 들어온 실측 요청을 한꺼번에 돌린다 (인스턴스를 켰을 때만).

    python infra/batch.py <URL> docs/zoro-requests/1.json [2.jsonl ...]
입력: {state, questions} 한 건, 그 배열, 또는 JSONL. 출력: 입력 옆에 <이름>.result.jsonl (요청 전문, 응답 전문, 지연)
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import ZoroClient, ZoroError  # noqa: E402


def load(path):
    text = open(path, encoding="utf-8").read().strip()
    if path.endswith(".jsonl"):
        return [json.loads(x) for x in text.splitlines() if x.strip()]
    v = json.loads(text)
    return v if isinstance(v, list) else [v]


def main():
    url, files = sys.argv[1], sys.argv[2:]
    z = ZoroClient(urls=url, timeout=120, retries=2)
    for f in files:
        out_path = os.path.splitext(f)[0] + ".result.jsonl"
        reqs, t_all, ok = load(f), time.perf_counter(), 0
        with open(out_path, "w", encoding="utf-8") as out:
            for i, r in enumerate(reqs):
                t = time.perf_counter()
                try:
                    res, err = z.decide(r), None
                    ok += 1
                except ZoroError as e:
                    res, err = None, str(e)
                out.write(json.dumps({"i": i, "request": r, "response": res, "error": err,
                                      "rtt_ms": round((time.perf_counter() - t) * 1000)}, ensure_ascii=False) + "\n")
        print(f"{f}: {ok}/{len(reqs)} ok in {time.perf_counter() - t_all:.1f}s -> {out_path}")


if __name__ == "__main__":
    main()
