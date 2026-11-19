"""주제 플러그인: 해외직구 식품·영양제의 '숨은 차단 성분' 검수 (크롬 확장 + 에이전트 서버, mvp-architecture.md).

입력: 확장 프로그램이 열린 상품 페이지에서 읽은 것 —
      {url, title, brand, page_text, ingredient_html?, role(seller|consumer)} + 선택: 캡처/영역 선택 이미지
항목:
  label    성분표 확보: 페이지 글자 → (없거나 못 믿으면) 자동 캡처 요청 → OCR → Zoro-1 글자 신뢰 판정
           → 낮으면 사용자에게 영역 선택 요청 → OCR → 재판정 → 2회 실패면 '성분표를 읽지 못함'
  ing:*    성분마다 ① 부분 일치 → ② 사전 → ③ Zoro-1 (판정 로직은 matching.py — research 비교 실행기와 같은 함수)
           ③ 이 애매하면 원료 재조회 → 재판정 → 셀러 질문 1개 → 그래도 애매하면 '확인 필요'
           → 근거 카드(목록 항목·지정일·근거·원문 링크·판정 단계, 성분 단위 설명은 코드가 구성)
결과 배지: 차단 성분 발견 → 등록 보류 권고 / 차단 성분을 찾지 못함(+확인 범위) / 확인 필요 / 성분표를 읽지 못함
행동(모의 상점 장부): 등록 보류·사람 검토 대기열·셀러 알림(자동, 되돌릴 수 있음). 소비자는 페이지 경고만.
P2(NEXTASK_P2=1): 판매 중지 권고, 해외 구매 보류(주문이 들어와도 사지 않도록 표시), 고객 안내 초안(셀러 승인).
"""
import difflib
import glob
import time
import json
import os
import re

from .. import caselog, store
from ..engine import fixed_pipeline
from ..generator import explain
from ..i2715 import lookup as i2715_lookup
from ..ingredient_knowledge import retrieve as retrieve_knowledge
from ..config import redact
from ..entry import photo_declared_names
from ..matching import (LIST_PAGE, Registry, judge_ingredient, load_dictionary, norm, read_zoro, registry_from_api,
                        stage_dictionary, stage_partial, string_candidates, zoro_request)
from ..mfds import AliasLookup
from ..ocr import get_ocr
from ..plugin import Item, ItemSpec, Plugin, Question, Tool
from ..sources import DSLDSearch, FDARecalls, PubChemSynonyms, label_amount_conflicts, label_lines

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "supplement_data")


def load_registry():
    """차단 원료 목록: MFDS_BLOCKLIST(지정 파일) → data/mfds/snapshot/registry_*.json(최근 고정 스냅숏, 기본)
    → [MFDS_LIVE=1 이거나 스냅숏이 없을 때] DATA_GO_KR_KEY 원료 API(하루 캐시) → 웹 목록 사본 → 샘플."""
    fixed = os.environ.get("MFDS_BLOCKLIST") or os.environ.get("MFDS_REGISTRY")
    if fixed and os.path.exists(fixed if os.path.isabs(fixed) else os.path.join(ROOT, fixed)):   # 촬영용 결과 고정
        return Registry.load(fixed if os.path.isabs(fixed) else os.path.join(ROOT, fixed))
    snaps = sorted(glob.glob(os.path.join(ROOT, "data", "mfds", "snapshot", "registry_*.json")))
    if snaps and os.environ.get("MFDS_LIVE") != "1":           # 기본: 가장 최근 고정 스냅숏(영상·평가 재현)
        return Registry.load(snaps[-1])
    p = (sorted(glob.glob(os.path.join(ROOT, "data", "mfds", "foodsafety_block_raw_irdnt_*.json"))) or [None])[-1]
    key = os.environ.get("DATA_GO_KR_KEY")
    if key:
        try:
            return registry_from_api(key, os.path.join(ROOT, "data", "mfds"), time.strftime("%Y-%m-%d"))
        except Exception as e:  # noqa: BLE001 -- 키 값이 메시지에 섞이지 않게 가린다
            print(f"[registry] 15132686 API 실패 → 사본 사용: {redact(f'{type(e).__name__}: {e}')}", flush=True)
            reg = Registry.load(p or os.path.join(DATA, "blocklist_sample.json"))
            reg.source += f" (원료 API 실패로 사본 사용: {redact(type(e).__name__)})"
            return reg
    return Registry.load(p or os.path.join(DATA, "blocklist_sample.json"))


def load_dicts():
    """② 사전: 원료 별칭 표(원료성분 API 자리, 지금은 샘플) + PubChem 동의어 사본(있으면)."""
    ds = [("원료 별칭(샘플)", load_dictionary(os.path.join(DATA, "aliases_sample.json")))]
    pc = os.environ.get("PUBCHEM_SYNONYMS") or os.path.join(ROOT, "data", "mfds", "pubchem-synonyms.json")
    if os.path.exists(pc):
        ds.append(("PubChem", load_dictionary(pc)))
    ds.append(("PubChem·공식 이명 연결", load_dictionary(os.path.join(DATA, "official_alias_links.json"))))
    return ds


with open(os.path.join(DATA, "verified_taxon_links.json"), encoding="utf-8") as _taxon_file:
    TAXON_LINKS = json.load(_taxon_file)["links"]
REG = load_registry()
DICTS = load_dicts()
ALIAS = AliasLookup()
PUBCHEM = PubChemSynonyms()
DSLD = DSLDSearch()
FDA = FDARecalls()
OCR = get_ocr()
NOT_INGREDIENT = re.compile(r"(?i)^(serving size|servings per|amount per|% ?daily|calories|total (fat|carbohydrate|sugars?)\b"
                            r"|1회\s*제공|용기당|칼로리|열량|총\s*(지방|탄수화물|당류)"
                            r"|less than\b|includes added\b|\*|each (capsule|tablet|softgel|serving|scoop)|net\s+(?:wt\b\.?|weight\b)"
                            r"|.*\b\d+\s*(veg(etarian)?\s*)?(capsules|caps|tablets|softgels|gummies|servings|count|ct)\b)")
AMOUNT = re.compile(r"(?<![\w.])\d[\d,.]*\s*(mcg|mg|[µμ]g|ug|g|iu|oz|%)(?![A-Za-z])", re.I)
STANDARD_NUTRIENT = re.compile(r"(?i)^(?:vitamin [abcdek][0-9]*|niacin|thiamin|thiamine|riboflavin|biotin|folate|folic acid|pantothenic acid|sodium|potassium|calcium|magnesium|iron|zinc|selenium|copper|manganese|chromium|molybdenum|iodine|chloride|phosphorus)$")
N_CANDIDATES = 5          # ③ Zoro-1 후보 수(문자열 유사 상위 k, research f5 top5 와 같음)


HEADER = re.compile(r"(?i)^(supplement facts|nutrition facts|serving size.*|servings? per.*|ingredients?|amount per serving|"
                    r"% ?daily value|\*+|†.*|daily value.*|\d+ (veg )?(capsule|tablet|softgel|scoop)s?)$")
LEAD_AMOUNT = re.compile(r"(?i)^\s*\d[\d,.]*\s*(mg|mcg|[µμ]g|ug|g|iu|oz)\b\s+([A-Za-z가-힣].*)$")
OTHER = re.compile(r"(?i)^other ingredients?\s*:?\s*(.*)$")


SENTENCE = re.compile(r"(?i)\b(may be|processed|processing|manufactured|basis|contains? less|facility|allergen)\b|^total\b")


def _split_top(t):
    """Split list separators outside parenthesized ingredient qualifiers."""
    out, depth, start = [], 0, 0
    for match in re.finditer(r"[\[\]();]|(?<!\d),|,(?!\d)|\s+및\s+|(?<!\bNatural)(?<!\bnatural)\s+and\s+(?=[A-Z])(?!Artificial\b)", t):
        token = match.group()
        if token in ("[", "("):
            depth += 1
        elif token in ("]", ")"):
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(t[start:match.start()])
            start = match.end()
    return out + [t[start:]]


def _split_other(t):
    parts = [re.sub(r"(?i)^(and|및)\s+", "", x.strip(" .\u00a0")) for x in _split_top(t)]
    return [x for x in parts if re.search(r"[A-Za-z가-힣]", x)
            and x.lower() != "none" and not SENTENCE.search(x) and not x.endswith((":", "："))]   # 끝이 ':' 이면 제목

# 함량이 혼합물 이름 줄에만 있고 자식 성분은 아래 줄에 쉼표로 나열되는 표기(예: "Energy and Focus Blend: 696 mg").
BLEND = re.compile(r"(?i)\b(blend|matrix|complex|proprietary|formula)\b|[:：]$")
# 각주·기준 문장은 함량 숫자가 들어 있어도 성분 이름이 아니다.
# "Gelatin. Contains <2% of: Silica, ..." 의 안내 문구는 성분 이름이 아니라 목록 구분이다
MINOR_HEAD = re.compile(r"(?i)\.?\s*\bcontains?\s*(?:<|less than)\s*\d+\s*%\s*(?:of)?\s*[:：]?\s*")
CONTAINS_HEAD = re.compile(r"(?i)^(contains|containing|consisting of|including)\s*[:：]?$")
FOOTNOTE = re.compile(r"(?i)^[❖*†‡♢◊♦◆§¹²³]|percent daily|daily values?\b|calorie diet|영양소\s*기준치|칼로리\s*식단")


def _amount_at_top(text):
    depth, cursor = 0, 0
    for match in AMOUNT.finditer(text):
        for ch in text[cursor:match.start()]:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth = max(0, depth - 1)
        if depth == 0:
            return match
        cursor = match.end()
    return None


LIST_END = re.compile(r"(?i)^(no\s|not\s|made\b|manufactured\b|distributed\b|suggested\b|directions\b|dosage\b|warnings?\b"
                      r"|caution\b|storage\b|keep\b|learn more\b|allergen\b|contains\s*:|supplement facts|nutrition facts"
                      r"|serving|amount per|\*|†|daily value)")


def parse_ingredients(lines):
    """Parse declared lists across wrapped OCR rows; quantities inside qualifiers are not row boundaries."""
    out, prev, n = [], None, 0
    texts = [ln["text"].strip() for ln in lines]
    while n < len(texts):
        t = texts[n]
        n += 1
        m = OTHER.match(t) or re.match(r"(?i)^(?:ingredients?|성분|재료)\s*[:：]\s*(.*)$", t)
        if m:
            parts = [m.group(1)]
            while n < len(texts) and not parts[-1].endswith("."):
                following = texts[n]
                if (LIST_END.match(following) or OTHER.match(following) or HEADER.match(following)
                        or _amount_at_top(following)):
                    break
                parts.append(following)
                n += 1
                if following.endswith("."):
                    break
            out.extend(_split_other(MINOR_HEAD.sub(", ", " ".join(parts))))
            prev = None
            continue
        lead = LEAD_AMOUNT.match(t)
        if lead and not NOT_INGREDIENT.match(t):
            out.append(lead.group(2).strip(" ,"))
            prev = None
            continue
        amount = _amount_at_top(t)
        if amount and t.count(")") > t.count("(") and "(" not in t[:t.index(")")]:
            prev = None
            continue
        if not amount and t.count("(") > t.count(")") and not NOT_INGREDIENT.match(t) and not OTHER.match(t):
            # OCR 이 한 줄을 괄호 중간에서 끊은 경우: "Fenugreek seed extract (standardized 500 mg" / "to 50% saponins)"
            inner = list(AMOUNT.finditer(t))
            if inner and re.search(r"[A-Za-z가-힣]", t[:inner[-1].start()]) and not FOOTNOTE.search(t):
                name = t[:inner[-1].start()].strip(" ,")
                if n < len(texts) and texts[n].count(")") > texts[n].count("("):
                    name += " " + texts[n]
                    n += 1
                out.append(name)
                prev = None
                continue
        if amount and not NOT_INGREDIENT.match(t):
            name = t[:amount.start()].strip(" ,") or prev
            if name and re.search(r"[A-Za-z가-힣]", name) and not FOOTNOTE.search(name):
                out.append(name)
                if BLEND.search(name):
                    children = []      # 자식은 쉼표로 이어지거나(Rage XXX) 한 줄에 하나씩(Echinacea Complex) 적힌다
                    while n < len(texts):
                        following = texts[n]
                        if CONTAINS_HEAD.match(following):
                            n += 1
                            continue
                        dose = _amount_at_top(following)
                        if (LIST_END.match(following) or OTHER.match(following) or HEADER.match(following)
                                or FOOTNOTE.search(following) or (dose and not (dose.group(1) == "%" and "," in following))):
                            break
                        last = children[-1] if children else ""
                        if children and last.endswith("-") and not last.endswith(" -"):
                            children[-1] = last + following          # "R-Beta-" + "Methylphenylethylamine"
                        elif children and (last.endswith(",") or following.startswith("(") or last.count("(") > last.count(")")
                                           or last.count("[") > last.count("]")):
                            children[-1] = last + " " + following
                        else:
                            children.append(following)
                        n += 1
                        if following.endswith("."):
                            break
                    for child in children:
                        out.extend(_split_other(child))
            prev = None
            continue
        prev = None if (HEADER.match(t) or NOT_INGREDIENT.match(t) or LIST_END.match(t) or len(t) > 120) else t
    return list(dict.fromkeys([*out, *photo_declared_names(lines)]))



def declared_rows_covered(lines):
    """Every substantive supplied row must be represented; omitted child rows fail closed."""
    names = [norm(name) for name in parse_ingredients(lines)]
    for line in lines:
        text = line["text"].strip()
        if not text or HEADER.match(text) or NOT_INGREDIENT.match(text) or LIST_END.match(text) or FOOTNOTE.search(text) or text.endswith((":", "：")):
            continue
        other = OTHER.match(text) or re.match(r"(?i)^(?:(?:main|active)\s+)?ingredients?\s*:\s*(.*)$", text)
        if other:
            if not other.group(1).strip():
                return False
            if norm(other.group(1)) == "none":
                continue
            if not all(any(norm(part) in name for name in names) for part in _split_other(other.group(1))):
                return False
            continue
        amount = _amount_at_top(text)
        part = text[:amount.start()].strip(" ,") if amount else text.strip(" †*")
        if part and not any(norm(part) in name for name in names):
            return False
    return bool(names)


