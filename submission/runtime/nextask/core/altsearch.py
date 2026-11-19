"""(P1) 특징으로 사이트 안 검색 + 대체 제안 — 같은 검수 엔진을 후보마다 돌린다.

1. interpret(query): 패널 자유 입력 → 조건 {use, form, exclude[], price_max}. 용도·제형은 Zoro-1 결정(보기 = classification.json),
   제외 성분·가격대는 코드(목록 이름 단어 일치, '없는/without/제외', 숫자+원/$). 용도 확률이 낮으면 질문 1개를 돌려준다.
2. (확장) 백그라운드 탭으로 사이트 검색 결과를 열어 후보 상품 페이지를 최대 5개 읽어 보낸다.
3. rank(conditions, candidates): 후보마다 검수 엔진(성분표 확보 → 분류 → 성분 판정) → 조건 일치 Zoro-1 결정
   {matches / partly / no} → 차단 성분이 있거나 제외 성분이 들어 있으면 제외(코드) → 상위 3개 + 근거 카드.
판정 결과·차단 여부는 엔진의 것을 그대로 쓰고, 이 모듈은 조건 해석과 순위만 맡는다.
"""
import json
import os
import re
import time

from . import caselog
from .matching import has_word, norm

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLS = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins", "supplement_data", "classification.json"),
                     encoding="utf-8"))
MATCH_OPTS = {"matches": "The product fits all the conditions (use, form, excluded ingredients, price).",
              "partly": "The product fits the main use but misses a secondary condition (form or price).",
              "no": "The product does not fit the requested use or contains an excluded ingredient."}


_EXCLUSION_NAMES = (
    ("카페인", "caffeine"), ("멜라토닌", "melatonin"), ("요힘빈", "yohimbine"),
    ("젤라틴", "gelatin", "gelatine"), ("유당", "lactose"), ("글루텐", "gluten"),
)

_SEARCH_USE = {"pre_workout": "pre workout", "weight": "weight management", "sleep": "sleep",
               "focus": "focus", "sexual": "male vitality", "joint": "joint", "general": "vitamin"}
_SEARCH_FORM = {"capsule": "capsules", "tablet": "tablets", "powder": "powder", "liquid": "liquid", "jelly": "gummies"}


def _search_query(conditions, use):
    # Search terms improve retrieval only; they never establish exclusion, price or fit.
    purpose = _SEARCH_USE.get(use)
    if purpose is None:
        # No grounded translation for an unclassified use. Keep the actual product words.
        return re.sub(r"\s*(?:찾아\s*(?:주세요|줘요?|주라)|검색해\s*(?:주세요|줘요?))[\s.!?]*$", "",
                      conditions["query"]).strip() or conditions["query"]
    terms = [purpose, _SEARCH_FORM.get(conditions["form"])]
    for term in conditions["exclude"]:
        names = exclusion_names(term)
        en = next((n for n in names if n.isascii()), None)
        terms.append(f"{en} free" if en else f"{term} 없는")
    return " ".join(t for t in terms if t)


def exclusion_names(term):
    """Exact bilingual label spellings, never substring matching (iron / environment)."""
    return next((names for names in _EXCLUSION_NAMES if norm(term) in names), (term,))


def _exclusions(query):
    # A positive request for a listed ingredient is not an exclusion.
    neg = r"(?:없는|없이|빼고|제외|안\s*들어간|무첨가|불포함)"
    out = []
    for m in re.finditer(r"([가-힣A-Za-z0-9\-]{2,30}?)\s*(?:이|가|을|를)?\s*" + neg, query, re.I):
        out.append(m[1])
    for m in re.finditer(r"\b(?:without|no|free of)\s+([a-z][a-z-]+)|\b([a-z]+)-free\b", query, re.I):
        out.append(m[1] or m[2])
    return list(dict.fromkeys(out))


_AMOUNT = re.compile(r"(?:(?P<prefix>[$₩])\s*(?P<a>\d[\d,]*(?:\.\d+)?)|"
                     r"(?P<b>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>만\s*원|원|krw|usd|달러|\$))", re.I)


