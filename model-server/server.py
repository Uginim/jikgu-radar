"""Zoro-1 판정 서버 (표준 라이브러리 HTTP + 공식 zoro_decide.Decider).

엔드포인트
  GET  /health          살아 있는지, 모델 로드 여부, 장치, 리비전, 처리 건수 (모델 잠금을 기다리지 않음)
  POST /v1/systemone    kev/System One 형식 {state, questions, image?} -> {answers, timing_ms, request_id}
  POST /v1/decide       trida-stack 형식 {request: {...}, reasoning?, temperature?} -> 같은 응답
  POST /v1/generate     Zoro-1 문장 생성(고쳐 쓰기) {messages | prompt, max_new_tokens?(<=512)} -> {text, tokens, timing_ms}
  POST /v1/ocr          GravityOCR-KO(ZORO_OCR=1 일 때) {image: base64, prompt?, max_new_tokens?} ->
                        {text, mean_token_prob, min_token_prob, tokens, timing_ms}. 토큰 확률은 "흐림 -> 재촬영" 판단용

설계
  - 모델은 한 번만 로드하고 잠금 하나로 직렬 처리한다(GPU 1장, MPS 스레드 안전성). 잠금을 QUEUE_TIMEOUT 초 안에
    못 잡으면 503 + Retry-After 로 돌려보내 클라이언트가 재시도/백업 서버로 넘어가게 한다.
  - 요청 단위 JSONL 로그(logs/requests-YYYYMMDD.jsonl): 입력 전문, 출력 전문(확률 포함), 대기/계산 지연, 오류.
    이미지는 base64 를 로그에 넣지 않고 logs/images/<sha256>.<ext> 로 따로 저장한 뒤 경로와 해시만 남긴다.
  - 표준 출력에는 한 줄 요약만 찍는다.
  - ZORO_FAKE=1 이면 모델 없이 가짜 확률을 돌려준다(배관·로그·재시도 검증용, 크레딧 0).

환경 변수는 infra/.env.example 참고.
"""
import base64
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = (os.environ.get("ZORO_MODEL_DIR") or os.path.join(HERE, "weights", "zoro1"))
REVISION = (os.environ.get("ZORO_REVISION") or "bf1b76b9b4df03b05fcbb73444e1cd4db0f566da")
HOST = (os.environ.get("ZORO_HOST") or "0.0.0.0")
PORT = int((os.environ.get("ZORO_PORT") or "30001"))
LOG_DIR = (os.environ.get("ZORO_LOG_DIR") or os.path.join(HERE, "logs"))
API_KEY = (os.environ.get("ZORO_API_KEY") or "")            # 비어 있으면 인증 없음
QUEUE_TIMEOUT = float((os.environ.get("ZORO_QUEUE_TIMEOUT") or "20"))
MAX_BODY = int((os.environ.get("ZORO_MAX_BODY") or str(8 * 1024 * 1024)))
ALLOW_REASONING = (os.environ.get("ZORO_ALLOW_REASONING") or "0") == "1"   # 생성은 수십 초라 기본 차단
REASONING_MAX = max(1, int(os.environ.get("ZORO_REASONING_MAX_TOKENS") or "256"))
FAKE = (os.environ.get("ZORO_FAKE") or "0") == "1"
SERVER_NAME = (os.environ.get("ZORO_SERVER_NAME") or "primary")
OCR_ON = (os.environ.get("ZORO_OCR") or "0") == "1"
OCR_DIR = (os.environ.get("ZORO_OCR_DIR") or os.path.join(HERE, "weights", "gravityocr-ko"))
OCR_REVISION = (os.environ.get("ZORO_OCR_REVISION") or "6c749d417879037da8a1f6af4cf9aaa7632308bc")
GEN_MAX = int((os.environ.get("ZORO_GEN_MAX_TOKENS") or "512"))
TELEMETRY_VERSION = "usage-v1"
# Fingerprint the serving source once, before accepting requests; never hash per request.
with open(__file__, "rb") as _source:
    CODE_SHA256 = hashlib.sha256(_source.read()).hexdigest()
CORS_HEADERS = ("Content-Type, Authorization, X-API-Key, X-Request-Id, "
                "X-Zoro-Owner, X-Zoro-Task, X-Zoro-Case-Id, X-Zoro-Run-Id")

