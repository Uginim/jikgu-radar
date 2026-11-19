"""시나리오 실행 → 제출용 '시나리오 3개 실행 로그' Markdown.

시나리오는 JSON 파일(목록): [{"name", "input"{url,title,brand,role,ingredients_text,claims}, "image"(캡처, 선택), "approve"(승인할 행동 id 또는 "all"), "answers": ["yes"|"no"|"unknown" | {"answer", "image"}, ...]}]
answers 는 되묻기에 차례로 답할 값(데모 대본). 없으면 영양제 검수 기본 시나리오 3개를 쓴다.

실행: .venv/bin/python scripts/run_scenarios.py [scenarios.json] [-o docs/submission/scenario-logs.md]
환경: ZORO_MODE=mock|remote|record, NEXTASK_PLUGIN=supplement
"""
import argparse
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
# 제출 로그를 만들 때 사용자 장부를 지우거나 바꾸지 않는다.
_store_dir = tempfile.TemporaryDirectory(prefix="jikgu-scenarios-")
os.environ["NEXTASK_STORE"] = os.path.join(_store_dir.name, "store.json")
from nextask.core import caselog  # noqa: E402
from nextask.core.engine import Engine  # noqa: E402
from nextask.core.plugins import load  # noqa: E402

DEFAULT = "nextask/core/plugins/supplement_data/scenarios.json"

ap = argparse.ArgumentParser()
ap.add_argument("scenarios", nargs="?")
ap.add_argument("-o", "--out", default="docs/submission/scenario-logs.md")
a = ap.parse_args()
scen = json.load(open(a.scenarios or DEFAULT, encoding="utf-8"))
if not os.path.exists("data/samples/labels"):
    print("data/samples/labels 없음: scripts/import_demo_labels.py 로 research 라벨을 먼저 가져오세요(시나리오 3 캡처 이미지)")
plugin = load()
eng = Engine(plugin)
parts = []
summary = []
for s in scen:
    inp = s.get("input") or {"claims": s.get("claims", "")}
    c = eng.start(text=inp.get("title") or s.get("text", ""), image_path=s.get("image"), inputs=inp)
    ans = list(s.get("answers", []))
    while c.status == "waiting_user":
        x = ans.pop(0) if ans else "unknown"
        x = x if isinstance(x, dict) else {"answer": x}          # {"answer": "yes", "image": 재촬영 사진}
        eng.answer(c, {"yes": True, "no": False}.get(x["answer"]), image_path=x.get("image"))
    for act in [x for x in c.actions if x["state"] == "pending"]:
        if "all" in s.get("approve", []) or act["id"] in s.get("approve", []):
            eng.approve(c, act["id"], True)
    print(f"== {s['name'][:40]}: {c.verdict['label']} | 상품 상태 {c.plugin.product_status(c)} | 행동 "
          + ", ".join(f"{x['label']}[{x['state']}]" for x in c.actions))
    ev = caselog.read_case(c.id)
    st = next((e for e in ev if e["event"] == "decision_stats"), {})
    summary.append((s["name"].split(" — ")[0], caselog.path_line(ev), st))
    parts += [f"# {s['name']}", "", caselog.markdown_report(c.id), "---", ""]
head = [f"# 시나리오 실행 로그 — {plugin.title}", "",
        f"Zoro-1 모드: `{eng.zoro.mode}` (mock 의 synthetic 출처는 실제 판정이 아님) · 엔진: "
        f"`{'고정 파이프라인(FIXED_PIPELINE=1)' if os.environ.get('FIXED_PIPELINE') == '1' else '에이전트(애매할 때만 System 2·도구·사람)'}`", "",
        "## 경로 요약", "", "| 시나리오 | 지나간 경로 | Zoro-1 호출(S2) | 도구 | 사람 | 시간 ms |", "|---|---|---:|---:|---:|---:|"]
head += [f"| {n} | {p} | {st.get('model_calls')}({st.get('model_calls_system2')}) | {st.get('tool_calls')} | {st.get('human')} | {st.get('total_ms')} |"
         for n, p, st in summary]
parts = head + ["", "---", ""] + parts
os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
open(a.out, "w", encoding="utf-8").write("\n".join(parts))
print("wrote", a.out)