def price_bounds(query):
    lo = hi = None
    # Expand a shared currency/unit before parsing ("1~3만원", "$10–20").
    query = re.sub(r"(\d[\d,.]*)\s*[~–-]\s*(\d[\d,.]*)\s*(만\s*원|원|달러|krw|usd)",
                   r"\1\3 이상 \2\3 이하", query, flags=re.I)
    query = re.sub(r"([$₩])(\d[\d,.]*)\s*[~–-]\s*([$₩])?(\d[\d,.]*)",
                   lambda m: f"from {m[1]}{m[2]} to {m[3] or m[1]}{m[4]}", query)
    for m in _AMOUNT.finditer(query):
        unit = (m["prefix"] or m["unit"]).lower().replace(" ", "")
        value = float((m["a"] or m["b"]).replace(",", ""))
        amount = {"value": value * (10000 if unit == "만원" else 1), "currency": "USD" if unit in ("usd", "$", "달러") else "KRW"}
        after, before = query[m.end():], query[:m.start()]
        if re.match(r"\s*(?:초과|미만)", after) or re.search(r"(?:under|over|less than|more than)\s*$", before, re.I):
            amount["exclusive"] = True
        if re.match(r"\s*(이상|부터|초과|[~–-])", after) or re.search(r"(?:at least|from|over|more than)\s*$", before, re.I):
            lo = amount
        else:
            hi = amount
    return lo, hi


def interpret(engine, query, case_id=None):
    """-> {conditions, question?, probs}. 질문은 용도 확률이 0.5 미만일 때 하나만."""
    req = {"state": {"user_request": query},
           "questions": {q: {"type": "choice", "instructions": (
               "Choose the intended use the seller explicitly wants. Choose other if not stated." if q == "use" else
               "Choose the dosage form explicitly requested. Choose other if no form is requested; do not infer a typical form."),
                             "criteria": {k: v["criteria"] for k, v in CLS[q].items()}} for q in ("use", "form")}}
    res, meta = engine.zoro.decide(req, mock_fn=_interpret_mock)
    a = {q: res["answers"][q]["probabilities"] for q in ("use", "form")}
    use = max(a["use"], key=a["use"].get)
    form = max(a["form"], key=a["form"].get)
    price_min, price_max = price_bounds(query)
    cond = {"query": query, "use": use, "use_label": CLS["use"][use]["label"], "use_p": round(a["use"][use], 3),
            "form": form if a["form"][form] >= 0.5 and form != "other" else None,
            "form_label": CLS["form"][form]["label"] if a["form"][form] >= 0.5 and form != "other" else "상관없음",
            "exclude": _exclusions(query), "price_min": price_min, "price_max": price_max}
    question = None
    if a["use"][use] < 0.5 or use == "other":
        top2 = sorted(a["use"], key=a["use"].get, reverse=True)[:2]
        question = {"text": f"찾는 제품이 '{CLS['use'][top2[0]]['label']}'용인가요, '{CLS['use'][top2[1]]['label']}'용인가요?",
                    "choices": {k: CLS["use"][k]["label"] for k in top2},
                    "search_queries": {k: _search_query(cond, k) for k in top2}}
    if case_id:
        caselog.log_event(case_id, "alt_interpret", query=query, conditions=cond, probs=a, question=question,
                          source=meta.get("source"), latency_ms=meta.get("latency_ms"))
    return {"conditions": cond, "search_query": _search_query(cond, use), "question": question, "probs": a, "source": meta.get("source")}


def _interpret_mock(request):
    """합성 확률(실제 판정 아님): 질의 키워드로."""
    t = request["state"]["user_request"].lower()
    def dist(keys, kw):
        w = {k: 0.3 for k in keys}
        for k, words in kw.items():
            if k in w and any(x in t for x in words):
                w[k] = 6.0
        z = sum(w.values())
        return {k: v / z for k, v in w.items()}
    use = dist(CLS["use"], {"pre_workout": ["운동", "프리워크아웃", "pre-workout", "에너지"], "weight": ["다이어트", "체중", "가르시니아"],
                            "sleep": ["수면", "잠", "sleep"], "focus": ["집중", "기억", "focus"], "joint": ["관절", "글루코사민"],
                            "general": ["비타민", "vitamin", "영양"], "sexual": ["남성", "활력"]})
    form = dist(CLS["form"], {k: v.get("keywords", []) for k, v in CLS["form"].items()})
    return {"use": use, "form": form}


