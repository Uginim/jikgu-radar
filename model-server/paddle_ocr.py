"""PaddleOCR(PP-OCRv5) 한 장 실행 -> JSON 한 줄. 별도 venv(.venv-paddle)에서 돈다(torch 환경과 분리).
    .venv-paddle/bin/python paddle_ocr.py <lang: korean|en> <image>..."""
import json
import sys
import time

lang, paths = sys.argv[1], sys.argv[2:]
t = time.perf_counter()
from paddleocr import PaddleOCR  # noqa: E402
ocr = PaddleOCR(lang=lang, use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False,
                enable_mkldnn=False)   # paddle 3.3.1 + oneDNN PIR 오류 우회(11/19 Brev 실측)
load_ms = round((time.perf_counter() - t) * 1000)
for p in paths:
    t = time.perf_counter()
    res = list(ocr.predict(p))
    texts, scores = [], []
    for r in res:
        texts += list(r["rec_texts"])
        scores += [float(s) for s in r["rec_scores"]]
    print(json.dumps({"image": p, "lang": lang, "load_ms": load_ms, "ms": round((time.perf_counter() - t) * 1000),
                      "text": "\n".join(texts), "lines": len(texts),
                      "mean_score": round(sum(scores) / len(scores), 4) if scores else None,
                      "min_score": round(min(scores), 4) if scores else None}, ensure_ascii=False), flush=True)