STATE = {"loaded": False, "device": None, "load_s": None, "started": time.time(), "served": 0, "errors": 0,
         "busy": False, "last_error": None}
LOCK = threading.Lock()
LOG_LOCK = threading.Lock()
DECIDER = None
OCR = None
RequestError = ValueError


# ---- 모델 -------------------------------------------------------------------------------------------

class FakeDecider:
    """모델 없이 같은 응답 모양을 돌려준다. 요청 검증은 진짜 decide_request.parse 를 쓰면 좋지만, 그 파일도 없을 수
    있으므로 여기서는 최소 검증만 한다."""
    device = "fake"

    def decide(self, request, image=None, reasoning=False, temperature=None):
        t0 = time.perf_counter()
        qs = request.get("questions")
        if not isinstance(qs, dict) or not qs:
            raise RequestError("'questions' must be a non-empty object {question id: question}")
        time.sleep(float((os.environ.get("ZORO_FAKE_DELAY") or "0.05")))
        rnd = random.Random(json.dumps(request, sort_keys=True, ensure_ascii=False))
        answers = {}
        for qid, q in qs.items():
            t = q.get("type")
            if t == "noul":
                answers[qid] = {"type": "noul", "noul": round(rnd.random(), 4)}
            elif t == "choice":
                keys = list(q["criteria"])
                w = [rnd.random() for _ in keys]
                p = [x / sum(w) for x in w]
                answers[qid] = {"type": "choice", "choice": keys[p.index(max(p))], "confidence": 0.5,
                                "probabilities": {k: round(v, 4) for k, v in zip(keys, p)}}
            elif t == "score":
                n = len(q["criteria"])
                p = [1 / n] * n
                answers[qid] = {"type": "score", "score": (n - 1) / 2, "legend": q["criteria"], "confidence": 0.0,
                                "probabilities": {str(i): round(v, 4) for i, v in enumerate(p)}}
            else:
                raise RequestError(f"question {qid!r}: type must be one of noul, choice, score, got {t!r}")
        return {"answers": answers, "timing_ms": round((time.perf_counter() - t0) * 1000, 1)}


class Generator:
    """Zoro-1 의 언어 모델로 짧은 글을 쓴다(상세페이지 문장 고쳐 쓰기). 생각(think) 없이, 탐욕적 디코딩으로 재현 가능하게."""

    def __init__(self, d):
        self.d = d

    def generate(self, body, *, usage=None):
        import torch
        from torch.nn.attention import SDPBackend, sdpa_kernel
        msgs = body.get("messages") or [{"role": "user", "content": body.get("prompt")}]
        if not isinstance(msgs, list) or not all(isinstance(m, dict) and isinstance(m.get("content"), str) for m in msgs):
            raise RequestError("give 'messages' [{role, content}] or a 'prompt' string")
        n = min(int(body.get("max_new_tokens") or 256), GEN_MAX)
        t0 = time.perf_counter()
        text = self.d.tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        ids = self.d.tok(text, return_tensors="pt").input_ids.to(self.d.device)
        if usage is not None:
            usage["input_tokens"] = int(ids.shape[1])
        temp = float(body.get("temperature") or 0)
        with torch.no_grad(), sdpa_kernel([SDPBackend.FLASH_ATTENTION, SDPBackend.EFFICIENT_ATTENTION, SDPBackend.MATH]):
            out = self.d.lm.generate(input_ids=ids, attention_mask=torch.ones_like(ids), max_new_tokens=n,
                                     do_sample=temp > 0, temperature=temp or None, top_p=0.95 if temp else None,
                                     eos_token_id=self.d.stops)
        new = out[0, ids.shape[1]:]
        if usage is not None:
            usage["output_tokens"] = int(new.shape[0])
        res = self.d.tok.decode(new, skip_special_tokens=True).replace("</think>", "").replace("<think>", "").strip()
        return {"text": res, "tokens": int(new.shape[0]), "timing_ms": round((time.perf_counter() - t0) * 1000, 1)}


