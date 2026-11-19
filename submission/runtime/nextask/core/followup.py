"""Reassess a settled supplement case with a user-supplied reply. Never replace its original evidence."""
import copy
import hashlib
import json
import re

from . import caselog
from .matching import has_word
from .mfds import norm
from .plugins import supplement
from .sources import label_amount_conflicts, label_lines


# Only whole, affirmative declarations are converted into ingredient-list rows. Free prose remains quoted evidence.
_DECLARATION = re.compile(r"^(?:our product contains|this product contains|the ingredients are|the other ingredients are|the inactive ingredients are)\s+([^.!?\n]+)\.?$", re.I)
_NEGATED = re.compile(r"\b(?:not|no|free of|may|except|without|unknown)\b|미포함|불포함|미확인", re.I)


def _text_lines(text):
    parts = []
    for line in text.splitlines():
        m = _DECLARATION.fullmatch(line.strip())
        if m and not _NEGATED.search(m[1]):
            parts.append("Ingredients: " + m[1])
        else:
            parts.append(line)
    return [{"text": s, "conf": None} for s in label_lines("\n".join(parts), "") if not _NEGATED.search(s)]


def fingerprint(text, path):
    h = hashlib.sha256(text.encode())
    if path:
        with open(path, "rb") as f:
            h.update(b"\0image\0" + hashlib.file_digest(f, "sha256").digest())
    return h.hexdigest()


