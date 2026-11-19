"""성분 하나 → 식약처 해외직구 차단 원료 판정. 에이전트(plugins/supplement.py)와 비교 실행기(research)가 같이 쓰는 순수 함수.

단계 (앞 단계가 결정하면 뒤 단계는 부르지 않는다)
  ① 부분 일치(문자열, 코드)
       preferred_exact       목록의 한글명·영문명이 라벨 글자에 단어 그대로 있음 → 차단 확정  (= Highlight This 방식, B1)
       registry_alias_exact  목록의 '기타명칭'이 단어 그대로 있음 → 차단 확정
       preferred_partial     이름 토큰 일부가 겹침 → 후보만 만든다(확정하지 않음; 같은 속 다른 종·부위 오판 방지)
  ② 사전(코드): 원료성분 API '기타명칭' 표 + PubChem 동의어 → 라벨 글자가 어떤 차단 원료의 동의어와 단어 그대로 일치 → 차단 확정 (B2)
  ③ Zoro-1: 남은 경우 후보(문자열 유사 상위 k ∪ 부분 일치 ∪ 사전 근접) + '해당 없음' 중 선택 → 확률 (B3)
       p ≥ hi → 확정(차단/해당 없음), lo ≤ p < hi → '재조회 필요', p < lo → '확인 필요'
  후보가 하나도 없으면 Zoro-1 을 부르지 않고 '해당 없음'(사유 기록).

데이터 형식
  Registry: 식품안전나라 해외직구 위해식품 차단 원료 목록 JSON({"infoList": [...]}: block_raw_irdnt_seq, raw_irdnt_nm,
            raw_irdnt_eng_nm, raw_irdnt_etc_nm, appn_dt, appn_rsn, pblanc_no, del_yn) 또는 [{id, kr, en, etc:[...]}] 또는
            샘플 [{label, names:[...]}].
  사전: {"이름": [동의어...]} 또는 research 의 pubchem-synonyms.json ({"items": {이름: {"synonyms": [...]}}}).
Zoro-1 질문 문구는 research 의 f5-requests 와 같다(비교 결과가 에이전트 동작과 같도록).
"""
import difflib
import json
import os
import re
from dataclasses import dataclass, field

LIST_PAGE = "https://www.foodsafetykorea.go.kr/portal/fooddanger/foodDirectImportBlockRawIrdnt.do"
INSTRUCTIONS = ("Which blocked ingredient from the Korean MFDS overseas direct-purchase hazard list is the same substance "
                "as the label ingredient? Count as the same substance only if the label ingredient is the candidate "
                "itself, a synonym, common or traditional name, scientific (Latin) name of the source plant when that "
                "plant is listed as the blocked item or is standardized to it, a salt or ester form, or an extract "
                "standardized to the candidate. A different compound that is only a precursor, metabolite, or "
                "similar-looking name is not the same substance. Choose none if no candidate qualifies.")
# PubChem 동의어 중 식별 코드(CAS, UNII, CHEBI ...)는 라벨에 쓰이지 않으므로 뺀다 (research f5_baselines.py 와 같은 규칙)
CODE = re.compile(r"(\d+-\d+-\d$|(?=[A-Z]*[0-9])[A-Z0-9]{10}$|(UNII|CHEBI|CHEMBL|SCHEMBL|DTXSID|DTXCID|NSC|BRN|EINECS|"
                  r"EC |HSDB|CCRIS|AKOS|ZINC|MFCD|BDBM|CAS|Q\d|SR-|NCGC|BIDD|HMS|SPECTRUM|PRESTW|LS-|AC-|AB0|FT-|KS-|"
                  r"CS-|HY-|BRD|SMR|MLS|STK|BBL|DB\d))", re.I)


def norm(s):
    return re.sub(r"[^0-9a-z가-힣]+", " ", str(s or "").lower()).strip()


def has_word(term, text):
    """term 이 text 에 단어 경계로 있는가(정규화 후, 3자 이상)."""
    t = norm(term)
    return len(t) >= 3 and re.search(r"(^| )" + re.escape(t) + r"($| )", norm(text)) is not None


