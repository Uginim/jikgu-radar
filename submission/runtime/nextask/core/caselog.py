"""요청(케이스) 단위 JSONL 로그와 사람이 읽는 Markdown 요약.

logs/cases/<case_id>.jsonl  케이스 하나의 사건 전부(입력 전문, Zoro-1 요청·응답 전문과 확률 전부,
                            분기 이유, 도구 인자·결과, 지연, 최종 판정). 글자 수로 자르지 않는다.
logs/agent-YYYYMMDD.jsonl   같은 레코드를 하루 파일에도 이어 쓴다(전체 검색용).
표준 출력에는 한 줄 요약만 찍는다.
"""
import json
import os
import threading
import time
from datetime import datetime

from .wirelog import sanitize

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG_DIR = os.environ.get("NEXTASK_LOG_DIR", os.path.join(ROOT, "logs"))
_LOCK = threading.Lock()
CURRENT = threading.local()     # 지금 이 스레드가 처리 중인 case_id (생성 호출 등 깊은 곳에서 로그를 남기려고)
LISTENERS = {}      # case_id -> [fn(rec)]  (판정 서버가 SSE 로 내보내려고 등록)


def subscribe(case_id, fn):
    LISTENERS.setdefault(case_id, []).append(fn)


def case_path(case_id):
    return os.path.join(LOG_DIR, "cases", f"{case_id}.jsonl")


