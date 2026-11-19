"""Zoro-1 판정 호출: 원격 서버(HTTP) / 녹화 응답(mock) / 원격+녹화(record).

ZORO_MODE
  remote  원격 서버만 쓴다. ZORO_URL(S)는 GPU 에 띄운 판정 서버.
  record  원격 호출 결과를 recordings/zoro.jsonl 에 저장한다(데모 리허설에서 한 번 돌려 녹화).
  mock    녹화된 응답을 돌려준다. 녹화에 없는 요청은 플러그인의 mock_probs(합성 확률)로 답하고
          응답에 "synthetic": true 를 붙인다. 서버·GPU 없이 화면·로그·시나리오를 검증하는 용도.
에이전트 쪽에서 Zoro-1 을 직접 로드하지 않는다(모델은 GPU 판정 서버에서만 돈다).

재시도·백업 서버 전환은 _infra_client.py(판정 서버 저장소의 클라이언트와 같은 코드)가 맡는다.
"""
import base64
import hashlib
import json
import os
import time

from ._infra_client import ZoroClient, ZoroError, ZoroUnavailable  # noqa: F401  (재노출)

HERE = os.path.dirname(os.path.abspath(__file__))
REC_PATH = os.environ.get("ZORO_RECORDINGS", os.path.join(HERE, "recordings", "zoro.jsonl"))


class ZoroIncomplete(ZoroError):
    """HTTP 성공이어도 요청한 생각 모드의 완결을 확인하지 못한 응답."""

    def __init__(self, response, meta):
        super().__init__("생각 모드 완료 미확인: " + str(meta.get("cut") or meta.get("answers_source") or "출처 없음"))
        self.response, self.meta = response, meta


def _answer_meta(response, reasoning, synthetic=False):
    """completion-checked-v1 서버 계약. 요청 모드와 실제 답 출처를 구별한다."""
    source = response.get("answers_source")
    stats = response.get("reasoning_stats") or {}
    complete = (source == "reasoned" and not response.get("cut")
                and stats.get("finish_reason") == "stop"
                and isinstance(response.get("reasoning"), str) and bool(response["reasoning"].strip()))
    system = (2 if reasoning else 1) if synthetic else (
        2 if complete else 1 if source == "direct" or (not reasoning and source is None) else None)
    return {"requested_system": 2 if reasoning else 1, "system": system,
            "answers_source": source, "cut": response.get("cut"), "reasoning_stats": stats,
            "reasoning_ms": response.get("reasoning_ms"),
            "complete": synthetic or (complete if reasoning or source == "reasoned" else system == 1)}

def image_b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def request_key(request):
    """요청 동일성 키. 이미지는 base64 대신 내용 해시로 바꿔 계산한다."""
    r = dict(request)
    if isinstance(r.get("image"), str):
        r["image"] = hashlib.sha256(r["image"].encode()).hexdigest()
    return hashlib.sha256(json.dumps(r, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def loggable_request(request):
    """로그용 요청 전문. 이미지 base64 는 해시·길이로 대체(이미지 파일 자체는 케이스 입력에 경로로 남는다)."""
    r = dict(request)
    if isinstance(r.get("image"), str):
        r["image"] = {"sha256_b64": hashlib.sha256(r["image"].encode()).hexdigest(), "b64_chars": len(r["image"])}
    return r


class Zoro:
    def __init__(self, mode=None, mock_fn=None):
        self.mode = (mode or os.environ.get("ZORO_MODE") or "mock").lower()
        self.mock_fn = mock_fn          # request -> {qid: {option: prob}}  (플러그인 제공)
        self.client = ZoroClient() if self.mode in ("remote", "record") else None
        self.recordings = {}
        if os.path.exists(REC_PATH):
            for line in open(REC_PATH, encoding="utf-8"):
                if line.strip():
                    r = json.loads(line)
                    self.recordings[r["key"]] = r["response"]

    def decide(self, request, mock_fn=None, reasoning=False):
        """-> (response, meta). system은 실제 답 출처이며 requested_system은 요청 모드.
        S2 완결 미확인·절단·direct 복귀는 ZoroIncomplete: 호출자는 기존 S1을 유지하거나 사람에게 넘긴다.
        synthetic은 화면 시험용이며 실제 완료 증거가 아니다."""
        key = request_key({**request, "_system": 2}) if reasoning else request_key(request)
        t0 = time.perf_counter()
        if self.mode == "mock":
            if key in self.recordings:
                res, src = self.recordings[key], "recording"
            else:
                mock_fn = mock_fn or self.mock_fn
                if not mock_fn:
                    raise ZoroUnavailable("mock mode: no recording and no mock_probs")
                probs = mock_fn(request)
                if reasoning:   # 합성 System 2: 같은 판단을 조금 더 확신 있게(실제 생각 모드 결과가 아님)
                    probs = {q: {k: v * v / (sum(x * x for x in p.values()) or 1) for k, v in p.items()} for q, p in probs.items()}
                res = {"answers": {q: {"type": "choice", "choice": max(p, key=p.get),
                                       "probabilities": {k: round(v, 4) for k, v in p.items()}}
                                   for q, p in probs.items()}, "synthetic": True}
                src = "synthetic"
            meta = {"mode": self.mode, "source": src, "key": key,
                    "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    **_answer_meta(res, reasoning, synthetic=res.get("synthetic") is True)}
            if not meta["complete"]:
                raise ZoroIncomplete(res, meta)
            return res, meta
        res = (self.client.call("/v1/decide", {"request": request, "reasoning": True}) if reasoning
               else self.client.decide(request))       # 실패 시 ZoroUnavailable / ZoroError
        meta = {"mode": self.mode, "source": "remote", "key": key, "server_url": res.get("server_url"),
                "attempts": res.get("attempts"), "server_request_id": res.get("request_id"),
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                **_answer_meta(res, reasoning)}
        if self.mode == "record":
            clean = {k: v for k, v in res.items() if k not in ("attempts", "server_url")}
            os.makedirs(os.path.dirname(REC_PATH), exist_ok=True)
            with open(REC_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"key": key, "request": loggable_request(request), "response": clean},
                                   ensure_ascii=False) + "\n")
            self.recordings[key] = clean
        if not meta["complete"]:
            raise ZoroIncomplete(res, meta)
        return res, meta