class OcrModel:
    """GravityOCR-KO (GLM-OCR 구조, 1.1B, MIT). transformers 기본 경로(AR)로 돌린다. 영역 하나씩 읽는 모델이라
    성분표 부분을 잘라 보내는 것이 원칙(전체 사진 한 장은 미검증)."""

    def __init__(self, path, device):
        import torch
        from transformers import AutoProcessor, GlmOcrForConditionalGeneration
        self.torch = torch
        self.proc = AutoProcessor.from_pretrained(path)
        self.model = GlmOcrForConditionalGeneration.from_pretrained(path, dtype=torch.bfloat16).to(device).eval()
        self.device = device

    def ocr(self, body, *, usage=None):
        if MODEL_DIR not in sys.path:
            sys.path.insert(0, MODEL_DIR)
        from decide_request import _load_image   # base64 / data URL -> RGB, 파일 경로는 받지 않음
        img = _load_image(body.get("image"))
        if img is None:
            raise RequestError("'image' (base64 or data URL) is required")
        n = min(int(body.get("max_new_tokens") or 768), 2048)   # 성분표 크롭 1장 = 164토큰 실측. 흐림 반복을 끊는 상한
        t0 = time.perf_counter()
        msgs = [{"role": "user", "content": [{"type": "image", "image": img},
                                             {"type": "text", "text": body.get("prompt") or "Text Recognition:"}]}]
        inputs = self.proc.apply_chat_template(msgs, add_generation_prompt=True, tokenize=True, return_dict=True,
                                               return_tensors="pt").to(self.device)
        if usage is not None:
            usage["input_tokens"] = int(inputs["input_ids"].shape[1])
        with self.torch.no_grad():
            out = self.model.generate(**inputs, max_new_tokens=n, do_sample=False, return_dict_in_generate=True,
                                      output_scores=True)
        seq = out.sequences[0, inputs["input_ids"].shape[1]:]
        if usage is not None:
            usage["output_tokens"] = int(seq.shape[0])
        probs = [float(self.torch.softmax(sc[0].float(), -1)[t]) for sc, t in zip(out.scores, seq)]
        tok = getattr(self.proc, "tokenizer", self.proc)
        return {"engine": "gravityocr-ko", "text": self.proc.decode(seq, skip_special_tokens=True),
                **lines_with_conf([tok.decode([int(t)], skip_special_tokens=True) for t in seq], probs),
                "tokens": len(probs), "mean_token_prob": round(sum(probs) / len(probs), 4) if probs else None,
                "min_token_prob": round(min(probs), 4) if probs else None,
                "timing_ms": round((time.perf_counter() - t0) * 1000, 1)}


def lines_with_conf(pieces, probs):
    """토큰 조각과 확률 -> {"lines": [{"text", "conf"(줄 평균 토큰 확률), "min"}], "mean_conf"}. 앱 OCR 인터페이스 형식."""
    lines, cur, cur_p = [], "", []
    for piece, p in zip(pieces, probs):
        parts = piece.split("\n")
        cur += parts[0]
        cur_p.append(p)
        for part in parts[1:]:
            lines.append((cur, cur_p))
            cur, cur_p = part, [p]
    lines.append((cur, cur_p))
    out = [{"text": t.strip(), "conf": round(sum(ps) / len(ps), 3), "min": round(min(ps), 3)}
           for t, ps in lines if t.strip() and ps]
    texts = [x["text"] for x in out]
    # 흐린 사진에서 GravityOCR 은 평균 확률이 높은 채로 같은 줄을 반복해 지어낸다(11/19 실측: blur12 평균 0.98, 0/11).
    # 그래서 반복 줄 비율과 최저 확률을 함께 준다. 에이전트는 이 값 + Zoro-1 판독 신뢰 판정으로 재촬영을 정한다.
    repeat = round(1 - len(set(texts)) / len(texts), 3) if texts else 0.0
    return {"lines": out, "mean_conf": round(sum(x["conf"] for x in out) / len(out), 3) if out else 0.0,
            "repeat_ratio": repeat}


class FakeAux:
    def generate(self, body, *, usage=None):
        return {"text": "[fake] " + str((body.get("messages") or [{}])[-1].get("content") or body.get("prompt")),
                "tokens": 0, "timing_ms": 1.0}

    def ocr(self, body, *, usage=None):
        if not body.get("image"):
            raise RequestError("'image' (base64 or data URL) is required")
        return {"engine": "fake", "text": "[fake] Supplement Facts\nVitamin C 1000 mg",
                **lines_with_conf(["[fake] Supplement Facts\n", "Vitamin C 1000 mg"], [0.95, 0.85]),
                "tokens": 0, "mean_token_prob": 0.9, "min_token_prob": 0.5, "timing_ms": 1.0}


GENERATOR = None


