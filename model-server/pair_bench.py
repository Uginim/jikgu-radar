"""같은 상품 사진 판정 1건 지연 (11/19 18:17 요청). 두 썸네일을 좌우로 붙인 한 장 + 두 상품명.
    .venv/bin/python pair_bench.py http://127.0.0.1:30001 <이미지A> <이미지B>
썸네일은 판정에만 쓰고 저장하지 않는다(서버 로그에는 이미지 해시와 파일이 남으니 실제 운영에서는 ZORO_LOG_IMAGES=0 검토)."""
import base64
import io
import json
import os
import statistics
import sys
import time

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import ZoroClient  # noqa: E402


def side_by_side(a, b, h=448):
    ims = [Image.open(p).convert("RGB") for p in (a, b)]
    ims = [im.resize((max(1, int(im.width * h / im.height)), h)) for im in ims]
    out = Image.new("RGB", (sum(i.width for i in ims) + 16, h), "white")
    out.paste(ims[0], (0, 0)); out.paste(ims[1], (ims[0].width + 16, 0))
    buf = io.BytesIO(); out.save(buf, "JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode(), out.size


url, a, b = sys.argv[1:4]
img, size = side_by_side(a, b)
req = {"state": {"left_listing": "Modern Warrior READY Pre-Workout 60 capsules (iHerb)",
                 "right_listing": "모던워리어 레디 프리워크아웃 60캡슐 (국내 판매)",
                 "note": "Left and right halves of the image are the two listings' photos."},
       "image": img,
       "questions": {"same": {"type": "choice",
                              "instructions": "Are the two listings the same product, judging by packaging shape, logo and label design plus names?",
                              "criteria": {"same_product": "the same product",
                                           "same_brand_other_size": "same brand and line, but a different size or option",
                                           "different_product": "a different product"}}}}
z = ZoroClient(urls=url, timeout=120, retries=1)
z.decide(req)
ts, last = [], None
for _ in range(5):
    t = time.perf_counter(); last = z.decide(req); ts.append((time.perf_counter() - t) * 1000)
print(json.dumps({"pair_image_px": size, "ms_median": round(statistics.median(ts), 1), "ms_min": round(min(ts), 1),
                  "server_ms": last.get("timing_ms"), "answer": last["answers"]["same"]}, ensure_ascii=False))
