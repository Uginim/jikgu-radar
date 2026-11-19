"""기준선: 셀러의 현재 방식 — 크롬 확장 'Highlight This' 에 식약처 차단 목록 이름을 붙여 넣고 페이지에서 단어 일치로 색칠.

이 모듈은 그 동작을 코드로 흉내 낸다: 차단 목록 레코드의 이름 전부(한글·영문·기타 명칭)를 단어 목록으로 쓰고,
라벨 줄에서 대소문자 무시·단어 경계 일치로 찾는다. 다른 이름·학명·추출물명·철자 변형은 목록에 그 글자가 없으면 못 찾는다.
(Highlight This 의 정확한 일치 규칙은 확인하지 않았다 — 대소문자 무시 부분 문자열 일치로 가정. 단어 경계를 둔 것은
'Ephedra' 가 'Ephedrine' 안에서 잡히는 식의 우연 일치까지 기준선 탓으로 돌리지 않으려는 보수적 선택.)
"""
import re


def terms_from_blocklist(block):
    """BlockList → [(term, entry_label)] (길이 3자 이상, 중복 제거)."""
    seen, out = set(), []
    for r in block.records:
        label = r.get("label") or (r.get("names") or [""])[0]
        names = r.get("names") or [v for v in r.values() if isinstance(v, str)]
        for n in names:
            t = (n or "").strip()
            if len(t) >= 3 and t.lower() not in seen:
                seen.add(t.lower())
                out.append((t, label))
    return out


def highlight_matches(lines, terms):
    """라벨 줄마다 일치한 단어. -> [{line, term, entry}]"""
    hits = []
    for ln in lines:
        for term, entry in terms:
            if re.search(r"(?<![0-9A-Za-z가-힣])" + re.escape(term) + r"(?![0-9A-Za-z가-힣])", ln, re.I):
                hits.append({"line": ln, "term": term, "entry": entry})
    return hits


def compare(ingredients, hits):
    """성분마다 기준선(단어 일치) vs 우리 판정.
    ingredients: [{name, line, ours: blocked|clear|review, entry}] → 행 목록과 집계.
    category: both_blocked | ours_only(단어 일치가 놓침) | baseline_only(단어 일치만 막음) | both_clear | ours_review"""
    rows = []
    for ing in ingredients:
        h = [x for x in hits if x["line"] == ing["line"] or ing["name"].lower() in x["line"].lower()]
        base = bool(h)
        ours = ing["ours"]
        cat = ("ours_review" if ours == "review" else
               "both_blocked" if base and ours == "blocked" else
               "ours_only" if ours == "blocked" else
               "baseline_only" if base else "both_clear")
        rows.append({**ing, "baseline": "blocked" if base else "clear", "baseline_terms": [x["term"] for x in h],
                     "category": cat})
    count = {k: sum(r["category"] == k for r in rows) for k in
             ("both_blocked", "ours_only", "baseline_only", "both_clear", "ours_review")}
    return {"rows": rows, "count": count, "baseline_blocked": sum(r["baseline"] == "blocked" for r in rows),
            "ours_blocked": sum(r["ours"] == "blocked" for r in rows)}
