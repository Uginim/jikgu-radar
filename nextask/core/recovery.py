"""Opt-in E1 action planning: generation selects tools, never facts or final verdicts.

Prompt/schema/one invalid-output repair follow research bench_logic_runner E1.
Production tools and existing item guards replace the benchmark's evidence replay.
"""
import json
import math
import time

from . import caselog
from ._infra_client import ZoroUnavailable

MAX_STEPS = 4
MAX_TOKENS = 64
INSTRUCTIONS = (
    "Select the next evidence action for the ambiguous label line. Use a newly obtained matching label "
    "or independent alias source if it may resolve the identity. Do not declare a final import result "
    "or repeat a tried action. If further evidence cannot be obtained, hand off."
)
ENVELOPE = (
    'Select exactly one available action for the following identical decision task. '
    'Return only a JSON object with exactly one field: {"action":"ACTION_ID"}. '
    'Do not output explanations or code fences.\n'
)


def active(case, item):
    # Acquired labels use bounded code actions; standalone ingredient callers keep evidence recovery.
    if item.spec.kind == "ingredient" and (case.data.get("label") or {}).get("lines"):
        return False
    return bool(case.data.get("agent_recovery_enabled") and getattr(item.spec, "recovery_supported", False))


def state(case, item):
    value = item.spec.recovery_state(case, item)
    used = item.data.get("_actions", []) if item.spec.kind == "label" else item.data.get("_recovery_actions", [])
    return {**value, "item": item.id, "item_title": item.title,
            "step": item.data.get("_generation_steps", 0),
            "budget_left": max(0, min(MAX_STEPS - item.data.get("_generation_steps", 0),
                                       item.spec.next_budget - len(used))),
            "attempted": list(used), "observations": item.data.get("_recovery_outcomes", []),
            "current_s1_observation": {"probabilities": item.probs,
                                       "status": "unconfirmed_not_independent_evidence"}}


def emit(case, item, stage, label, **details):
    value = {**state(case, item), "stage": stage, "label": label, **details}
    case.data["agent_state"] = value
    caselog.log_event(case.id, "agent_stage", **value)
    case.steps.append({"kind": "agent_stage", **value})


def generate(engine, request):
    if engine.zoro.mode not in ("remote", "record") or engine.zoro.client is None:
        raise ZoroUnavailable("Generation recovery requires an actual provider; no synthetic fallback")
    return engine.zoro.client.generate(prompt=ENVELOPE + json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                                       max_new_tokens=MAX_TOKENS)


def parse_action(response, options):
    if not isinstance(response, dict):
        return None, "invalid_response", True
    tokens = response.get("tokens")
    if (type(tokens) is not int or not 0 < tokens < MAX_TOKENS or response.get("cut")
            or response.get("incomplete") or response.get("finish_reason") not in (None, "stop")):
        return None, "generation_completion_unverified", True
    try:
        value = json.loads(response["text"])
    except (KeyError, TypeError, ValueError):
        return None, "invalid_json_action", False
    if not isinstance(value, dict) or set(value) != {"action"} or not isinstance(value["action"], str):
        return None, "invalid_json_schema", False
    action = value["action"]
    return (action, None, False) if action in options else (None, "action_not_available", False)


def choose_action_head(engine, request):
    """Constrained choice retry; candidate/ingredient IDs are never action IDs."""
    response, meta = engine.zoro.decide(request)
    answer = response["answers"]["main"]
    options = request["questions"]["main"]["criteria"]
    probs, action = answer.get("probabilities", {}), answer.get("choice")
    if (set(probs) != set(options) or action not in options
            or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in probs.values())
            or not probs or abs(sum(probs.values()) - 1) > .02 or probs[action] != max(probs.values())):
        raise ValueError("head_action_not_available")
    return action, response, meta


def advance(engine, case, item):
    """One executable action; an invalid choice may be repaired once for this item."""
    while item.data.get("_generation_steps", 0) < MAX_STEPS:
        current = state(case, item)
        if current["budget_left"] <= 0:
            break
        options = item.spec.recovery_options(case, item)
        options = {k: v for k, v in options.items() if k not in current["attempted"]}
        if not options:
            break
        instructions = INSTRUCTIONS
        if item.data.get("_generation_reasked"):
            instructions += "\nReturn exactly one currently offered action ID; no removed or already attempted action."
        request = {"state": current, "questions": {"main": {"type": "choice", "instructions": instructions,
                                                           "criteria": options}}}
        item.data["_generation_steps"] = current["step"] + 1
        head_retry = item.data.pop("_generation_head_pending", False)
        source = "zoro-1-head" if head_retry else "zoro-1-generate"
        emit(case, item, "generation", "허용된 선택지로 한 번 다시 판단" if head_retry else "다음 근거 행동 선택", source=source)
        started = time.perf_counter()
        response, error, terminal = None, None, False
        try:
            if head_retry:
                action, response, meta = choose_action_head(engine, request)
                caselog.log_event(case.id, "agent_head_retry", item=item.id, request=request,
                                  response=response, meta=meta, action=action, source=meta.get("source"))
            else:
                response = generate(engine, request)
                action, error, terminal = parse_action(response, options)
            case.n_zoro += 1
        except Exception as ex:  # Transport/config failure cannot be repaired into invented evidence.
            action, error, terminal = None, f"{type(ex).__name__}: {ex}", True
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        case.latency.setdefault("generation", []).append(elapsed)
        caselog.log_event(case.id, "agent_generation", item=item.id, stage="generation", request=request,
                          response=response, action=action, error=error, latency_ms=elapsed,
                          completion_basis="validated_choice_head" if head_retry else "whole_json_below_64_token_cap; EOS metadata unavailable",
                          source=source)
        if error:
            emit(case, item, "code_check", "생성 행동 거부", reason=error)
            if terminal or item.data.get("_generation_reasked"):
                reason = "다음 확인 방법을 정하지 못해 이 성분만 확인 남음" if head_retry else f"생성 단계 미확정({error}) → 남은 근거 확인 필요"
                engine._close(case, item, "handoff", reason)
                return "done"
            item.data["_generation_reasked"] = True
            item.data["_generation_head_pending"] = True
            continue
        used_key = "_actions" if item.spec.kind == "label" else "_recovery_actions"
        item.data.setdefault(used_key, []).append(action)
        emit(case, item, "generation", "허용 행동 실행", action=action, source=source)
        return item.spec.recovery_execute(engine, case, item, action)
    engine._close(case, item, "handoff", "생성/도구 걸음 한도 소진 → 남은 근거 확인 필요")
    return "done"