# 기타명칭·사전에 섞여 있는 일반어(염·형태·부위). 이것만으로는 원료를 가리키지 않는다(예: 'HCl' 이 모든 염산염에 걸림)
GENERIC = {"hcl", "hydrochloride", "sulfate", "sulphate", "citrate", "phosphate", "extract", "powder", "salt", "root",
           "leaf", "bark", "seed", "herb", "fruit", "추출물", "분말", "등", "의 이성체", "이성체"}


def _usable(name):
    """단어 일치에 쓸 수 있는 이름인가: 정규화 4자 이상(3자 약어는 'pea protein' 같은 오탐), 일반어 아님."""
    n = norm(name)
    return len(n.replace(" ", "")) >= 4 and n not in GENERIC


def _split_names(s):
    """한 칸에 여러 이름이 든 문자열 → 이름 목록. '1,3-Dimethylamylamine, 1,3-Dimethylbutylamine(Octodrine)' 처럼
    숫자 속 쉼표는 두고 ', ' 와 ; / · 괄호에서 나눈다. 괄호 앞뒤도 각각 이름으로 쓴다."""
    parts = re.split(r",\s+|[;/·\n]|\s및\s|\(|\)", s or "")
    return [p.strip(" .,") for p in parts if _usable(p)]


def _split_etc(s):
    return _split_names(s)


# ---- 목록 ---------------------------------------------------------------------------------------
@dataclass
class Entry:
    id: str
    kr: str = ""
    en: str = ""
    etc: list = field(default_factory=list)
    appn_dt: str = ""
    basis: str = ""
    notice_no: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def label(self):
        return f"{self.en} ({self.kr})" if self.en and self.kr else self.en or self.kr or self.id

    @property
    def preferred(self):
        """한글명·영문명과 그 칸 안에 함께 적힌 이름들(쉼표·괄호로 나눔)."""
        out = []
        for x in (self.kr, self.en):
            if x:
                out += [x] + [p for p in _split_names(x) if norm(p) != norm(x)]
        return list(dict.fromkeys(out))

    @property
    def names(self):
        return self.preferred + list(self.etc)

    def card(self):
        return {"id": self.id, "label": self.label, "kr": self.kr, "en": self.en, "other_names": self.etc,
                "designated": self.appn_dt, "basis": self.basis, "notice_no": self.notice_no, "list_page": LIST_PAGE}


class Registry:
    def __init__(self, entries, source="", as_of="", is_sample=False):
        self.entries = entries
        self.by_id = {e.id: e for e in entries}
        self.source = source
        self.as_of = as_of              # 목록 기준일(파일 이름의 YYYYMMDD)
        self.is_sample = is_sample

    @classmethod
    def load(cls, path):
        d = json.load(open(path, encoding="utf-8"))
        if isinstance(d, dict) and "infoList" in d:          # 식품안전나라 목록 JSON
            es = [Entry(str(r.get("block_raw_irdnt_seq")), r.get("raw_irdnt_nm", "").strip(),
                        r.get("raw_irdnt_eng_nm", "").strip(), _split_etc(r.get("raw_irdnt_etc_nm", "")),
                        r.get("appn_dt", ""), r.get("appn_rsn", ""), r.get("pblanc_no", ""), r)
                  for r in d["infoList"] if r.get("del_yn", "N") != "Y" and r.get("appn_rels_dvs", "Y") != "N"]   # 해제(N) 제외
            m = re.search(r"(\d{4})(\d{2})(\d{2})", os.path.basename(path))
            return cls(es, f"식품안전나라 해외직구 차단 원료 {len(es)}건", f"{m[1]}-{m[2]}-{m[3]}" if m else "")
        if isinstance(d, dict) and "entries" in d:               # 고정 스냅숏(scripts/snapshot_lists.py)
            m = d.get("meta", {})
            es = [Entry(str(r["id"]), r.get("kr", ""), r.get("en", ""), list(r.get("etc", [])), r.get("appn_dt", ""),
                        r.get("basis", ""), r.get("notice_no", ""), r) for r in d["entries"]]
            return cls(es, f"{m.get('source', '스냅숏')} (고정 스냅숏 {os.path.basename(path)})", m.get("as_of", ""))
        recs = d if isinstance(d, list) else d.get("items", [])
        es = []
        for i, r in enumerate(recs):
            if "names" in r:                                   # 샘플 형식
                n = r["names"]
                es.append(Entry(r.get("id") or f"s{i}", next((x for x in n if re.search("[가-힣]", x)), ""),
                                next((x for x in n if not re.search("[가-힣]", x)), ""),
                                [x for x in n[1:] if x], raw=r))
            else:
                es.append(Entry(str(r["id"]), r.get("kr", ""), r.get("en", ""), list(r.get("etc", [])), raw=r))
        sample = any("names" in r for r in recs)
        return cls(es, f"샘플 {len(es)}건(실제 목록 아님)" if sample else os.path.basename(path), "", sample)


