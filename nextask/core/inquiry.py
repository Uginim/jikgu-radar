"""Draft only. The model supplies the polite request; code owns every product fact and question."""
import os
import re
import unicodedata

from ._infra_client import ZoroClient, ZoroError, ZoroUnavailable


# These are requests for information, not assertions about the product.
_REQUESTS = {
    "label": ("the full ingredient list for the current product, including other ingredients and excipients",
              "현재 상품의 기타 성분·부원료를 포함한 전체 원재료 목록"),
    "photo": ("a legible photo of all ingredient panels on the current package",
              "현재 포장의 성분 표시 전체를 읽을 수 있는 사진"),
    "edition": ("confirmation of which label applies to the product currently supplied",
                "현재 공급 중인 상품에 적용되는 라벨 판본"),
    "amount": ("the serving size and amounts printed on the current label, to resolve differing source values",
               "자료 간 함량 차이를 확인하기 위한 현행 라벨의 1회 제공량 및 성분별 함량"),
    "identity": ("the exact ingredient name or specification for “{}”, to clarify its identity",
                 "동일성 확인을 위한 ‘{}’의 정확한 원료명 또는 규격"),
    "blend": ("the component names and amounts per serving for “{}”",
              "‘{}’의 세부 원료명과 1회 제공량당 각 함량"),
}


def _field(value):
    if not isinstance(value, str):
        return ""
    # Keep captured names verbatim apart from controls; never let a product title add mail headers/lines.
    return " ".join("".join(" " if unicodedata.category(c) in ("Cc", "Cf") else c for c in value).split())


def collect_facts(snapshot):
    """Read only the current case's identity and unfinished items; never put guesses in the letter."""
    inp = snapshot.get("input") or {}
    title = _field(inp.get("title")) or _field((snapshot.get("product") or {}).get("title"))
    barcode = _field(inp.get("barcode"))
    requests = []
    seen = set()

    def need(kind, reason, name=""):
        if (kind, name) not in seen:
            seen.add((kind, name))
            requests.append({"kind": kind, "name": name, "reason": reason})

    scope = ((snapshot.get("extras") or {}).get("label_evidence") or {}).get("review_requirements") or {}
    checks = scope.get("checks") or {}
    if any(checks.get(k) is False for k in ("ingredient_sections", "ingredient_coverage")):
        need("label", "ingredient sections or coverage unconfirmed")
    if any(checks.get(k) is False for k in ("read_quality", "model_readable")):
        need("photo", "label readability unconfirmed")
    if checks.get("source_consistency") is False:
        need("amount", "recorded label amount conflict")
    if any(checks.get(k) is False for k in ("current_source", "seller_scope_confirmed")):
        need("edition", "current product or label scope unconfirmed")
    for item in snapshot.get("items") or []:
        if item.get("status") != "handoff":
            continue
        iid = item["id"]
        reason = _field(item.get("reason"))
        if iid == "label":
            need("photo", reason)
        elif iid == "scope" and not checks:
            need("label", reason)
        elif iid.startswith("ing:"):
            card = item.get("card") or {}
            name = _field(card.get("ingredient")) or _field(item.get("title")).removeprefix("성분: ").strip()
            if name:
                blend = re.search(r"\b(?:blend|mixture|matrix)\b|혼합(?:물|분말|원료)", name, re.I)
                need("blend" if blend else "identity", reason, name)
    return {"product": title, "barcode": barcode if re.fullmatch(r"(?:[0-9]{8}|[0-9]{12,14})", barcode) else "",
            "evidence_id": scope.get("evidence_id"), "requests": requests}


# Validate whole clauses, in order, in both languages. The only permissible grammatical object is
# the opaque reference issued for that clause. Thus negation, omitted/added facts and swapped
# translations cannot pass; product data never enters the model prompt.
_EN = re.compile(r"(?:(?:Could|Would|Can) you (?:please )?(?:provide|share) (@[1-6])\?|Please (?:provide|share) (@[1-6])[.?]?)", re.I | re.ASCII)
_KO = re.compile(r"(@[1-6])을 (?:알려|제공해|보내)\s?주(?:시겠(?:습니까|어요)\?|실 수 있(?:나요|을까요)\?|세요\.)")


