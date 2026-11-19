"""패널 대화 문장 생성: Zoro-1 생성이 기본, 템플릿은 비상용.

쓰는 곳: ① 질문 문장(재조회 후에도 애매할 때, 캡처 요청) ② 판정 설명·근거 카드 문장 ③ (P1) 패널 자유 질문 답변
ZoroGenerator  POST {ZORO_URL}/v1/generate {"prompt", "max_new_tokens"} -> {"text"}  (판정 서버 계약, zoro-1-infra)
               mock 모드이거나 서버가 없거나 실패하면 템플릿. 생성 호출마다 지연·출처·실패 사유를 케이스 로그에 남긴다
               (생성 지연·한국어 품질 측정용).
"""
import json
import os
import time
import urllib.request

from . import caselog
from .wirelog import logged_json_request


class ZoroGenerator:
    name = "zoro-1"

    def __init__(self):
        self.url = (os.environ.get("ZORO_URLS") or os.environ.get("ZORO_URL") or "").split(",")[0].strip().rstrip("/")
        if os.environ.get("ZORO_MODE", "mock") == "mock" or not self.url:
            raise RuntimeError("ZoroGenerator needs ZORO_MODE=remote|record and ZORO_URL")

    def generate(self, prompt, max_new_tokens=200):
        body = json.dumps({"prompt": prompt, "max_new_tokens": max_new_tokens}).encode()
        req = urllib.request.Request(self.url + "/v1/generate", data=body, headers={"Content-Type": "application/json"})
        if os.environ.get("ZORO_API_KEY"):
            req.add_header("X-API-Key", os.environ["ZORO_API_KEY"])
        return logged_json_request(req, float(os.environ.get("ZORO_GEN_TIMEOUT", "20")),
                                   opener=urllib.request.urlopen)["text"].strip()


def explain(prompt, fallback, purpose="", max_new_tokens=200):
    """-> {"text", "by", "latency_ms", "error"?}. Zoro-1 생성이 안 되면 fallback(템플릿)."""
    t0 = time.perf_counter()
    try:
        text, by, err = ZoroGenerator().generate(prompt, max_new_tokens), "zoro-1", None
        if not text:
            raise ValueError("empty generation")
    except Exception as e:  # noqa: BLE001
        text, by, err = fallback, "template", f"{type(e).__name__}: {e}"
    ms = round((time.perf_counter() - t0) * 1000, 1)
    cid = getattr(caselog.CURRENT, "case_id", None)
    if cid:
        caselog.log_event(cid, "generate", purpose=purpose, by=by, latency_ms=ms, prompt=prompt, text=text, error=err)
    return {"text": text, "by": by, "latency_ms": ms, "prompt": prompt, **({"error": err} if err else {})}