def rank(engine, conditions, candidates, case_id=None):
    """후보마다 검수 엔진 → 조건 일치 결정 → 순위. 질문이 필요한 후보는 '모름'으로 넘겨 끝까지 판정(셀러 개입 없이)."""
    out = []
    for cand in candidates[:5]:
        t0 = time.perf_counter()
        c = engine.start(text=cand.get("title") or "", inputs={**cand, "role": "seller", "alt_of": case_id})
        guard = 0
        while c.status == "waiting_user" and guard < 6:
            guard += 1
            engine.answer(c, None)
        cl = c.data.get("classification") or {}
        ings = [it.data["name"] for it in c.items if it.spec.kind == "ingredient"]
        blocked = [it.data["name"] for it in c.items if it.spec.kind == "ingredient" and it.result == "blocked"]
        excl_hit = [x for x in conditions.get("exclude", []) if any(has_word(x, n) or norm(x) in norm(n) for n in ings)]
        req = {"state": {"conditions": {k: conditions.get(k) for k in ("use_label", "form_label", "exclude", "price_max")},
                         "product": cand.get("title"), "classification": {k: (cl.get(k) or {}).get("label") for k in ("product_type", "form", "use")},
                         "ingredients": ings[:25], "price": cand.get("price")},
               "questions": {"fit": {"type": "choice", "instructions": "Does this product fit the seller's conditions?",
                                     "criteria": MATCH_OPTS}}}
        res, meta = engine.zoro.decide(req, mock_fn=lambda r, cl=cl: _fit_mock(r, conditions, cl))
        fit = res["answers"]["fit"]["probabilities"]
        label_gate = next((it for it in c.items if it.spec.kind == "label"), None)
        scope_complete = bool(label_gate and label_gate.spec.completion_guard(c, label_gate)["complete"])
        excluded_by = ("차단 성분 포함: " + ", ".join(blocked)) if blocked else ("제외 성분 포함: " + ", ".join(excl_hit)) if excl_hit \
            else (c.verdict["label"] if c.verdict["key"] != "not_found"
                  else ("전체 성분표 확인 남음 — 대체 추천 보류" if not scope_complete else None))
        row = {"url": cand.get("url"), "title": cand.get("title"), "brand": cand.get("brand"), "image": cand.get("image"),
               "case_id": c.id, "verdict": c.verdict, "classification": {k: (cl.get(k) or {}).get("label") for k in ("product_type", "form", "use")},
               "fit": fit, "fit_top": max(fit, key=fit.get), "excluded_by": excluded_by, "n_ingredients": len(ings),
               "scope": "읽은 범위에서 찾지 못함" if c.verdict["key"] == "not_found" else c.verdict["label"],
               "scope_complete": scope_complete,
               "ms": round((time.perf_counter() - t0) * 1000, 1)}
        out.append(row)
    ok = [r for r in out if not r["excluded_by"] and r["fit_top"] != "no"]
    ok.sort(key=lambda r: (r["fit"].get("matches", 0) + 0.5 * r["fit"].get("partly", 0)), reverse=True)
    result = {"top": ok[:3], "excluded": [r for r in out if r["excluded_by"] or r["fit_top"] == "no"], "checked": len(out)}
    if case_id:
        caselog.log_event(case_id, "alt_rank", conditions=conditions,
                          candidates=[{k: r[k] for k in ("title", "url", "case_id", "fit", "excluded_by", "verdict")} for r in out],
                          top=[r["title"] for r in result["top"]])
    return result


def _fit_mock(request, conditions, cl):
    """합성 확률(실제 판정 아님): 분류의 용도·제형이 조건과 같은지로."""
    st = request["state"]
    u_ok = (cl.get("use") or {}).get("key") == conditions.get("use")
    f_ok = not conditions.get("form") or (cl.get("form") or {}).get("key") == conditions.get("form")
    p = {"matches": 0.85, "partly": 0.1, "no": 0.05} if u_ok and f_ok else \
        {"matches": 0.15, "partly": 0.7, "no": 0.15} if u_ok else {"matches": 0.05, "partly": 0.15, "no": 0.8}
    return {"fit": p}
