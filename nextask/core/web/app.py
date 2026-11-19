"""에이전트 서버 API (크롬 확장 ↔ 여기). 계약: 메인 저장소 docs/plan/mvp-architecture.md §4.

POST /cases                 {url, title?, brand?, page_text, ingredient_html?, images?[], role} → {case_id}
                            엔진은 백그라운드에서 돈다. 진행은 SSE 로 받는다.
GET  /cases/{id}/events     SSE: step | tool_call | zoro_probs | decision | question | highlight | stats | result (처음부터 다시 보내 준다)
                            decision = 결정 노드 {보기 확률, thresholds{hi,lo}, system 1|2, next_action}, stats = 상품 1개 결정 통계
POST /cases/{id}/answer     {question_id, answer: yes|no|unknown}
POST /cases/{id}/capture    {question_id, source_url?, image?: base64|dataURL, region?: {x,y,w,h,dpr}, capture_error?}
                            또는 {question_id, source_url, page_text, ingredient_html, images} — 페이지 다시 읽기(kind=page_reread)에 대한 답
                            현재 질문 ID는 필수. 자료의 source_url이 현재 상품 URL과 다르거나 없으면 완료 근거로 쓰지 않는다.
POST /cases/{id}/chat       (P1) {message} → {answer, by, latency_ms}  근거 카드 사실만으로 답함
GET  /cases/{id}            현재 상태(판정·확률·근거 카드·강조할 성분·행동·상품 상태)
GET  /cases/{id}/report     실행 로그 Markdown
POST /alt/interpret         (P1) {query, case_id?} → {conditions, question?}  특징으로 검색: 조건 해석
POST /alt/rank              (P1) {conditions, candidates[{url,title,brand,page_text,ingredient_html}], case_id?} → {top[3], excluded}
GET  /store                 모의 상점 장부
POST /market                (P1) {url, title, brand, case_id?} → 국내 판매 현황 상태
GET  /health
사용자 무응답: ANSWER_TIMEOUT_S(기본 300초) 지나면 '모름'으로 처리 → 확인 필요.
"""
import asyncio
import base64
import hashlib
import json
import os
import threading
import time
import urllib.request
from contextlib import nullcontext

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse

from .. import caselog, entry, followup, inquiry, store
from ..engine import Engine
from ..plugins import load
from ..search_first import Batch, review as review_batch
from ..sources import choose_gallery_image, label_lines
from ..wirelog import case_scope

UPLOAD_DIR = os.path.join(caselog.LOG_DIR, "uploads")
ANSWER_TIMEOUT_S = float(os.environ.get("ANSWER_TIMEOUT_S", "300"))