def photo_scope_complete(lab):
    """A complete supplied facts panel, not proof of the seller's physical SKU."""
    lines = lab.get("lines", [])
    start = next((i for i, row in enumerate(lines)
                  if re.fullmatch(r"(?i)\s*(supplement|nutrition)\s+facts\s*", row["text"])), None)
    if start is None:
        return False
    end = next((i for i in range(start + 1, len(lines))
                if re.match(r"(?i)^other ingredients?\s*:", lines[i]["text"])), None)
    if end is None:
        return False
    other_end = end
    while other_end < min(end + 5, len(lines) - 1) and not re.search(r"\.(?:\s|$)", lines[other_end]["text"]):
        other_end += 1
    panel = lines[start:other_end + 1]
    text = "\n".join(row["text"] for row in panel)
    # A dangling declaration or unbalanced qualifier can be a cropped panel.
    return (bool(re.search(r"\.(?:\s|$)", lines[other_end]["text"]))
            and text.count("(") == text.count(")")
            and bool(re.search(r"(?i)serving\s+size", text))
            and any(_amount_at_top(row["text"]) for row in panel[1:-1])
            and not re.search(r"(?i)unreadable|illegible|truncated|\.\.\.", text)
            and all(isinstance(row.get("conf"), (int, float)) and row["conf"] >= .85 for row in panel))


def _inp(case):
    return case.data.get("input", {})


# ---- 성분표 확보 (페이지 글자 → 자동 캡처 → OCR → 영역 선택 요청) --------------------------------------------------

def read_dom(case, item):
    """확장이 보낸 페이지 글자(page_text)·성분표 블록(ingredient_html)에서 성분표 줄을 찾는다."""
    i = _inp(case)
    lines = label_lines(i.get("page_text") or i.get("ingredients_text") or "", i.get("ingredient_html") or "")
    if i.get("ingredient_html") and not any(OTHER.match(line) for line in lines):
        page_lines = label_lines(i.get("page_text") or "")
        declarations = [line for line in page_lines if OTHER.match(line)]
        # Match the entire scoped facts table before borrowing its page declaration.
        if lines and page_lines[:len(lines)] == lines and len(declarations) == 1:
            lines.extend(declarations)
    if not parse_ingredients([{"text": x} for x in lines]):
        raise ValueError("no ingredient lines found in page text")
    return {"source": "dom", "source_url": i.get("page_source_url", i.get("url")), "lines": [{"text": x, "conf": None} for x in lines], "mean_conf": None,
            "_args": {"page_text_chars": len(i.get("page_text") or ""), "ingredient_html_chars": len(i.get("ingredient_html") or "")}}


def ocr_capture(case, item):
    """T2: 캡처·영역 선택 이미지 OCR."""
    from ..ocr import OCRUnavailable
    args = {"image": case.image_path, "engine": OCR.name}
    if not case.image_path:
        r = {"status": "capture_missing", "reason": "캡처 미제공 — OCR 미실행"}
    else:
        try:
            r = OCR.read(case.image_path)
            r.update(status="read" if r.get("lines") else "read_empty",
                     reason="OCR 글자 확보" if r.get("lines") else "OCR 실행 결과 글자 없음")
        except OCRUnavailable as ex:
            r = {"status": "ocr_unavailable", "reason": str(ex)}
        except Exception as ex:
            r = {"status": "ocr_failed", "reason": f"OCR 실행 실패: {redact(ex)}"}
            if hasattr(ex, "attempts"):
                r.update(attempts=ex.attempts, attempt_count=len(ex.attempts))
    r.update(source="ocr", source_url=case.data.get("capture_source_url", _inp(case).get("url")), _args=args)
    owner = item.data if item is not None else case.data
    owner.setdefault("_acquisitions", []).append(
        {k: r[k] for k in ("source", "status", "reason", "attempts", "attempt_count") if k in r})
    if _inp(case).get("image_origin") in ("seller", "gallery"):
        origin = _inp(case)["image_origin"]
        r["image_origin"] = origin
        r["source_description"] = "판매자가 준 사진" if origin == "seller" else "현재 상품 갤러리에서 선택한 성분표 사진"
    return r


def dsld_label(case, item):
    """T3: NIH DSLD 후보 조회. 판본 미확인 후보는 현재 상품 성분 근거가 아니다."""
    i = _inp(case)
    try:
        r = DSLD.search(i.get("title") or "", i.get("brand") or "", i.get("barcode") or "")
    except Exception as ex:
        r = {"source": "dsld", "status": "lookup_failed", "reason": f"DSLD 조회 실패: {redact(ex)}", "found": False}
    r["_args"] = {k: i.get(k) for k in ("title", "brand", "brand_source", "barcode", "barcode_source")}
    item.data.setdefault("_acquisitions", []).append({k: r.get(k) for k in ("source", "status", "reason", "candidate")})
    return r


def retain_label(case, item, result):
    """Keep each acquisition; only replace current evidence without losing known ingredient coverage."""
    incoming = {**result, "lines": [dict(x) if isinstance(x, dict) else {"text": x}
                                   for x in result.get("lines", [])]}
    previous = case.data.get("label") or {}
    url = _inp(case).get("url")

    def coverage(label):
        text = "\n".join(x.get("text", "") for x in label.get("lines", []))
        names = {norm(n) for n in parse_ingredients(label.get("lines", []))}
        areas = {key for key, pattern in (
            ("facts", r"(?i)\b(supplement|nutrition)\s+facts\b"),
            ("serving", r"(?i)serving\s+size"),
            ("other", r"(?im)^other ingredients?\s*:\s*\S"),
            ("ingredients", r"(?im)^ingredients\s*:\s*\S")) if re.search(pattern, text)}
        valid = label.get("source") in ("dom", "ocr") and bool(url) and label.get("source_url") == url
        valid = valid and label.get("identity") not in ("conflict", "unverified") and label.get("status") in (None, "read")
        if label.get("source") == "ocr":
            confidence, repeat = label.get("mean_conf"), label.get("repeat_ratio", 0)
            valid = valid and isinstance(confidence, (int, float)) and confidence >= .7
            valid = valid and isinstance(repeat, (int, float)) and repeat <= .2
        return names, areas, bool(valid and names)

    old_names, old_areas, old_valid = coverage(previous)
    names, areas, valid = coverage(incoming)
    if old_valid and valid and {previous.get("source"), incoming.get("source")} == {"dom", "ocr"}:
        dom, ocr = (previous, incoming) if previous["source"] == "dom" else (incoming, previous)
        conflicts = label_amount_conflicts(dom["lines"], ocr["lines"])
        if conflicts:
            case.data["label_amount_conflicts"] = conflicts
            caselog.log_event(case.id, "label_amount_conflict", source="code", conflicts=conflicts,
                              reason="동일 명칭의 DOM·사진 함량 불일치 — 판본 확인 전 전체 검토 완료 금지")
    gallery_over_partial = (
        valid and old_valid and previous.get("source") == "dom" and incoming.get("source") == "ocr"
        and incoming.get("image_origin") == "gallery"
        and case.data.get("gallery_selection", {}).get("selected_index") is not None
        and old_areas in ({"ingredients"}, {"other"}) and len(old_names) == 1
        and {"facts", "serving"} <= areas and bool(areas & {"other", "ingredients"})
        and not case.data.get("label_amount_conflicts")
        and not (item and item.data.get("confirmed_evidence_id"))
        and not any(judge_ingredient(name, REG, DICTS, zoro=None)["decision"] == "match" for name in old_names))
    def doses(label):
        return {re.sub(r"\s+", "", m.group(0)).lower() for row in label.get("lines", [])
                for m in AMOUNT.finditer(row.get("text", "")) if m.group(1) != "%"}
    # 페이지 표에는 기타 성분 선언이 없고, 사진에는 같은 함량 전부 + 성분 선언이 있다(예: 한국어 페이지 표와 영문 라벨 사진).
    fuller_same_doses = (
        valid and old_valid and previous.get("source") == "dom" and incoming.get("source") == "ocr"
        and not (old_areas & {"other", "ingredients"})
        and {"facts", "serving"} <= areas and bool(areas & {"other", "ingredients"})
        and len(doses(previous)) >= 2 and doses(previous) <= doses(incoming) and len(names) > len(old_names)
        and not case.data.get("label_amount_conflicts")
        and not (item and item.data.get("confirmed_evidence_id"))
        and not any(judge_ingredient(name, REG, DICTS, zoro=None)["decision"] == "match" for name in old_names))
    selected = not previous or (valid and (not old_valid or (old_names <= names and old_areas <= areas)
                                          or gallery_over_partial or fuller_same_doses))
    history = case.data.setdefault("label_sources", [])
    if previous and not history:
        history.append({"label": previous, "selected": True, "reason": "existing_evidence"})
    reason = "coverage_preserved_or_improved" if selected else "retained_prior_evidence_no_coverage_loss"
    if gallery_over_partial:
        reason = "structured_gallery_for_scope_confirmation"
    elif fuller_same_doses and not (old_names <= names and old_areas <= areas):
        reason = "fuller_label_with_same_doses"
    history.append({"label": incoming, "selected": selected, "reason": reason})
    if selected:
        case.data["label"] = incoming
    caselog.log_event(case.id, "label_selection", item=item.id if item else None, source="code",
                      incoming_source=incoming.get("source"), selected_source=(case.data.get("label") or {}).get("source"),
                      selected=selected, reason=reason, previous_ingredients=sorted(old_names),
                      incoming_ingredients=sorted(names), lost_ingredients=sorted(old_names - names),
                      lost_sections=sorted(old_areas - areas))
    return selected


NEXT_OPTIONS = {
    "capture": {"label": "화면 캡처 → OCR", "criteria": "Capture the visible product page (label image area) and run OCR on it."},
    "ask": {"label": "셀러에게 성분표 영역 요청", "criteria": "Ask the seller to drag-select the ingredient panel or provide a clear label photo."},
    "stop": {"label": "멈추고 사람에게", "criteria": "Stop and hand over to a human reviewer; the remaining sources are unlikely to help."},
}


HIDDEN_DRUG_USE = re.compile(r"(?i)(slim|skinny|diet|fat ?burn|weight ?loss|thermogenic|appetite|male enhancement|libido|"
                             r"erection|stamina for men|sexual|testosterone booster|다이어트|체중|살 빠|정력|성기능|남성 활력)")


def _hidden_drug_use(case):
    i = _inp(case)
    return bool(HIDDEN_DRUG_USE.search(f"{i.get('title') or ''} {i.get('brand') or ''}"))


def fda_recalls(case, item):
    i = _inp(case)
    r = FDA.search(i.get("title") or "", i.get("brand") or "")
    r["_args"] = {"title": i.get("title"), "brand": i.get("brand")}
    return r


def _lab_ingredients(case):
    return parse_ingredients((case.data.get("label") or {}).get("lines", []))


def _next_answer(engine, case, item, ans):
    """질문형 다음 행동(캡처·영역 선택·페이지 다시 읽기)의 답 처리. 결과는 상태(_outcomes)에 붙이고 다시 결정한다."""
    act = item.data.get("_pending_action")
    out = item.data.setdefault("_outcomes", [])
    if act == "reread":
        res = engine._run_tool(case, item, Tool("read_dom", read_dom, retries=0, on_fail="continue"))
        selected = bool(res and item.spec.apply_tool(case, item, "read_dom", res))
        out.append({"action": act, "selected": selected,
                    "result": f"성분 줄 {len(_lab_ingredients(case))}개" if res else "펼쳐도 성분표 글자 없음"})
        return "rejudge" if selected else "noop"
    if ans is not True or not case.image_path:
        failed = bool(item.data.get("_capture_error"))
        outcome = {"action": act, "source": "capture", "status": "capture_failed" if failed else "capture_missing",
                   "result": item.data.pop("_capture_error", None) or "캡처 미제공 — OCR 미실행"}
        out.append(outcome)
        item.data.setdefault("_acquisitions", []).append({**outcome, "reason": outcome["result"]})
        return "noop"
    res = engine._run_tool(case, item, Tool("ocr_capture", ocr_capture, retries=0))
    if not res or res.get("status") != "read":
        out.append({"action": act, "status": (res or {}).get("status", "ocr_failed"), "result": (res or {}).get("reason", "OCR 도구 호출 실패")})
        return "noop"
    selected = item.spec.apply_tool(case, item, "ocr_capture", res)
    out.append({"action": act, "selected": selected,
                "result": f"OCR 평균 신뢰도 {res.get('mean_conf')}, 채택 자료 성분 줄 {len(_lab_ingredients(case))}개"})
    return "rejudge" if selected else "noop"


def _confirm_label(engine, case, item, ans):
    gate = next(it for it in case.items if it.spec is case.plugin.LABEL)
    status = case.plugin.LABEL.completion_guard(case, gate)
    expected = item.data.pop("_confirmation_requested", None)
    if ans is not True or expected != status["evidence_id"] or not status["ready_for_confirmation"]:
        item.data["handoff_reason"] = "전체 성분자료 확인 거절·미확인 또는 자료 변경 — 읽은 범위의 대조 결과는 유지"
        return "handoff"
    gate.data["confirmed_evidence_id"] = expected
    item.data.setdefault("_outcomes", []).append({"action": "confirm", "result": "셀러가 이 자료의 현재 상품 적용·성분 영역 전체 제공을 확인",
                                                  "evidence_id": expected})
    case.plugin.LABEL.completion_guard(case, gate)
    caselog.log_event(case.id, "label_scope_confirmed", evidence_id=expected, by="seller",
                      review_requirements=gate.data["review_requirements"])
    return "rejudge"


