"""국내 판매 가격 조회 (교체 가능 인터페이스).

    s = get_price_search()
    r = s.search("NOW Foods Vitamin C 1000mg 100정")   # -> {"source", "query", "items": [{title, mall, price, link, image}]}

NaverShoppingSearch  네이버 쇼핑 검색 API(공식). NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 이 .env 에 있을 때.
                     응답은 data/samples/market/recorded/ 에 녹화해 두어 키 없이 재현할 수 있게 한다.
RecordedSearch       키가 없을 때: 녹화된 응답 → 없으면 합성 사본(data/samples/market/*.json, scripts/make_samples.py).
                     합성 사본은 실제 판매 데이터가 아니다(응답 source 에 표시).
"""
import hashlib
import json
import os
import re
import urllib.parse
import urllib.request

from .mfds import norm

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MARKET_DIR = os.path.join(ROOT, "data", "samples", "market")
REC_DIR = os.path.join(MARKET_DIR, "recorded")


def _key(query):
    return hashlib.sha1(norm(query).encode()).hexdigest()[:16]


def _clean(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


class NaverShoppingSearch:
    name = "naver-shopping"

    def __init__(self):
        self.cid, self.secret = os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")
        if not (self.cid and self.secret):
            raise RuntimeError("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET not set")

    def search(self, query, n=20):
        url = ("https://openapi.naver.com/v1/search/shop.json?"
               + urllib.parse.urlencode({"query": query, "display": n, "sort": "sim"}))
        req = urllib.request.Request(url, headers={"X-Naver-Client-Id": self.cid, "X-Naver-Client-Secret": self.secret})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = json.load(r)
        items = [{"title": _clean(x.get("title")), "mall": x.get("mallName"), "price": int(x.get("lprice") or 0),
                  "link": x.get("link"), "image": x.get("image"), "brand": x.get("brand"), "maker": x.get("maker")}
                 for x in raw.get("items", [])]
        out = {"source": self.name, "query": query, "items": items}
        os.makedirs(REC_DIR, exist_ok=True)       # 키 없이 재현할 수 있게 녹화
        json.dump(out, open(os.path.join(REC_DIR, _key(query) + ".json"), "w", encoding="utf-8"), ensure_ascii=False)
        return out


class RecordedSearch:
    name = "recorded"

    def search(self, query, n=20):
        p = os.path.join(REC_DIR, _key(query) + ".json")
        if os.path.exists(p):
            out = json.load(open(p, encoding="utf-8"))
            out["source"] = "recorded:" + out.get("source", "")
            return out
        q = norm(query)
        if os.path.isdir(MARKET_DIR):
            for f in sorted(os.listdir(MARKET_DIR)):
                if f.endswith(".json"):
                    rec = json.load(open(os.path.join(MARKET_DIR, f), encoding="utf-8"))
                    if norm(rec["match"]) in q:
                        return {"source": "synthetic(실제 판매 데이터 아님)", "query": query, "items": rec["items"][:n]}
        return {"source": "synthetic(실제 판매 데이터 아님)", "query": query, "items": []}


def get_price_search():
    try:
        return NaverShoppingSearch()
    except Exception:  # noqa: BLE001 -- 키가 없으면 저장된 응답으로
        return RecordedSearch()
