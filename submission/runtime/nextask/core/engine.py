"""에이전트 루프 엔진 (주제 무관).

케이스 = 입력(사진·텍스트) → 사전 도구(OCR 등) → 판정 항목 N개 → 결론·결과물.
항목마다:
    판정(Zoro-1 또는 규격의 score)
    ≥ θ_hi 이고 고쳐 쓸 필요 없음      → 확정
    도구 미실행                       → 도구(재시도 → 대체 경로) → 상태 갱신 → 재판정
    고쳐 쓸 필요 있음(rewrite_when)   → 고쳐 쓰기 → 재판정 (max_rewrites 소진 시 사람 인계)
    < θ_lo                           → 사람 인계
    그 밖                             → 되묻기(기대 정보량 최대) → 갱신(베이즈 | 재판정)  (질문 소진 시 사람 인계)
모델은 확률만 낸다. 질문 문장·분기·멈춤 판단은 전부 코드다 (되묻기는 결정 엔트로피를 가장 많이 줄이는 질문).

웹 화면에서 답을 기다려야 하므로 루프는 사용자 입력이 필요할 때 멈춘다. answer() 로 이어서 진행한다.
"""
import hashlib
import json
import math
import os
import time
import uuid

from . import caselog, recovery
from .zoro import Zoro, ZoroError, ZoroIncomplete, ZoroUnavailable, image_b64, loggable_request
from .wirelog import file_image, sanitize


def fixed_pipeline():
    """Comparison pipeline: always use tools; always use S2 only when NEXTASK_SYSTEM2=1.
    Ingredient rules still reach the model so the same cases can be compared."""
    return os.environ.get("FIXED_PIPELINE") == "1"


SYS_LABEL = {0: "규칙", 1: "System 1", 2: "System 2"}
NEXT_ACTION = {"auto": "확정", "system2": "→ 생각 모드(System 2) 재결정", "tool": "→ 도구로 재조회",
               "ask": "→ 사용자에게 질문", "handoff": "→ 사람 검토", "rewrite": "→ 고쳐 쓰기", "tool_failed": "→ 도구 없이 진행"}


def entropy(d):
    return -sum(v * math.log(v) for v in d.values() if v > 1e-12)


