"""Zoro-1 판정 서버 클라이언트: 타임아웃, 재시도, 백업 서버 전환. 표준 라이브러리만 쓴다.

    from client import ZoroClient
    z = ZoroClient()   # ZORO_URLS="https://gpu.example,https://mac-backup.example" (앞에서부터 시도)
    res = z.decide({"state": "...", "questions": {"q": {"type": "noul", "instructions": "..."}}})
    res["answers"]["q"]["noul"], res["server_url"], res["attempts"]

규칙
  - 서버마다 최대 `retries`번, 지수 백오프(0.5s, 1s, 2s ...)로 재시도한다. 재시도 대상: 연결 실패, 타임아웃, 502/503/504.
  - 400(요청 오류)·401 은 재시도하지 않고 바로 ZoroError. 같은 요청을 다른 서버에 보내도 결과가 같기 때문.
  - 한 서버가 끝까지 실패하면 다음 URL 로 넘어간다. 모두 실패하면 ZoroUnavailable -> 에이전트는 규칙 기반 대체 경로를 탄다.
"""
import json
import os
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

RETRY_STATUS = {502, 503, 504, 520, 521, 522, 523, 524, 530}   # 5xx 게이트웨이 + Cloudflare 터널 오류


class ZoroError(Exception):
    """요청 자체가 잘못됨(400/401/404). 재시도해도 같다."""


class ZoroUnavailable(Exception):
    """모든 서버가 응답하지 않음. 호출 측은 대체 경로(규칙, 캐시, '판단 보류')로 가야 한다."""


class ZoroClient:
    def __init__(self, urls=None, api_key=None, timeout=None, retries=None, verbose=False):
        urls = urls or os.environ.get("ZORO_URLS") or os.environ.get("ZORO_URL") or "http://127.0.0.1:30001"
        self.urls = [u.strip().rstrip("/") for u in (urls.split(",") if isinstance(urls, str) else urls) if u.strip()]
        self.api_key = api_key if api_key is not None else os.environ.get("ZORO_API_KEY", "")
        self.timeout = float(timeout or os.environ.get("ZORO_TIMEOUT", "30"))
        self.retries = int(retries or os.environ.get("ZORO_RETRIES", "3"))
        self.verbose = verbose

    def _post(self, url, path, body, rid):
        headers = {"Content-Type": "application/json", "X-Request-Id": rid}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        req = Request(url + path, data=json.dumps(body, ensure_ascii=False).encode(), headers=headers, method="POST")
        with urlopen(req, timeout=self.timeout) as r:
            return json.load(r)

    def health(self, url=None):
        with urlopen((url or self.urls[0]) + "/health", timeout=min(self.timeout, 10)) as r:
            return json.load(r)

    def decide(self, request, path="/v1/systemone"):
        return self.call(path, request)

    def generate(self, prompt=None, messages=None, max_new_tokens=256):
        """Zoro-1 문장 생성(고쳐 쓰기). -> {text, tokens, timing_ms, ...}"""
        return self.call("/v1/generate", {"prompt": prompt, "messages": messages, "max_new_tokens": max_new_tokens})

    def ocr(self, image_b64, prompt="Text Recognition:", max_new_tokens=1024):
        """GravityOCR-KO. -> {text, mean_token_prob, min_token_prob, ...}. 서버가 ZORO_OCR=1 이어야 한다."""
        return self.call("/v1/ocr", {"image": image_b64, "prompt": prompt, "max_new_tokens": max_new_tokens})

    def call(self, path, request):
        rid = uuid.uuid4().hex[:12]
        attempts, last = [], None
        for url in self.urls:
            for i in range(self.retries):
                t0 = time.perf_counter()
                try:
                    out = self._post(url, path, {k: v for k, v in request.items() if v is not None}
                                     if isinstance(request, dict) else request, rid)
                    attempts.append({"url": url, "ok": True, "ms": round((time.perf_counter() - t0) * 1000)})
                    return {**out, "server_url": url, "attempts": attempts}
                except HTTPError as e:
                    try:
                        detail = json.load(e).get("error")
                    except Exception:   # noqa: BLE001
                        detail = e.reason
                    attempts.append({"url": url, "status": e.code, "error": detail})
                    if e.code not in RETRY_STATUS:
                        raise ZoroError(f"{e.code}: {detail}") from e
                    last = e
                except (URLError, TimeoutError, ConnectionError, OSError) as e:
                    attempts.append({"url": url, "error": f"{type(e).__name__}: {e}"})
                    last = e
                if self.verbose:
                    print(f"[zoro-client] retry {i + 1}/{self.retries} {url}: {attempts[-1]}")
                if i + 1 < self.retries:
                    time.sleep(0.5 * 2 ** i)
        raise ZoroUnavailable(f"all Zoro servers failed (last: {last!r}); attempts={attempts}")


if __name__ == "__main__":
    import sys
    body = json.load(sys.stdin if len(sys.argv) < 2 or sys.argv[1] == "-" else open(sys.argv[1]))
    print(json.dumps(ZoroClient(verbose=True).decide(body), ensure_ascii=False, indent=1))
