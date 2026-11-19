"""외부 조회 도구 (교체 가능).

DSLDSearch       T3 외부 라벨 후보 검색: 검색 순위·UPC 일치는 현 상품 판본의 성분 근거가 아니다.
                 후보 정보만 반환하며 현재 라벨로 자동 채택하지 않는다.
PubChemSynonyms  T5 다른 이름 조회 보조: PubChem PUG REST 의 화합물 동의어. PUBCHEM_LIVE=1 일 때만 호출.
                 학명·식물 추출물명은 PubChem 에 없을 수 있어 원료성분 표(mfds.AliasLookup)가 먼저다.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .mfds import norm

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DSLD_DIR = os.path.join(ROOT, "data", "samples", "dsld")


class DSLDSearch:
    """T3 미국 NIH DSLD 라벨 조회(키 없음, 2026-11-19 실측: search-filter 의 hits[]._source{brandName, fullName, entryDate,
    offMarket}, label/{id} 의 ingredientRows[{name, quantity[]}], otheringredients.ingredients[{name}], upcSku).
    검색 후보와 현재 상품의 식별 충돌을 기록한다. 판본 확인 근거가 없어 성분은 판정에 쓰지 않는다."""
    name = "dsld"
    BASE = "https://api.ods.od.nih.gov/dsld/v9"

    def _get(self, path):
        import hashlib
        cache = os.path.join(ROOT, "data", "samples", "dsld-cache", hashlib.sha1(path.encode()).hexdigest()[:16] + ".json")
        if os.path.exists(cache):
            return json.load(open(cache, encoding="utf-8"))
        if os.environ.get("DSLD_OFFLINE") == "1":
            raise RuntimeError("DSLD offline and not cached")
        with urllib.request.urlopen(self.BASE + path, timeout=20) as r:
            d = json.load(r)
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        json.dump(d, open(cache, "w", encoding="utf-8"), ensure_ascii=False)
        return d

    def search(self, product_title, brand="", upc=""):
        words = [w for w in norm(product_title).split() if len(w) >= 3 and not w.isdigit()][:4]
        q = urllib.parse.quote(" ".join(([brand] if brand else []) + words))
        hits = (self._get(f"/search-filter?q={q}&size=10") or {}).get("hits") or []
        bt = [w for w in norm(brand).split() if len(w) >= 3 and w not in ("foods", "food", "labs", "nutrition", "inc", "co", "op")]
        brand_ok = lambda h: not bt or any(w in norm(h.get("_source", {}).get("brandName")).split() for w in bt)  # noqa: E731
        def score(h):
            s_ = h.get("_source", {})
            return (brand_ok(h) * 4 + (str(s_.get("offMarket")) == "0") * 2
                    + len(set(words) & set(norm(s_.get("fullName")).split())), s_.get("entryDate") or "")
        hits = sorted(hits, key=score, reverse=True)
        if not hits or not brand_ok(hits[0]) or not set(words) & set(norm(hits[0].get("_source", {}).get("fullName")).split()):
            return {"source": "dsld", "found": False, "status": "no_candidates", "query": urllib.parse.unquote(q), "candidates": len(hits)}
        lab = self._get(f"/label/{hits[0]['_id']}")
        candidate = {"dsld_id": lab.get("id"), "title": lab.get("fullName"), "brand": lab.get("brandName"),
                     "entry_date": lab.get("entryDate"), "off_market": lab.get("offMarket"), "upc": lab.get("upcSku")}
        conflicts = []
        if brand and candidate["brand"] and norm(brand) != norm(candidate["brand"]):
            conflicts.append("brand_mismatch")
        def gtin(value):
            value = str(value or "").strip().replace("-", "").replace(" ", "")
            return value.zfill(14) if value.isdigit() and len(value) in (8, 12, 13, 14) else None
        supplied, listed = gtin(upc), gtin(candidate["upc"])
        if supplied and listed and supplied != listed:
            conflicts.append("barcode_mismatch")
        if str(candidate["off_market"]) == "1":
            conflicts.append("off_market")
        identity = "conflict" if conflicts else "barcode_match" if supplied and supplied == listed else "unverified"
        candidate.update(identity=identity, conflicts=conflicts, version_verified=False)
        return {"source": "dsld", "found": True, "status": "candidate_unverified", "verified": False,
                "candidate": candidate, "query": urllib.parse.unquote(q),
                "reason": "DSLD 후보의 현재 상품·판본 동일성 미확인 — 성분 판정에 사용하지 않음"}


class FDARecalls:
    """openFDA drug/enforcement(키 없음, 2026-11-19 실측): 미표시 성분(undeclared) 회수 이력.
    결과는 한국 반입 차단 근거가 아니다 → 에이전트는 '확인 필요'까지만 낸다. 응답은 data/samples/fda-cache/ 에 저장."""
    BASE = "https://api.fda.gov/drug/enforcement.json"

    def search(self, product_title, brand=""):
        import hashlib
        words = [w for w in norm(product_title).split() if len(w) >= 4 and not w.isdigit()][:3]
        terms = ([norm(brand).split()[0]] if brand and norm(brand) else []) + words[:2]
        if not terms:
            return {"source": "openFDA", "found": False, "query": ""}
        q = "+AND+".join(f'product_description:"{urllib.parse.quote(t)}"' for t in terms[:2])
        url = f"{self.BASE}?search=reason_for_recall:%22undeclared%22+AND+{q}&limit=5"
        cache = os.path.join(ROOT, "data", "samples", "fda-cache", hashlib.sha1(url.encode()).hexdigest()[:16] + ".json")
        if os.path.exists(cache):
            d = json.load(open(cache, encoding="utf-8"))
        else:
            try:
                with urllib.request.urlopen(url, timeout=20) as r:
                    d = json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 404:          # openFDA 는 결과 없음을 404 로 준다
                    d = {"results": []}
                else:
                    raise
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            json.dump(d, open(cache, "w", encoding="utf-8"), ensure_ascii=False)
        rows = [{k: r.get(k) for k in ("recall_number", "product_description", "reason_for_recall", "recall_initiation_date",
                                       "recalling_firm", "classification", "status")} for r in d.get("results") or []]
        return {"source": "openFDA drug/enforcement", "found": bool(rows), "query": " ".join(terms), "recalls": rows}


class PubChemSynonyms:
    name = "pubchem"

    def lookup(self, name, *, complete=False):
        if os.environ.get("PUBCHEM_LIVE") != "1":
            return None
        url = ("https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/"
               f"{urllib.parse.quote(name)}/synonyms/JSON")
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                records = json.load(r)["InformationList"]["Information"]
            if complete and len(records) != 1:
                return None
            record = records[0]
            synonyms = record["Synonym"]
            if complete and (not isinstance(record.get("CID"), int) or record["CID"] <= 0
                             or not synonyms or not all(isinstance(s, str) for s in synonyms)
                             or norm(name) not in {norm(s) for s in synonyms}):
                return None
            syn = synonyms if complete else synonyms[:30]
            if not syn:
                return None
        except Exception:  # No record, malformed reply and transport failure are not negative evidence.
            return None
        result = {"query": name, "canonical": syn[0], "aliases": syn[1:], "source": "pubchem"}
        if complete:
            result.update(cid=record["CID"], lookup_complete=True)
        return result


# ---- 페이지 글자 파싱 (확장이 보낸 page_text / ingredient_html → 성분표 줄) -----------------------------
import html as _html
import re as _re

_FACTS = _re.compile(r"^(?:(?:supplement|nutrition) facts|active ingredients per (?:melt|tablet|capsule|serving)(?:\s*\([^()]{1,60}\))?|성분표(?:\s*\(영문\))?|영양\s*(?:성분표?|정보))\s*:?\s*$", _re.I)
_SERVING = _re.compile(r"^(?:serving size|amount per serving|1회\s*제공량)\b", _re.I)
# The scoped collector serializes all header cells as one line, not necessarily starting with "Amount".
_SCOPED_COLUMNS = _re.compile(r"(?:(?:description|nutrient|ingredient)\s+)?amount per serving\s+%\s*(?:daily value|dv)", _re.I)
_DOSE = _re.compile(r"\d[\d,.]*\s*(?:mg|mcg|[µμ]g|ug|g|iu|㎎|㎍)\b", _re.I)
_DECL = _re.compile(r"^(other ingredients?|기타\s*(?:성분|원료)|main ingredients?|ingredients?|"
                    r"주(?:요)?\s*성분|원재료(?:명)?|성분|재료)(?:\s*[:：]\s*(.*)|\s*)$", _re.I)
_STOP = _re.compile(r"^(?:suggested use|directions|warnings?|caution|disclaimer|reviews?|customer reviews|"
                    r"q\s*&\s*a|questions|shipping|recommended|frequently bought|similar (?:formulas|products)|compare|"
                    r"you may also|customers also|related products|use and warnings|details|섭취 방법|사용법|방향|경고|안전 정보)\b", _re.I)


def html_to_text(h):
    h = _re.sub(r"(?is)<(script|style).*?</\1>", " ", h or "")
    h = _re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d)>", "\n", h)
    h = _re.sub(r"(?i)</t[dh]>", " ", h)
    return _html.unescape(_re.sub(r"<[^>]+>", "", h))


def _declarations(lines):
    """Read only the value of each declared list, not the next advertising paragraph."""
    out = []
    for i, line in enumerate(lines):
        match = _DECL.fullmatch(line)
        if not match:
            continue
        name, value = match.groups()
        if not value and i + 1 < len(lines) and not _DECL.fullmatch(lines[i + 1]):
            value = lines[i + 1]
        if not value or _STOP.match(value):
            continue
        nested = _DECL.fullmatch(value)
        if nested:
            if not nested[2]:
                continue
            name, value = nested.groups()
        parts = _re.split(r"\s*(?=(?:기타\s*(?:성분|원료)|other ingredients?)\s*[:：])", value, maxsplit=1, flags=_re.I)
        if len(parts) == 2:
            main, other = parts
            # A serving legend can share one paragraph with an actual quantity row.
            for row in _re.split(r"\*\*|\.\s+(?=[가-힣A-Za-z])", main):
                if _DOSE.search(row) and not _SERVING.match(row.strip()):
                    out.append(row.strip())
            match = _DECL.fullmatch(other.strip())
            if not match:
                continue
            name, value = match.groups()
        if not value:
            continue
        # Separate the list from precaution / "free of" prose. Preserve the raw
        # localized text in the caller's input for source-by-source comparison.
        sentences = _re.split(r"(?<=\.)\s+", value)
        for n, sentence in enumerate(sentences[1:], 1):
            if _re.search(r"(?i)^(?:not |free of|contains |manufactur|made in|produced|processed)|"
                          r"없습니다|제조되지|시설에서|함유하지", sentence):
                sentences = sentences[:n]
                break
        value = " ".join(sentences).strip(" .")
        if not value or _re.search(r"may be mixed during processing|mass balance", value, _re.I):
            continue
        if _re.match(r"(?i)^(?:helps?|supports?|promotes?|designed to|formulated to|shop now)\b", value):
            continue
        if _re.match(r"other|기타", name, _re.I):
            prefix = "Other ingredients"
        elif _re.match(r"main|주", name, _re.I):
            prefix = "Main ingredients"  # Never upgrade a partial list to the entire declaration.
        else:
            prefix = "Ingredients"
        out.append(f"{prefix}: {value}")
    return list(dict.fromkeys(out))


def label_lines(page_text="", ingredient_html=""):
    """Extract a declared list or a facts block; a heading or product dose alone is not a label."""
    def clean(src):
        return [line for x in src.splitlines() if (line := _re.sub(r"\s+", " ", x).strip())]

    # Never append unscoped body text (e.g. comparison-card ingredients) to a
    # scoped block. The collector includes separate current-product declarations.
    lines = clean(html_to_text(ingredient_html) if ingredient_html else (page_text or ""))
    if not ingredient_html:
        for i, line in enumerate(lines):
            if _re.match(r"(?i)^(?:related products|frequently bought together|you may also like|추천 상품|함께 본 상품)\b", line):
                lines = lines[:i]
                break
    declarations = _declarations(lines)
    if os.environ.get("NEXTASK_SITE_GENERIC", "1") == "0":
        if not any(_FACTS.fullmatch(x) for x in lines):
            return []
        declarations = [y for y in declarations if y.startswith("Other ingredients:")]
    for i, x in enumerate(lines):
        if not (_FACTS.fullmatch(x) or _SERVING.match(x) or (ingredient_html and _SCOPED_COLUMNS.fullmatch(x))):
            continue
        facts = []
        for y in lines[i:i + 160]:
            if _STOP.match(y) or _DECL.fullmatch(y):
                break
            facts.append(y)
        if any(_DOSE.search(y) for y in facts[1:]):
            return list(dict.fromkeys(facts + declarations))
    return list(dict.fromkeys(declarations))


def label_amount_conflicts(dom_lines, ocr_lines):
    """Dose differences on unambiguous same-name rows; no differences is not proof of identity."""
    from decimal import Decimal

    amount = _re.compile(r"(?<![\w.])(\d+(?:,\d{3})*(?:\.\d+)?)\s*(mcg|[µμ]g|ug|mg|g|iu)\b", _re.I)
    # IU/mass equivalence depends on the ingredient.
    units = {"g": 1000000, "mg": 1000, "mcg": 1, "ug": 1, "μg": 1}

    def quantities(lines):
        by_name = {}
        for value in lines:
            line = value.get("text", "") if isinstance(value, dict) else value
            if not isinstance(line, str) or len(line) > 600 or _SERVING.match(line):
                continue
            # Omit nested units in qualifiers, e.g. "Folate (400 mcg folic acid) 667 mcg".
            flat, depth = [], 0
            for ch in line:
                if ch in "([":
                    depth += 1
                elif ch in ")]" and depth:
                    depth -= 1
                    if not depth:
                        flat.append(" ")
                elif not depth:
                    flat.append(ch)
            plain = "".join(flat)
            m = amount.search(plain)
            if not m:
                continue
            name = norm(plain[:m.start()])
            if not name or len(name) > 100 or not _re.match(r"[a-z가-힣]", name):
                continue
            unit = m[2].casefold()
            quantity = Decimal(m[1].replace(",", "")) * units.get(unit, 1)
            by_name.setdefault((name, "IU" if unit == "iu" else "mcg"), {})[quantity] = line
        return by_name

    dom, ocr = quantities(dom_lines), quantities(ocr_lines)
    differences = []
    for key, values in dom.items():
        other = ocr.get(key, {})
        if len(values) == len(other) == 1 and values.keys() != other.keys():
            differences.append({"name": key[0], "dom": next(iter(values.values())), "ocr": next(iter(other.values()))})
    return differences


GALLERY_MAX_BYTES = 10 * 1024 * 1024
GALLERY_MAX_PIXELS = 64_000_000


def _gallery_preview(data):
    """Retain OCR resolution for the winner; send only a bounded preview to Zoro."""
    import base64
    import io
    from PIL import Image, ImageOps

    raw = base64.b64decode(data.split(",", 1)[1], validate=True)
    if len(raw) > GALLERY_MAX_BYTES:
        raise ValueError("gallery image exceeds upload limit")
    with Image.open(io.BytesIO(raw)) as im:
        expected = "PNG" if data.startswith("data:image/png;") else "JPEG"
        if im.format != expected or min(im.size) < 1 or im.width * im.height > GALLERY_MAX_PIXELS:
            raise ValueError("invalid gallery dimensions or type")
        im = ImageOps.exif_transpose(im)
        im.thumbnail((448, 448), Image.Resampling.LANCZOS)
        with io.BytesIO() as out:
            im.convert("RGB").save(out, format="JPEG", quality=88)
            return base64.b64encode(out.getvalue()).decode("ascii")


def choose_gallery_image(images, *, zoro, photo_options, threshold, save_image, observe):
    """Rank current-gallery pixels before OCR. Never dereference a page-supplied URL."""
    import time
    from PIL import Image
    from ._infra_client import ZoroError, ZoroUnavailable

    if not isinstance(images, list) or not images:
        return None
    if os.environ.get("NEXTASK_SITE_GENERIC", "1") == "0":
        observe({"source": "code", "reason": "site_generic_disabled", "candidates": [], "selected_index": None,
                 "model_calls": 0, "model_latency_ms": 0, "next_action": "request_evidence"})
        return None
    best = None
    rows = []
    calls, total_ms = 0, 0.0
    for i, image in enumerate(images[:8]):
        url = image.get("url") if isinstance(image, dict) else None
        data = image.get("data") if isinstance(image, dict) else None
        # Log only the asset path (queries may contain tracking or signed tokens).
        asset = url.split("?", 1)[0].split("#", 1)[0][:512] if isinstance(url, str) else None
        row = {"index": i, "asset": asset}
        rows.append(row)
        if (not isinstance(data, str) or len(data) > ((GALLERY_MAX_BYTES + 2) // 3) * 4 + 32
                or not data.startswith(("data:image/jpeg;base64,", "data:image/png;base64,"))):
            row["status"] = "pixels_unavailable"
            continue
        if zoro.mode not in ("remote", "record"):
            row["status"] = "model_unavailable"
            continue
        try:
            preview = _gallery_preview(data)
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
            row["status"] = "invalid_pixels"
            continue
        request = {"state": "An image from the current product gallery.",
                   "questions": {"photo": {"type": "choice", "criteria": photo_options,
                                           "instructions": "Is this captured image usable for reading the ingredient label?"}},
                   "image": preview}
        calls += 1
        started = time.perf_counter()
        try:
            response, meta = zoro.decide(request)
            row.update(meta=meta, source=meta.get("source"), request_id=meta.get("server_request_id"))
            answer = response["answers"]["photo"]
            scores = {key: float(answer["probabilities"][key]) for key in photo_options}
            if any(not 0 <= p <= 1 for p in scores.values()):
                raise ValueError("invalid probability")
        except (ZoroError, ZoroUnavailable) as exc:
            info = getattr(exc, "meta", None) or {"attempts": exc.attempts}
            attempts = info.get("attempts") or []
            row.update(status="model_failed", error=type(exc).__name__, meta=info, source=info.get("source", "remote"),
                       request_id=info.get("server_request_id") or (attempts[-1].get("request_id") if attempts else None))
            continue
        except (KeyError, TypeError, ValueError) as exc:
            row.update(status="invalid_response", error=type(exc).__name__)
            continue
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            total_ms += elapsed
            row["latency_ms"] = round(elapsed, 1)
        row.update(status="judged", scores=scores, choice=answer.get("choice"))
        if answer.get("choice") == "usable" and scores["usable"] >= threshold and (best is None or scores["usable"] > best[0]):
            best = (scores["usable"], i, data)
    selected = None
    if best:
        try:
            selected = save_image(best[2])
        except (ValueError, OSError):
            rows[best[1]]["status"] = "save_failed"
    # Keep one other verified gallery panel for bounded rereading; never fetch page URLs.
    if selected:
        rows[best[1]]["path"] = selected
        alternate = next((row for row in rows if row["index"] != best[1] and row.get("choice") == "usable"
                          and row.get("scores", {}).get("usable", 0) >= threshold), None)
        if alternate:
            try:
                alternate["path"] = save_image(images[alternate["index"]]["data"])
            except (ValueError, OSError):
                alternate["status"] = "save_failed"
    observe({"source": "Zoro-1", "options": photo_options, "threshold": threshold, "candidates": rows,
             "model_calls": calls, "model_latency_ms": round(total_ms, 1),
             "selected_index": best[1] if selected else None, "next_action": "ocr" if selected else "request_evidence"})
    return selected