class Case:
    def __init__(self, plugin, text="", image_path=None, case_id=None):
        self.id = case_id or time.strftime("%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.plugin = plugin
        self.text = text or ""
        self.image_path = image_path
        self.image_b64 = image_b64(image_path) if image_path else None
        self.data: dict = {"agent_recovery_enabled": os.environ.get("NEXTASK_AGENT_RECOVERY", "0") == "1"
                           and not fixed_pipeline() and os.environ.get("NEXT_POLICY", "agent") == "agent"}
        self.items = []
        self.cursor = 0                 # 지금 처리 중인 항목
        self.pending = None             # (item, Question)
        self.status = "new"             # new | running | waiting_user | done
        self.steps = []
        self.output = None
        self.verdict = None
        self.stats = {}
        self.latency = {}               # system1 / system2 → [지연 ms] (결정 통계용)
        self.actions = []               # 실행·대기·거절된 행동 [{id, name, label, auto, state, args, result, reason}]
        self.n_zoro = 0
        self.t0 = time.perf_counter()

    def set_image(self, path):
        self.image_path = path
        self.image_b64 = image_b64(path) if path else None

    def snapshot(self):
        q = self.pending
        return {"case_id": self.id, "status": self.status, "plugin": self.plugin.name, "title": self.plugin.title,
                "product_status": self.plugin.product_status(self), "actions": self.actions,
                "highlights": self.plugin.highlights(self) if hasattr(self.plugin, "highlights") else [],
                "comparison": self.plugin.comparison(self) if hasattr(self.plugin, "comparison") else None,
                "extras": self.plugin.panel_extras(self) if hasattr(self.plugin, "panel_extras") else None,
                "decision_stats": getattr(self, "stats", None),
                "input": self.data.get("input"), "product": self.data.get("product_info"),
                "verdict": self.verdict, "question": q and {"id": q[1].id, "text": q[1].text, "item": q[0].title, "kind": q[1].kind},
                "items": [{"id": it.id, "title": it.title, "status": it.status, "result": it.result,
                           "result_label": it.spec.decision_labels(it).get(it.result, it.result) if it.result else None,
                           "reason": it.reason, "labels": it.spec.decision_labels(it),
                           "decision_probs": it.decision_probs(), "rewrites": it.rewrites,
                           "card": it.data.get("card"), "last_decision": it.data.get("_last_decision"),
                           "path": it.data.get("_path"),
                           "ended_by": it.data.get("ended_by"),
                           "thresholds": {"hi": it.spec.theta_hi, "lo": it.spec.theta_lo}} for it in self.items],
                "steps": self.steps, "output": self.output}


class Engine:
    def __init__(self, plugin, zoro=None):
        self.plugin = plugin
        self.zoro = zoro or Zoro()

    # ---- 진입점 -----------------------------------------------------------------------------------
    def start(self, text="", image_path=None, case_id=None, inputs=None, acquisition=None):
        """Start a case; acquisition contains server-measured gallery selection, never client assertions."""
        c = Case(self.plugin, text, image_path, case_id)
        caselog.CURRENT.case_id = c.id
        c.data["input"] = dict(inputs or {})
        if acquisition is not None:
            selection = acquisition["selection"]
            c.t0 = acquisition["started_at"]
            c.n_zoro += selection["model_calls"]
            if selection["model_calls"]:
                c.latency["gallery_selection"] = [selection["model_latency_ms"]]
            c.data["gallery_selection"] = selection
            caselog.log_event(c.id, "gallery_selection", **selection)
        caselog.log_event(c.id, "case_start", plugin=self.plugin.name, zoro_mode=self.zoro.mode, text=c.text,
                          inputs=c.data["input"], image=self._img_meta(image_path))
        c.status = "running"
        for tool in self.plugin.pre_tools:
            if not tool.when(c, None):
                continue
            c.data["_pre_tools_run"] = c.data.get("_pre_tools_run", 0) + 1
            result = self._run_tool(c, None, tool)
            if result is not None:
                self.plugin.apply_pre_tool(c, tool.name, result)
        c.items = self.plugin.make_items(c)
        caselog.log_event(c.id, "items", items=[{"id": it.id, "kind": it.spec.kind, "title": it.title, "data": it.data,
                                                 "status": it.status, "reason": it.reason} for it in c.items])
        c.steps.append({"kind": "items", "label": f"판정 항목 {len(c.items)}개", "items": [it.title for it in c.items]})
        return self._run(c)

    def answer(self, c, ans, image_path=None):
        """ans: True/False/None(모름). image_path: 재촬영 사진(선택)."""
        if c.status != "waiting_user" or not c.pending:
            return c
        caselog.CURRENT.case_id = c.id
        it, q = c.pending
        c.pending = None
        if image_path:
            c.set_image(image_path)
            caselog.log_event(c.id, "image_update", item=it.id, image=self._img_meta(image_path))
        it.asked.append((q.id, ans))
        label = {True: "예", False: "아니오", None: "모름"}[ans]
        if recovery.active(c, it) and q.kind in ("label_confirm", "yesno"):
            recovery.emit(c, it, "seller", "판매자의 실제 확인 답변", answer=label)
        update, rejudge = "none(모름)", False
        if q.on_answer:
            r = q.on_answer(self, c, it, ans)
            update = f"on_answer:{r}"
            if r == "handoff":
                self._log_answer(c, it, q, label, update)
                it.status = "open"
                self._close(c, it, "handoff", it.data.pop("handoff_reason", None)
                            or f"사용자 요청({q.id})에 '{label}' → 사람 검토로 인계")
                c.status = "running"
                return self._run(c)
            rejudge = r == "rejudge"
        elif ans is not None and q.predicate:
            update = "bayes"
            it.probs = self._bayes(it, q, ans)
        elif ans is not None:
            update, rejudge = "rejudge", True
            it.facts.append(q.fact_yes if ans else q.fact_no)
        self._log_answer(c, it, q, label, update)
        it.status = "open"
        c.status = "running"
        if rejudge and not self._judge(c, it, f"재판정(답: {q.id})"):
            return self._run(c)
        return self._run(c)

    # ---- 루프 -------------------------------------------------------------------------------------
    def _run(self, c):
        while c.cursor < len(c.items):
            it = c.items[c.cursor]
            if it.status == "open":
                if it.probs is None and not self._judge(c, it, "초기 판정"):
                    pass  # _judge 가 인계 처리
                if it.status == "open":
                    self._item_loop(c, it)
            if it.status == "waiting_user":
                c.status = "waiting_user"
                return c
            c.cursor += 1
        return self._finish(c)

    def _item_loop(self, c, it):
        S = it.spec
        reasoning_enabled = os.environ.get("NEXTASK_SYSTEM2", "0") == "1"
        while it.status == "open":
            if hasattr(S, "resolve_official") and S.resolve_official(self, c, it):
                return
            top, conf = it.top()
            need_rw = S.rewrite_when(it)
            if S.agentic_next:
                action = self._choose_next(c, it, code_only=True)
                if action == "continue":
                    continue
                if action in ("wait", "done"):
                    return
            if fixed_pipeline() and S.use_zoro and S.theta_hi > 0 and it.n_zoro:
                if reasoning_enabled and it.data.get("_s2_state") is None and S.system2:
                    self._decide(c, it, "system2", f"고정 파이프라인: '{S.decision_labels(it).get(top, top)}' {conf:.1%} 이어도 생각 모드 재결정")
                    it.data["_s2_state"] = self._state(it)
                    if not self._judge(c, it, "System 2 재결정(고정 파이프라인)", reasoning=True):
                        return
                    continue
                fixed_tools = [t for t in S.tools if t.name not in it.tools_done and t.when(c, it)]
                if fixed_tools:                                              # 고정: 도구도 전부
                    t = fixed_tools[0]
                    self._decide(c, it, "tool", f"고정 파이프라인: 도구 `{t.name}` 항상 실행")
                    res = self._run_tool(c, it, t)
                    if res is not None and S.apply_tool(c, it, t.name, res):
                        self._judge(c, it, f"재판정(도구: {t.name}, 고정 파이프라인)")
                    continue
            if recovery.active(c, it):
                recovery.emit(c, it, "code_check", "확보한 성분과 남은 필수 근거 점검")
            if conf >= S.theta_hi and not need_rw and S.can_resolve(c, it, top):
                return self._close(c, it, "resolved", f"확신 {conf:.1%} ≥ θ_hi {S.theta_hi}" if S.theta_hi > 0
                                   else f"'{S.decision_labels(it).get(top, top)}' {conf:.1%} (분류는 묻지 않고 확률 그대로 기록)", top)
            state = self._state(it)
            if (reasoning_enabled and S.use_zoro and S.system2 and it.n_zoro and not need_rw and S.theta_lo <= conf < S.theta_hi
                    and it.data.get("_s2_state") != state):
                self._decide(c, it, "system2", f"'{S.decision_labels(it).get(top, top)}' {conf:.1%}: "
                                               f"{S.theta_lo} ≤ p < {S.theta_hi} → 생각 모드로 같은 질문 재결정")
                it.data["_s2_state"] = state
                if not self._judge(c, it, "System 2 재결정(생각 모드)", reasoning=True):
                    return
                continue
            if recovery.active(c, it):
                if recovery.advance(self, c, it) == "wait":
                    return
                continue
            if S.agentic_next:                       # 다음 행동을 Zoro-1 이 고른다(도구·질문·충분·멈춤)
                if self._choose_next(c, it) == "wait":
                    return
                continue
            pending = [t for t in S.tools if t.name not in it.tools_done and t.when(c, it)]
            if pending:
                t = pending[0]
                self._decide(c, it, "tool", f"'{S.decision_labels(it).get(top, top)}' {conf:.1%} → 도구 `{t.name}` 조회")
                res = self._run_tool(c, it, t)
                if res is None and t.on_fail == "handoff":
                    return self._close(c, it, "handoff", f"도구 `{t.name}` 실패(재시도 {t.retries}회 포함) → 사람 검토")
                if res is not None and S.apply_tool(c, it, t.name, res):
                    self._judge(c, it, f"재판정(도구: {t.name})")
                continue
            if hasattr(S, "before_question") and S.before_question(self, c, it):
                continue
            if need_rw:
                if len(it.rewrites) >= S.max_rewrites:
                    return self._close(c, it, "handoff", f"고쳐 쓰기 {len(it.rewrites)}회 후에도 기준 미달 → 사람 검토")
                self._decide(c, it, "rewrite", f"{S.decision_labels(it).get(top, top)} {conf:.1%} → 고쳐 쓰기 #{len(it.rewrites) + 1}")
                if not self._rewrite(c, it):
                    return self._close(c, it, "handoff", "고쳐 쓰기 실패 → 사람 검토")
                self._judge(c, it, f"재판정(고쳐 쓰기 #{len(it.rewrites)})")
                continue
            if conf < S.theta_lo:
                return self._close(c, it, "handoff", f"확신 {conf:.1%} < θ_lo {S.theta_lo} → 사람 인계")
            if len(it.asked) >= S.max_questions:
                return self._close(c, it, "handoff", f"질문 {len(it.asked)}개 소진, '{S.decision_labels(it).get(top, top)}' {conf:.1%} → 사람 인계")
            cap = getattr(self.plugin, "max_questions_per_case", None)
            if cap is not None and S.counts_toward_cap and sum(
                    len(x.asked) for x in c.items if x.spec.counts_toward_cap) >= cap:
                return self._close(c, it, "handoff", f"상품당 질문 한도 {cap}개 소진 → 확인 필요")
            best = self._best_question(it)
            if not best:
                return self._close(c, it, "handoff", f"더 물을 질문 없음, '{S.decision_labels(it).get(top, top)}' {conf:.1%} → 사람 인계")
            voi, q = best
            why = (f"'{S.decision_labels(it).get(top, top)}' {conf:.1%}"
                   + (" (이 결정으로는 끝낼 수 없음)" if not S.can_resolve(c, it, top) else f" < θ_hi {S.theta_hi}"))
            self._decide(c, it, "ask", f"{why} → 되묻기")
            c.pending = (it, q)
            it.status = "waiting_user"
            caselog.log_event(c.id, "asked_seller", item=it.id, qid=q.id, question=q.text, evidence_lines=it.data.get("_review_context", {}).get("text", ""))
            caselog.log_event(c.id, "ask", item=it.id, n=len(it.asked) + 1, qid=q.id, question=q.text, voi=voi,
                              kind=q.kind, item_title=it.title)
            c.steps.append({"kind": "ask", "item": it.title, "label": f"되묻기 #{len(it.asked) + 1}",
                            "question": q.text, "voi": voi})
            return

    # ---- 단계 -------------------------------------------------------------------------------------
    def _choose_next(self, c, it, code_only=False):
        """'다음에 무엇을 할까' 결정 노드. 상태(얻은 것·남은 의문·쓴 도구·남은 예산)를 주고 보기 중 하나를 고른다.
        안전장치(코드): 같은 행동 반복 금지(보기에서 뺌), 예산 초과 → 확인 필요, 차단 확정은 항상 목록 근거(여기서는 판정 안 함).
        NEXT_POLICY=fixed 면 고정 순서(비교용)."""
        S = it.spec
        used = it.data.setdefault("_actions", [])
        if len(used) >= S.next_budget:
            if code_only:
                return None
            self._close(c, it, "handoff", f"다음 행동 예산 {S.next_budget}회 소진 → 확인 필요")
            return "done"
        opts = {k: v for k, v in S.next_options(c, it).items() if k not in used}
        if not opts:
            if code_only:
                return None
            self._close(c, it, "handoff", "더 할 수 있는 행동 없음 → 확인 필요")
            return "done"
        policy = os.environ.get("NEXT_POLICY", "agent")
        t0 = time.perf_counter()
        code_selection = S.next_code_action(c, it, opts)
        if code_only and not code_selection:
            return None
        if code_selection:
            choice = code_selection["choice"]
            probs, meta, req = {}, {"source": "code", "system": 0}, None
            caselog.log_event(c.id, "code_selection", item=it.id, item_title=it.title,
                              policy=policy, source="code", **code_selection)
            c.steps.append({"kind": "code_selection", "item": it.title,
                            "label": code_selection["reason"], **code_selection})
            if recovery.active(c, it):
                recovery.emit(c, it, "code_check", code_selection["reason"], action=choice, source="code")
        elif policy == "fixed":
            choice = next((k for k in S.next_fixed_order if k in opts), next(iter(opts)))
            probs, meta, req = {k: float(k == choice) for k in opts}, {"source": "fixed-order", "system": 0}, None
        else:
            req = S.next_request(c, it, opts)
            try:
                res, meta = self.zoro.decide(req, mock_fn=S.next_mock)
                c.n_zoro += 1
                probs = res["answers"]["next"]["probabilities"]
            except (ZoroUnavailable, ZoroError) as e:
                self._close(c, it, "handoff", f"판정 서버 불가({type(e).__name__}) → 사람 인계")
                return "done"
            probs = {k: v for k, v in probs.items() if k in opts}
            choice = max(probs, key=probs.get)
        used.append(choice)
        labels = {k: v["label"] for k, v in opts.items()}
        node = {"item": it.id, "item_title": it.title, "kind": "next_action", "policy": policy, "options": labels,
                "probs": probs, "choice": choice, "choice_label": labels[choice], "p": probs.get(choice),
                "budget_left": S.next_budget - len(used), "state": (req or {}).get("state"),
                "source": meta.get("source"), "code_selection": code_selection,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
        caselog.log_event(c.id, "next_action", request=loggable_request(req) if req else None, **node)
        c.steps.append({"kind": "next_action", "item": it.title, "label": f"다음 행동: {labels[choice]}",
                        "decision_probs": probs, "labels": labels, "reason": f"{policy} · 남은 예산 {node['budget_left']}"})
        path = it.data.setdefault("_path", [])
        if not path and it.n_zoro:          # 첫 System 1 판정을 경로 맨 앞에
            top, conf = it.top()
            path.append({"kind": "s1", "label": f"System 1 {S.decision_labels(it).get(top, top)}", "p": round(conf, 2)})
        path.append({"kind": "next", "label": labels[choice],
                     "p": round(probs[choice], 2) if choice in probs else None})
        it.data["_last_decision"] = {"branch": "next_action", "next_action": f"→ {labels[choice]}",
                                    "system": meta.get("system", 1)}
        return S.next_execute(self, c, it, choice)

    @staticmethod
    def _state(it):
        """System 2 를 같은 상태에서 두 번 부르지 않게: 사실·답·후보가 바뀌면 새 상태."""
        return json.dumps([len(it.facts), len(it.asked), it.data.get("candidates")], default=str)

    def _judge(self, c, it, purpose, reasoning=False):
        S = it.spec
        if not S.use_zoro:
            it.probs = S.score(c, it)
            caselog.log_event(c.id, "score", item=it.id, purpose=purpose, raw_probs=it.probs,
                              decision_probs=it.decision_probs(), source="code")
            c.steps.append({"kind": "judge", "item": it.title, "label": purpose, "source": "code",
                            "decision_probs": it.decision_probs(), "labels": S.decision_labels(it)})
            return True
        req = S.build_request(c, it)
        try:
            res, meta = self.zoro.decide(req, mock_fn=S.mock_probs, reasoning=reasoning)
        except ZoroIncomplete as e:
            c.n_zoro += 1
            it.n_zoro += 1
            c.data["reasoning_incomplete"] = c.data.get("reasoning_incomplete", 0) + 1
            c.latency.setdefault("reasoning_attempt", []).append(e.meta.get("latency_ms") or 0)
            keep_s1 = reasoning and it.probs is not None
            reason = f"{e} → " + ("기존 판정 유지, 도구·사람 경로로" if keep_s1 else "확인 필요")
            caselog.log_event(c.id, "zoro_incomplete", item=it.id, purpose=purpose, reason=reason,
                              request=loggable_request(req), response=e.response, meta=e.meta,
                              fallback="keep_previous" if keep_s1 else "handoff")
            c.steps.append({"kind": "error", "item": it.title, "label": "생각 모드 완료 미확인", "reason": reason})
            it.data.setdefault("_path", []).append({"kind": "tool", "label": "S2 완료 미확인 · 기존 판정 유지" if keep_s1 else "추론 미확인"})
            if keep_s1:
                return True
            it.data["model_unverified"] = reason
            self._close(c, it, "handoff", reason)
            return False
        except (ZoroUnavailable, ZoroError) as e:
            caselog.log_event(c.id, "zoro_error", item=it.id, purpose=purpose, error=f"{type(e).__name__}: {e}",
                              request=loggable_request(req), fallback="handoff")
            c.steps.append({"kind": "error", "item": it.title, "label": f"판정 서버 실패 ({purpose})", "error": str(e)[:300]})
            it.data["server_error"] = f"{type(e).__name__}"
            self._close(c, it, "handoff", "판정 서버에 연결하지 못함 → 사람 인계")
            return False
        c.n_zoro += 1
        it.n_zoro += 1
        c.latency.setdefault(f"system{meta.get('system', 1)}", []).append(meta.get("latency_ms") or 0)
        if reasoning:
            c.latency.setdefault("reasoning_attempt", []).append(meta.get("latency_ms") or 0)
        it.data["_last_system"] = meta.get("system", 1)
        it.probs = S.read_probs(it, res)
        top, conf = it.top()
        caselog.log_event(c.id, "zoro_call", item=it.id, purpose=purpose, request=loggable_request(req), response=res,
                          meta=meta, raw_probs=it.probs, decision_probs=it.decision_probs(), top=top, conf=conf,
                          labels=S.decision_labels(it), source=meta.get("source"), latency_ms=meta.get("latency_ms"),
                          system=meta.get("system", 1), thresholds={"hi": S.theta_hi, "lo": S.theta_lo})
        c.steps.append({"kind": "judge", "item": it.title, "label": purpose, "decision_probs": it.decision_probs(),
                        "labels": S.decision_labels(it), "source": meta.get("source"), "system": meta.get("system", 1),
                        "latency_ms": meta.get("latency_ms")})
        if recovery.active(c, it):
            recovery.emit(c, it, "judgment", purpose, source=meta.get("source"))
        return True

    def _run_tool(self, c, it, tool):
        """-> 결과 dict, 모두 실패하면 None."""
        if it is not None:
            it.tools_done.append(tool.name)
        iid = it.id if it else None
        for attempt in range(1, tool.retries + 3):   # 본 경로 1 + 재시도 retries 회, 그다음 대체 경로 1회
            fn, name = (tool.fn, tool.name) if attempt <= tool.retries + 1 else (tool.fallback, tool.name + ":fallback")
            if fn is None:
                break
            t = time.perf_counter()
            arguments = sanitize({"case": {"id": c.id, "text": c.text, "image": file_image(c.image_path), "data": c.data},
                                  "item": None if it is None else {"id": it.id, "data": it.data, "facts": it.facts}})
            try:
                result = fn(c, it)
                ms = round((time.perf_counter() - t) * 1000, 1)
                declared = result.get("_args") or {}
                args = {**declared, **arguments} if isinstance(declared, dict) else {"declared": declared, **arguments}
                caselog.log_event(c.id, "tool_call", item=iid, tool=name, attempt=attempt, ok=True,
                                  args=args, result=result, latency_ms=ms)
                result.pop("_args", None)
                c.steps.append({"kind": "tool", "item": it.title if it else None, "label": f"도구 {name}",
                                "result": result, "latency_ms": ms})
                return result
            except Exception as e:  # noqa: BLE001 -- 도구 실패는 재시도/대체 경로로 흡수, 전문은 로그에
                ms = round((time.perf_counter() - t) * 1000, 1)
                caselog.log_event(c.id, "tool_call", item=iid, tool=name, attempt=attempt, ok=False,
                                  args=arguments, result=None, error=f"{type(e).__name__}: {e}",
                                  status=getattr(e, "code", None), attempts=getattr(e, "attempts", None), latency_ms=ms)
                c.steps.append({"kind": "tool_error", "item": it.title if it else None,
                                "label": f"도구 {name} 실패 (시도 {attempt})", "error": str(e)})
        caselog.log_event(c.id, "decision", item=iid, branch="tool_failed",
                          reason=f"도구 `{tool.name}` 재시도·대체 경로 모두 실패")
        return None

    def _rewrite(self, c, it):
        t = time.perf_counter()
        try:
            r = it.spec.rewrite(c, it)
        except Exception as e:  # noqa: BLE001
            caselog.log_event(c.id, "rewrite", item=it.id, ok=False, error=f"{type(e).__name__}: {e}")
            return False
        ms = round((time.perf_counter() - t) * 1000, 1)
        it.rewrites.append({"from": r["from"], "to": r["text"], "by": r.get("by"), "attempts": r.get("attempts")})
        caselog.log_event(c.id, "rewrite", item=it.id, ok=True, latency_ms=ms, **{k: v for k, v in r.items()})
        c.steps.append({"kind": "rewrite", "item": it.title, "label": f"고쳐 쓰기 #{len(it.rewrites)} ({r.get('by')})",
                        "from": r["from"], "to": r["text"], "latency_ms": ms})
        return True

    def _bayes(self, it, q, ans):
        e = it.spec.answer_noise
        new = {k: v * ((1 - e) if q.predicate(k) == ans else e) for k, v in it.probs.items()}
        z = sum(new.values()) or 1.0
        return {k: v / z for k, v in new.items()}

    def _best_question(self, it):
        """결정 단위 엔트로피를 가장 많이 줄이는 질문. predicate 없는 질문은 정보량을 몰라 뒤로 미룬다."""
        asked = {qid for qid, _ in it.asked}
        agg = lambda p: it.spec.aggregate(it, p)  # noqa: E731
        h0 = entropy(it.decision_probs())
        best, fallback = None, None
        for q in it.spec.questions_for(it):
            if q.id in asked:
                continue
            if q.predicate is None:
                fallback = fallback or (0.0, q)
                continue
            py = sum(v for k, v in it.probs.items() if q.predicate(k))
            if py < 1e-6 or py > 1 - 1e-6:
                continue
            eh = py * entropy(agg(self._bayes(it, q, True))) + (1 - py) * entropy(agg(self._bayes(it, q, False)))
            if best is None or h0 - eh > best[0]:
                best = (h0 - eh, q)
        return best or fallback

    def _decide(self, c, it, branch, reason):
        """결정 노드: {보기 + 보기별 확률 + 기준선 + 선택된 행동}. 로그·SSE·패널 공통 형식."""
        top, conf = it.top()
        S = it.spec
        node = {"item": it.id, "item_title": it.title, "kind": S.kind, "branch": branch, "reason": reason,
                "top": top, "top_label": S.decision_labels(it).get(top, top), "conf": conf,
                "decision_probs": it.decision_probs(), "labels": S.decision_labels(it),
                "thresholds": {"hi": S.theta_hi, "lo": S.theta_lo},
                "system": it.data.get("_last_system", 1 if it.n_zoro else 0),
                "next_action": NEXT_ACTION.get(branch, branch)}
        caselog.log_event(c.id, "decision", **node)
        c.steps.append({"kind": "decision", "item": it.title, "label": branch, "reason": reason,
                        "next_action": node["next_action"]})
        it.data["_last_decision"] = {"branch": branch, "next_action": node["next_action"], "system": node["system"]}
        # 디자인 요청 7: 항목별 지나간 경로(결정 System n → 도구 → 사람 …)
        path = it.data.setdefault("_path", [])
        if node["system"] and not any(n.get("kind") == "next" for n in path) and (not path or path[-1].get("kind") != f"s{node['system']}" or branch == "system2"):
            path.append({"kind": f"s{node['system']}", "label": SYS_LABEL[node["system"]], "p": round(conf, 2)})
        extra = {"system2": ("s2", "System 2"), "tool": ("tool", "재조회"), "ask": ("human", "사람에게 질문"),
                 "handoff": ("human", "사람")}.get(branch)
        if extra and not (branch == "system2"):
            path.append({"kind": extra[0], "label": extra[1]})

    def _close(self, c, it, status, reason, result=None):
        if status == "resolved" and not it.spec.can_resolve(c, it, result):
            status, result, reason = "handoff", None, "필수 완료 근거 미충족 → 사람 검토"
        if recovery.active(c, it):
            recovery.emit(c, it, "code_check", "코드가 최종 근거 확인", status=status, reason=reason)
        self._decide(c, it, "auto" if status == "resolved" else "handoff", reason)
        it.status, it.result, it.reason = status, result, reason
        it.data["ended_by"] = self.ended_by(it)
        top, conf = it.top()
        caselog.log_event(c.id, "item_final", item=it.id, status=status, result=result, conf=conf, reason=reason,
                          rewrites=it.rewrites, n_questions=len(it.asked), n_tools=len(it.tools_done))
        before = {x.id for x in c.items}
        hook = getattr(self.plugin, "on_item_closed", None)
        if hook:
            hook(c, it)
        if it.data.get("card"):
            caselog.log_event(c.id, "card", item=it.id, card=it.data["card"])
        new = [x for x in c.items if x.id not in before]
        if new:
            caselog.log_event(c.id, "items_added", after=it.id, items=[{"id": x.id, "kind": x.spec.kind, "title": x.title,
                              "data": x.data, "status": x.status, "result": x.result, "reason": x.reason} for x in new])
            c.steps.append({"kind": "items", "label": f"항목 {len(new)}개 추가", "items": [x.title for x in new]})

    @staticmethod
    def ended_by(it):
        """이 결정이 어디서 끝났나: rule(코드 규칙) / system1 / system2 / tool / human."""
        if it.status == "handoff" or it.asked:
            return "human"
        if it.tools_done:
            return "tool"
        if it.data.get("_last_system") == 2:
            return "system2"
        return "system1" if it.n_zoro else "rule"

    def decision_stats(self, c):
        """상품 1개의 결정 통계: 결정 노드 수, 어디서 끝났나(비율), 모델 호출 수, 결정 1회 지연(System 1 vs 2)."""
        done = [it for it in c.items if it.status in ("resolved", "handoff")]
        by = {k: 0 for k in ("rule", "system1", "system2", "tool", "human")}
        for it in done:
            by[it.data.get("ended_by") or self.ended_by(it)] += 1
        n = len(done) or 1
        lat = {k: round(sum(v) / len(v), 1) for k, v in c.latency.items() if v}
        finals = [{"item": it.id, "title": it.title, "p": round(it.top()[1], 4),
                   "model": bool(it.n_zoro) and it.spec.theta_hi > 0,     # 기준선으로 갈리는 모델 결정만(슬라이더 대상)
                   "ended_by": it.data.get("ended_by") or self.ended_by(it)} for it in done]
        tool_calls = sum(len(it.tools_done) for it in c.items) + c.data.get("_pre_tools_run", 0)
        return {"mode": "fixed_pipeline" if fixed_pipeline() else "agent", "decisions": len(done), "model_calls": c.n_zoro,
                "model_calls_system2": len(c.latency.get("system2", [])), "tool_calls": tool_calls,
                "generation_calls": len(c.latency.get("generation", [])),
                "reasoning_attempts": len(c.latency.get("reasoning_attempt", [])),
                "reasoning_incomplete": c.data.get("reasoning_incomplete", 0),
                "human": by["human"], "questions": sum(len(it.asked) for it in c.items),
                "total_ms": round((time.perf_counter() - c.t0) * 1000, 1), "ended_by": by,
                "ratio": {k: round(v / n, 3) for k, v in by.items()}, "latency_ms": lat, "finals": finals}

    def _finish(self, c):
        key, label, why = self.plugin.verdict(c)
        c.verdict = {"key": key, "label": label, "why": why}
        c.status = "done"
        c.output = self.plugin.generate(c)
        c.stats = self.decision_stats(c)
        caselog.log_event(c.id, "decision_stats", **c.stats)
        caselog.log_event(c.id, "final", verdict=c.verdict, n_zoro=c.n_zoro,
                          items=[{"id": it.id, "status": it.status, "result": it.result, "reason": it.reason,
                                  "rewrites": it.rewrites} for it in c.items],
                          total_ms=round((time.perf_counter() - c.t0) * 1000, 1), output=c.output)
        c.steps.append({"kind": "final", "label": f"결론: {label}", "reason": why})
        cmp = self.plugin.comparison(c) if hasattr(self.plugin, "comparison") else None
        if cmp:
            caselog.log_event(c.id, "comparison", **cmp)
        for a in self.plugin.plan_actions(c):       # 되돌릴 수 있는 행동은 자동, 고객 발송·돈 관련은 승인 대기
            a = {"id": f"a{len(c.actions) + 1}", "state": "pending", **a}
            c.actions.append(a)
            if a["auto"]:
                self._do_action(c, a)
            else:
                caselog.log_event(c.id, "action_pending", action=a["name"], label=a["label"], args=a.get("args"),
                                  reason=a.get("reason"))
                c.steps.append({"kind": "action", "label": f"승인 대기: {a['label']}", "reason": a.get("reason")})
        return c

    def approve(self, c, action_id, ok=True):
        a = next((x for x in c.actions if x["id"] == action_id and x["state"] == "pending"), None)
        if a is None:
            return c
        if ok:
            self._do_action(c, a, approved=True)
        else:
            a["state"] = "rejected"
            caselog.log_event(c.id, "action_rejected", action=a["name"], label=a["label"])
            c.steps.append({"kind": "action", "label": f"셀러 거절: {a['label']}"})
        return c

    def _do_action(self, c, a, approved=False):
        t = time.perf_counter()
        try:
            a["result"] = self.plugin.run_action(c, a)
            a["state"] = "done"
        except Exception as e:  # noqa: BLE001 -- 행동 실패는 기록하고 사람에게
            a["state"], a["result"] = "failed", {"error": f"{type(e).__name__}: {e}"}
        caselog.log_event(c.id, "action", action=a["name"], label=a["label"], auto=a["auto"], approved=approved,
                          args=a.get("args"), reason=a.get("reason"), state=a["state"], result=a.get("result"),
                          latency_ms=round((time.perf_counter() - t) * 1000, 1))
        c.steps.append({"kind": "action", "label": f"{'자동' if a['auto'] else '승인'} 실행: {a['label']}"
                        + ("" if a["state"] == "done" else " (실패)"), "reason": a.get("reason"), "result": a.get("result")})

    def _log_answer(self, c, it, q, label, update):
        caselog.log_event(c.id, "answer", item=it.id, question=q.text, qid=q.id, answer=label, update=update,
                          raw_probs=it.probs, decision_probs=it.decision_probs())
        c.steps.append({"kind": "answer", "item": it.title, "label": f"답: {label}", "question": q.text})

    @staticmethod
    def _img_meta(path):
        if not path:
            return None
        return {"path": os.path.relpath(path, caselog.ROOT) if os.path.isabs(path) else path,
                "sha256": hashlib.sha256(open(path, "rb").read()).hexdigest(), "bytes": os.path.getsize(path)}
