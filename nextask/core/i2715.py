"""I2715 해외직구 위해식품 차단정보(제품 단위) — '이 제품 자체가 차단된 이력이 있나' 확인.

성분 대조는 원료 목록(식품안전나라 차단 원료, matching.Registry)으로 하고, 이 모듈은 제품명·바코드로 I2715 행을 찾는다.
필드(2026-11-19 실제 호출 확인): SELF_IMPORT_SEQ, PRDT_NM, INGR_NM_LST, MUFC_NM(제조사), MUFC_CNTRY_NM, BARCD_CTN,
      STT_YMD(차단 시작), END_YMD, IMAGE_URL, CRET_DTM, LAST_UPDT_DTM. 총 5,221건.
데이터: MFDS_I2715_FILE(고정 스냅숏, 촬영용) → MFDS_API_KEY 로 Open API 제품명 조회(응답은 data/mfds/i2715-cache.jsonl)
        → data/mfds/i2715*.json · data/mfds/snapshot/i2715_*.json 사본 → 없으면 '확인 못 함'.
공개 sample 키는 호출 한도가 있어(INFO-310) 쓰지 않는다.
판정(코드): 바코드 일치 또는 제품명 정규화 일치(+브랜드 단어 포함) → 'match'. 제품명만 비슷하면 'similar'(참고만).
"""
import glob
import json
import os
import re
import urllib.parse
import urllib.request
import difflib

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE = os.path.join(ROOT, "data", "mfds", "i2715-cache.jsonl")


def _n(s):
    return re.sub(r"[^0-9a-z가-힣]+", " ", str(s or "").lower()).strip()


def _load_file(path):
    """I2715 사본: CSV(식품안전나라 내려받기, UTF-8 BOM) 또는 JSON({"row": [...]} / 목록)."""
    if path.lower().endswith(".csv"):
        import csv
        return list(csv.DictReader(open(path, encoding="utf-8-sig", newline="")))
    d = json.load(open(path, encoding="utf-8"))
    return d.get("row") or d.get("I2715", {}).get("row") or (d if isinstance(d, list) else [])


def snapshot_path():
    """제품 목록 기본 사본: MFDS_I2715_FILE → data/mfds/I2715-*.csv(사용자 내려받기) → data/mfds/snapshot/i2715_*.json → 옛 i2715*.json."""
    fixed = os.environ.get("MFDS_I2715_FILE")
    if fixed:
        return fixed if os.path.isabs(fixed) else os.path.join(ROOT, fixed)
    for pat in (("data", "mfds", "I2715-*.csv"), ("data", "mfds", "snapshot", "i2715_*.json"), ("data", "mfds", "i2715*.json")):
        files = sorted(glob.glob(os.path.join(ROOT, *pat)))
        if files:
            return files[-1]
    return None


def _rows_offline():
    p = snapshot_path()
    return (_load_file(p), os.path.basename(p)) if p else (None, None)


def _query_api(key, name):
    q = urllib.parse.quote(name)
    url = f"https://openapi.foodsafetykorea.go.kr/api/{key}/I2715/json/1/100/PRDT_NM={q}"
    with urllib.request.urlopen(url, timeout=20) as r:
        body = json.load(r).get("I2715", {})
    if body.get("RESULT", {}).get("CODE") not in (None, "INFO-000", "INFO-200"):
        raise RuntimeError(f"I2715 {body.get('RESULT')}")
    rows = body.get("row") or []
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"query": name, "rows": rows}, ensure_ascii=False) + "\n")
    return rows


def lookup(title, brand="", barcode=""):
    """-> {status: match|similar|none|unchecked, rows: [...], source, reason}"""
    # 기본은 고정 사본(영상·평가 재현). MFDS_LIVE=1 이고 키가 있을 때만 실시간 API 조회
    key = os.environ.get("MFDS_API_KEY") if os.environ.get("MFDS_LIVE") == "1" else None
    words = [w for w in _n(title).split() if len(w) >= 3]
    try:
        rows, fname = (None, None) if key else _rows_offline()
        if key or rows is None and os.environ.get("MFDS_API_KEY"):
            key = key or os.environ.get("MFDS_API_KEY")
            name = " ".join(words[:3]) or title
            rows, source = _query_api(key, name), "I2715 Open API(실시간)"
        else:
            source = f"I2715 사본 {fname}"
            if rows is None:
                return {"status": "unchecked", "rows": [], "source": None,
                        "reason": "I2715 키(MFDS_API_KEY)·사본이 없어 제품 차단 이력을 확인하지 못함"}
    except Exception as e:  # noqa: BLE001
        return {"status": "unchecked", "rows": [], "source": "I2715", "reason": f"조회 실패: {type(e).__name__}: {e}"}
    hits = []
    nt, nb = _n(title), _n(brand)
    bt = [w for w in nb.split() if len(w) >= 3][:2]          # 브랜드 단어
    for r in rows:
        pn = _n(r.get("PRDT_NM"))
        mf = _n(r.get("MUFC_NM"))
        bc = re.sub(r"\D", "", str(r.get("BARCD_CTN") or ""))
        if barcode and len(bc) >= 8 and re.sub(r"\D", "", barcode) in bc:
            hits.append({**r, "_match": "barcode"})
            continue
        # 이름 일치는 엄격하게: I2715 제품명이 2단어·8자 이상이고 서로 포함, 그리고 브랜드가 그 행의 제품명이나 제조사에 있어야 한다
        # (제품명이 'muscle', 'yohimbe' 처럼 짧은 행이 아무 제목에나 걸리던 오탐을 막음 — 19:57 확인)
        brand_ok = bool(bt) and any(b in pn or b in mf for b in bt)
        long_name = len(pn) >= 8 and len(pn.split()) >= 2
        if long_name and (pn in nt or nt in pn) and (brand_ok or pn == nt):
            hits.append({**r, "_match": "name"})
        elif long_name and difflib.SequenceMatcher(None, pn, nt).ratio() >= 0.85:
            hits.append({**r, "_match": "similar"})
    strong = [h for h in hits if h["_match"] in ("barcode", "name")]
    return {"status": "match" if strong else "similar" if hits else "none", "rows": strong or hits, "source": source,
            "reason": (f"I2715 차단 행 {strong[0].get('SELF_IMPORT_SEQ')} ({strong[0].get('PRDT_NM')}, 차단 시작 {strong[0].get('STT_YMD')})"
                       if strong else "비슷한 제품명 행만 있음(참고)" if hits else "이 제품명으로 차단된 이력 없음")}