class LabelSource(ItemSpec):
    kind = "label"
    theta_hi, theta_lo, max_questions = 0.85, 0.0, 2
    counts_toward_cap = False
    agentic_next = True
    next_budget = 4
    system2 = False
    recovery_supported = False

    def recovery_state(self, case, item):
        guard = self.completion_guard(case, item)
        names = _lab_ingredients(case)
        missing = list(guard["missing"])
        return {"goal": "이 상품의 전체 성분자료를 확보해 차단 목록과 대조",
                "obtained": [f"판독 성분 {len(names)}개", *names],
                "missing": missing, "review_requirements": guard,
                "label_source": (case.data.get("label") or {}).get("source"),
                "actions_tried": item.data.get("_outcomes", [])}

    def recovery_check(self, case, item):
        if not case.data.get("agent_recovery_enabled"):
            return True
        return bool(_lab_ingredients(case)) and item.data.get("coverage_probs", {}).get("complete", 0) >= self.theta_hi


    def completion_guard(self, case, item):
        """Bounded ingredient-review contract, not SKU/version or legal completeness certification."""
        import hashlib
        lab = case.data.get("label") or {}
        lines = [x.get("text", "") for x in lab.get("lines", [])]
        text = "\n".join(lines)
        current = _inp(case)
        evidence_id = hashlib.sha256(json.dumps(
            {"label": lab, "url": current.get("url"), "title": current.get("title"),
             "image": case.image_path if lab.get("source") == "ocr" else None},
            sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        facts = bool(re.search(r"(?i)\b(supplement|nutrition)\s+facts\b", text))
        other = bool(re.search(r"(?im)^other ingredients?\s*:\s*\S", text))
        plain = bool(re.search(r"(?im)^ingredients\s*:\s*\S", text))
        confidence = lab.get("mean_conf")
        quality = lab.get("source") != "ocr" or (
            isinstance(confidence, (int, float)) and confidence >= 0.7 and
            isinstance(lab.get("repeat_ratio", 0), (int, float)) and lab.get("repeat_ratio", 0) <= 0.2)
        checks = {
            "current_source": lab.get("source") in ("dom", "ocr") and bool(current.get("url"))
                and lab.get("source_url") == current["url"] and lab.get("identity") not in ("conflict", "unverified"),
            "ingredient_sections": bool(_lab_ingredients(case)) and (
                (facts and bool(re.search(r"(?i)serving\s+size", text)) and (other or plain)) or (not facts and plain)),
            "read_quality": quality and lab.get("status") in (None, "read") and (
                lab.get("source") != "ocr" or item.data.get("photo_probs", {}).get("usable", 0) >= self.theta_hi),
            "source_consistency": not case.data.get("label_amount_conflicts") and not case.data.get("additional_source_review"),
            "model_readable": (item.probs or {}).get("readable", 0) >= self.theta_hi
                and not item.data.get("model_unverified") and not item.data.get("server_error"),
            "seller_scope_confirmed": item.data.get("confirmed_evidence_id") == evidence_id,
        }
        checks["ingredient_coverage"] = (declared_rows_covered(lab.get("lines", []))
                                         if lab.get("source") == "dom" else photo_scope_complete(lab))
        coverage = item.data.get("coverage_probs") or {}
        checks["model_coverage"] = not coverage or coverage.get("complete", 0) >= .5
        page_complete = all(v for k, v in checks.items() if k != "seller_scope_confirmed")
        if page_complete:
            checks["seller_scope_confirmed"] = True
            if item.data.get("self_confirmed_evidence_id") != evidence_id:
                item.data["self_confirmed_evidence_id"] = evidence_id
                record_self_answer(case, item, "성분표 본문과 기타성분을 읽고 서로 맞는지 확인", "읽은 자료 기준 전체 확인", lines)
        labels = {"model_coverage": "모델이 발견한 범위 누락 확인", "current_source": "현재 상품 출처 연결", "ingredient_sections": "성분표/원재료·기타성분 범위",
                  "read_quality": "OCR 판독 신뢰·반복 점검", "model_readable": "성분표 판독 판단",
                  "seller_scope_confirmed": "제공된 성분표 전체 범위 확인",
                  "source_consistency": "DOM·사진에서 발견된 함량 충돌 해결",
                  "ingredient_coverage": "읽은 성분 수·라벨 영역 대응 확인"}
        status = {"scope": "현재 상품의 제공된 성분표/원재료와 기타성분을 차단 목록에 대조; 등록·통관 허가 아님",
                  "evidence_id": evidence_id, "checks": checks,
                  "missing": [labels[k] for k, ok in checks.items() if not ok],
                  "ready_for_confirmation": all(v for k, v in checks.items() if k != "seller_scope_confirmed"),
                  "complete": all(checks.values()), "confirmation_by": ("code" if page_complete else "seller") if checks["seller_scope_confirmed"] else None}
        # 후속 자료(제조사 답 글·사진)가 푼 확인은 '남은 확인'에서 뺀다. checks·complete 는 그대로 둔다:
        # 후속 자료는 현재 상품 출처로 검증된 것이 아니므로 전체 범위 완료의 근거가 아니다.
        resolved = {}
        for evidence in case.data.get("followup_evidence") or []:
            rows = evidence.get("lines") or []
            via = {"followup_id": evidence.get("followup_id"), "source": evidence.get("source")}
            if not checks["ingredient_sections"] and self.has_ingredient_span(rows):
                resolved.setdefault("ingredient_sections", {**via, "why": "후속 자료에서 성분표 구획과 성분을 읽음"})
            if not checks["model_readable"] and parse_ingredients(rows):
                resolved.setdefault("model_readable", {**via, "why": "후속 자료의 글자를 읽어 성분을 분해함"})
            if not checks["ingredient_coverage"] and declared_rows_covered(rows):
                resolved.setdefault("ingredient_coverage", {**via, "why": "후속 자료의 모든 성분 줄이 대조에 반영됨"})
        if resolved:
            status["resolved_by_followup"] = [{"check": labels[k], **v} for k, v in resolved.items()]
            status["missing"] = [labels[k] for k, ok in checks.items() if not ok and k not in resolved]
        item.data["review_requirements"] = status
        item.data["_review_context"] = {"title": current.get("title") or case.text, "text": text}
        return status

    def comparison_ready(self, case, item):
        """Acquire a bounded, attributable ingredient span, not whole-label completion."""
        guard = self.completion_guard(case, item)
        return bool(self.has_ingredient_span((case.data.get("label") or {}).get("lines", []))
                    and guard["checks"]["current_source"] and guard["checks"]["read_quality"])

    def has_ingredient_span(self, lines):
        text = "\n".join(line["text"] for line in lines)
        declared = bool(re.search(r"(?im)^\s*(?:(?:supplement|nutrition)\s+facts\b|(?:(?:other|active|inactive|main|medicinal)\s+)?ingredients?\b|성분\s*[:：]|재료\s*[:：]|영양\s*성분(?:\s*정보|표)?|영양\s*정보|1회\s*제공량|용기당\s*제공|기타\s*(?:성분|원료)|원재료)", text))
        return bool(declared and parse_ingredients(lines))

    def can_resolve(self, case, item, top):
        return top == "readable" and self.comparison_ready(case, item)

    # ---- 다음 행동 결정 노드 -------------------------------------------------------------------
    def next_options(self, case, item):
        available = {}
        if self.comparison_ready(case, item):
            available["compare"] = {"label": "읽은 성분 범위 대조", "criteria": "Compare acquired ingredients; whole-label confirmation remains separate."}
        attempts = [entry for entry in item.data.get("_acquisitions", []) if entry.get("source") == "ocr"]
        if case.image_path:
            if not attempts:
                available["read_image"] = {"label": "제공된 사진 읽기", "criteria": "Read the supplied image."}
            elif len(attempts) == 1:
                available["retry_image"] = {"label": "사진 읽기 한 번 재시도", "criteria": "Retry the failed image read once."}
        elif case.data.get("gallery_selection", {}).get("next_action") != "request_evidence":
            available["capture"] = NEXT_OPTIONS["capture"]
        if not self.comparison_ready(case, item) and not item.data.get("_region_requested"):
            available["ask"] = NEXT_OPTIONS["ask"]
        available["stop"] = NEXT_OPTIONS["stop"]
        return available

    def next_code_action(self, case, item, options):
        for choice in ("compare", "read_image", "retry_image", "capture", "ask", "stop"):
            if choice in options:
                return {"rule": "bounded_label_acquisition", "choice": choice,
                        "reason": options[choice]["label"] + " — 성분표 확보 순서는 코드가 결정"}

    def next_execute(self, engine, case, item, choice):
        item.data["_pending_action"] = choice
        if choice == "compare":
            lab = case.data.get("label") or {}
            guard = self.completion_guard(case, item)
            if (lab.get("source") == "dom" and not guard["checks"]["model_readable"]
                    and all(v for k, v in guard["checks"].items() if k not in ("model_readable", "seller_scope_confirmed"))
                    and not item.data.get("scope_text_reread")):
                item.data["scope_text_reread"] = True
                caselog.log_event(case.id, "tool_retry", item=item.id, action="read_ingredient_rows",
                                  evidence_lines=lab.get("lines", []))
                engine._judge(case, item, "각주를 제외한 성분표 본문 다시 읽기")
            engine._close(case, item, "resolved", "확보한 성분 범위 대조 시작 — 전체 자료 확인은 마지막 별도 조건", "readable")
            return "done"
        if choice in ("read_image", "retry_image"):
            caselog.log_event(case.id, "tool_retry", item=item.id, action=choice, evidence_lines=(case.data.get("label") or {}).get("lines", []))
            result = engine._run_tool(case, item, Tool("ocr_capture", ocr_capture, retries=0, on_fail="continue"))
            if result and self.apply_tool(case, item, "ocr_capture", result):
                engine._judge(case, item, "사진 읽기 뒤 성분표 사용 가능성 판단")
            return "continue"
        if choice == "stop":
            caselog.log_event(case.id, "inquiry_draft", item=item.id, reason="성분표를 읽지 못함 — 제조사에 전체 라벨 요청", evidence_lines=(case.data.get("label") or {}).get("lines", []))
            engine._close(case, item, "handoff", "한정된 사진 읽기·영역 요청 뒤에도 대조할 성분 근거 미확보")
            return "done"
        if choice == "fda":
            res = engine._run_tool(case, item, Tool("fda_recalls", fda_recalls, retries=1, on_fail="continue"))
            out = item.data.setdefault("_outcomes", [])
            if res and res.get("found"):
                case.data["fda"] = res
                out.append({"action": "fda", "result": f"FDA 미표시 성분 회수 {len(res['recalls'])}건 (한국 차단 근거 아님)"})
            else:
                out.append({"action": "fda", "result": "FDA 미표시 성분 회수 이력 없음" if res else "FDA 조회 실패"})
            return "continue"
        if choice == "dsld":
            res = engine._run_tool(case, item, Tool("dsld_label", dsld_label, retries=0, on_fail="continue"))
            out = item.data.setdefault("_outcomes", [])
            status = (res or {}).get("status", "lookup_failed")
            result = (res or {}).get("reason") or ("검색 조건에 맞는 DSLD 후보 없음(제품 부재를 뜻하지 않음)"
                       if status == "no_candidates" else "DSLD 조회 실패")
            out.append({"action": "dsld", "status": status, "result": result, "candidate": (res or {}).get("candidate")})
            return "continue"
        if choice == "ask":
            item.data["_region_requested"] = True
        q = {
             "reread": Question("q_next_reread", "접힌 부분을 펼쳐 페이지를 다시 읽습니다.", on_answer=_next_answer, kind="page_reread"),
             "capture": Question("q_next_capture", "성분표 부분 화면을 캡처해 읽습니다.", on_answer=_next_answer, kind="capture_auto"),
             "ask": Question("q_next_ask", "현재 페이지에서 성분표 영역을 선택해 주세요. 없으면 '성분표 없음'을 선택해 주세요.",
                             on_answer=_next_answer, kind="capture_region")}[choice]
        case.pending = (item, q)
        item.status = "waiting_user"
        caselog.log_event(case.id, "ask", item=item.id, n=len(item.asked) + 1, qid=q.id, question=q.text, voi=None,
                          kind=q.kind, item_title=item.title)
        case.steps.append({"kind": "ask", "item": item.title, "label": "다음 행동 실행", "question": q.text})
        return "wait"

    instructions = ("Read the supplied ingredient text. Is it legible and coherent enough to identify the stated ingredient names? "
                    "This judgment does NOT establish current product identity, full label coverage or review completion; "
                    "those require separate code checks and seller confirmation.")
    OPTS = {"readable": "Ingredient text is legible and coherent; this alone does not certify complete coverage.",
            "unreadable": "Ingredient text is garbled, empty or not an ingredient list."}
    PHOTO = {"usable": "The image clearly shows a supplement facts / ingredient panel that can be read.",
             "unusable": "The image is blurry, cropped, or does not show the ingredient panel."}

    def options(self, item):
        return dict(self.OPTS)

    def labels(self, item):
        return {"readable": "성분 글자 판독 가능", "unreadable": "성분표 근거 부족"}

    def resolvable(self, item, top):
        return top == "readable"

    def apply_tool(self, case, item, tool_name, result):
        if result.get("status") not in (None, "read"):
            return False
        return retain_label(case, item, result)

    def build_request(self, case, item):
        lab = case.data.get("label") or {}
        state = {"product": f"{_inp(case).get('brand', '')} {_inp(case).get('title', '')}".strip(),
                 "label_source": lab.get("source", "none"),
                 "label_text": [ln["text"] for ln in lab.get("lines", [])],
                 "ocr_mean_confidence": lab.get("mean_conf"),
                 "ocr_repeated_text_ratio": lab.get("repeat_ratio")}   # 판정 서버 OCR 이 주는 반복(환각) 비율
        if item.data.get("scope_text_reread"):
            state["product"] = _inp(case).get("title", "")
            state["label_text"] = [ln["text"] for ln in lab.get("lines", []) if not FOOTNOTE.search(ln["text"])]
        qs = {"main": {"type": "choice", "instructions": self.instructions, "criteria": self.OPTS}}
        if case.data.get("agent_recovery_enabled"):
            state["parsed_ingredients"] = _lab_ingredients(case)
            qs["coverage"] = {"type": "choice", "criteria": {
                "complete": "Every visible ingredient declaration is represented in parsed_ingredients; no missing ingredient section or cut-off rows.",
                "incomplete": "A visible ingredient is absent from parsed_ingredients, a required section is missing, or completeness cannot be checked."},
                "instructions": "Check the supplied text and image against the parsed ingredient names. Readability alone is not coverage."}
        req = {"state": state, "questions": qs}
        if case.image_b64 and lab.get("source") == "ocr":
            req["image"] = case.image_b64
            qs["photo"] = {"type": "choice", "criteria": self.PHOTO,
                           "instructions": "Is this captured image usable for reading the ingredient label?"}
        return req

    def read_probs(self, item, response):
        answers = response["answers"]
        item.data["coverage_probs"] = (answers.get("coverage") or {}).get("probabilities", {})
        item.data["photo_probs"] = (answers.get("photo") or {}).get("probabilities", {})
        return dict(answers["main"]["probabilities"])

    def mock_probs(self, request):
        """합성 확률(실제 판정 아님): 페이지 글자는 성분 줄이 있으면 믿고, OCR 은 평균 신뢰도, 없으면 판독 불가."""
        st = request["state"]
        has = bool(parse_ingredients([{"text": t} for t in st["label_text"]]))
        if st["label_source"] == "ocr":
            c = st.get("ocr_mean_confidence") or 0.0
        else:
            c = 0.95 if has else 0.03
        out = {"main": {"readable": c, "unreadable": 1 - c}}
        if "coverage" in request["questions"]:
            out["coverage"] = {"complete": c, "incomplete": 1 - c}
        if "photo" in request["questions"]:
            out["photo"] = {"usable": c, "unusable": 1 - c}
        return out

# ---- 성분 판정: ① 부분 일치 → ② 사전 → ③ Zoro-1 (판정 로직은 matching.py, research 비교 실행기와 같은 함수) ----------

def alias_lookup(case, item):
    """③ 뒤 재조회: 라벨 성분 이름과 후보 원료 이름을 원료 별칭 표·PubChem(PUBCHEM_LIVE=1) 으로 다시 찾아 사실로 붙인다."""
    names = [item.data["name"]] + [REG.by_id[i].en or REG.by_id[i].kr for i in item.data["candidates"]]
    hits = []
    for n in names:
        h = ALIAS.lookup(n)
        if not h["canonical"]:
            h = PUBCHEM.lookup(n) or h
        if h["canonical"]:
            hits.append(h)
    return {"_args": {"names": names}, "hits": hits, "source": ALIAS.source,
            "knowledge": retrieve_knowledge(REG, item.data["name"])}


DECIDED_BY = {"preferred_exact": "① 목록 이름(한글·영문)이 라벨에 그대로 있음",
              "registry_alias_exact": "① 목록 '기타명칭'이 라벨에 그대로 있음",
              "dictionary": "② 사전(원료 별칭·PubChem 동의어) 일치",
              "no_candidates": "① 비슷한 이름의 목록 원료가 없음",
              "alias_exclusion": "코드: 현재 성분 별칭 조회 성공·전체 목록 연결 없음",
              "official_knowledge": "공식 원료 이름·이명이 지정 항목에 직접 연결됨",
              "verified_taxon": "학명·이름 자료와 지정 원료의 범위를 확인함",
              "common_identity": "확인된 일반 성분이며 목록 연결 없음",
              "zoro": "③ Zoro-1 판정"}


def verified_taxon_links(name):
    """Only reviewed taxon links to the unchanged, unrestricted registry wording."""
    text = norm(name)
    return [link for link in TAXON_LINKS if link["registry_id"] in REG.by_id
            and norm(REG.by_id[link["registry_id"]].en) == norm(link["registry_name"])
            and REG.by_id[link["registry_id"]].kr == link["registry_kr"]
            and REG.by_id[link["registry_id"]].etc == link["registry_etc"]
            and re.search(r"(^| )" + re.escape(norm(link["observed_name"])) + r"($| )", text)]


def registry_links(names):
    """All-list name/dictionary connections, not merely the model's shortlist."""
    ids = set()
    normalized = {norm(name) for name in names}
    for entry in REG.entries:
        if any(norm(name) in normalized for name in entry.names):
            ids.add(entry.id)
    for name in names:
        partial = stage_partial(name, REG)
        ids.update(partial["preferred_exact"]["candidate_ids"])
        ids.update(partial["registry_alias_exact"]["candidate_ids"])
        ids.update(stage_dictionary(name, REG, DICTS)["candidate_ids"])
    return sorted(ids)


def verify_ingredient_aliases(case, item):
    """A cache miss/disabled provider/error is not a negative identity observation."""
    query = item.data["name"]
    hit = None
    for source, dictionary in DICTS:
        canonical = dictionary.get(norm(query)) if source == "PubChem" else None
        aliases = sorted(getattr(dictionary, "syns", {}).get(canonical, set())) if canonical else []
        if canonical and any(norm(alias) != norm(canonical) for alias in aliases):
            hit = {"query": query, "canonical": canonical, "aliases": aliases, "source": "PubChem snapshot"}
            break
    if hit and not registry_links([query, hit["canonical"], *hit.get("aliases", [])]):
        hit = None  # The legacy snapshot omits lookup completeness/CID; a miss there cannot prove exclusion.
    if hit is None:
        hit = PUBCHEM.lookup(query, complete=True)
        if (not hit or not hit.get("lookup_complete") or not hit.get("cid")
                or not hit.get("aliases") or norm(query) not in
                {norm(n) for n in [hit.get("canonical", ""), *hit.get("aliases", [])]}):
            return {"status": "unverified", "query": query,
                    "reason": "현재 성분의 식별자·전체 별칭 조회 미확인/미가용/실패 — 해당 없음 근거로 쓰지 않음"}
    if norm(hit.get("query", "")) != norm(query) or not hit.get("canonical"):
        return {"status": "unverified", "query": query, "reason": "조회 레코드의 현재 성분 동일성 미확인"}
    names = list(dict.fromkeys([query, hit["canonical"], *hit.get("aliases", [])]))
    return {"status": "verified", "query": query, "hits": [hit], "checked_names": names,
            "matched_ids": registry_links(names), "source": hit["source"],
            "registry": REG.source, "registry_as_of": REG.as_of, "registry_entries": len(REG.entries)}


def record_self_answer(case, item, question, answer, lines, **extra):
    source_lines = [row["text"] for row in (case.data.get("label") or {}).get("lines", [])]
    evidence = [{"line": source_lines.index(line) + 1 if line in source_lines else None, "text": line} for line in lines]
    record = {"item": item.id, "question": question, "answer": answer, "evidence_lines": evidence, **extra}
    case.data.setdefault("self_answers", []).append(record)
    caselog.log_event(case.id, "self_answered", **record)
    case.steps.append({"kind": "self_answered", "item": item.title,
                       "label": "스스로 확인: " + question + " → " + answer, "evidence_lines": evidence})


def ingredient_context(case, item):
    lines = [x["text"] for x in (case.data.get("label") or {}).get("lines", [])]
    indexes = [i for i, text in enumerate(lines) if norm(item.data["name"]) in norm(text)]
    return lines, [lines[j] for i in indexes for j in range(max(0, i - 1), min(len(lines), i + 2))]


def repeats_outside_ingredients(label):
    """OCR 반복이 경고문처럼 성분과 무관한 줄에만 있는가. 차단 규칙 대조에만 쓰고 범위 완료·통과 근거로는 쓰지 않는다."""
    lines = [ln["text"].strip() for ln in label.get("lines", []) if ln.get("text", "").strip()]
    confidence = label.get("mean_conf")
    if (label.get("source") != "ocr" or label.get("status") not in (None, "read") or not lines
            or not isinstance(confidence, (int, float)) or confidence < .7):
        return False
    seen, repeated = set(), []
    for text in lines:
        (repeated if text in seen else seen).append(text) if text in seen else seen.add(text)
    if not repeated:
        return False
    names = {norm(name) for name in parse_ingredients([{"text": text} for text in lines])}
    for text in set(repeated):
        if _amount_at_top(text) or OTHER.match(text) or HEADER.match(text) or any(name and name in norm(text) for name in names):
            return False
    return bool(names)


def retain_additional_ingredients(case, item, result):
    if not result or result.get("source_url") != _inp(case).get("url"):
        return False
    if result.get("source") == "ocr" and not (
            isinstance(result.get("mean_conf"), (int, float)) and result["mean_conf"] >= .7
            and isinstance(result.get("repeat_ratio", 0), (int, float)) and result.get("repeat_ratio", 0) <= .2):
        return False
    conflicts = label_amount_conflicts((case.data.get("label") or {}).get("lines", []), result.get("lines", []))
    case.data.setdefault("label_amount_conflicts", []).extend(conflicts)
    existing = {norm(it.data["name"]) for it in case.items if it.spec is case.plugin.ING}
    if any(norm(name) not in existing for name in parse_ingredients(result.get("lines", []))):
        case.data["additional_source_review"] = True
        case.plugin._add_ingredients(case, item, label=result)
    return True


def _ingredient_reacquired(engine, case, item, answer):
    action = item.data.pop("_self_source_action")
    if action == "reread":
        result = engine._run_tool(case, item, Tool("self_answer_dom", read_dom, retries=0, on_fail="continue"))
    elif answer is True and case.image_path:
        result = engine._run_tool(case, item, Tool("self_answer_capture", ocr_capture, retries=0, on_fail="continue"))
    else:
        return "noop"
    if not retain_additional_ingredients(case, item, result):
        return "noop"
    item.facts.append("Additional current-page transcription (not identity proof): " +
                      " | ".join(row["text"] for row in result.get("lines", [])))
    item.data["self_retry_done"] = action == "capture"
    return "rejudge"


class Ingredient(ItemSpec):
    kind = "ingredient"
    theta_hi, theta_lo, max_questions = 0.85, 0.50, 1
    tools = [Tool("ingredient_alias_lookup", alias_lookup)]
    recovery_supported = True

    def needs_alias_exclusion(self, item):
        probabilities = item.probs or {}
        if (not probabilities or max(probabilities, key=probabilities.get) != "none"
                or not self.theta_lo <= probabilities["none"] < self.theta_hi
                or item.data.get("model_unverified") or item.data.get("server_error")):
            return False
        names = [item.data["name"]]
        for hit in item.data.get("label_alias_hits", []):
            if self.linked_lookup(item, hit):
                names.extend([hit["canonical"], *hit.get("aliases", [])])
        return not registry_links(names)

    def recovery_state(self, case, item):
        top, confidence = item.top()
        missing = []
        if confidence < self.theta_hi and item.data.get("decided_by") != "alias_exclusion":
            missing.append(f"동일 성분 판정 기준 미달 ({confidence:.1%})")
        if not self.can_resolve(case, item, top):
            missing.append(item.data.get("no_evidence") or "현재 성분과 목록 항목의 독립 연결 근거")
        return {"goal": f"현재 라벨 성분 '{item.data['name']}'을 차단 목록과 대조",
                "obtained": [item.data["line"], *item.facts], "missing": missing,
                "candidate_names": {i: REG.by_id[i].label for i in item.data["candidates"]},
                "knowledge_retrieval": item.data.get("stages", {}).get("kb_retrieval")}

    def alternate_query(self, item):
        original = norm(item.data["name"])
        for hit in item.data.get("label_alias_hits", []):
            if norm(hit.get("query", "")) == original:
                for name in [hit["canonical"], *hit.get("aliases", [])]:
                    if name and norm(name) != original:
                        return name
        return None

    def recovery_options(self, case, item):
        if self.needs_alias_exclusion(item) and "ingredient_alias_verify" not in item.tools_done:
            return {"verify_none": "The leading S1 answer is none but confidence is insufficient. Retrieve this label name's real aliases and compare all observed names against the entire official blocked list before any conclusion.",
                    "stop": "The required independent ingredient identity evidence cannot be obtained; hand off."}
        options = {}
        if "ingredient_alias_lookup" not in item.tools_done:
            options["alias"] = "Look up this actual label ingredient in the existing alias databases."
        alternate = self.alternate_query(item)
        if alternate and "ingredient_alias_alternate" not in item.tools_done:
            options["alias_alternate"] = f"Look up the independently observed alternate name: {alternate}."
        cap = getattr(case.plugin, "max_questions_per_case", None)
        if len(item.asked) < self.max_questions and (cap is None or
                sum(len(x.asked) for x in case.items if x.spec.counts_toward_cap) < cap) and self.questions_for(item):
            options["ask"] = "Ask the seller to verify the specific candidate alias on this ingredient's label."
        options["stop"] = "Evidence cannot establish this ingredient identity; hand off with remaining work."
        return options

    def recovery_execute(self, engine, case, item, action):
        if action == "verify_none":
            result = engine._run_tool(case, item, Tool("ingredient_alias_verify", verify_ingredient_aliases,
                                                       retries=0, on_fail="continue"))
            item.data.setdefault("_recovery_outcomes", []).append({"action": action, "result": result})
            item.data["alias_verification"] = result or {"status": "failed"}
            if not result or result.get("status") != "verified":
                engine._close(case, item, "handoff", (result or {}).get("reason", "별칭 조회 실패 → 직접 확인 필요"))
                return "done"
            self.apply_tool(case, item, "ingredient_alias_verify", result)
            matches = result["matched_ids"]
            if matches:
                item.data["candidates"] = list(dict.fromkeys(item.data["candidates"] + matches))
                engine._judge(case, item, "재판정(별칭 추가 조회에서 목록 연결 발견)")
                return "continue"
            if self.needs_alias_exclusion(item):
                if not self.resolvable(item, "clear"):
                    engine._close(case, item, "handoff", item.data["no_evidence"])
                    return "done"
                item.data["decided_by"] = "alias_exclusion"
                item.facts.append("조회된 현재 성분 이름·별칭 모두를 현재 차단 목록과 대조했으며 연결을 찾지 못함. 판매 승인 아님.")
                caselog.log_event(case.id, "alias_exclusion", item=item.id, source="code", result=result,
                                  model_probabilities=item.probs, threshold=self.theta_hi)
                engine._close(case, item, "resolved", "코드: 현재 성분 별칭 조회 성공·전체 목록 연결 없음 → 차단 근거 없음", "clear")
                return "done"
            engine._close(case, item, "handoff", "별칭 확인 뒤 해당 없음 적용 조건 미충족 → 직접 확인 필요")
            return "done"
        if action == "stop":
            engine._close(case, item, "handoff", "추가 동일성 근거 미확보 → 사람 검토")
            return "done"
        if action == "ask":
            q = self.questions_for(item)[0]
            case.pending, item.status = (item, q), "waiting_user"
            caselog.log_event(case.id, "ask", item=item.id, qid=q.id, question=q.text,
                              kind=q.kind, item_title=item.title, voi=None, n=len(item.asked) + 1)
            case.steps.append({"kind": "ask", "item": item.title, "label": "생성 단계가 요청한 셀러 확인", "question": q.text})
            return "wait"
        if action == "alias":
            tool = self.tools[0]
        else:
            query = self.alternate_query(item)

            def lookup_alternate(c, it):
                hit = ALIAS.lookup(query)
                if not hit.get("canonical"):
                    hit = PUBCHEM.lookup(query) or hit
                return {"hits": [hit] if hit.get("canonical") else [], "_args": {"query": query},
                        "source": hit.get("source")}

            tool = Tool("ingredient_alias_alternate", lookup_alternate)
        result = engine._run_tool(case, item, tool)
        item.data.setdefault("_recovery_outcomes", []).append({"action": action, "result": result})
        if result and self.apply_tool(case, item, tool.name, result):
            engine._judge(case, item, "재판정(생성 단계가 회수한 별칭 근거)")
        return "continue"

    def linked_lookup(self, item, hit):
        original, query = norm(item.data["name"]), norm(hit.get("query", ""))
        if query == original:
            return True
        # Two-way corroboration, rooted in a lookup of the actual label name.
        reverse = original in {norm(n) for n in [hit.get("canonical", ""), *hit.get("aliases", [])]}
        return reverse and any(norm(parent.get("query", "")) == original and query in
                               {norm(n) for n in [parent["canonical"], *parent.get("aliases", [])]}
                               for parent in item.data.get("label_alias_hits", []))

    def questions_for(self, item):                            # 재조회 뒤에도 애매하면 하나만 묻는다
        e = self.matched(item)
        if not e:
            return []
        alts = ", ".join(e.names[:4])
        n = item.data["name"]
        q = Question("q_label_alias",
                     f"성분표에서 '{n}'의 같은 성분명으로 {alts} 중 하나가 명시되어 있나요? 다른 성분 줄의 이름은 제외해 주세요.",
                     fact_yes=f"The seller confirms '{n}' is also named as one of: {alts}.",
                     fact_no=f"The seller does not confirm an alias for '{n}' among: {alts}.")

        def on_answer(engine, case, it, ans):
            it.data["seller_alias_confirmation"] = {"name": n, "entry_id": e.id, "answer": ans}
            if ans is not None:
                it.facts.append(q.fact_yes if ans else q.fact_no)
            return "rejudge" if ans is not None else "noop"

        q.on_answer = on_answer
        return [q]

    def resolve_official(self, engine, case, item):
        lines, context = ingredient_context(case, item)
        names = [item.data["name"]]
        for hit in item.data.get("label_alias_hits", []):
            if self.linked_lookup(item, hit):
                names.extend([hit["canonical"], *hit.get("aliases", [])])
        taxon = [link for name in names for link in verified_taxon_links(name)]
        linked = sorted(set(registry_links(names)) | {link["registry_id"] for link in taxon})
        if linked:
            ident = linked[0]
            item.data.update(entry_id=ident, decided_by="verified_taxon" if taxon else "dictionary")
            item.probs = {ident: 1.0, "none": 0.0}
            if taxon:
                item.data.setdefault("stages", {})["verified_taxon"] = taxon
            record_self_answer(case, item, item.data["name"] + "의 목록 이름·이명", "있음", context or lines,
                               matched_ids=linked, identity_evidence=taxon)
            engine._close(case, item, "resolved", "성분 이름·이명과 목록의 직접 연결 확인", "blocked")
            return True
        return False

    def before_question(self, engine, case, item):
        if self.resolve_official(engine, case, item):
            return True
        lines, context = ingredient_context(case, item)
        # A missing candidate spelling never proves that the ingredient/product is clear.
        gate = next((it for it in case.items if it.spec is case.plugin.LABEL), None)
        if gate is None:
            engine._close(case, item, "handoff", "성분표 근거 없음 — 확인 남음")
            return True
        complete = case.plugin.LABEL.completion_guard(case, gate)["complete"]
        observed = [entry.id for entry in (REG.by_id[ident] for ident in item.data["candidates"]) if any(
            len(norm(n).replace(" ", "")) >= 4 and re.search(r"(^| )" + re.escape(norm(n)) + r"($| )", norm(" ".join(lines)))
            for n in entry.names)]
        record_self_answer(case, item, item.data["name"] + " 주변 후보 이름 표기",
                           "다른 줄의 차단 성분은 별도 유지" if observed else "없음" if complete else "확인 남음",
                           context or lines, absence_scope="candidate_spelling_only", observed_ids=observed)
        actions = case.data.setdefault("self_answer_source_actions", [])
        if not complete and not case.image_path and case.data.get("self_answer_tool_retries", 0) < 2:
            action = "reread" if "reread" not in actions else "capture"
            actions.append(action)
            case.data["self_answer_tool_retries"] = case.data.get("self_answer_tool_retries", 0) + 1
            item.data["_self_source_action"] = action
            q = Question("q_self_" + action, "성분표를 펼쳐 다시 읽습니다." if action == "reread" else "성분표 사진을 다시 읽습니다.",
                         on_answer=_ingredient_reacquired, kind="page_reread" if action == "reread" else "capture_auto")
            case.pending, item.status = (item, q), "waiting_user"
            caselog.log_event(case.id, "tool_retry", item=item.id, action=action, evidence_lines=context)
            caselog.log_event(case.id, "ask", item=item.id, qid=q.id, kind=q.kind, question=q.text,
                              n=len(item.asked) + 1, automatic=True, item_title=item.title)
            case.steps.append({"kind": "ask", "item": item.title, "label": "스스로 다시 읽기", "question": q.text})
            return True
        if case.image_path and not item.data.get("self_retry_done") and case.data.get("self_answer_tool_retries", 0) < 2:
            item.data["self_retry_done"] = True
            case.data["self_answer_tool_retries"] = case.data.get("self_answer_tool_retries", 0) + 1
            original_image = case.image_path
            alternate = next((row.get("path") for row in case.data.get("gallery_selection", {}).get("candidates", [])
                              if row.get("path") and row["path"] != original_image), None)
            if alternate:
                case.set_image(alternate)
            caselog.log_event(case.id, "tool_retry", item=item.id, action="ocr_capture",
                              evidence_lines=context, reason="글만으로 동일성 미확정 — 제공된 사진 재독")
            result = engine._run_tool(case, item, Tool("self_answer_ocr", ocr_capture, retries=0, on_fail="continue"))
            if result and result.get("status") == "read" and retain_additional_ingredients(case, item, result):
                item.facts.append("Additional label transcription (not identity proof): " +
                                  " | ".join(x["text"] for x in result.get("lines", [])))
                try:
                    engine._judge(case, item, "사진 재독 뒤 동일성 재확인")
                finally:
                    case.set_image(original_image)
                return True
            case.set_image(original_image)
        caselog.log_event(case.id, "inquiry_draft", item=item.id, evidence_lines=context,
                          reason="동일 성분 근거 부족", ingredient=item.data["name"], candidates=item.data["candidates"])
        engine._close(case, item, "handoff", "제조사에 원료명·학명 확인 필요 — 문의할 항목에 추가")
        return True

    def labels(self, item):
        return {"blocked": "차단 성분", "clear": "차단 근거 없음"}

    def build_request(self, case, item):
        request = zoro_request(item.data["name"], REG, item.data["candidates"], item.facts)
        knowledge = item.data.get("stages", {}).get("kb_retrieval")
        if knowledge and knowledge["mode"] == "candidates":
            request["state"] += "\nCandidate retrieval (reference only, not identity proof): " + json.dumps(knowledge, ensure_ascii=False)
        # shortcut: OCR has no row coordinates; crop once capture metadata supplies reliable row bounds.
        if item.data.get("self_retry_done") and case.image_b64:
            request["image"] = case.image_b64
        return request

    def read_probs(self, item, response):
        return read_zoro(response, item.data["candidates"])

    def aggregate(self, item, probs):
        return {"blocked": sum(v for k, v in probs.items() if k != "none"), "clear": probs.get("none", 0.0)}


    def evidence(self, item):
        """해당 성분 구간, 그 이름에서 출발한 조회, 해당 후보에 대한 명시적 셀러 확인만 인정한다."""
        e = self.matched(item)
        if not e:
            return False, "차단 후보 없음"
        knowledge = item.data.get("stages", {}).get("kb_retrieval") or {}
        retrieved = next((row for row in knowledge.get("candidates", []) if row["id"] == e.id), None)
        if knowledge.get("mode") == "candidates" and retrieved:
            if retrieved["scope_check"]:
                return False, "검색된 원료의 식물·부위·적용 범위 미확인 — 참고 검색과 셀러 답만으로 차단 확정하지 않음"
            if not retrieved["official"]:
                observed = [item.data["name"]]
                for hit in item.data.get("label_alias_hits", []):
                    if self.linked_lookup(item, hit):
                        observed.extend([hit["canonical"], *hit.get("aliases", [])])
                if e.id not in registry_links(observed):
                    return False, "참고 이름으로만 검색된 후보 — 현재 성분에서 출발한 독립 목록 연결이 없음"
        confirmation = item.data.get("seller_alias_confirmation") or {}
        if (confirmation.get("answer") is True and confirmation.get("entry_id") == e.id
                and norm(confirmation.get("name", "")) == norm(item.data["name"])):
            return True, f"셀러가 해당 성분의 목록 이명 '{e.label}' 표시를 확인함"
        texts = [item.data["name"]]
        for hit in item.data.get("label_alias_hits", []):
            if self.linked_lookup(item, hit):
                texts.extend([hit["canonical"]] + list(hit.get("aliases") or []))
        for n in e.names:
            nn = norm(n)
            if len(nn.replace(" ", "")) >= 4 and any(
                    re.search(r"(^| )" + re.escape(nn) + r"($| )", norm(text)) for text in texts):
                return True, f"목록 이름 '{n}' 이 해당 성분 구간·연결된 조회에 있음"
        return False, f"Zoro-1 은 '{e.label}' 쪽이지만 해당 성분 구간·연결된 조회에 그 목록 이름·이명의 근거가 없음"

    def resolvable(self, item, top):
        """차단 확정은 목록 근거가 있을 때만(중앙 결정 20:31). 근거가 없으면 확정하지 않고 재조회·질문 → 확인 필요."""
        if top == "clear" and (item.data.get("alias_verification") or {}).get("matched_ids"):
            item.data["no_evidence"] = "별칭 조회에서 목록 연결 발견 — 해당 없음으로 종료하지 않음"
            return False
        knowledge = item.data.get("stages", {}).get("kb_retrieval") or {}
        if top == "clear" and knowledge.get("mode") == "candidates" and any(
                (not row["official"] or row["scope_check"])
                and row.get("match") in ("normalized_exact", "identifier", "reference_trail", "whole_phrase")
                for row in knowledge.get("candidates", [])):
            item.data["no_evidence"] = "참고 자료의 목록 연결 가능성 미해결 — 검색 결과나 높은 해당 없음 점수로 배제하지 않음"
            return False
        if top != "blocked" or item.data.get("decided_by") in ("preferred_exact", "registry_alias_exact", "dictionary", "official_knowledge", "verified_taxon"):
            return True
        ok, why = self.evidence(item)
        if not ok and not item.data.get("_evidence_logged"):
            item.data["_evidence_logged"] = why
            cid = getattr(caselog.CURRENT, "case_id", None)
            if cid:
                caselog.log_event(cid, "evidence_check", item=item.id, ok=False, reason=why)
        item.data["no_evidence"] = None if ok else why
        return ok

    def matched(self, item):
        if item.data.get("entry_id"):
            return REG.by_id[item.data["entry_id"]]
        p = {k: v for k, v in (item.probs or {}).items() if k != "none"}
        return REG.by_id[max(p, key=p.get)] if p else None

    def apply_tool(self, case, item, tool_name, result):
        knowledge = result.get("knowledge")
        previous_candidates = set(item.data["candidates"])
        if knowledge:
            item.data.setdefault("stages", {})["kb_retrieval"] = knowledge
            if knowledge["mode"] == "candidates" and knowledge["status"] == "retrieved":
                for candidate in knowledge["candidates"]:
                    if candidate["id"] in REG.by_id and candidate["id"] not in item.data["candidates"]:
                        item.data["candidates"].append(candidate["id"])
        for h in result.get("hits", []):
            if norm(h.get("query", "")) == norm(item.data["name"]) or (
                    tool_name == "ingredient_alias_alternate" and self.linked_lookup(item, h)):
                item.data.setdefault("label_alias_hits", []).append(h)
                item.facts.append(f"'{h['query']}' is another name for '{h['canonical']}' (other names: "
                                  f"{', '.join(h['aliases'][:12])}; source {h['source']}).")
            for c in string_candidates(h["canonical"], REG, k=2, min_score=0.6):   # 정식 이름으로 후보 보강
                if c["id"] not in item.data["candidates"]:
                    item.data["candidates"].append(c["id"])
        return bool(result.get("hits")) or set(item.data["candidates"]) != previous_candidates

    def mock_probs(self, request):
        """합성 확률(실제 판정 아님, 화면·흐름 재현용): 후보 원료 이름과 라벨 글자의 유사도 구간 + 사실 문장 규칙."""
        st = request["state"]
        span = norm(st.split("Label ingredient (OCR text):", 1)[-1].split("\n")[0])
        facts = norm(st.split("Facts from ingredient databases:", 1)[-1]) if "Facts from" in st else ""
        crit = request["questions"]["same"]["criteria"]
        p = {k: 0.01 for k in crit}
        names = {}
        for k, d in crit.items():
            if k != "none":
                nm = d.split("same substance as ", 1)[-1].rstrip(".")
                e = next((x for x in REG.entries if nm in (x.en, x.kr)), None)
                names[k] = [norm(x) for x in (e.names if e else [nm])]
        sims = {k: max(difflib.SequenceMatcher(None, span, n).ratio() for n in ns) for k, ns in names.items()}
        generic = {"acid", "extract", "powder", "root", "leaf", "bark", "seed", "herb", "fruit", "from", "with",
                   # 염·미네랄 일반어(mock 전용): 'Sodium'↔'Sodium chlorite', 'Boron Citrate'↔'Sildenafil citrate' 같은 우연 겹침 방지
                   "sodium", "potassium", "calcium", "magnesium", "zinc", "boron", "citrate", "chloride", "sulfate",
                   "phosphate", "acetate", "bitartrate", "hydrochloride", "hcl", "standardized", "containing"}
        stoks = {t for t in span.split() if len(t) >= 4 and t not in generic}
        for k, ns in names.items():     # 일반어가 아닌 단어가 하나도 안 겹치면 갈림·차단 구간에 넣지 않는다
            if sims[k] < 0.9 and not stoks & {t for n in ns for t in n.split()}:   # 'Malic acid'↔'Mefenamic acid'(0.75),
                sims[k] = min(sims[k], 0.3)                                       # 'Glucosamine HCl'↔'Histamine HCl'(0.71) 실페이지 오탐
        if "none of these names" in facts:
            p["none"] = 0.97
        elif any(any(len(n) > 3 and n in facts for n in ns) for ns in names.values()):
            p[next(k for k, ns in names.items() if any(len(n) > 3 and n in facts for n in ns))] = 0.97
        elif sims:
            top = max(sims, key=sims.get)
            s = sims[top]
            p[top] = 0.9 if s >= 0.7 else 0.5 if s >= 0.45 else 0.04
            p["none"] = 1 - p[top]
        z = sum(p.values())
        return {"same": {k: v / z for k, v in p.items()}}


# ---- 제품 분류 (페이지 읽기 직후, 성분 판정 전) — 보기·검사 세트는 supplement_data/classification.json ---------------

CLS = json.load(open(os.path.join(DATA, "classification.json"), encoding="utf-8"))


def _kw(word, text):
    return re.search(r"(?<![0-9a-z가-힣])" + re.escape(word.lower()) + r"(?![0-9a-z가-힣])", text.lower()) is not None


def form_by_code(case):
    """제형을 코드로 먼저(상품명·라벨 글자의 단어). 한 종류만 걸리면 확정, 여럿·없음이면 None(→ Zoro-1)."""
    i = _inp(case)
    title = i.get("title") or ""
    t_hits = [k for k, v in CLS["form"].items() if any(_kw(w, title) for w in v.get("keywords", []))]
    if len(t_hits) == 1:                     # 상품 제목('60 Veggie Capsules')에서 하나로 정해지면 그것
        return t_hits[0], t_hits
    text = " ".join([title] + [ln["text"] for ln in (case.data.get("label") or {}).get("lines", [])])
    hits = [k for k, v in CLS["form"].items() if any(_kw(w, text) for w in v.get("keywords", []))]
    return (hits[0], hits) if len(hits) == 1 else (None, hits)


class Classify(ItemSpec):
    kind = "classify"
    theta_hi, theta_lo, max_questions = 0.0, 0.0, 0     # 분류는 묻지 않고 한 번에 끝낸다(확률은 그대로 보여 줌)
    counts_toward_cap = False

    def options(self, item):
        return {k: v["criteria"] for k, v in CLS["product_type"].items()}

    def labels(self, item):
        return {k: v["label"] for k, v in CLS["product_type"].items()}

    def build_request(self, case, item):
        i = _inp(case)
        lines = [ln["text"] for ln in (case.data.get("label") or {}).get("lines", [])]
        state = {"product_name": i.get("title"), "brand": i.get("brand"), "label_text": lines}
        # 추출 성분 줄에서 빠지는 실제 표제·표시 문구도 전달한다. 없는 표제를 만들어 넣지 않는다.
        page = i.get("page_text") or ""
        heading = re.search(r"(?i)supplement facts|drug facts|nutrition facts|dietary supplement|영양 성분|성분 정보", page)
        start = max(0, heading.start() - 300) if heading else 0
        state["visible_presentation"] = page[start:start + 4000]
        form, hits = item.data.get("form_code"), item.data.get("form_hits")
        if form:
            state["dosage_form"] = CLS["form"][form]["criteria"]
        qs = {q: {"type": "choice", "instructions": CLS["instructions"][q],
                  "criteria": {k: v["criteria"] for k, v in CLS[q].items()}}
              for q in (["product_type", "use"] + ([] if form else ["form"]))}
        return {"state": state, "questions": qs}

    def read_probs(self, item, response):
        item.data["answers"] = {q: a["probabilities"] for q, a in response["answers"].items()}
        return dict(response["answers"]["product_type"]["probabilities"])

    def mock_probs(self, request):
        """합성 확률(실제 판정 아님): 라벨 글자 키워드로 기울인다."""
        st = request["state"]
        t = (" ".join(st.get("label_text") or []) + " " + (st.get("product_name") or "")).lower()
        def dist(keys, boost):
            w = {k: 1.0 for k in keys}
            for k, x in boost:
                if k in w:
                    w[k] *= x
            z = sum(w.values())
            return {k: v / z for k, v in w.items()}
        pt = [("health_functional", 4 if "supplement facts" in t else 1), ("general_food", 3 if "vitamin" in t else 1.5),
              ("drug_suspect", 25 if any(x in t for x in ("sildenafil", "tadalafil", "sibutramine")) else 0.3),
              ("quasi_drug", 0.2), ("cosmetic", 0.2), ("unknown", 0.5)]
        use = [("pre_workout", 8 if any(x in t for x in ("pre-workout", "caffeine", "beta-alanine")) else 1),
               ("weight", 8 if any(x in t for x in ("garcinia", "fat burn", "diet")) else 1),
               ("sexual", 8 if "yohimbe" in t or "horny goat" in t else 1), ("sleep", 8 if "melatonin" in t else 1),
               ("focus", 8 if any(x in t for x in ("dmae", "nootropic", "focus")) else 1),
               ("general", 8 if "vitamin" in t else 1),
               ("joint", 12 if any(x in t for x in ("glucosamine", "chondroitin", "joint", "msm")) else 1), ("other", 0.5)]
        out = {"product_type": dist(CLS["product_type"], pt), "use": dist(CLS["use"], use)}
        if "form" in request["questions"]:
            out["form"] = dist(CLS["form"], [("other", 2)])
        return out


def run_checks(case, cls_item, ingredient_names):
    """분류 결과로 고른 검사 세트를 돌린다(코드). -> [{id, label, applies, result, detail}]"""
    if not cls_item.probs:
        return [{"id": "classification_unavailable", "label": "제품 분류", "applies": True,
                 "result": "참고 정보 미확인", "detail": cls_item.reason, "advisory": True}]
    d = cls_item.data
    ptype = max(cls_item.probs, key=cls_item.probs.get)
    form = d.get("form_code") or max(d["answers"].get("form", {"other": 1}), key=d["answers"].get("form", {"other": 1}).get)
    # 표기 검사는 성분 이름에만 한다: 라벨 전체 줄을 보면 'Not manufactured with ... fish' 같은 알레르기 문구에 걸린다
    # (실제 저장 페이지 E2E 에서 확인)
    lines = list(ingredient_names)
    out = []
    for cs in CLS["check_sets"]:
        if cs.get("always"):
            out.append({"id": cs["id"], "label": cs["label"], "applies": True, "result": "성분 판정에서 확인", "detail": ""})
            continue
        applies = (form in cs.get("when_form", []) or ptype in cs.get("when_type", [])
                   or any(any(_kw(w, n) for w in cs.get("when_any_ingredient", [])) for n in ingredient_names))
        if not applies:
            continue
        if cs.get("patterns"):
            found = [ln for ln in lines if any(_kw(p, ln) for p in cs["patterns"])]
            out.append({"id": cs["id"], "label": cs["label"], "applies": True,
                        "result": "표기 있음" if found else "표기 없음(라벨 글자 기준)", "detail": "; ".join(found)})
        elif cs.get("verdict"):
            direct_risk = ptype == "drug_suspect"
            out.append({"id": cs["id"], "label": cs["label"], "applies": True,
                        "result": "확인 필요" if direct_risk else "참고 정보(성분 대조 결과와 별도)",
                        "detail": f"{CLS['product_type'][ptype]['label']} {cls_item.probs[ptype]:.0%}",
                        "advisory": not direct_risk, **({"verdict": cs["verdict"]} if direct_risk else {})})
        else:
            out.append({"id": cs["id"], "label": cs["label"], "applies": True, "result": "안내", "detail": cs.get("text", "")})
    return out


# ---- 플러그인 ------------------------------------------------------------------------------------

VERDICT = {   # 결과 배지 4개 (mvp-architecture.md 18:40 문구 정리). '등록 가능'은 쓰지 않는다
    "blocked": "차단 성분 발견 → 등록 보류 권고",
    "not_found": "읽은 범위에서 찾지 못함",
    "review": "확인 필요",
    "unreadable": "성분표를 읽지 못함",
}


class ScopeConfirmation(ItemSpec):
    """Seller confirmation after comparison; never a prerequisite for inspecting a readable span."""
    kind = "scope"
    use_zoro = False
    system2 = False
    theta_hi, theta_lo, max_questions = 1.0, 0.0, 1
    counts_toward_cap = False

    def score(self, case, item):
        gate = next(it for it in case.items if it.spec is case.plugin.LABEL)
        guard = case.plugin.LABEL.completion_guard(case, gate)
        ingredients = [it for it in case.items if it.spec is case.plugin.ING]
        item.data["review_requirements"] = guard
        item.data["_review_context"] = gate.data["_review_context"]
        item.data["blocked_found"] = any(it.result == "blocked" for it in ingredients) or (case.data.get("i2715") or {}).get("status") == "match"
        item.data["comparison_done"] = bool(ingredients) and all(it.status == "resolved" for it in ingredients)
        return {"confirmed": float(guard["complete"]), "unconfirmed": float(not guard["complete"])}

    def labels(self, item):
        return {"confirmed": "전체 성분자료 확인 완료", "unconfirmed": "전체 성분표 확인 남음"}

    def resolvable(self, item, top):
        return top == "confirmed"

    def before_question(self, engine, case, item):
        if item.data.get("blocked_found") or not item.data["review_requirements"]["ready_for_confirmation"]:
            caselog.log_event(case.id, "inquiry_draft", item=item.id,
                              evidence_lines=item.data["_review_context"]["text"].splitlines(),
                              reason="; ".join(item.data["review_requirements"]["missing"]),
                              blocking=False if item.data.get("blocked_found") else True)
            engine._close(case, item, "handoff", "차단 결과 먼저 표시 · 남은 확인은 제조사 문의 항목에 추가" if item.data.get("blocked_found")
                          else "전체 성분표 확인 남음 — 제조사에 전체 라벨 요청")
            return True
        return False

    def questions_for(self, item):
        guard = item.data["review_requirements"]
        if item.data.get("blocked_found") or not guard["ready_for_confirmation"] or not item.data["comparison_done"]:
            return []
        item.data["_confirmation_requested"] = guard["evidence_id"]
        context = item.data["_review_context"]
        return [Question("q_confirm_label_" + guard["evidence_id"][:12],
                         f"실제로 판매할 '{context['title']}'의 옵션과 실물 라벨이 아래 자료와 같은가요? "
                         "다른 상품·판본이거나 일부만 있거나 모르면 예를 누르지 마세요. 판매·통관 승인을 뜻하지 않습니다.\n\n"
                         + context["text"], on_answer=_confirm_label, kind="label_confirm")]


class Supplement(Plugin):
    name = "supplement"
    title = "숨은 차단 성분 검수"
    max_questions_per_case = 2          # 사용자 질문은 상품당 최대 2개(캡처 요청은 별도)
    pre_tools = [Tool("read_dom", read_dom, retries=0, on_fail="continue",
                      when=lambda c, it: _inp(c).get("image_origin") != "seller" and bool(label_lines(_inp(c).get("page_text") or "", _inp(c).get("ingredient_html") or ""))),
                 Tool("seller_photo_ocr", ocr_capture, retries=0, on_fail="continue",
                      when=lambda c, it: _inp(c).get("image_origin") == "seller"),
                 Tool("gallery_photo_ocr", ocr_capture, retries=0, on_fail="continue",
                      when=lambda c, it: _inp(c).get("image_origin") == "gallery"),
                 Tool("i2715_product", lambda case, item: {**i2715_lookup(_inp(case).get("title") or "", _inp(case).get("brand") or "",
                                                                         _inp(case).get("barcode") or ""),
                                                          "_args": {"title": _inp(case).get("title"), "brand": _inp(case).get("brand")}},
                      retries=1, on_fail="continue")]
    LABEL, ING, CLASSIFY, SCOPE = LabelSource(), Ingredient(), Classify(), ScopeConfirmation()

    def apply_pre_tool(self, case, tool_name, result):
        if tool_name == "i2715_product":
            case.data["i2715"] = result
        else:
            retain_label(case, None, result)

    def make_items(self, case):
        i = _inp(case)
        case.data["product_info"] = {"url": i.get("url"), "title": i.get("title"), "brand": i.get("brand"),
                                     "role": i.get("role", "seller"),
                                     "store_status_before": (store.product(i.get("url")) or {}).get("status")}
        return [Item("label", self.LABEL, "성분표 확보", data={"_acquisitions": case.data.pop("_acquisitions", [])})]

    def scope_note(self):
        return (f"확인 범위: 라벨에 적힌 성분만 · 식약처 차단 원료 목록 기준일 {REG.as_of or '미상'} "
                f"({REG.source}) · 라벨에 없는 혼입 성분은 확인할 수 없음")

    def on_item_closed(self, case, item):
        if item.spec is self.LABEL:
            caselog.log_event(case.id, "label_review", status=item.status, result=item.result,
                              review_requirements=self.LABEL.completion_guard(case, item))
        if item.spec is self.LABEL and self.LABEL.comparison_ready(case, item):
            form, hits = form_by_code(case)
            c = Item("classify", self.CLASSIFY, "제품 분류", data={"form_code": form, "form_hits": hits})
            case.items.insert(case.items.index(item) + 1, c)
        elif item.spec is self.LABEL and item.status == "handoff":
            guard = self.LABEL.completion_guard(case, item)
            if guard["checks"]["current_source"] and (guard["checks"]["read_quality"]
                                                      or repeats_outside_ingredients(case.data.get("label") or {})):
                self._add_ingredients(case, item, rules_only=True)
        if item.spec is self.CLASSIFY:                                   # 분류 → 검사 세트 → 성분 판정
            names = parse_ingredients(case.data["label"]["lines"])
            case.data["checks"] = run_checks(case, item, names)
            case.data["classification"] = self.classification(case)
            caselog.log_event(case.id, "classification", **(case.data["classification"] or
                              {"status": "unavailable", "advisory": True, "reason": item.reason}))
            caselog.log_event(case.id, "checks", checks=case.data["checks"])
            self._add_ingredients(case, item)
            case.items.append(Item("scope", self.SCOPE, "전체 성분표 확인"))
        if item.spec is self.ING:
            item.data["card"] = self._card(case, item)

    def classification(self, case):
        """패널 맨 위 한 줄: 종류·제형·용도와 확률."""
        it = next((x for x in case.items if x.spec is self.CLASSIFY and x.probs), None)
        if not it:
            return None
        def top(q, probs):
            k = max(probs, key=probs.get)
            return {"key": k, "label": CLS[q][k]["label"], "p": round(probs[k], 4), "probs": probs}
        a = it.data.get("answers", {})
        form = ({"key": it.data["form_code"], "label": CLS["form"][it.data["form_code"]]["label"], "p": None, "by": "code"}
                if it.data.get("form_code") else {**top("form", a.get("form", {"other": 1.0})), "by": "zoro"})
        return {"product_type": top("product_type", it.probs), "form": form, "use": top("use", a.get("use", {"other": 1.0})),
                "advisory": max(it.probs, key=it.probs.get) != "drug_suspect",
                "config": "classification.json(초안)"}

    def panel_extras(self, case):
        gate = next((it for it in case.items if it.spec is self.LABEL), None)
        lab = case.data.get("label") or {}
        ingredients = [it for it in case.items if it.spec is self.ING]
        read_result = None
        if ingredients and all(it.status in ("resolved", "handoff") for it in ingredients):
            key, label, why = self.verdict(case)
            read_result = {"key": key, "label": label, "why": why}
        return {"classification": case.data.get("classification"), "checks": case.data.get("checks"),
                "read_scope_result": read_result,
                "self_answers": case.data.get("self_answers", []),
                "agent_state": case.data.get("agent_state"),
                "gallery_selection": case.data.get("gallery_selection"),
                "label_amount_conflicts": case.data.get("label_amount_conflicts", []),
                "product_history": {k: v for k, v in (case.data.get("i2715") or {}).items() if k != "rows"} or None,
                "label_evidence": {"source": lab.get("source"), "engine": lab.get("engine"),
                                   "image_origin": lab.get("image_origin"), "source_description": lab.get("source_description"),
                                   "parsed_count": len(_lab_ingredients(case)),
                                   "coverage_probs": gate.data.get("coverage_probs") if gate else None,
                                   "review_requirements": self.LABEL.completion_guard(case, gate) if gate else None,
                                   "brand_source": _inp(case).get("brand_source"),
                                   "acquisitions": gate.data.get("_acquisitions", []) if gate else []}}

    def _add_ingredients(self, case, gate, rules_only=False, label=None):
        lines = (label or case.data["label"])["lines"]
        existing = {norm(it.data["name"]) for it in case.items if it.spec is self.ING}
        start = sum(it.spec is self.ING for it in case.items)
        names = [name for name in parse_ingredients(lines) if norm(name) not in existing]
        for n, name in enumerate(names):
            line = next((ln["text"] for ln in lines if name in ln["text"]), name)
            j = judge_ingredient(name, REG, DICTS, zoro=None, k=N_CANDIDATES)      # ①② (코드) — ③ 은 엔진이 부른다
            taxon = verified_taxon_links(name)
            if taxon:
                j = {**j, "decision": "match", "entry_id": taxon[0]["registry_id"],
                     "candidates": sorted({link["registry_id"] for link in taxon}), "decided_by": "verified_taxon"}
            if rules_only and j["decision"] != "match":
                continue
            stages = {k: v for k, v in j["stages"].items()}
            if taxon:
                stages["verified_taxon"] = taxon
            rule_decision = j["decided_by"]
            knowledge = retrieve_knowledge(REG, name)
            if knowledge:
                stages["kb_retrieval"] = knowledge
                if (not rules_only and j["decision"] != "match" and knowledge["mode"] == "candidates"
                        and knowledge["status"] == "retrieved" and knowledge["candidates"]):
                    j = {**j, "candidates": [row["id"] for row in knowledge["candidates"]],
                         "decision": "undecided", "decided_by": None}
            if not rules_only and j["decision"] != "match" and knowledge and knowledge.get("status") == "retrieved" and knowledge.get("mode") == "candidates":
                official = [r for r in knowledge["candidates"] if r["official"] and not r["scope_check"]
                            and r["match"] in ("normalized_exact", "whole_phrase")]
                if official:
                    j = {**j, "decision": "match", "entry_id": official[0]["id"],
                         "candidates": [r["id"] for r in official], "decided_by": "official_knowledge"}
                elif not knowledge["candidates"]:
                    j = {**j, "decision": "none", "entry_id": None, "candidates": [], "decided_by": "no_candidates"}
            if not rules_only and j["decision"] != "match" and STANDARD_NUTRIENT.fullmatch(name.strip()) and not registry_links([name]):
                strong = (knowledge or {}).get("candidates", [])
                if not any(r.get("match") in ("normalized_exact", "identifier", "reference_trail", "whole_phrase") for r in strong):
                    j = {**j, "decision": "none", "entry_id": None, "candidates": [], "decided_by": "common_identity"}
            if not rules_only and fixed_pipeline() and j["decision"] == "none":
                j = {**j, "candidates": list(dict.fromkeys(([j["entry_id"]] if j["entry_id"] else []) + j["candidates"]
                                                          + [x["id"] for x in string_candidates(name, REG, N_CANDIDATES)])),
                     "decision": "undecided", "decided_by": None}
            caselog.log_event(case.id, "match_stages", ingredient=name, decision=j["decision"],
                              decided_by=j["decided_by"], rule_decision=rule_decision, entry_id=j["entry_id"],
                              candidates=j["candidates"], stages=stages, registry=REG.source,
                              fixed_pipeline=fixed_pipeline())
            it = Item(f"ing:{start + n + 1}", self.ING, f"성분: {name}",
                      data={"name": name, "line": line, "candidates": list(j["candidates"]), "stages": stages,
                            "baseline_hit": stages["partial"]["preferred_exact"]["decision"] == "match",
                            "rule_decision": rule_decision})
            if j["decision"] == "match":            # ① 또는 ② 가 확정 → Zoro-1 을 부르지 않는다
                it.data["entry_id"] = j["entry_id"]
                it.probs, it.status, it.result = {j["entry_id"]: 1.0, "none": 0.0}, "resolved", "blocked"
                it.reason = DECIDED_BY[j["decided_by"]]
            elif j["decision"] == "none":           # 후보 없음
                it.probs, it.status, it.result = {"none": 1.0}, "resolved", "clear"
                it.reason = DECIDED_BY[j["decided_by"]]
            it.data["decided_by"] = j["decided_by"]
            if it.status == "resolved":
                record_self_answer(case, it, name + "의 목록 대조", "있음" if it.result == "blocked" else "목록에 없음",
                                   ([row["text"] for row in lines if any(norm(link["observed_name"]) in norm(row["text"]) for link in taxon)] or [line]),
                                   decided_by=j["decided_by"], entry_id=j["entry_id"], identity_evidence=taxon)
                it.data["card"] = self._card(case, it)
            case.items.insert(case.items.index(gate) + 1 + n, it)

    # ---- 근거 카드 ------------------------------------------------------------------------------
    def _card(self, case, it):
        n = it.data["name"]
        d = it.decision_probs()
        by = it.data.get("decided_by") if it.data.get("decided_by") != "zoro" else None
        by = by or ("zoro" if it.n_zoro else it.data.get("decided_by"))
        e = self.ING.matched(it) if it.result != "clear" else None
        remaining = []
        if it.status == "handoff":
            verdict = "확인 필요"
            if it.data.get("no_evidence"):
                remaining.append(it.data["no_evidence"])
            why = (f"라벨의 '{n}'이(가) 차단 원료 {e.label if e else ''}와 같은 성분인지 확신이 부족합니다"
                        f"(차단 확률 {d.get('blocked', 0):.0%}). 사람 확인이 필요합니다.")
            remaining.append("제조사 성분 규격서(원료명·학명) 확인")
        elif it.result == "blocked":
            verdict = "차단 성분"
            via = next((f for f in it.facts if "another name" in f), "")
            how = DECIDED_BY.get(by, by or "")
            why = (f"라벨의 '{n}'은(는) 식약처 차단 원료 '{e.label}'(지정일 {e.appn_dt})과 같은 성분으로 판정했습니다. "
                        f"근거: {how}" + (f" ({d.get('blocked', 0):.0%})" if by == "zoro" else "") + (f" · {via}" if via else ""))
        else:
            verdict = "차단 근거 없음"
            why = (f"라벨의 '{n}'과 같은 성분을 식약처 차단 원료 목록에서 찾지 못했습니다"
                        + (f"(Zoro-1 해당 없음 {d.get('clear', 0):.0%})." if by == "zoro" else f"({DECIDED_BY.get(by, '')})."))
        if REG.is_sample:
            remaining.append("샘플 목록으로 판정함: 실제 식약처 목록으로 재확인")
        identity_evidence = it.data.get("stages", {}).get("verified_taxon", [])
        if by == "verified_taxon" and it.result == "blocked":
            why = f"라벨의 '{n}'이 지정 원료 '{e.label}'의 대상 범위에 들어갑니다. " + " / ".join(
                f"{link['source']}: {link['observed_name']} → {link['registry_name']}" for link in identity_evidence)
        # Ingredient evidence is not product-action authority; do not display generated recommendations.
        return {"ingredient": n, "label_line": it.data.get("line"), "verdict": verdict, "decided_by": by,
                "decided_by_label": DECIDED_BY.get(by, by), "entry": e.card() if e else None,
                "matched_entry": e.label if e else None, "entry_names": e.names if e else [],
                "link": LIST_PAGE if e else None, "probability": d, "option_probs": it.probs, "facts": it.facts,
                "why": why, "why_by": "code", "identity_evidence": identity_evidence,
                "remaining_checks": remaining, "blocklist_source": REG.source,
                "baseline_hit": it.data.get("baseline_hit")}

    # ---- 단어 일치(Highlight This 방식) 비교: 기준선 = ① preferred_exact 와 같은 함수 ----------------------
    def comparison(self, case):
        ings = [it for it in case.items if it.spec is self.ING]
        if not ings:
            return None
        rows = []
        for it in ings:
            ours = "blocked" if it.result == "blocked" else "clear" if it.result == "clear" else "review"
            base = "blocked" if it.data.get("baseline_hit") else "clear"
            cat = ("ours_review" if ours == "review" else "both_blocked" if base == "blocked" and ours == "blocked" else
                   "ours_only" if ours == "blocked" else "baseline_only" if base == "blocked" else "both_clear")
            e = self.ING.matched(it) if ours == "blocked" else None
            rows.append({"name": it.data["name"], "line": it.data.get("line"), "baseline": base, "ours": ours,
                         "entry": e.label if e else None, "decided_by": it.data.get("decided_by"), "category": cat})
        count = {k: sum(r["category"] == k for r in rows) for k in
                 ("both_blocked", "ours_only", "baseline_only", "both_clear", "ours_review")}
        return {"rows": rows, "count": count, "baseline_blocked": sum(r["baseline"] == "blocked" for r in rows),
                "ours_blocked": sum(r["ours"] == "blocked" for r in rows),
                "baseline": "목록 한글명·영문명 단어 일치(Highlight This 방식, research B1 과 같은 정의)"}

    def verdict(self, case):
        gate = next((it for it in case.items if it.spec is self.LABEL), None)
        ings = [it for it in case.items if it.spec is self.ING]
        blocked = [it for it in ings if it.result == "blocked"]
        prod = case.data.get("i2715") or {}
        if prod.get("status") == "match":
            return "blocked", VERDICT["blocked"], "이 제품 자체가 차단 이력 있음: " + prod["reason"] + (
                "; 성분: " + "; ".join(f"{it.data['name']} = {self.ING.matched(it).label}" for it in blocked) if blocked else "")
        if blocked:
            reason = "; ".join(f"{it.data['name']} = {self.ING.matched(it).label}" for it in blocked)
            if gate and not self.LABEL.completion_guard(case, gate)["complete"]:
                reason += "; 일부 자료의 차단 근거이며 지정 성분자료 검토는 미완료"
            return "blocked", VERDICT["blocked"], reason
        unverified = [it for it in ings if it.data.get("model_unverified")]
        if unverified:
            return "review", VERDICT["review"], "모델 답의 완결·출처를 확인하지 못해 판정하지 않음(" + ", ".join(
                it.title for it in unverified) + ")"
        server_down = [it for it in ings if it.data.get("server_error")]
        if server_down:     # 판정 서버 장애는 '성분표를 읽지 못함'이 아니다 → 확인 필요(사유 명시)
            return "review", VERDICT["review"], "판정 서버에 연결하지 못해 판정하지 않음(" + ", ".join(
                it.title for it in server_down) + ")"
        if gate and not self.LABEL.comparison_ready(case, gate):
            outcomes = gate.data.get("_outcomes", []) + gate.data.get("_acquisitions", [])
            details = "; ".join(dict.fromkeys(o.get("result") or o.get("reason") or o.get("status", "") for o in outcomes))
            missing = "; ".join(self.LABEL.completion_guard(case, gate)["missing"])
            return "review", VERDICT["review"], "지정 성분자료 검토 미완료 → 사람 검토: " + missing + "; " + (details or gate.reason)
        review = [f"{it.data['name']}: {it.reason}" for it in ings if it.status == "handoff"]
        fda = case.data.get("fda")
        if fda and fda.get("found"):
            r0 = fda["recalls"][0]
            review.append(f"미국 FDA 미표시 성분 회수 이력 {len(fda['recalls'])}건(예: {r0['recall_number']} {r0['recall_initiation_date']}) "
                          "— FDA 이력은 한국 차단 근거가 아님, 확인 필요")
        review += [f"제품 종류 {c['detail']} → 확인 필요" for c in case.data.get("checks") or [] if c.get("verdict") == "review"]
        if review:
            return "review", VERDICT["review"], "; ".join(review)
        if not gate or not self.LABEL.comparison_ready(case, gate) or not ings or any(it.status != "resolved" for it in ings):
            return "review", VERDICT["review"], "대조할 성분자료 또는 성분 판정 미완료 → 사람 검토"
        if not self.LABEL.completion_guard(case, gate)["complete"]:
            return "review", VERDICT["review"], f"읽은 성분 {len(ings)}개 대조 완료. 전체 성분표 확인이 남아 제조사 자료가 필요합니다."
        return "not_found", VERDICT["not_found"], f"읽은 성분 {len(ings)}개 대조. {self.scope_note()}"

    def generate(self, case):
        key, label, why = self.verdict(case)
        L = [f"# 등록 전 검수 결과 — {label}", "", f"- 상품: {(case.data.get('product_info') or {}).get('title') or case.text}",
             f"- 케이스: `{case.id}`", f"- 근거: {why}", f"- {self.scope_note()}"]
        c = case.data.get("classification")
        if c:
            f = c["form"]
            L.append(f"- 분류: {c['product_type']['label']} {c['product_type']['p']:.0%} · 제형 {f['label']}"
                     f"{'(코드)' if f['by'] == 'code' else f' {f[chr(112)]:.0%}'} · 용도 {c['use']['label']} {c['use']['p']:.0%} (보기: {c['config']})")
        fda = case.data.get("fda")
        if fda and fda.get("found"):
            L.append("- **FDA 회수 이력(참고)**: " + "; ".join(f"{r['recall_number']} {r['recall_initiation_date']} {(r['product_description'] or '')[:60]} — {(r['reason_for_recall'] or '')[:80]}" for r in fda["recalls"][:3])
                     + " · **FDA 이력은 한국 반입 차단 근거가 아님** → 확인 필요")
        ph = case.data.get("i2715")
        if ph:
            L.append(f"- 제품 차단 이력(I2715): {ph['status']} — {ph['reason']}")
        for k in case.data.get("checks") or []:
            if k["id"] != "blocked":
                L.append(f"- 검사 세트 · {k['label']}: {k['result']}" + (f" — {k['detail']}" if k["detail"] else ""))
        L += ["", "## 성분별 근거 카드", ""]
        ings = [it for it in case.items if it.spec is self.ING]
        if not ings:
            L.append("(판독된 성분 없음)")
        for it in ings:
            c = it.data.get("card") or {}
            L += [f"### {c.get('ingredient')} — {c.get('verdict')}", f"- 라벨 원문: {c.get('label_line')}",
                  f"- 판정 단계: {c.get('decided_by_label')}"]
            if c.get("entry"):
                en = c["entry"]
                L += [f"- 차단 원료: {en['label']} (목록 번호 {en['id']}, 지정일 {en['designated']}, 기타명칭: {', '.join(en['other_names']) or '없음'})",
                      f"- 근거: {en['basis']} · 원문: {en['list_page']}"]
            if c.get("decided_by") == "zoro":
                L.append("- 확률: " + ", ".join(f"{self.ING.labels(it).get(k, k)} {v:.0%}" for k, v in c.get("probability", {}).items()))
            L.append(f"- 판정 이유({c.get('why_by')}): {c.get('why')}")
            L += [f"- 남은 확인: {r}" for r in c.get("remaining_checks", [])]
            L.append("")
        cmp = self.comparison(case)
        if cmp:
            CAT = {"both_blocked": "둘 다 차단", "ours_only": "**단어 일치가 놓침 → 우리가 잡음**",
                   "baseline_only": "단어 일치만 차단(우리: 차단 근거 없음)", "both_clear": "둘 다 표시 안 함",
                   "ours_review": "우리: 확인 필요"}
            L += ["## 단어 일치(Highlight This 방식) vs 우리 판정", "", f"- 기준선: {cmp['baseline']}",
                  f"- 단어 일치가 표시한 성분 {cmp['baseline_blocked']}개 / 우리가 차단한 성분 {cmp['ours_blocked']}개 / "
                  f"**단어 일치가 놓친 차단 {cmp['count']['ours_only']}개**, 단어 일치만 막은 것 {cmp['count']['baseline_only']}개", "",
                  "| 성분 | 단어 일치 | 우리 | 판정 단계 | 구분 |", "|---|---|---|---|---|"]
            for r in cmp["rows"]:
                L.append(f"| {r['name']} | {'표시' if r['baseline'] == 'blocked' else '표시 안 함'} | "
                         f"{dict(blocked='차단', clear='차단 근거 없음', review='확인 필요')[r['ours']]}"
                         f"{' = ' + r['entry'] if r['entry'] else ''} | {DECIDED_BY.get(r['decided_by'], r['decided_by'] or '')} | {CAT[r['category']]} |")
            L.append("")
        gate = next((it for it in case.items if it.spec is self.LABEL), None)
        if gate and gate.status == "handoff":
            o = case.data.get("label") or {}
            L += ["## 성분표 근거 미확보 — 사람 검토", "", f"- 사유: {why}",
                  "- 시도한 경로: " + ", ".join(["페이지 글자"] + gate.tools_done),
                  f"- 마지막 출처: {o.get('source', '없음')}, OCR 평균 신뢰도: {o.get('mean_conf')}",
                  "- 읽은 것: " + (" / ".join(ln["text"] for ln in o.get("lines", [])) or "없음"),
                  "- 남은 것: 성분표 전체 판독, 성분별 차단 원료 대조", ""]
        return {"title": f"검수 결과: {label}", "markdown": "\n".join(L) + "\n"}

    # ---- 행동(모의 상점 장부) / 상품 상태 ------------------------------------------------------------
    def plan_actions(self, case):
        key = case.verdict["key"]
        info = case.data["product_info"]
        url, title, role = info.get("url"), info.get("title"), info.get("role", "seller")
        if role == "consumer":      # 소비자: 장부 행동 없이 페이지 경고만(확장이 그린다)
            return [{"name": "page_warning", "label": "구매 버튼 옆 경고 표시", "auto": True,
                     "args": {"url": url}, "reason": case.verdict["why"]}] if key == "blocked" else []
        p2 = os.environ.get("NEXTASK_P2") == "1"       # P2(판매 중지·고객 안내·해외 구매 보류)는 슬라이드용, 기본 끔
        before = info.get("store_status_before")
        acts = []
        if key == "blocked":
            if p2 and before == "판매 중":
                acts.append({"name": "stop_sale", "label": "판매 중지 권고", "auto": True, "args": {"url": url},
                             "reason": "판매 중 상품에서 차단 성분 발견(되돌릴 수 있음)"})
                for o in store.sold_orders(url):
                    acts.append({"name": "customer_notice", "label": f"고객 안내 초안 (주문 {o['id']})", "auto": False,
                                 "args": {"url": url, "order_id": o["id"], "text": self._notice(case, o)},
                                 "reason": "고객 발송은 셀러 승인 필요"})
            else:
                acts.append({"name": "hold_listing", "label": "등록 보류", "auto": True, "args": {"url": url, "title": title},
                             "reason": "차단 성분 발견(되돌릴 수 있음)"})
            if p2:
                acts.append({"name": "hold_po", "label": "해외 구매 보류(주문이 들어와도 사지 않도록 표시)", "auto": True,
                             "args": {"url": url}, "reason": "되돌릴 수 있음"})
        elif key in ("review", "unreadable"):
            acts.append({"name": "hold_listing", "label": "등록 보류(사람 확인 끝날 때까지)", "auto": True,
                         "args": {"url": url, "title": title, "status": VERDICT[key]}, "reason": case.verdict["why"]})
            acts.append({"name": "review_queue", "label": "사람 검토 대기열에 올림", "auto": True,
                         "args": {"url": url}, "reason": case.verdict["why"]})
        else:
            gate = next((it for it in case.items if it.spec is self.LABEL), None)
            complete = bool(gate and self.LABEL.completion_guard(case, gate)["complete"])
            status = VERDICT["not_found"] + ("" if complete else " · 전체 성분표 확인 남음")
            acts.append({"name": "mark_checked", "label": "읽은 범위 대조 기록", "auto": True,
                         "args": {"url": url, "title": title, "status": status}, "reason": case.verdict["why"]})
        acts.append({"name": "notify", "label": "셀러 알림", "auto": True, "args": {"url": url},
                     "reason": f"{case.verdict['label']}: {case.verdict['why']}"})
        return acts

    def _notice(self, case, order):
        names = ", ".join(it.data["name"] for it in case.items if it.spec is self.ING and it.result == "blocked")
        fallback = (f"안녕하세요. 주문하신 상품에 식약처 해외직구 차단 원료({names})가 포함된 것으로 확인되어 "
                    "판매를 중지했습니다. 복용을 멈추시고, 환불 절차를 안내해 드리겠습니다.")
        return explain(f"Write a short polite Korean notice to a customer: the product they bought contains a blocked "
                       f"ingredient ({names}); sales stopped; advise to stop taking it; refund will be guided.",
                       fallback, purpose="customer_notice")["text"]

    def run_action(self, case, a):
        g = a["args"]
        if a["name"] == "hold_listing":
            return store.set_status(g["url"], g.get("status", "등록 보류"), g.get("title"))
        if a["name"] == "stop_sale":
            return store.set_status(g["url"], "판매 중지")
        if a["name"] == "hold_po":
            return store.hold_purchase_orders(g["url"])
        if a["name"] == "mark_checked":
            return store.set_status(g["url"], g["status"], g.get("title"))
        if a["name"] == "notify":
            return store.notify(a["reason"], g["url"])
        if a["name"] == "review_queue":
            return store.enqueue_review(case.id, g["url"], a["reason"])
        if a["name"] == "customer_notice":
            return store.send_customer_notice(g["url"], g["order_id"], g["text"])
        if a["name"] == "page_warning":
            return {"shown_by": "extension"}
        raise ValueError(a["name"])

    def product_status(self, case):
        if case.status in ("running", "new"):
            return "재검사" if any(it.asked for it in case.items) else "검사 중"
        if case.status == "waiting_user":
            return "답변 대기"
        key = (case.verdict or {}).get("key")
        before = (case.data.get("product_info") or {}).get("store_status_before")
        if key == "blocked":
            return "판매 중지 권고" if before == "판매 중" and os.environ.get("NEXTASK_P2") == "1" else "등록 보류 권고"
        return VERDICT.get(key, case.status)

    # ---- (P1) 패널 자유 질문: 근거 카드의 사실만으로 답한다 --------------------------------------------
    NO_EVIDENCE = "확인한 근거가 없습니다."

    def chat(self, case, message):
        started = time.perf_counter()
        # Rebuild facts from current item decisions; never reuse historical generated card prose.
        cards = [self._card(case, it) for it in case.items
                 if it.spec is self.ING and it.status in ("resolved", "handoff")]
        verdict = case.verdict if case.status == "done" else None
        if verdict and verdict.get("key") in VERDICT:
            authority = f"상품 검토 결과: {VERDICT[verdict['key']]} — {verdict.get('why') or self.NO_EVIDENCE}"
        else:
            authority = "상품 검토 미완료 — 남은 확인 또는 사람 검토가 필요합니다."
        evidence = self._chat_template(message, cards)
        return {"text": f"{authority}\n질문에 대응하는 성분 근거: {evidence}", "by": "code",
                "latency_ms": round((time.perf_counter() - started) * 1000, 3)}

    def _chat_template(self, message, cards):
        """질문에 명시된 성분·목록 원료의 현재 근거만 선택한다. 상품 행동을 결정하지 않는다."""
        m = norm(message)
        for f in cards:
            e = f.get("entry") or {}
            names = [f["ingredient"], f["ingredient"].split("(", 1)[0].strip()] + [x for x in [e.get("kr"), e.get("en")] + list(e.get("other_names") or []) if x]
            if any(len(norm(x)) > 3 and norm(x) in m for x in names):
                return f"{f['ingredient']}: {f['verdict']} — {f['why']}" + (f" 원문: {f['link']}" if f["link"] else "")
        asks_list = re.search(r"어떤\s*성분|무슨\s*성분|어느\s*성분|(확인|대조|검사|검토)\s*한\s*성분|성분\s*(목록|전부|모두)|무엇을\s*(확인|대조|검사)"
                              r"|which ingredients|what ingredients|what did you (check|compare)|list (of |the )?ingredients", message, re.I)
        if cards and asks_list:      # 확인한 성분 목록을 묻는 질문에만: 사건의 대조 결과를 그대로 나열한다(안심 문구 없음)
            listed = "; ".join(f"{f['ingredient']}: {f['verdict']}" for f in cards[:40])
            more = f" 외 {len(cards) - 40}개" if len(cards) > 40 else ""
            return f"대조한 성분 {len(cards)}개 — {listed}{more}"
        return self.NO_EVIDENCE

    def highlights(self, case):
        """확장이 페이지 위에 강조할 문자열(차단·확인 필요 성분의 라벨 원문)."""
        out = []
        for it in case.items:
            if it.spec is self.ING and (it.result == "blocked" or it.status == "handoff"):
                e = self.ING.matched(it)
                by = it.data.get("decided_by")
                out.append({"text": it.data["name"], "level": "blocked" if it.result == "blocked" else "review",
                            "entry": e.label if e else None, "p": round(it.top()[1], 2),
                            "how": DECIDED_BY.get(by, "Zoro-1 판정") if by and by != "zoro" else "Zoro-1 판정"})
        return out


PLUGIN = Supplement()