def _sentences(text, kinds):
    if not isinstance(text, str):
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != len(kinds) * 2:
        return None
    sentences = {}
    for pos, kind in enumerate(kinds):
        en, ko = lines[pos * 2:pos * 2 + 2]
        a, b = _EN.fullmatch(en), _KO.fullmatch(ko)
        ref = f"@{pos + 1}"
        if not a or not b or (a[1] or a[2]) != ref or b[1] != ref:
            return None
        if a[2]:  # Typography only, after the entire imperative and its sole fact reference matched.
            en = en.rstrip("?.") + "."
        sentences[kind] = (en.replace(ref, "{}"), ko.replace(ref, "{}"))
    return sentences


def _prompt(kinds):
    menu = "\n".join(f"@{i}: {_REQUESTS[kind][0]}" for i, kind in enumerate(kinds, 1))
    return ("Draft a polite manufacturer inquiry about the requested facts below. Each @number stands for "
            "one noun phrase inserted by the application. Keep the marker, do not copy its description. "
            "For each marker, in order, write one English request then its Korean translation on the next line. "
            "Use only these forms: Could you please provide @1? / Would you share @1? / Please provide @1. "
            "Korean: @1을 제공해 주세요. or @1을 보내 주시겠습니까? Replace 1 with the current number. "
            "No headings, lists, extra statements or greetings. Output only the finished bilingual sentences.\n"
            + menu)


def make_draft(facts):
    """Returns the user-visible draft and a separate audit record; rejected model output is audit-only."""
    if not facts["requests"]:
        raise ValueError("no outstanding ingredient questions")
    kinds = list(dict.fromkeys(row["kind"] for row in facts["requests"]))
    prompt = _prompt(kinds)
    sentences = None
    generated = ""
    request_id = None
    error = None
    state = "generation_unavailable"
    if os.environ.get("ZORO_MODE", "mock") in ("remote", "record"):
        try:
            response = ZoroClient(retries=1).generate(prompt=prompt, max_new_tokens=48 * len(kinds))
            generated = response.get("text") or ""
            request_id = response.get("request_id")
            sentences = _sentences(generated, kinds)
            state = "accepted" if sentences else "outside_grounded_grammar"
        except (ZoroError, ZoroUnavailable) as exc:
            error = type(exc).__name__
    source = "zoro" if sentences else "template"
    fallback = ("Could you please provide {}?", "{}을 보내 주시겠습니까?")
    en, ko = ["Hello,"], ["안녕하세요."]
    if facts["product"]:
        en.append("Product: " + facts["product"])
        ko.append("상품명: " + facts["product"])
    if facts["barcode"]:
        en.append("Barcode: " + facts["barcode"])
        ko.append("바코드: " + facts["barcode"])
    en.append("")
    ko.append("")
    for item in facts["requests"]:
        a, b = _REQUESTS[item["kind"]]
        en_form, ko_form = sentences[item["kind"]] if sentences else fallback
        en.append(en_form.format(a.format(item["name"])))
        ko.append(ko_form.format(b.format(item["name"])))
    en.extend(("", "Thank you."))
    ko.extend(("", "감사합니다."))
    content = {"subject_en": "Ingredient information request", "subject_ko": "제품 성분 정보 문의",
               "body_en": "\n".join(en), "body_ko": "\n".join(ko)}
    draft = {"status": "ready", "draft": content, "source": source, "facts": facts,
             "meta": {"request_id": request_id}, "validation": {"status": state, "fallback_used": not bool(sentences)}}
    return draft, {"prompt": prompt, "raw_generation": generated, "source": source,
                   "status": state, "request_id": request_id, "error": error}