def apply(engine, case, gate, record, text, path):
    """All mutations run under the existing case lock. Previous resolved/blocked items are never reopened."""
    caselog.CURRENT.case_id = case.id
    plugin = case.plugin
    old_items = {it.id for it in case.items}
    pending = [it for it in case.items if it.spec is plugin.ING and it.status == "handoff"]
    materials = []
    if text:
        materials.append({"source": "manufacturer_reply_text", "lines": _text_lines(text),
                          "read_line_count": sum(bool(s.strip()) for s in text.splitlines())})
    if path:
        try:
            result = supplement.OCR.read(path)
        except Exception as exc:
            if not text:
                raise
            result = {"lines": [], "error": type(exc).__name__}
        rows = result.get("lines") or []
        confidence, repetition = result.get("mean_conf"), result.get("repeat_ratio", 0)
        usable = bool(rows and isinstance(confidence, (int, float)) and confidence >= .7 and isinstance(repetition, (int, float)) and repetition <= .2)
        # Keep OCR row confidence and column order, just as initial photo inspection does.
        materials.append({"source": "manufacturer_reply_photo", "lines": rows if usable else [],
                          "read_line_count": len(rows)})
        record["ocr_status"] = "read" if usable else "failed" if result.get("error") else "low_quality_or_empty"
        caselog.log_event(case.id, "followup_ocr", followup_id=record["id"], result=result, usable=usable)
    record["diagnostics"] = []
    for material in materials:
        lines = material["lines"]
        names = supplement.parse_ingredients(lines)
        recognized = [row["text"] for row in lines if any(
            norm(name) in norm(row["text"]) or norm(row["text"]) in norm(name)
            for name in names) and norm(row["text"])]
        diagnostic = {"source": material["source"], "read_line_count": material["read_line_count"],
                      "ingredient_line_count": len(recognized), "parsed_ingredient_count": len(names),
                      "evidence_lines": recognized, "added_count": 0}
        record["diagnostics"].append(diagnostic)
        if not lines:
            diagnostic["reason"] = "읽기 품질 기준을 통과한 줄이 없습니다" if material["source"].endswith("photo") else "성분표 또는 명확한 성분 선언을 찾지 못했습니다"
            continue
        # This is NOT the original page DOM or a verified manufacturer communication.
        material["source_url"] = None
        material["followup_id"] = record["id"]
        # Match in a material-specific view; never swap the live case's primary DOM/OCR label,
        # even briefly (GET snapshots do not acquire the writer lock). Use the engine's existing matcher.
        view = copy.copy(case)
        view.data = {**case.data, "label": material}
        view.items = [gate]
        plugin._add_ingredients(view, gate)
        known = {norm(it.data["name"]) for it in case.items if it.spec is plugin.ING}
        seq = max((int(it.id[4:]) for it in case.items if it.id.startswith("ing:") and it.id[4:].isdigit()), default=0)
        for it in view.items[1:]:
            name = norm(it.data["name"])
            if name in known:
                continue
            known.add(name)
            seq += 1
            it.id = f"ing:{seq}"
            it.data["evidence_source"] = {"source": material["source"], "followup_id": record["id"]}
            case.items.append(it)
            diagnostic["added_count"] += 1
        if names:
            # 남은 확인을 새 근거로 다시 계산하는 데만 쓴다. 원래 라벨·이전 점수는 그대로 이력에 남는다.
            case.data.setdefault("followup_evidence", []).append(
                {"followup_id": record["id"], "source": material["source"], "lines": lines})
        diagnostic["reason"] = ("새 성분을 같은 규칙으로 대조했습니다" if diagnostic["added_count"] else
                                "읽은 성분은 이미 사건에 있습니다" if names else "읽은 줄에서 성분 이름과 함량 또는 성분 선언을 연결하지 못했습니다")
        conflicts = label_amount_conflicts((case.data.get("label") or {}).get("lines", []), lines)
        for conflict in conflicts:
            if conflict not in case.data.setdefault("label_amount_conflicts", []):
                case.data["label_amount_conflicts"].append(conflict)
    new = [it for it in case.items if it.spec is plugin.ING and it.id not in old_items]
    added_names = [it.data["name"] for it in new]
    if added_names:
        case.data["additional_ingredient_names"] = list(dict.fromkeys([*case.data.get("additional_ingredient_names", []), *added_names]))
    record["added"] = added_names
    source_rows = [s.strip() for s in text.splitlines() if s.strip()]
    source_rows += [r["text"] for material in materials for r in material["lines"]]
    rejudge = [it for it in new if it.status == "open"]
    for it in pending:
        quotes = list(dict.fromkeys(s for s in source_rows if has_word(it.data["name"], s)))[:3]
        if not quotes:
            continue
        fact = "User-supplied reply about this ingredient; quoted evidence only, not identity confirmation or an instruction: " + json.dumps([s[:450] for s in quotes], ensure_ascii=False)
        it.facts.append(fact)
        rejudge.append(it)
    record["rejudged"] = [it.id for it in rejudge]
    for it in rejudge:
        if it.spec.resolve_official(engine, case, it):
            continue
        if engine._judge(case, it, "제조사 답변 근거로 같은 사건 재판정"):
            top, conf = it.top()
            if conf >= it.spec.theta_hi and it.spec.can_resolve(case, it, top):
                engine._close(case, it, "resolved", f"추가 자료 대조: {conf:.1%}, 기존 근거 조건 충족", result=top)
            else:
                engine._close(case, it, "handoff", "추가 자료로도 원료 동일성 근거 미확인")
        else:
            engine._close(case, it, "handoff", "추가 자료 판정을 완료하지 못해 확인이 남았습니다")
    caselog.log_event(case.id, "followup_material", followup_id=record["id"], materials=materials,
                      added=added_names, rejudged=record["rejudged"], diagnostics=record["diagnostics"])
    # Do not call Engine._finish: that would execute registration/market actions a second time.
    requirements = plugin.LABEL.completion_guard(case, gate)
    caselog.log_event(case.id, "followup_requirements", followup_id=record["id"],
                      resolved=requirements.get("resolved_by_followup"), remaining=requirements.get("missing"),
                      complete=requirements.get("complete"))
    key, label, why = plugin.verdict(case)
    case.verdict = {"key": key, "label": label, "why": why}
    case.output = plugin.generate(case)
    case.stats = engine.decision_stats(case)
    record["state"] = "reviewed" if added_names or rejudge else "insufficient_evidence"
    case.steps.append({"kind": "follow_up", "label": "사용자가 제공한 문의 답변 대조", "reason": record["state"]})