def log_event(case_id, event, **data):
    rec = sanitize({"ts": datetime.now().astimezone().isoformat(timespec="milliseconds"), "case_id": case_id,
                    "event": event, **data})
    line = json.dumps(rec, ensure_ascii=False, default=str)
    os.makedirs(os.path.join(LOG_DIR, "cases"), exist_ok=True)
    with _LOCK:
        with open(case_path(case_id), "a", encoding="utf-8") as f:
            f.write(line + "\n")
        with open(os.path.join(LOG_DIR, f"agent-{time.strftime('%Y%m%d')}.jsonl"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    short = {k: v for k, v in rec.items() if k in ("branch", "reason", "tool", "question", "answer", "top", "conf",
                                                    "source", "latency_ms", "status")}
    print(f"[case {case_id}] {event} {json.dumps(short, ensure_ascii=False, default=str)}", flush=True)
    for fn in LISTENERS.get(case_id, []):
        try:
            fn(json.loads(line))
        except Exception as e:  # noqa: BLE001 -- 구독자 오류로 에이전트를 멈추지 않는다
            print(f"[case {case_id}] listener error {e}", flush=True)
    return rec


def read_case(case_id):
    with open(case_path(case_id), encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def _bar(p, width=20):
    n = round(p * width)
    return "█" * n + "·" * (width - n)


def _probs_table(probs, labels):
    rows = ["| 선택지 | 확률 | |", "|---|---:|---|"]
    for k, v in sorted(probs.items(), key=lambda kv: -kv[1]):
        rows.append(f"| {labels.get(k, k)} | {v:.1%} | `{_bar(v)}` |")
    return "\n".join(rows)


SYS = {0: "규칙", 1: "S1", 2: "S2"}


def path_rows(ev):
    """로그 → '지나간 경로' 표 행: 결정 → 도구 → 상태 → 결정 … (② 에이전트임을 보여 주는 표)."""
    rows, titles = [], {}
    for e in ev:
        for it in e.get("items") or []:
            if isinstance(it, dict) and it.get("id"):
                titles[it["id"]] = it.get("title", it["id"])
        k = e["event"]
        item = titles.get(e.get("item"), e.get("item_title") or e.get("item") or "")
        if k == "decision" and e.get("thresholds"):
            rows.append((item, f"결정 {SYS.get(e.get('system'), '')}", f"{e.get('top_label')} {(e.get('conf') or 0):.0%}",
                         e.get("next_action") or e.get("branch")))
        elif k == "next_action":
            rows.append((titles.get(e.get("item"), e.get("item_title") or ""), "다음 행동",
                         f"{e.get('choice_label')} {(e.get('p') or 0):.0%} ({'고정 순서' if e.get('policy') == 'fixed' else 'Zoro-1'}, 남은 예산 {e.get('budget_left')})",
                         "→ 실행"))
        elif k == "zoro_incomplete":
            rows.append((item, "S2 완료 미확인", str((e.get("meta") or {}).get("cut") or "출처 미확인"),
                         "기존 판정 유지 → 도구·사람" if e.get("fallback") == "keep_previous" else "→ 사람 검토"))
        elif k == "match_stages":
            rows.append((f"성분: {e.get('ingredient')}", "결정 규칙 ①②",
                         {"match": "차단 원료와 일치", "none": "후보 없음", "undecided": "규칙으로 못 정함"}.get(e.get("decision"), e.get("decision")),
                         "확정" if e.get("decision") in ("match", "none") else "→ Zoro-1"))
        elif k == "tool_call":
            res = e.get("result") or {}
            note = (f"별칭 {len(res.get('hits', []))}건" if "hits" in res else f"글자 {len(res.get('lines', []))}줄" if "lines" in res
                    else res.get("status") or res.get("reason") or "")
            rows.append((item or "(케이스)", "도구", f"{e.get('tool')} {'성공' if e.get('ok') else '실패'} {note}".strip(),
                         "→ 상태 갱신" if e.get("ok") else "→ 재시도/대체"))
        elif k == "answer":
            rows.append((item, "사람 답", f"{e.get('answer')}", f"→ {e.get('update')}"))
        elif k == "capture_received":
            rows.append(("성분표 확보", "사람 답", "캡처 보냄", "→ OCR"))
        elif k == "action":
            rows.append(("(상품)", "행동", e.get("label"), "자동" if e.get("auto") else "셀러 승인"))
        elif k == "final":
            rows.append(("(상품)", "결론", (e.get("verdict") or {}).get("label"), ""))
    return rows


def path_line(ev):
    """경로 한 줄(③): 항목마다 'S1 97% → S2 90% → 도구 → … → 확정/사람', 마지막에 결론·행동."""
    seq, order = {}, []
    final, acts = None, []
    for item, kind, what, nxt in path_rows(ev):
        if kind == "결론":
            final = what
            continue
        if kind == "행동":
            acts.append(what)
            continue
        if item not in seq:
            seq[item] = []
            order.append(item)
        tok = (f"{kind.replace('결정 ', '')} {what.split()[-1]}" if kind.startswith("결정 S") else
               {"차단 원료와 일치": "규칙 일치", "후보 없음": "규칙 후보 없음"}.get(what, "규칙 미결") if kind.startswith("결정 규칙") else
               "도구" if kind == "도구" else "사람 답" if kind == "사람 답" else
               "▶" + what.split(" (")[0].rsplit(" ", 1)[0] if kind == "다음 행동" else kind)
        for t in [tok] + (["확정" if nxt == "확정" else "사람"] if nxt in ("확정", "→ 사람 검토") else []):
            if not seq[item] or seq[item][-1] != t:      # 같은 토큰이 연달아 나오면 한 번만
                seq[item].append(t)
    parts = [f"{i}: " + " → ".join(seq[i]) for i in order if i != "(케이스)"]
    return " | ".join(parts) + (f" ⇒ **{final}**" if final else "") + (f" ⇒ {', '.join(acts)}" if acts else "")


def markdown_report(case_id):
    """케이스 로그 -> 제출용 실행 로그 Markdown (단계별)."""
    ev = read_case(case_id)
    out = [f"## 케이스 `{case_id}`", "", f"**경로**: {path_line(ev)}", "",
           "<details><summary>지나간 경로 표 (결정 → 도구 → 상태 → 결정)</summary>", "",
           "| # | 항목 | 종류 | 내용 | 다음 |", "|---|---|---|---|---|"]
    out += [f"| {n + 1} | {a} | {b} | {c} | {d} |" for n, (a, b, c, d) in enumerate(path_rows(ev))]
    out += ["", "</details>", ""]
    step = 0
    tag = lambda e: f"[{e['item']}] " if e.get("item") else ""  # noqa: E731
    for e in ev:
        k = e["event"]
        if k == "case_start":
            out += [f"- 시작: {e['ts']}  ·  플러그인: `{e.get('plugin')}`  ·  Zoro 모드: `{e.get('zoro_mode')}`",
                    "", "**입력 텍스트(전문)**", "", "```", e.get("text") or "(없음)", "```"]
            if e.get("image"):
                out += ["", f"**입력 사진**: `{e['image'].get('path')}` (sha256 {e['image'].get('sha256', '')[:12]}…)"]
            out.append("")
        elif k in ("items", "items_added"):
            out += ["**판정 항목" + (" 추가**" if k == "items_added" else "**"), ""]
            for it in e["items"]:
                extra = ""
                cands = it.get("data", {}).get("candidates")
                if cands is not None:
                    extra = " — 후보 " + (", ".join(c["label"] if isinstance(c, dict) else str(c) for c in cands) or "없음")
                if it.get("data", {}).get("decided_by"):
                    extra += f" — 판정 단계 {it['data']['decided_by']}"
                if it.get("reason"):
                    extra += f" — {it['status']}: {it['reason']}"
                out.append(f"- `{it['id']}` {it['title']}{extra}")
            out.append("")
        elif k in ("zoro_call", "score"):
            step += 1
            src = (e.get("meta") or {}).get("source") or e.get("source")
            ms = (e.get("meta") or {}).get("latency_ms")
            sysn = f" System {e.get('system')}" if e.get("system") else ""
            out += [f"### {step}. {tag(e)}{'Zoro-1' + sysn + ' 판정' if k == 'zoro_call' else '코드 점수'} ({e.get('purpose')})"
                    f"{f' — {ms} ms' if ms is not None else ''}, 출처 `{src}`", ""]
            if e.get("decision_probs"):
                out += [_probs_table(e["decision_probs"], e.get("labels") or {}), ""]
            if e.get("raw_probs") and set(e["raw_probs"]) != set(e.get("decision_probs") or {}):
                out += ["<details><summary>세부 선택지 확률 전부</summary>", "", _probs_table(e["raw_probs"], {}), "",
                        "</details>", ""]
        elif k == "zoro_error":
            step += 1
            out += [f"### {step}. {tag(e)}Zoro-1 호출 실패 — {e.get('error')}", f"- 대체 경로: {e.get('fallback')}", ""]
        elif k == "zoro_incomplete":
            step += 1
            meta = e.get("meta") or {}
            out += [f"### {step}. {tag(e)}생각 모드 완료 미확인 — 확률 미채택",
                    f"- 실제 답 출처: `{meta.get('answers_source')}` · cut: `{meta.get('cut')}`",
                    f"- 종료 정보: `{meta.get('reasoning_stats')}` · 대체 경로: `{e.get('fallback')}`",
                    f"- {e.get('reason')}", ""]
        elif k == "tool_call":
            step += 1
            ok = "성공" if e.get("ok") else "실패"
            res = e.get("result") if e.get("ok") else e.get("error")
            out += [f"### {step}. {tag(e)}도구 `{e['tool']}` 시도 {e.get('attempt')} — {ok}, {e.get('latency_ms')} ms",
                    f"- 인자: `{json.dumps(e.get('args'), ensure_ascii=False)}`",
                    f"- 결과: `{json.dumps(res, ensure_ascii=False)}`", ""]
        elif k == "rewrite":
            step += 1
            if e.get("ok"):
                out += [f"### {step}. {tag(e)}고쳐 쓰기 ({e.get('by')})", f"- 전: {e.get('from')}", f"- 후: {e.get('text')}"]
                if e.get("attempts"):
                    out.append(f"- 앞선 경로 실패: {'; '.join(e['attempts'])}")
                out.append("")
            else:
                out += [f"### {step}. {tag(e)}고쳐 쓰기 실패 — {e.get('error')}", ""]
        elif k == "decision":
            sysl = {0: "규칙", 1: "System 1", 2: "System 2"}.get(e.get("system"), "")
            th = e.get("thresholds") or {}
            out += [f"> {tag(e)}**결정** [{sysl}] {e.get('top_label') or e.get('top')} {(e.get('conf') or 0):.1%} "
                    f"(기준선 {th.get('lo')} / {th.get('hi')}) **{e.get('next_action') or e['branch']}** — {e['reason']}", ""]
        elif k == "next_action":
            pr = e.get("probs") or {}
            out += [f"> {tag(e)}**다음 행동** [{'고정 순서' if e.get('policy') == 'fixed' else 'Zoro-1'}] **{e.get('choice_label')}** "
                    f"{(e.get('p') or 0):.0%} — 보기: " + ", ".join(f"{(e.get('options') or {}).get(k, k)} {v:.0%}" for k, v in sorted(pr.items(), key=lambda x: -x[1]))
                    + f" · 남은 예산 {e.get('budget_left')}", ""]
        elif k == "decision_stats":
            b, r = e.get("ended_by", {}), e.get("ratio", {})
            out += [f"**결정 통계**: 결정 {e.get('decisions')}개, 모델 호출 {e.get('model_calls')}회 — 끝난 곳: "
                    + ", ".join(f"{dict(rule='규칙', system1='System 1', system2='System 2', tool='도구', human='사람')[k]} {b[k]}({r.get(k, 0):.0%})" for k in b)
                    + f" · 평균 지연 {e.get('latency_ms')}",
                    f"- S2 시도 {e.get('reasoning_attempts', '미기록')}회 / 완료 {e.get('model_calls_system2', '미기록')}회"
                    f" / 완료 미확인 {e.get('reasoning_incomplete', '미기록')}회 (미채택)", ""]
        elif k == "ask":
            out += [f"**{tag(e)}되묻기 #{e['n']}**: {e['question']}", ""]
        elif k == "answer":
            out += [f"**{tag(e)}셀러 답**: {e['answer']}  → 갱신 방식 `{e.get('update')}`", ""]
        elif k == "item_final":
            out += [f"**{tag(e)}항목 종료: {e['status']}** {e.get('result') or ''} ({(e.get('conf') or 0):.1%})", ""]
        elif k == "card":
            cd = e["card"]
            out += [f"**{tag(e)}근거 카드** — {cd.get('verdict')}"
                    + (f" · 차단 항목 {cd['matched_entry']}" if cd.get("matched_entry") else ""),
                    f"- 이유({cd.get('why_by')}): {cd.get('why')}"]
            out += [f"- 남은 확인: {r}" for r in cd.get("remaining_checks", [])]
            out.append("")
        elif k in ("action", "action_pending", "action_rejected"):
            st = {"action": "실행", "action_pending": "승인 대기", "action_rejected": "셀러 거절"}[k]
            extra = "" if k != "action" else f" ({'자동' if e.get('auto') else '셀러 승인'}, {e.get('state')})"
            out += [f"**행동 {st}**: {e.get('label')}{extra}" + (f" — {e.get('reason')}" if e.get("reason") else ""), ""]
        elif k == "match_stages":
            p = e.get("stages", {}).get("partial", {})
            out += [f"**① 부분 일치 / ② 사전** `{e.get('ingredient')}`: preferred_exact {p.get('preferred_exact', {}).get('candidate_ids')}, "
                    f"registry_alias_exact {p.get('registry_alias_exact', {}).get('candidate_ids')}, "
                    f"dictionary {e.get('stages', {}).get('dictionary', {}).get('candidate_ids', '(① 에서 결정, 실행 안 함)')} → "
                    f"**{e.get('decision')}** ({e.get('decided_by') or 'Zoro-1 로'})", ""]
        elif k == "baseline":
            out += [f"**기준선(단어 일치, Highlight This 방식)**: 단어 {e.get('n_terms')}개 중 라벨에서 일치 "
                    + (", ".join(f"`{h['term']}`" for h in e.get("hits", [])) or "없음"), ""]
        elif k == "comparison":
            c = e.get("count", {})
            out += [f"**단어 일치 vs 우리**: 단어 일치 표시 {e.get('baseline_blocked')}개, 우리 차단 {e.get('ours_blocked')}개, "
                    f"단어 일치가 놓친 차단 **{c.get('ours_only')}개**, 단어 일치만 막음 {c.get('baseline_only')}개, 우리 확인 필요 {c.get('ours_review')}개", ""]
        elif k == "final":
            v = e.get("verdict") or {}
            out += [f"### 결론: **{v.get('label')}** — {v.get('why')}",
                    f"- Zoro-1 호출 {e.get('n_zoro')}회, 총 {e.get('total_ms')} ms", "",
                    "<details><summary>생성된 결과물</summary>", "", e.get("output", {}).get("markdown", ""), "",
                    "</details>", ""]
    return "\n".join(out)
