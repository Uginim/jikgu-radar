"""주제 플러그인 2: 이 해외 상품을 국내에서 누가 얼마에 파는가 (요구사항 v3 18:02 추가, 확장 패널 두 번째 탭).

입력: 확장이 읽은 해외 상품 {url, title, brand} + 선택: 성분 검수 케이스 id(check_case_id), 그 결론(check_verdict)
흐름: 가격 조회 도구(교체 가능)로 국내 후보 목록 → 후보마다 Zoro-1 판정 {같은 상품 / 용량·옵션만 다름 / 다른 상품}
      (영문·한글 상품명, 용량, 정수, 맛이 섞여 규칙으로 맞추기 어려운 부분) → 확신 낮은 후보는 '확인 필요'로 통계에서 뺀다
      → 같은 상품만 모아 판매자 수·최저가·가격 분포. 성분 검수에서 차단이면 "다른 셀러도 팔지만 차단 대상" 위험 신호.
"""
import re
import statistics

from ..market import get_price_search
from ..mfds import norm
from ..plugin import Item, ItemSpec, Plugin, Tool

SEARCH = get_price_search()
MAX_LISTINGS = 12


def price_search(case, item):
    i = case.data["input"]
    q = f"{i.get('brand') or ''} {i.get('title') or ''}".strip()
    r = SEARCH.search(q)
    r["_args"] = {"query": q, "engine": SEARCH.name}
    return r


def _nums(s):
    """용량·정수 같은 숫자 토큰 (1000mg, 120정/120 tablets 등)."""
    return set(re.findall(r"\d+(?:\.\d+)?", s or ""))


class SameProduct(ItemSpec):
    kind = "listing"
    theta_hi, theta_lo, max_questions = 0.80, 0.0, 0
    instructions = ("An overseas supplement product is compared with one Korean online-store listing. Listings mix "
                    "English and Korean names, dose, count, and flavor. Is the listing the same product?")
    OPTS = {"same": "Same product: same brand, same product line, same dose and same count/size.",
            "variant": "Same brand and product line, but a different size, count, flavor, or bundle.",
            "different": "A different product (different brand or a different product line)."}

    def options(self, item):
        return dict(self.OPTS)

    def labels(self, item):
        return {"same": "같은 상품", "variant": "용량·옵션만 다름", "different": "다른 상품"}

    def build_request(self, case, item):
        i = case.data["input"]
        L = item.data["listing"]
        return {"state": {"overseas_product": {"brand": i.get("brand"), "title": i.get("title"), "url": i.get("url")},
                          "korean_listing": {"title": L["title"], "mall": L.get("mall"), "brand": L.get("brand"),
                                             "price_krw": L.get("price")}},
                "questions": {"main": {"type": "choice", "instructions": self.instructions, "criteria": self.OPTS}}}

    def mock_probs(self, request):
        """합성 확률(실제 판정 아님): 숫자 토큰과 브랜드 문자열로 흉내."""
        st = request["state"]
        src, lst = st["overseas_product"], st["korean_listing"]
        brand_ok = bool(src.get("brand")) and (norm(src["brand"]) in norm(lst["title"]) or
                                              norm(src["brand"]) == norm(lst.get("brand") or ""))
        a, b = _nums(src.get("title")), _nums(lst["title"])
        if brand_ok and a and a <= b:
            p = {"same": 0.9, "variant": 0.07, "different": 0.03}
        elif brand_ok and a & b:
            p = {"same": 0.15, "variant": 0.8, "different": 0.05}
        elif brand_ok:
            p = {"same": 0.35, "variant": 0.4, "different": 0.25}     # 확신 낮음 → 확인 필요
        else:
            p = {"same": 0.03, "variant": 0.07, "different": 0.9}
        return {"main": p}


class MarketMatch(Plugin):
    name = "market_match"
    title = "국내 판매 현황"
    pre_tools = [Tool("price_search", price_search)]
    SPEC = SameProduct()

    def apply_pre_tool(self, case, tool_name, result):
        case.data["search"] = result

    def make_items(self, case):
        items = []
        for n, L in enumerate(((case.data.get("search") or {}).get("items") or [])[:MAX_LISTINGS]):
            items.append(Item(f"l{n + 1}", self.SPEC, f"{L.get('mall') or '?'} · {L['title'][:40]}", data={"listing": L}))
        return items

    def summary(self, case):
        same = [it for it in case.items if it.result == "same"]
        prices = sorted(it.data["listing"]["price"] for it in same if it.data["listing"].get("price"))
        sellers = sorted({it.data["listing"].get("mall") for it in same})
        return {"n_candidates": len(case.items), "n_same": len(same),
                "n_variant": sum(it.result == "variant" for it in case.items),
                "n_uncertain": sum(it.status == "handoff" for it in case.items),
                "sellers": sellers, "min_price": prices[0] if prices else None,
                "median_price": int(statistics.median(prices)) if prices else None,
                "max_price": prices[-1] if prices else None, "prices": prices,
                "search_source": (case.data.get("search") or {}).get("source")}

    def verdict(self, case):
        s = self.summary(case)
        check = (case.data["input"].get("check_verdict") or {}).get("key")
        if check == "reject" and s["n_same"]:
            return "risk", "위험 신호", f"국내 셀러 {len(s['sellers'])}명이 이미 팔고 있지만 식약처 차단 성분 상품입니다(판매 시 적발 위험)."
        if not s["n_same"]:
            return "none", "국내 판매 없음", f"후보 {s['n_candidates']}개 중 같은 상품 없음(확인 필요 {s['n_uncertain']}개)."
        return "competition", "경쟁 현황", (f"경쟁 {len(s['sellers'])}명, 최저가 {s['min_price']:,}원"
                                            f"(중간 {s['median_price']:,}원, 최고 {s['max_price']:,}원)")

    def generate(self, case):
        key, label, why = self.verdict(case)
        s = self.summary(case)
        L = [f"# 국내 판매 현황 — {label}", "", f"- {why}", f"- 검색 출처: {s['search_source']}",
             f"- 후보 {s['n_candidates']}개: 같은 상품 {s['n_same']}, 용량·옵션만 다름 {s['n_variant']}, 확인 필요 {s['n_uncertain']}",
             "", "| 판정 | 확률 | 판매처 | 가격 | 상품명 |", "|---|---:|---|---:|---|"]
        for it in case.items:
            top, conf = it.top()
            lab = self.SPEC.labels(it).get(it.result, "확인 필요") if it.status != "handoff" else "확인 필요"
            Ls = it.data["listing"]
            L.append(f"| {lab} | {conf:.0%} | {Ls.get('mall')} | {Ls.get('price', 0):,} | {Ls['title']} |")
        return {"title": f"국내 판매: {label}", "markdown": "\n".join(L) + "\n"}

    def product_status(self, case):
        return {"running": "조회 중", "done": "완료"}.get(case.status, case.status)


PLUGIN = MarketMatch()