def _rows(obj):
    """공공데이터포털 응답에서 행 목록을 찾는다(body.items / items.item / 바로 목록 — 실제 응답 형식 미확인이라 넓게)."""
    if isinstance(obj, list):
        return obj if obj and isinstance(obj[0], dict) else []
    if isinstance(obj, dict):
        for k in ("items", "item", "body", "response", "data", "row"):
            if k in obj:
                r = _rows(obj[k])
                if r:
                    return r
    return []


def _pick(row, *must, exclude=()):
    """필드 이름을 짐작해 값 고르기(대소문자 무시): 필드명이 must 를 모두 포함하고 exclude 를 하나도 포함하지 않는 첫 칸."""
    for k, v in row.items():
        K = k.upper()
        if all(m in K for m in must) and not any(x in K for x in exclude):
            v = v.strip() if isinstance(v, str) else v
            return "" if v in (None, "None", "null", "NULL") else v     # 실제 응답은 빈 칸을 문자열 'None' 으로 준다
    return ""


def registry_from_api(key, cache_dir, today):
    """공공데이터포털 15132686 국내 반입차단 대상 원료·성분 (GET /1471000/BlockRawIrdntInfoService/getBlockRawIrdntInfo).
    전체를 받아 cache_dir/raw_api_<날짜>.json 에 원 응답 행을 저장(키는 저장 안 함). 해제일(RELS_DT)이 오늘 이전이면 뺀다.
    필드 이름은 공개 명세(Swagger) 요약 기준으로 짐작한다 — 실제 키 응답은 아직 확인 전."""
    import urllib.parse
    import urllib.request
    cache = os.path.join(cache_dir, f"raw_api_{today.replace('-', '')}.json")
    if os.path.exists(cache):
        rows = json.load(open(cache, encoding="utf-8"))
    else:
        k = key if "%" in key else urllib.parse.quote(key, safe="")     # 인코딩된 키(Encoding)·원래 키(Decoding) 둘 다
        rows, page = [], 1
        while True:
            url = ("https://apis.data.go.kr/1471000/BlockRawIrdntInfoService/getBlockRawIrdntInfo"
                   f"?serviceKey={k}&pageNo={page}&numOfRows=500&type=json")
            with urllib.request.urlopen(url, timeout=30) as r:
                body = json.load(r)
            got = _rows(body)
            rows += got
            if len(got) < 500 or page > 20:
                break
            page += 1
        if not rows:
            raise RuntimeError("15132686 응답에 행이 없음(키·형식 확인 필요)")
        os.makedirs(cache_dir, exist_ok=True)
        json.dump(rows, open(cache, "w", encoding="utf-8"), ensure_ascii=False)
    es = []
    for n, r in enumerate(rows):
        rels = str(_pick(r, "RELS", "DT") or "")
        if rels and rels.replace("-", "")[:8] <= today.replace("-", ""):
            continue                                            # 해제된 원료
        kr = _pick(r, "IRDNT", "NM", exclude=("ENG", "ETC"))
        en = _pick(r, "ENG", "NM")
        etc = _split_etc(_pick(r, "ETC") or "")
        es.append(Entry(str(_pick(r, "SEQ") or _pick(r, "ID") or f"api{n}"), kr, en, etc, str(_pick(r, "APPN", "DT") or ""),
                        str(_pick(r, "APPN", "RSN") or ""), "", r))
    # API 에는 원료 번호가 없다 → 같은 날 웹 목록 사본(block_raw_irdnt_seq)에서 한글명·영문명이 같은 행의 번호를 빌린다
    # (research 정답·근거 카드가 이 번호를 쓴다). 못 찾으면 api<n> 그대로.
    import glob as _glob
    webs = sorted(_glob.glob(os.path.join(cache_dir, "foodsafety_block_raw_irdnt_*.json")))
    if webs:
        web = {(norm(e.kr), norm(e.en)): e.id for e in Registry.load(webs[-1]).entries}
        for e in es:
            e.id = web.get((norm(e.kr), norm(e.en)), e.id)
    return Registry(es, f"공공데이터포털 15132686 원료 API {len(es)}건", today)