app = FastAPI(title="직구레이더 agent server")
# 확장(chrome-extension://<id>)과 로컬 개발에서 부른다. 배포 시 확장 ID 로 좁힌다.
app.add_middleware(CORSMiddleware, allow_origin_regex=r"^(chrome-extension://.*|http://(127\.0\.0\.1|localhost)(:\d+)?)$",
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type"])
PLUGIN = load()
ENGINE = Engine(PLUGIN)
MARKET = None                       # P1: 처음 부를 때 만든다
CASES = {}                          # case_id -> Case
EVENTS = {}                         # case_id -> [sse event dict]
LOCKS = {}                          # case_id -> Lock (답변·캡처를 한 번에 하나씩)


# ---- SSE 이벤트: 내부 로그 사건 → 계약의 6종 ----------------------------------------------------------
def to_sse(rec):
    e = rec["event"]
    if e == "agent_stage":
        return "step", {"event": e, **{k: rec.get(k) for k in
                        ("stage", "item", "item_title", "label", "reason", "goal", "obtained", "missing",
                         "step", "budget_left", "action", "source", "status", "answer")}}
    if e == "tool_call":
        return "tool_call", {k: rec.get(k) for k in ("item", "tool", "attempt", "ok", "args", "result", "error", "latency_ms")}
    if e in ("zoro_call", "score"):
        return "zoro_probs", {k: rec.get(k) for k in ("item", "purpose", "decision_probs", "raw_probs", "labels", "source",
                                                      "latency_ms", "system", "thresholds")}
    if e == "decision" and rec.get("thresholds"):     # 결정 노드: 보기·확률·기준선·선택된 행동(next_action)
        return "decision", {k: rec.get(k) for k in ("item", "item_title", "kind", "branch", "reason", "top", "top_label",
                                                    "conf", "decision_probs", "labels", "thresholds", "system", "next_action")}
    if e == "next_action":  # '다음 행동' 결정 노드: 보기·확률·선택·남은 예산
        return "next_action", {k: rec.get(k) for k in ("item", "item_title", "policy", "options", "probs", "choice",
                                                       "choice_label", "p", "budget_left", "source", "code_selection")}
    if e == "code_selection":
        return "step", {"event": e, **{k: rec.get(k) for k in
                        ("item", "item_title", "policy", "source", "rule", "choice", "reason")}}
    if e == "decision_stats":
        return "stats", {k: rec.get(k) for k in ("decisions", "model_calls", "model_calls_system2", "reasoning_attempts",
                                               "reasoning_incomplete", "generation_calls", "ended_by", "ratio", "latency_ms", "finals")}
    if e == "ask":
        return "question", {"question_id": rec.get("qid"), "text": rec.get("question"), "kind": rec.get("kind"),
                            "item": rec.get("item"), "item_title": rec.get("item_title")}
    if e == "final":
        return "result", {"verdict": rec.get("verdict"), "total_ms": rec.get("total_ms")}
    keep = ("item", "branch", "reason", "answer", "update", "status", "result", "label", "action", "state", "items")
    return "step", {"event": e, **{k: rec.get(k) for k in keep if rec.get(k) is not None}}


def _emit(case_id, typ, data):
    EVENTS.setdefault(case_id, []).append({"type": typ, "data": data, "ts": time.time()})


def _listener(case_id):
    def fn(rec):
        typ, data = to_sse(rec)
        _emit(case_id, typ, data)
        if rec["event"] == "item_final" and rec.get("result") == "blocked":
            c = CASES.get(case_id)
            if c:
                _emit(case_id, "highlight", {"terms": PLUGIN.highlights(c)})
    return fn


def _run_bg(case_id, fn):
    """엔진 호출을 직렬화한다. fn이 False를 반환하면 낡은 작업이므로 타이머도 재설정하지 않는다."""
    def go():
        lock = LOCKS.setdefault(case_id, threading.Lock())
        with lock, case_scope(case_id):
            try:
                if fn() is False:
                    return
            except Exception as e:  # noqa: BLE001 -- 에이전트 오류는 이벤트로 알리고 사람 검토로
                _emit(case_id, "step", {"event": "error", "reason": f"{type(e).__name__}: {e}"})
                caselog.log_event(case_id, "error", error=f"{type(e).__name__}: {e}")
            c = CASES.get(case_id)
            if c and c.status == "waiting_user" and c.pending:
                _arm_timeout(case_id, c.pending[1].id)
    threading.Thread(target=go, daemon=True).start()


def _arm_timeout(case_id, qid):
    c = CASES.get(case_id)
    if not c or c.status != "waiting_user" or not c.pending or c.pending[1].id != qid:
        return
    pending = c.pending

    def expire():
        # _run_bg holds the case lock: a queued old timer cannot answer a replacement question.
        if (CASES.get(case_id) is not c or c.status != "waiting_user" or
                c.pending is not pending or c.pending[1].id != qid):
            return False
        caselog.log_event(case_id, "answer_timeout", qid=qid, timeout_s=ANSWER_TIMEOUT_S)
        ENGINE.answer(c, None)

    def fire():
        _run_bg(case_id, expire)
    t = threading.Timer(ANSWER_TIMEOUT_S, fire)
    t.daemon = True
    t.start()


# ---- 이미지 ---------------------------------------------------------------------------------------
SELLER_IMAGE_MAX_BYTES = 10 * 1024 * 1024


def save_image(img, *, seller=False):
    """base64 / data URL / http(s) URL 이미지를 logs/uploads/<sha>.<ext> 로 저장."""
    if not img:
        return None
    if seller:
        if not isinstance(img, str) or not img.startswith(("data:image/png;base64,", "data:image/jpeg;base64,")):
            raise HTTPException(400, "판매자 사진은 PNG/JPEG data URL이어야 합니다")
        if len(img.split(",", 1)[1]) > ((SELLER_IMAGE_MAX_BYTES + 2) // 3) * 4:
            raise HTTPException(413, "판매자 사진은 10 MiB 이하여야 합니다")
    try:
        if isinstance(img, str) and img.startswith(("http://", "https://")):
            req = urllib.request.Request(img, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
        else:
            raw = base64.b64decode(img.split(",", 1)[-1], validate=seller)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"invalid image: {e}") from e
    if seller:
        if len(raw) > SELLER_IMAGE_MAX_BYTES:
            raise HTTPException(413, "판매자 사진은 10 MiB 이하여야 합니다")
        signature = b"\x89PNG\r\n\x1a\n" if img.startswith("data:image/png;") else b"\xff\xd8\xff"
        if not raw.startswith(signature):
            raise HTTPException(400, "사진 형식과 파일 내용이 일치하지 않습니다")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    ext = ".jpg" if raw[:2] == b"\xff\xd8" else ".png"
    path = os.path.join(UPLOAD_DIR, hashlib.sha256(raw).hexdigest()[:16] + ext)
    with open(path, "wb") as f:
        f.write(raw)
    return path


def _case(case_id):
    c = CASES.get(case_id)
    if c is None:
        raise HTTPException(404, "no such case")
    return c


# ---- 엔드포인트 -------------------------------------------------------------------------------------
@app.get("/health")
def health():
    supported = PLUGIN.name == "supplement"
    return {"ok": True, "plugin": PLUGIN.name, "zoro_mode": ENGINE.zoro.mode,
            "capabilities": {"entry": supported, "seller_image": supported,
                             "inquiry_draft": supported, "follow_up": supported,
                             "search_first": supported and os.environ.get("NEXTASK_SEARCH_FIRST", "0") == "1",
                             "site_generic": supported and os.environ.get("NEXTASK_SITE_GENERIC", "1") != "0",
                             "ingredient_kb": supported and os.environ.get("NEXTASK_INGREDIENT_KB", "0") == "1",
                             "system2": os.environ.get("NEXTASK_SYSTEM2", "0") == "1",
                             "agent_recovery": supported and os.environ.get("NEXTASK_AGENT_RECOVERY", "0") == "1"
                                 and os.environ.get("NEXT_POLICY", "agent") == "agent" and os.environ.get("FIXED_PIPELINE") != "1",
                             "seller_image_mime": ["image/png", "image/jpeg"],
                             "seller_image_max_bytes": SELLER_IMAGE_MAX_BYTES}}


@app.post("/entry")
def entry_route(body: dict = Body(...)):
    """Return a bounded action for the extension to execute; never approve sale."""
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        raise HTTPException(400, "message 가 필요합니다")
    cid = body.get("case_id")
    c = _case(cid) if cid else None
    lock = LOCKS.setdefault(cid, threading.Lock()) if cid else nullcontext()
    audit_id = cid or "entry-" + time.strftime("%m%d-%H%M%S-") + os.urandom(4).hex()
    with lock, case_scope(audit_id):
        current_url = ((getattr(c, "data", {}) or {}).get("input") or {}).get("url")
        context = {"case_id": cid, "case_status": getattr(c, "status", None),
                   "url": body.get("url"), "has_image": body.get("has_image") is True,
                   "same_product": bool(current_url and current_url == body.get("url"))}
        result, observation = entry.choose(ENGINE, message.strip(), context)
        if result["route"] == "explain" and result["ready"]:
            answer = PLUGIN.chat(c, message.strip())
            result.update(answer=answer["text"], by=answer["by"])
        caselog.log_event(audit_id, "entry_route", message=message, context=context, result=result, **observation)
        return {**result, "audit_id": audit_id}


@app.post("/cases")
def create_case(body: dict = Body(...)):
    inp = {k: body.get(k) for k in ("url", "title", "brand", "brand_source", "barcode", "barcode_source",
                                   "page_text", "ingredient_html", "role", "image_origin")}
    inp["role"] = inp.get("role") or "seller"
    images = body.get("images") or []
    seller_image = inp.get("image_origin") == "seller"
    path = save_image(images[0], seller=True) if seller_image and images else None
    if inp.get("image_origin") not in (None, "seller"):
        raise HTTPException(400, "image_origin 은 seller 또는 생략만 가능합니다")
    if inp.get("image_origin") == "seller" and (not path or not inp.get("url")):
        raise HTTPException(400, "판매자 사진과 현재 상품 url이 필요합니다")
    if not (inp.get("page_text") or inp.get("ingredient_html") or path or inp.get("url")):
        raise HTTPException(400, "page_text, ingredient_html, images, url 중 하나는 있어야 합니다")
    case_id = time.strftime("%m%d-%H%M%S-") + hashlib.sha1(os.urandom(8)).hexdigest()[:4]
    caselog.subscribe(case_id, _listener(case_id))
    CASES[case_id] = _Pending(case_id)      # SSE·조회가 엔진이 Case 를 만들기 전에도 404 가 나지 않게

    def run():
        selected_path, acquisition = path, None
        if not seller_image and PLUGIN.name == "supplement":
            started_at = time.perf_counter()
            observations = []
            dom_lines = [{"text": text} for text in label_lines(inp.get("page_text") or "", inp.get("ingredient_html") or "")]
            import re as _re
            # 페이지 표에 기타 성분·원재료 선언이 없고 사진 후보가 있으면 사진도 읽는다(예: 한국어 영양 표 + 영문 라벨 사진).
            declared_list = any(_re.match(r"(?i)\s*(?:(?:other|main|active|inactive)\s+)?ingredients?\s*[:：]|\s*(?:기타\s*(?:성분|원료)|원재료|성분|재료)\s*[:：]", row["text"])
                                for row in dom_lines)
            if inp.get("url") and PLUGIN.LABEL.has_ingredient_span(dom_lines) and (declared_list or not images):
                observations.append({"source": "code", "candidates": [], "selected_index": None,
                                     "next_action": "compare", "reason": "DOM 성분 범위 확보 — 사진 선택 없이 먼저 대조",
                                     "model_calls": 0, "model_latency_ms": 0})
            else:
                selected_path = choose_gallery_image(
                    images, zoro=ENGINE.zoro, photo_options=PLUGIN.LABEL.PHOTO, threshold=PLUGIN.LABEL.theta_hi,
                    save_image=save_image, observe=observations.append)
            selection = observations[-1] if observations else {
                "source": "code", "candidates": [], "selected_index": None,
                "next_action": "request_evidence", "model_calls": 0, "model_latency_ms": 0}
            acquisition = {"started_at": started_at, "selection": selection}
            if selected_path:
                inp["image_origin"] = "gallery"
            inp["image_candidates_count"] = len(images)
        c = ENGINE.start(text=inp.get("title") or inp.get("url") or "", image_path=selected_path,
                         inputs=inp, case_id=case_id, acquisition=acquisition)
        CASES[case_id] = c
    _run_bg(case_id, run)
    return {"case_id": case_id}


class _Pending:
    """엔진이 Case 를 만들기 전 아주 짧은 순간의 자리표시."""
    def __init__(self, cid):
        self.id, self.status, self.pending = cid, "running", None

    def snapshot(self):
        return {"case_id": self.id, "status": "running", "items": [], "steps": []}


@app.get("/cases/{case_id}/events")
async def events(case_id: str):
    _case(case_id)

    async def gen():
        i, idle = 0, 0.0
        while True:
            evs = EVENTS.get(case_id, [])
            while i < len(evs):
                ev = evs[i]
                i += 1
                yield f"id: {i - 1}\nevent: {ev['type']}\ndata: {json.dumps(ev['data'], ensure_ascii=False, default=str)}\n\n"
                idle = 0.0
            await asyncio.sleep(0.2)
            idle += 0.2
            if idle >= 15:              # 연결 유지용 주석 줄
                yield ": keep-alive\n\n"
                idle = 0.0
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/cases/{case_id}/answer")
def answer(case_id: str, body: dict = Body(...)):
    c = _case(case_id)
    if not c.pending or body.get("question_id") != c.pending[1].id:
        raise HTTPException(409, "no such pending question")
    ans = {"yes": True, "no": False}.get(body.get("answer"))
    qid = c.pending[1].id
    def resume():
        if not c.pending or c.pending[1].id != qid:
            raise RuntimeError("질문이 바뀌어 이전 답변을 적용하지 않음")
        ENGINE.answer(c, ans)
    _run_bg(case_id, resume)
    return {"ok": True}


@app.post("/cases/{case_id}/capture")
def capture(case_id: str, body: dict = Body(...)):
    c = _case(case_id)
    if not c.pending or body.get("question_id") != c.pending[1].id or not (
            c.pending[1].kind.startswith("capture") or c.pending[1].kind == "page_reread"):
        raise HTTPException(409, "no such pending capture request")
    qid, kind = c.pending[1].id, c.pending[1].kind
    path = save_image(body.get("image")) if kind != "page_reread" else None
    def resume():
        if not c.pending or c.pending[1].id != qid:
            raise RuntimeError("질문이 바뀌어 이전 캡처를 적용하지 않음")
        if kind == "page_reread":
            c.data["input"]["page_source_url"] = body.get("source_url")
            c.data["input"].pop("images", None)
            c.data["input"]["image_candidates_count"] = len(body.get("images") or [])
            for k in ("page_text", "ingredient_html"):
                if body.get(k) is not None:
                    c.data["input"][k] = body[k]
            caselog.log_event(case_id, "capture_received", qid=qid, reread=True,
                              page_text_chars=len(body.get("page_text") or ""))
            ENGINE.answer(c, True)
            return
        c.data["capture_source_url"] = body.get("source_url")
        error = body.get("capture_error")
        if not path and error:
            c.pending[0].data["_capture_error"] = "캡처 실행 실패 — OCR 미실행: " + str(error)[:300]
        caselog.log_event(case_id, "capture_received", qid=qid, region=body.get("region"),
                          image_bytes=os.path.getsize(path) if path else 0,
                          status="provided" if path else "capture_failed" if error else "capture_missing",
                          error=str(error)[:300] if error else None)
        ENGINE.answer(c, bool(path), image_path=path)
    _run_bg(case_id, resume)
    return {"ok": True}


@app.post("/cases/{case_id}/chat")
def chat(case_id: str, body: dict = Body(...)):
    """(P1) 패널 자유 질문. 근거 카드 사실만으로 답하고, 근거 밖이면 '확인한 근거가 없습니다'."""
    c = _case(case_id)
    msg = (body.get("message") or "").strip()
    if not msg:
        raise HTTPException(400, "message 가 필요합니다")
    with LOCKS.setdefault(case_id, threading.Lock()), case_scope(case_id):
        r = PLUGIN.chat(c, msg)
        caselog.log_event(case_id, "chat", message=msg, answer=r["text"], by=r["by"], latency_ms=r["latency_ms"])
    return {"answer": r["text"], "by": r["by"], "latency_ms": r["latency_ms"]}


@app.post("/cases/{case_id}/inquiry-draft")
def inquiry_draft(case_id: str):
    """Read a settled case, return a draft to copy; never contact a manufacturer or change the case."""
    c = _case(case_id)
    if c.status != "done":
        raise HTTPException(409, "검사가 끝난 뒤 문의 초안을 만들 수 있습니다")
    lock = LOCKS.setdefault(case_id, threading.Lock())
    with lock:
        facts = inquiry.collect_facts(c.snapshot())
    if not facts["requests"]:
        raise HTTPException(409, "문의할 미확인 성분·라벨 항목이 없습니다")
    draft, audit = inquiry.make_draft(facts)
    with lock:
        current = _case(case_id)
        if current is not c or current.status != "done" or inquiry.collect_facts(current.snapshot()) != facts:
            caselog.log_event(case_id, "inquiry_draft_stale", **audit)
            raise HTTPException(409, "사건 근거가 바뀌었습니다. 새 근거로 초안을 다시 만드세요")
        caselog.log_event(case_id, "inquiry_draft", facts=facts, **audit)
    return {"case_id": case_id, **draft}


@app.post("/cases/{case_id}/follow-up")
def manufacturer_reply(case_id: str, body: dict = Body(...)):
    """Admit text/photo for the same settled case; never treat a pasted reply as a seller confirmation."""
    c = _case(case_id)
    if PLUGIN.name != "supplement" or c.status != "done":
        raise HTTPException(409, "완료된 기존 상품 사건의 미확인 항목에만 답변을 추가할 수 있습니다")
    text, image, expected = body.get("text", ""), body.get("image"), body.get("evidence_id")
    if (set(body) - {"text", "image", "evidence_id"} or not isinstance(text, str) or len(text) > 16000 or
            not isinstance(expected, str) or not expected or (not text.strip() and not image)):
        raise HTTPException(400, "현재 근거 번호와 제조사 답변 글(16,000자 이하) 또는 PNG/JPEG 사진이 필요합니다")
    text = text.strip()
    with LOCKS.setdefault(case_id, threading.Lock()):
        if _case(case_id) is not c or c.status != "done":
            raise HTTPException(409, "이미 다른 답변을 검토 중입니다")
        facts = inquiry.collect_facts(c.snapshot())
        gate = next((it for it in c.items if it.spec is PLUGIN.LABEL), None)
        if gate is None or not facts["requests"] or expected != facts["evidence_id"]:
            raise HTTPException(409, "사건 근거가 변경되었거나 남은 확인이 없습니다")
        path = save_image(image, seller=True) if image else None
        fid = followup.fingerprint(text, path)
        history = c.data.setdefault("manufacturer_replies", [])
        if any(r["id"] == fid for r in history):
            raise HTTPException(409, "같은 답변을 이미 검토했습니다")
        record = {"id": fid, "parent_evidence_id": expected, "state": "processing", "has_text": bool(text),
                  "has_photo": bool(path), "added": [], "rejudged": []}
        history.append(record)
        previous = c.verdict
        caselog.log_event(case_id, "followup_received", followup_id=fid, previous_verdict=previous,
                          evidence_id=expected, text=text, image_file=path, source="user_supplied_manufacturer_reply")
        # Rotate the evidence token but preserve all original label rows and acquisition provenance.
        c.data["label"] = {**(c.data.get("label") or {}), "supplemental_revision": fid}
        record["evidence_id"] = PLUGIN.LABEL.completion_guard(c, gate)["evidence_id"]
        c.status, c.pending, c.verdict, c.output = "running", None, None, None
        for action in c.actions:
            if action.get("state") == "pending":
                action["state"] = "superseded"

    def review():
        try:
            followup.apply(ENGINE, c, gate, record, text, path)
        except Exception as exc:
            record.update(state="failed", error=type(exc).__name__)
            c.verdict = previous if previous and previous["key"] == "blocked" else {
                "key": "review", "label": "직접 확인 필요", "why": "추가 자료를 판독하지 못했습니다. 이전 근거는 보존했습니다."}
            caselog.log_event(case_id, "followup_error", followup_id=fid, error=type(exc).__name__)
        c.status = "done"
        caselog.log_event(case_id, "final", verdict=c.verdict, followup_id=fid, followup_state=record["state"],
                          n_zoro=c.n_zoro, output=c.output, total_ms=round((time.perf_counter() - c.t0) * 1000))

    _run_bg(case_id, review)
    return {"case_id": case_id, "status": "accepted", "evidence_id": record["evidence_id"], "followup_id": fid}


@app.get("/cases/{case_id}")
def get_case(case_id: str):
    c = _case(case_id)
    # Count first: an event appended while rendering the snapshot must still be visible on reconnect.
    event_count = len(EVENTS.get(case_id, ()))
    snap = c.snapshot()
    snap["event_count"] = event_count
    replies = getattr(c, "data", {}).get("manufacturer_replies")
    if replies:
        snap["follow_up"] = dict(replies[-1])
    return snap


@app.get("/cases/{case_id}/report", response_class=PlainTextResponse)
def report(case_id: str):
    if not os.path.exists(caselog.case_path(case_id)):
        raise HTTPException(404, "no such case")
    return caselog.markdown_report(case_id)


@app.post("/alt/interpret")
def alt_interpret(body: dict = Body(...)):
    """(P1) 패널 자유 입력 → 조건 {use, form, exclude, price_max} (+ 애매하면 질문 1개)."""
    from .. import altsearch
    q = (body.get("query") or "").strip()
    if not q:
        raise HTTPException(400, "query 가 필요합니다")
    return altsearch.interpret(ENGINE, q, body.get("case_id"))


@app.post("/alt/rank")
def alt_rank(body: dict = Body(...)):
    """(P1) 확장이 읽어 온 후보 페이지들(최대 5)을 같은 검수 엔진으로 판정하고 조건 일치로 순위 → 상위 3."""
    from .. import altsearch
    cands = body.get("candidates") or []
    if not cands:
        raise HTTPException(400, "candidates 가 필요합니다")
    return altsearch.rank(ENGINE, body.get("conditions") or {}, cands, body.get("case_id"))


@app.post("/search-first/review")
def search_first_review(body: Batch):
    """Browser-supplied product evidence only. No login, retailer navigation or listing mutation."""
    if PLUGIN.name != "supplement":
        raise HTTPException(409, "이 검수 엔진은 상품 묶음을 지원하지 않습니다")
    if os.environ.get("NEXTASK_SEARCH_FIRST", "0") != "1":
        raise HTTPException(409, "묶음 검수가 꺼져 있습니다")
    lines = (json.dumps(event, ensure_ascii=False) + "\n" for event in review_batch(ENGINE, body, save_image))
    return StreamingResponse(lines, media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/store")
def get_store():
    return store.snapshot()


@app.post("/market")
def market(body: dict = Body(...)):
    """(P1) 국내에서 같은 상품을 누가 얼마에 파는가."""
    global MARKET
    MARKET = MARKET or Engine(load("market_match"))
    inp = {k: body.get(k) for k in ("url", "title", "brand")}
    chk = CASES.get(body.get("case_id") or "")
    inp["check_verdict"] = getattr(chk, "verdict", None)
    if not inp.get("title"):
        raise HTTPException(400, "title 이 필요합니다")
    c = MARKET.start(text=inp["title"], inputs=inp)
    snap = c.snapshot()
    snap["summary"] = MARKET.plugin.summary(c)
    snap["listings"] = [{**it.data["listing"], "item_id": it.id} for it in c.items]
    return snap
