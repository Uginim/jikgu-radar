"""모의 상점 장부 (행동 도구 A1~A5 의 대상). 실제 도입 시 스마트스토어·쿠팡 API 로 교체한다.

data/store.json (gitignore). 없으면 plugins/supplement_data/store_seed.json 으로 시작한다.
상품: {url: {title, status, purchase_orders: [{id, qty, status}], sold_orders: [{id, customer, qty}]}}
상품 status: 등록 준비 | 차단 성분을 찾지 못함 | 등록 보류 | 확인 필요 | 성분표를 읽지 못함 | 판매 중 | 판매 중지
"""
import copy
import json
import os
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PATH = os.environ.get("NEXTASK_STORE", os.path.join(ROOT, "data", "store.json"))
SEED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins", "supplement_data", "store_seed.json")
_LOCK = threading.Lock()


def _load():
    if os.path.exists(PATH):
        return json.load(open(PATH, encoding="utf-8"))
    return json.load(open(SEED, encoding="utf-8")) if os.path.exists(SEED) else {"products": {}, "notifications": [],
                                                                                  "review_queue": []}


def _save(db):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    json.dump(db, open(PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def snapshot():
    with _LOCK:
        return copy.deepcopy(_load())


def product(url):
    return snapshot()["products"].get(url)


def _mutate(fn):
    with _LOCK:
        db = _load()
        out = fn(db)
        _save(db)
        return out


def set_status(url, status, title=None):
    """등록 보류 / 판매 중지 / 검사 기록 표시. 이전 상태를 돌려줘 되돌릴 수 있게 한다."""
    def f(db):
        p = db["products"].setdefault(url, {"title": title or url, "status": "등록 준비", "purchase_orders": [],
                                            "sold_orders": []})
        prev, p["status"] = p["status"], status
        p.setdefault("history", []).append({"ts": time.time(), "from": prev, "to": status})
        return {"url": url, "from": prev, "to": status}
    return _mutate(f)


def hold_purchase_orders(url):
    """(P2) 해외 구매 보류: 주문이 들어와도 사지 않도록 열린 구매 건에 표시. 되돌릴 수 있다."""
    def f(db):
        held = []
        for po in db["products"].get(url, {}).get("purchase_orders", []):
            if po["status"] == "open":
                po["status"] = "held"
                held.append(po["id"])
        return {"url": url, "held": held}
    return _mutate(f)


def sold_orders(url):
    return (product(url) or {}).get("sold_orders", [])


def notify(message, url=None):
    """A5 셀러 알림(모의: 장부에 기록)."""
    return _mutate(lambda db: db.setdefault("notifications", []).append({"ts": time.time(), "url": url,
                                                                          "message": message}) or {"sent": True})


def enqueue_review(case_id, url, summary):
    """T7 사람 검토 대기열."""
    return _mutate(lambda db: db.setdefault("review_queue", []).append({"ts": time.time(), "case_id": case_id,
                                                                         "url": url, "summary": summary}) or
                   {"queued": True})


def send_customer_notice(url, order_id, text):
    """A4 고객 안내 발송(모의: 장부에 기록). 셀러 승인 뒤에만 부른다."""
    def f(db):
        for o in db["products"].get(url, {}).get("sold_orders", []):
            if o["id"] == order_id:
                o.setdefault("notices", []).append({"ts": time.time(), "text": text})
                return {"order": order_id, "sent": True}
        raise KeyError(order_id)
    return _mutate(f)