def load_dictionary(path):
    """사전 파일 → {정규화 동의어: 원래 이름 키}. pubchem-synonyms.json 형식이면 식별 코드를 거른다."""
    d = json.load(open(path, encoding="utf-8"))
    items = d.get("items", d) if isinstance(d, dict) else {}
    out = SynDict()
    for key, v in items.items():
        if key.startswith("_"):
            continue
        syns = v.get("synonyms", []) if isinstance(v, dict) else v
        usable = [s for s in [key] + _split_names(key) + list(syns) if not CODE.match(s) and _usable(s)]
        for s in usable:
            out.setdefault(norm(s), key)
        out.syns[key] = {norm(s) for s in usable}
    return out


class SynDict(dict):
    """{정규화 동의어: 사전 키} + .syns {사전 키: 그 키의 정규화 동의어 전부}."""
    def __init__(self):
        super().__init__()
        self.syns = {}


# ---- 단계 ---------------------------------------------------------------------------------------
def stage_partial(span, reg):
    """① 부분 일치. research 방식 이름과 같은 키로 돌려준다."""
    ex = [e.id for e in reg.entries if any(has_word(n, span) for n in e.preferred if _usable(n))]
    al = [e.id for e in reg.entries if e.id not in ex and any(has_word(n, span) for n in e.etc if _usable(n))]
    toks = {t for t in norm(span).split() if len(t) >= 4}
    pa = [e.id for e in reg.entries if e.id not in ex + al and toks & {t for n in e.names for t in norm(n).split() if len(t) >= 4}]
    return {"preferred_exact": {"candidate_ids": ex, "decision": "match" if ex else "none"},
            "registry_alias_exact": {"candidate_ids": al, "decision": "match" if al else "none"},
            "preferred_partial": {"candidate_ids": pa, "decision": "candidate" if pa else "none"}}


def stage_dictionary(span, reg, dictionaries):
    """② 사전: 라벨 글자 안에 어떤 사전 동의어가 단어 그대로 있고, 그 키가 목록 원료의 이름과 같으면 차단 확정.
    dictionaries: [(이름, {정규화 동의어: 키})]."""
    hits = []
    nspan = norm(span)
    for dname, dmap in dictionaries:
        for syn, key in dmap.items():
            if re.search(r"(^| )" + re.escape(syn) + r"($| )", nspan):
                # 사전 키 이름뿐 아니라 그 키의 동의어가 목록 원료 이름과 같아도 일치(예: PubChem '2-Aminoisoheptane'
                # 의 동의어 'DMHA' = 목록 2023070142 기타명칭 — 키 이름만 보면 놓쳐 Zoro-1 이 엉뚱한 후보를 골랐다, 20:30 실측)
                keys = {norm(key)} | {norm(x) for x in _split_names(key)} | getattr(dmap, "syns", {}).get(key, set())
                for e in reg.entries:
                    if any(norm(n) in keys for n in e.names):
                        hits.append({"id": e.id, "dictionary": dname, "synonym": syn, "key": key})
    ids = list(dict.fromkeys(h["id"] for h in hits))
    return {"candidate_ids": ids, "decision": "match" if ids else "none", "hits": hits}