def load_model():
    global DECIDER, RequestError
    t0 = time.time()
    global GENERATOR, OCR
    if FAKE:
        DECIDER = FakeDecider()
        GENERATOR = OCR = FakeAux()
    else:
        if not os.path.exists(os.path.join(MODEL_DIR, "zoro_decide.py")):
            raise SystemExit(f"웨이트가 없습니다: {MODEL_DIR} (infra/fetch_weights.sh 먼저 실행)")
        sys.path.insert(0, MODEL_DIR)
        from decide_request import RequestError as RE   # noqa: E402  공식 코드, 웨이트 리비전과 함께 고정됨
        from safe_decider import SafeDecider           # pinned HF engine + completion-checked reasoning
        RequestError = RE
        DECIDER = SafeDecider(MODEL_DIR, device=os.environ.get("ZORO_DEVICE") or None)
        # 첫 요청이 느리지 않도록 예열(커널 컴파일·메모리 할당)
        DECIDER.decide({"state": "warm-up", "questions": {"q": {"type": "noul", "instructions": "Is this a test?"}}})
        GENERATOR = Generator(DECIDER)
        if OCR_ON:
            if not os.path.exists(os.path.join(OCR_DIR, "model.safetensors")):
                raise SystemExit(f"OCR 웨이트가 없습니다: {OCR_DIR} (infra/fetch_weights.sh 가 ZORO_OCR=1 일 때 받음)")
            OCR = OcrModel(OCR_DIR, DECIDER.device)
    STATE.update(loaded=True, device=DECIDER.device, load_s=round(time.time() - t0, 1), ocr=OCR is not None)
    print(f"[zoro] loaded device={DECIDER.device} in {STATE['load_s']}s model={MODEL_DIR} rev={REVISION[:7]}",
          flush=True)


# ---- 로그 -------------------------------------------------------------------------------------------

def _identifier(value):
    """Bound untrusted header identifiers; never copy arbitrary header text into attribution."""
    if not value:
        return None
    return re.sub(r"[^A-Za-z0-9_.:-]", "-", value[:128]).strip("-") or None


def _attribution(headers, request_id):
    owner = _identifier(headers.get("X-Zoro-Owner"))
    if owner is None:
        owner = next((name for name in ("app", "research", "infra")
                      if request_id.startswith(name + "-")), "unattributed")
    return {"owner": owner, "task": _identifier(headers.get("X-Zoro-Task")),
            "case_id": _identifier(headers.get("X-Zoro-Case-Id")),
            "run_id": _identifier(headers.get("X-Zoro-Run-Id"))}


def _image_usage(body, logged_input, usage):
    """Count existing image references only; hash-only/invalid references have unknown bytes."""
    if not isinstance(body, dict) or not isinstance(logged_input, dict):
        return
    source = body.get("request") if isinstance(body.get("request"), dict) else body
    if not isinstance(source.get("image"), str):
        return
    request = logged_input.get("request")
    image = (request if isinstance(request, dict) else logged_input).get("image")
    if not isinstance(image, dict):
        return
    if not any(key in image for key in ("sha256", "sha256_b64", "invalid_base64")):
        return
    usage["image_count"] = 1
    size = image.get("bytes")
    if type(size) is int and size >= 0:
        usage["image_bytes"] = size


def _hash_image(b64):
    return {"sha256_b64": hashlib.sha256(b64.encode()).hexdigest(), "b64_chars": len(b64), "saved": False}


def _save_image(b64):
    """base64 / data URL 이미지를 파일로 떼어 내고 {sha256, bytes, path} 를 돌려준다. 로그에는 이것만 남긴다."""
    try:
        raw = base64.b64decode(b64.split(",", 1)[-1] if b64.startswith("data:") else b64)
    except Exception:   # noqa: BLE001 -- 잘못된 이미지는 판정 단계에서 RequestError 로 다시 걸러진다
        return {"invalid_base64": True, "chars": len(b64)}
    sha = hashlib.sha256(raw).hexdigest()
    ext = "png" if raw[:4] == b"\x89PNG" else "jpg" if raw[:2] == b"\xff\xd8" else "bin"
    d = os.path.join(LOG_DIR, "images")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{sha}.{ext}")
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(raw)
    return {"sha256": sha, "bytes": len(raw), "path": os.path.relpath(path, HERE)}


