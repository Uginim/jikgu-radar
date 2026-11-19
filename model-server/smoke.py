"""스모크 테스트 3건 + 지연 측정. GPU 를 띄운 뒤 딱 한 번 돌리고 결과를 남긴다.

    python infra/smoke.py http://127.0.0.1:30001            # 결과: infra/logs/smoke-<server>-<시각>.json
    python infra/smoke.py https://xxxx.trycloudflare.com    # 공개 URL 까지 한 번에 확인

1) 짧은 예/아니오  2) 이미지 1장(웨이트에 들어 있는 assets/zoro-1.png, 없으면 생성한 단색 PNG)  3) 보기 10개
4) 문장 고쳐 쓰기(/v1/generate)  5) OCR 1건(/v1/ocr, 서버가 ZORO_OCR=1 일 때만)
각 건을 3번 보내 최소/최대 지연(클라이언트 왕복)과 서버 compute 시간을 기록한다.
"""
import base64
import json
import os
import struct
import sys
import time
import zlib
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import ZoroClient  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def _png_solid(w=64, h=64, rgb=(220, 40, 40)):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))  # noqa: E731
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def _image_b64():
    p = os.path.join(os.environ.get("ZORO_MODEL_DIR", os.path.join(HERE, "weights", "zoro1")), "assets", "zoro-1.png")
    if os.path.exists(p):
        return base64.b64encode(open(p, "rb").read()).decode(), "zoro-1.png"
    return base64.b64encode(_png_solid()).decode(), "solid-red-64.png"


INTENTS = ["refund", "exchange", "delivery_status", "cancel_order", "change_address", "payment_failed",
           "coupon", "membership", "product_question", "complaint"]


def cases():
    img, img_name = _image_b64()
    return [
        ("short_noul", {"state": "Customer: I want to cancel my order and get my money back.",
                        "questions": {"refund": {"type": "noul", "instructions": "Is the customer asking for a refund?"}}}),
        (f"image_1 ({img_name})", {"state": "An image is attached.", "image": img,
                                   "questions": {"kind": {"type": "choice", "instructions": "What does the image mainly show?",
                                                          "criteria": {"logo_or_text": "a logo, text or graphic",
                                                                       "photo": "a photograph of a real scene",
                                                                       "solid_color": "a single flat color"}}}}),
        ("choice_10", {"state": "Customer: The package says delivered but I never got it. Where is it?",
                       "questions": {"intent": {"type": "choice", "instructions": "Which intent best fits the message?",
                                                "criteria": {k: k.replace("_", " ") for k in INTENTS}}}}),
    ]


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ZORO_URL", "http://127.0.0.1:30001")
    reps = int(os.environ.get("SMOKE_REPS", "3"))
    z = ZoroClient(urls=url, timeout=120, retries=2)
    t = time.perf_counter()
    health = z.health()
    report = {"url": url, "at": datetime.now().isoformat(timespec="seconds"), "health": health,
              "health_ms": round((time.perf_counter() - t) * 1000), "cases": []}
    print(f"health: {health}")
    todo = [(n, "/v1/systemone", r) for n, r in cases()]
    todo.append(("generate_rewrite", "/v1/generate", {"max_new_tokens": 80, "prompt":
                 "Rewrite this Korean product sentence so it makes no disease-prevention claim, one sentence only: "
                 "'면역력 강화로 감기를 예방합니다.'"}))
    if health.get("ocr"):
        todo.append(("ocr_label", "/v1/ocr", {"image": _image_b64()[0], "max_new_tokens": 256}))
    for name, path, req in todo:
        rtt, comp, out = [], [], None
        for _ in range(reps):
            t = time.perf_counter()
            out = z.call(path, req)
            rtt.append(round((time.perf_counter() - t) * 1000))
            comp.append(out.get("timing_ms"))
        res = out.get("answers") or {k: out.get(k) for k in ("text", "tokens", "mean_token_prob", "min_token_prob")}
        report["cases"].append({"case": name, "rtt_ms": rtt, "server_timing_ms": comp, "result": res})
        print(f"{name:28s} rtt {min(rtt)}-{max(rtt)} ms  server {comp}  -> {json.dumps(res, ensure_ascii=False)[:160]}")
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    path = os.path.join(HERE, "logs", f"smoke-{health.get('server')}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    json.dump(report, open(path, "w"), ensure_ascii=False, indent=1)
    print(f"saved {path}")


if __name__ == "__main__":
    main()