def string_candidates(span, reg, k=5, min_score=0.3):
    """문자열 유사 상위 k (Zoro-1 후보용)."""
    q = norm(span)
    sc = []
    for e in reg.entries:
        s = max((difflib.SequenceMatcher(None, q, norm(n)).ratio() for n in e.names if norm(n)), default=0)
        if s >= min_score:
            sc.append((s, e.id))
    sc.sort(reverse=True)
    return [{"id": i, "score": round(s, 3)} for s, i in sc[:k]]


def zoro_request(span, reg, candidate_ids, facts=None):
    """③ Zoro-1 요청 (research f5-requests 와 같은 문구). 선택지 키 c1..cn + none."""
    state = f"Label ingredient (OCR text): {span}"
    if facts:
        state += "\nFacts from ingredient databases: " + " ".join(facts)
    crit = {f"c{n + 1}": f"The label ingredient is the same substance as {reg.by_id[i].en or reg.by_id[i].kr}."
            for n, i in enumerate(candidate_ids)}
    crit["none"] = "None of the listed candidates is the same substance as the label ingredient."
    return {"state": state, "questions": {"same": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": crit}}}


def read_zoro(response, candidate_ids):
    """Zoro-1 응답 → {후보 id 또는 'none': 확률}."""
    p = response["answers"]["same"]["probabilities"]
    out = {candidate_ids[int(k[1:]) - 1]: v for k, v in p.items() if k != "none"}
    out["none"] = p.get("none", 0.0)
    return out


def decide(probs, hi=0.85, lo=0.5):
    """확률 → decision. 차단 쪽은 후보 확률의 합으로 본다."""
    blocked = sum(v for k, v in probs.items() if k != "none")
    top = max((k for k in probs if k != "none"), key=probs.get, default=None)
    p, d = (blocked, "match") if blocked >= probs.get("none", 0) else (probs.get("none", 0), "none")
    if p >= hi:
        return {"decision": d, "entry_id": top if d == "match" else None, "p": p}
    return {"decision": "recheck" if p >= lo else "review", "entry_id": top, "p": p}


def judge_ingredient(span, reg, dictionaries=(), zoro=None, hi=0.85, lo=0.5, k=5, facts=None):
    """①→②→③. zoro: request -> response 함수(없으면 ③ 앞에서 멈추고 'undecided').
    -> {decision: match|none|recheck|review|undecided, entry_id, decided_by, stages, candidates, probs}"""
    st = {"partial": stage_partial(span, reg)}
    for m in ("preferred_exact", "registry_alias_exact"):
        if st["partial"][m]["decision"] == "match":
            i = st["partial"][m]["candidate_ids"][0]
            return {"decision": "match", "entry_id": i, "decided_by": m, "stages": st, "candidates": [i], "probs": None}
    st["dictionary"] = stage_dictionary(span, reg, dictionaries)
    if st["dictionary"]["decision"] == "match":
        i = st["dictionary"]["candidate_ids"][0]
        return {"decision": "match", "entry_id": i, "decided_by": "dictionary", "stages": st, "candidates": [i],
                "probs": None}
    cands = list(dict.fromkeys(st["partial"]["preferred_partial"]["candidate_ids"]
                               + [c["id"] for c in string_candidates(span, reg, k)]))[:max(k, 1) + 2]
    st["string_top"] = string_candidates(span, reg, k)
    if not cands:
        return {"decision": "none", "entry_id": None, "decided_by": "no_candidates", "stages": st, "candidates": [],
                "probs": None}
    if zoro is None:
        return {"decision": "undecided", "entry_id": None, "decided_by": None, "stages": st, "candidates": cands,
                "probs": None}
    req = zoro_request(span, reg, cands, facts)
    probs = read_zoro(zoro(req), cands)
    d = decide(probs, hi, lo)
    st["zoro"] = {"request": req, "probs": probs, **d}
    return {**d, "decided_by": "zoro", "stages": st, "candidates": cands, "probs": probs}
