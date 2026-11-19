"""식약처 데이터 조회 (교체 가능).

BlockList  해외직구 위해식품 차단 목록(식품안전나라 Open API 서비스 I2715). 후보 추리기는 코드(문자열 유사도),
           "같은 성분인가" 최종 선택은 Zoro-1 이 한다.
AliasLookup 원료성분 다른 이름 조회 (식품안전나라 원료 API 자리).

데이터 출처 우선순위
  1) MFDS_BLOCKLIST=<json 파일>   (내려받아 둔 I2715 레코드 목록)
  2) MFDS_API_KEY 가 있으면 Open API 에서 내려받아 data/mfds/i2715.json 에 캐시
  3) 없으면 nextask/core/plugins/supplement_data/blocklist_sample.json — 구조 확인용 샘플이다. 실제 목록이 아니다.
I2715 의 필드 이름은 아직 확인하지 않았다(미확인). 그래서 레코드의 문자열 필드를 모두 이어 붙여 매칭하고,
원 레코드는 그대로 보관해 로그에 남긴다.
"""
import difflib
import json
import os
import re
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SAMPLE_DIR = os.path.join(HERE, "plugins", "supplement_data")


def norm(s):
    return re.sub(r"[^0-9a-z가-힣]+", " ", str(s).lower()).strip()


class BlockList:
    def __init__(self, path=None):
        self.source, self.records = self._load(path)
        self.index = []
        for i, r in enumerate(self.records):
            names = r.get("names") or [v for v in r.values() if isinstance(v, str)]
            self.index.append((i, [norm(n) for n in names if n]))

    def _load(self, path):
        path = path or os.environ.get("MFDS_BLOCKLIST")
        if path and os.path.exists(path):
            return f"file:{path}", json.load(open(path, encoding="utf-8"))
        key = os.environ.get("MFDS_API_KEY")
        cache = os.path.join(ROOT, "data", "mfds", "i2715.json")
        if os.path.exists(cache):
            return f"cache:{cache}", json.load(open(cache, encoding="utf-8"))
        if key:
            recs = []
            for start in range(1, 6001, 1000):
                url = f"http://openapi.foodsafetykorea.go.kr/api/{key}/I2715/json/{start}/{start + 999}"
                body = json.load(urllib.request.urlopen(url, timeout=30)).get("I2715", {})
                rows = body.get("row") or []
                recs += rows
                if len(rows) < 1000:
                    break
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            json.dump(recs, open(cache, "w", encoding="utf-8"), ensure_ascii=False)
            return "api:I2715", recs
        return "sample(실제 목록 아님)", json.load(open(os.path.join(SAMPLE_DIR, "blocklist_sample.json"), encoding="utf-8"))

    def candidates(self, name, k=3, min_score=0.35):
        """성분 이름과 비슷한 차단 목록 레코드 상위 k개 -> [{id, label, score, record}]"""
        q = norm(name)
        scored = []
        for i, names in self.index:
            s = max((max(difflib.SequenceMatcher(None, q, n).ratio(), 0.9 if q and (q in n or n in q) else 0)
                     for n in names), default=0)
            if s >= min_score:
                scored.append((s, i))
        scored.sort(reverse=True)
        out = []
        for s, i in scored[:k]:
            r = self.records[i]
            label = r.get("label") or (r.get("names") or [str(r)[:60]])[0]
            out.append({"id": f"b{i}", "label": label, "score": round(s, 3), "record": r})
        return out


class AliasLookup:
    """원료 다른 이름 조회. 지금은 샘플 표(aliases_sample.json). 실제 API 가 붙으면 lookup 만 바꾼다."""
    def __init__(self):
        self.table = json.load(open(os.path.join(SAMPLE_DIR, "aliases_sample.json"), encoding="utf-8"))
        self.source = "sample(실제 원료 API 아님)"

    def lookup(self, name):
        q = norm(name)
        for canon, alts in self.table.items():
            names = [norm(canon)] + [norm(a) for a in alts]
            if any(q == n or (len(n) > 4 and n in q) for n in names):
                return {"query": name, "canonical": canon, "aliases": alts, "source": self.source}
        return {"query": name, "canonical": None, "aliases": [], "source": self.source}
