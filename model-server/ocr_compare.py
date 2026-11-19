"""OCR 결정용 비교: GravityOCR-KO(서버 /v1/ocr) vs PaddleOCR PP-OCRv5(korean, en). Brev 인스턴스에서 1회 실행.
    python3 ocr_compare.py http://127.0.0.1:30001 <labels 디렉터리>
결과: logs/ocr-compare-<시각>.json + 표 출력. 채점 = 정답 성분명이 OCR 결과 어느 줄에 (거의) 그대로 있는가."""
import base64
import difflib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

from PIL import Image, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from client import ZoroClient  # noqa: E402

TRUTH = {  # label_preworkout_photo.jpg 성분표 (사람이 사진에서 옮겨 적음)
    "preworkout": ["Choline Bitartrate", "Caffeine Anhydrous", "4-Amino Methylpentane Citrate", "Tianeptine Sodium",
                   "Phenyl Piracetam", "Nefiracetam", "L-Theanine", "Phenylethylamine HCl", "Noopept", "Sunifram",
                   "Rauwolscine"],
    "fda": ["Calories", "Total Fat", "Saturated Fat", "Cholesterol", "Total Carbohydrate", "Vitamin A", "Vitamin C",
            "Vitamin D", "Calcium", "Iron"],
}
CROP = (348, 366, 1740, 2180)   # 1920px 사진 기준 성분표 상자


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def recall(text, truth):
    lines = [norm(x) for x in re.split(r"[\n|]", text) if x.strip()]
    hits = []
    for t in truth:
        nt = norm(t)
        ok = any(nt in l or difflib.SequenceMatcher(None, nt, l[:len(nt) + 4]).ratio() >= 0.85 for l in lines)
        hits.append(ok)
    return sum(hits), [t for t, h in zip(truth, hits) if not h]


def variants(d, out):
    os.makedirs(out, exist_ok=True)
    pw = Image.open(os.path.join(d, "label_preworkout_photo.jpg")).convert("RGB")
    v = {}
    full = pw.copy(); full.thumbnail((1600, 1600)); full.save(p := os.path.join(out, "pw_full.jpg"), quality=92); v["pw_full"] = (p, "preworkout")
    crop = pw.crop(CROP); crop.save(p := os.path.join(out, "pw_crop.jpg"), quality=92); v["pw_crop"] = (p, "preworkout")
    crop.filter(ImageFilter.GaussianBlur(6)).save(p := os.path.join(out, "pw_crop_blur6.jpg"), quality=92); v["pw_blur6"] = (p, "preworkout")
    crop.filter(ImageFilter.GaussianBlur(12)).save(p := os.path.join(out, "pw_crop_blur12.jpg"), quality=92); v["pw_blur12"] = (p, "preworkout")
    v["fda_clean"] = (os.path.join(d, "label_fda_clean.png"), "fda")
    return v


def main():
    url, d = sys.argv[1], sys.argv[2]
    vs = variants(d, os.path.join(HERE, "logs", "ocr-variants"))
    z = ZoroClient(urls=url, timeout=300, retries=1)
    rows = []
    for name, (path, key) in vs.items():
        b64 = base64.b64encode(open(path, "rb").read()).decode()
        t = time.perf_counter()
        r = z.ocr(b64, max_new_tokens=1536)
        hit, miss = recall(r["text"], TRUTH[key])
        rows.append({"engine": "gravityocr-ko", "variant": name, "ms": round((time.perf_counter() - t) * 1000),
                     "recall": f"{hit}/{len(TRUTH[key])}", "missed": miss, "mean_p": r.get("mean_token_prob"),
                     "min_p": r.get("min_token_prob"), "text": r["text"]})
        print(rows[-1]["engine"], name, rows[-1]["recall"], rows[-1]["ms"], "ms", r.get("mean_token_prob"), flush=True)
    py = os.path.join(HERE, ".venv-paddle", "bin", "python")
    if os.path.exists(py):
        for lang in ("en", "korean"):
            out = subprocess.run([py, os.path.join(HERE, "paddle_ocr.py"), lang] + [p for p, _ in vs.values()],
                                 capture_output=True, text=True, timeout=900)
            res = [json.loads(x) for x in out.stdout.splitlines() if x.startswith("{")]
            if not res:
                print("paddle failed:", out.stderr[-1500:])
            for (name, (path, key)), r in zip(vs.items(), res):
                hit, miss = recall(r["text"], TRUTH[key])
                rows.append({"engine": f"paddle-{lang}", "variant": name, "ms": r["ms"], "load_ms": r["load_ms"],
                             "recall": f"{hit}/{len(TRUTH[key])}", "missed": miss, "mean_p": r["mean_score"],
                             "min_p": r["min_score"], "text": r["text"]})
                print(rows[-1]["engine"], name, rows[-1]["recall"], r["ms"], "ms", r["mean_score"], flush=True)
    path = os.path.join(HERE, "logs", f"ocr-compare-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    json.dump(rows, open(path, "w"), ensure_ascii=False, indent=1)
    print("\n| engine | variant | recall | ms | mean_p | min_p | missed |\n|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['engine']} | {r['variant']} | {r['recall']} | {r['ms']} | {r['mean_p']} | {r['min_p']} | {', '.join(r['missed'])} |")
    print("saved", path)


if __name__ == "__main__":
    main()