def _loggable(body, keep_image=True):
    """요청 본문 전문. 이미지 base64 만 파일 참조로 바꾼다(글자 수로 자르지 않는다).
    keep_image=False(요청의 "log_image": false, 예: 국내 판매 썸네일)면 파일로 저장하지 않고 해시만 남긴다."""
    def strip(req):
        if isinstance(req, dict) and isinstance(req.get("image"), str):
            req = {**req, "image": _save_image(req["image"]) if keep_image else _hash_image(req["image"])}
        return req
    if isinstance(body, dict) and isinstance(body.get("request"), dict):
        return {**body, "request": strip(body["request"])}
    return strip(body)


def write_log(rec):
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"requests-{datetime.now().strftime('%Y%m%d')}.jsonl")
    line = json.dumps(rec, ensure_ascii=False)
    with LOG_LOCK, open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---- HTTP -------------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "zoro1-judge/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):   # 기본 접근 로그는 끄고 요약 한 줄은 직접 찍는다
        pass

    def _send(self, code, obj, extra=None):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", CORS_HEADERS)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Expose-Headers", "X-Request-Id")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        """CORS 예비 요청(크롬 확장의 service worker / content script). 204 는 본문 없이."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", CORS_HEADERS)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/health", "/healthz", "/"):
            code = 200 if STATE["loaded"] else 503
            self._send(code, {"status": "ok" if STATE["loaded"] else "loading", "server": SERVER_NAME,
                              "device": STATE["device"], "fake": FAKE, "revision": REVISION,
                              "telemetry_version": TELEMETRY_VERSION, "code_sha256": CODE_SHA256,
                              "load_s": STATE["load_s"], "ocr": STATE.get("ocr", False), "uptime_s": round(time.time() - STATE["started"]),
                              "reasoning_contract": "completion-checked-v1", "allow_reasoning": ALLOW_REASONING,
                              "reasoning_token_limit": REASONING_MAX,
                              "served": STATE["served"], "errors": STATE["errors"], "busy": STATE["busy"]})
        else:
            self._send(404, {"error": f"no route {path}"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/v1/systemone", "/v1/decide", "/v1/generate", "/v1/ocr"):
            return self._send(404, {"error": f"no route {path}"})
        rid = _identifier(self.headers.get("X-Request-Id")) or uuid.uuid4().hex[:12]
        t_start = time.perf_counter()
        rec = {"ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"), "request_id": rid,
               "server": SERVER_NAME, "route": path, "client": self.headers.get("X-Forwarded-For") or
               self.client_address[0], "device": STATE["device"], "revision": REVISION,
               "telemetry_version": TELEMETRY_VERSION, "code_sha256": CODE_SHA256, "fake": FAKE,
               "usage_source": "fake" if FAKE else "runtime",
               "attribution": _attribution(self.headers, rid), "queue_ms": None, "compute_ms": None,
               "usage": {"input_tokens": None, "output_tokens": None, "reasoning_tokens": None,
                         "image_count": None, "image_bytes": None}}
        code, out = 500, None
        try:
            if API_KEY and API_KEY not in (self.headers.get("X-API-Key"),
                                           (self.headers.get("Authorization") or "").removeprefix("Bearer ")):
                code, out = 401, {"error": "invalid or missing API key"}
                return
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                code, out = 413 if n > MAX_BODY else 400, {"error": f"body must be 1..{MAX_BODY} bytes, got {n}"}
                return
            try:
                body = json.loads(self.rfile.read(n))
            except json.JSONDecodeError as e:
                code, out = 400, {"error": f"invalid JSON: {e}"}
                return
            keep = True
            if isinstance(body, dict) and "log_image" in body:
                keep = bool(body.pop("log_image"))   # 모델 요청 검증은 모르는 키를 거부하므로 여기서 뺀다
            rec["input"] = _loggable(body, keep)
            _image_usage(body, rec["input"], rec["usage"])
            if path in ("/v1/generate", "/v1/ocr"):
                if not isinstance(body, dict):
                    code, out = 400, {"error": "body must be a JSON object"}
                    return
                if path == "/v1/ocr" and STATE["loaded"] and OCR is None:
                    code, out = 404, {"error": "OCR is off on this server (start with ZORO_OCR=1)"}
                    return
                req, reasoning, temp = body, False, None
            elif path == "/v1/decide":
                if not isinstance(body, dict) or not isinstance(body.get("request"), dict):
                    code, out = 400, {"error": "/v1/decide body must be {\"request\": {state, questions}, ...}"}
                    return
                req, reasoning, temp = body["request"], body.get("reasoning", False), body.get("temperature")
                reasoning_budget = body.get("max_reasoning_tokens")
                if reasoning_budget is None:
                    reasoning_budget = REASONING_MAX
                if type(reasoning_budget) is not int or not 1 <= reasoning_budget <= REASONING_MAX:
                    code, out = 400, {"error": f"max_reasoning_tokens must be an integer from 1 to {REASONING_MAX}"}
                    return
            else:
                req, reasoning, temp = body, False, None
            if reasoning and not ALLOW_REASONING:
                code, out = 400, {"error": "reasoning is disabled on this server (set ZORO_ALLOW_REASONING=1)"}
                return
            if not STATE["loaded"]:
                code, out = 503, {"error": "model is still loading"}
                return
            t_wait = time.perf_counter()
            acquired = LOCK.acquire(timeout=QUEUE_TIMEOUT)
            rec["queue_ms"] = round((time.perf_counter() - t_wait) * 1000, 1)
            if not acquired:
                code, out = 503, {"error": f"server busy for {QUEUE_TIMEOUT:.0f}s, retry"}
                return
            t_c = time.perf_counter()
            try:
                STATE["busy"] = True
                if path == "/v1/generate":
                    res = GENERATOR.generate(req, usage=rec["usage"])
                elif path == "/v1/ocr":
                    res = OCR.ocr(req, usage=rec["usage"])
                elif reasoning and not FAKE:
                    res = DECIDER.decide(req, reasoning=reasoning, temperature=temp,
                                         max_reasoning_tokens=reasoning_budget)
                else:
                    res = DECIDER.decide(req, reasoning=reasoning, temperature=temp)
                if path not in ("/v1/generate", "/v1/ocr") and FAKE:
                    res["answers_source"] = "direct"
                if not FAKE and isinstance(res.get("reasoning_stats"), dict):
                    count = res["reasoning_stats"].get("completion_tokens")
                    if type(count) is int and count >= 0:
                        rec["usage"]["reasoning_tokens"] = count
            finally:
                rec["compute_ms"] = round((time.perf_counter() - t_c) * 1000, 1)
                STATE["busy"] = False
                LOCK.release()
            code, out = 200, {**res, "request_id": rid, "server": SERVER_NAME}
        except RequestError as e:
            code, out = 400, {"error": str(e)}
        except Exception as e:   # noqa: BLE001 -- 서버는 살아 있어야 한다. 전문은 로그에
            rec["traceback"] = traceback.format_exc()
            code, out = 500, {"error": f"{type(e).__name__}: {e}"}
        finally:
            out = out or {"error": "unknown"}
            out.setdefault("request_id", rid)
            out.update(usage=dict(rec["usage"]), usage_source=rec["usage_source"],
                       telemetry_version=TELEMETRY_VERSION, code_sha256=CODE_SHA256)
            rec.update(status=code, output=out, total_ms=round((time.perf_counter() - t_start) * 1000, 1))
            STATE["served"] += 1
            if code >= 500:
                STATE["errors"] += 1
            try:
                write_log(rec)
            except Exception as e:   # noqa: BLE001 -- 로그 실패로 응답을 막지 않는다
                print(f"[zoro] LOG WRITE FAILED {e}", flush=True)
            extra = {"X-Request-Id": rid}
            if code == 503:
                extra["Retry-After"] = "2"
            self._send(code, out, extra)
            qn = len((rec.get("input") or {}).get("questions") or (rec.get("input") or {}).get("request", {})
                     .get("questions") or {}) if isinstance(rec.get("input"), dict) else 0
            print(f"[zoro] {rec['ts']} {rid} {path} {code} q={qn} queue={rec.get('queue_ms')}ms "
                  f"compute={rec.get('compute_ms')}ms total={rec['total_ms']}ms", flush=True)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    # 로드 중에도 /health 가 "loading" 으로 답하도록 HTTP 를 먼저 띄운다
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[zoro] listening on http://{HOST}:{PORT} (server={SERVER_NAME}, fake={FAKE})", flush=True)
    try:
        load_model()
    except BaseException as e:
        STATE["last_error"] = repr(e)
        print(f"[zoro] MODEL LOAD FAILED: {e!r}", flush=True)
        srv.shutdown()
        raise
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
