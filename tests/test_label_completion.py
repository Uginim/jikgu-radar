"""Bounded scope regressions; label strings transcribe DSLD 245739 sections, not live OCR calls."""
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nextask.core import caselog, store
from nextask.core.engine import Case, Engine
from nextask.core.matching import Entry, Registry
from nextask.core.plugin import Question
from nextask.core.plugins import supplement
from nextask.core.zoro import Zoro

PARTIAL = ["Supplement Facts", "Serving Size 1 Veg Capsule", "DMAE (Dimethylaminoethanol) 250 mg"]
FULL = PARTIAL + ["Other ingredients: Hypromellose (cellulose capsule), Stearic Acid (vegetable source), Rice Flour and Silicon Dioxide."]


class LabelCompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patches = [patch.object(caselog, "LOG_DIR", self.tmp.name),
                   patch.object(store, "PATH", str(Path(self.tmp.name) / "store.json")),
                   patch.dict(os.environ, {"NEXT_POLICY": "agent", "FIXED_PIPELINE": "", "ZORO_MODE": "mock", "PUBCHEM_LIVE": "0"}),
                   patch.object(supplement, "REG", Registry([Entry("deanol", en="Deanol", etc=["DMAE", "Dimethylaminoethanol"])], source="isolated test registry")),
                   patch.object(supplement, "DICTS", []),
                   patch.object(supplement.DSLD, "search", return_value={"source": "dsld", "status": "no_candidates", "found": False})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        previous = getattr(caselog.CURRENT, "case_id", None)
        self.addCleanup(setattr, caselog.CURRENT, "case_id", previous)
        self.plugin = supplement.Supplement()
        self.plugin.LABEL = supplement.LabelSource()
        self.engine = Engine(self.plugin, zoro=Zoro(mode="mock"))
        self.engine.zoro.recordings = {}
        self.case = Case(self.plugin, case_id="completion-regression")
        self.case.data["input"] = {"url": "test://current-dmae", "title": "NOW Foods DMAE", "brand": "NOW Foods"}
        self.case.items = self.plugin.make_items(self.case)
        self.item = self.case.items[0]
        self.item.probs = {"readable": .99, "unreadable": .01}
        self.case.status = "running"
        self.set_label(PARTIAL)

    def set_label(self, lines, **overrides):
        self.case.data["label"] = {"source": "dom", "source_url": "test://current-dmae",
                                   "lines": [{"text": x, "conf": None} for x in lines], "mean_conf": None, **overrides}

    def test_partial_label_is_compared_without_claiming_whole_label_completion(self):
        self.engine._run(self.case)
        self.assertEqual(self.case.status, "done")
        self.assertEqual(self.item.result, "readable")
        self.assertEqual(self.case.verdict["key"], "blocked")
        self.assertIn("Deanol", self.case.verdict["why"])
        self.assertFalse(self.item.data["review_requirements"]["complete"])

    def test_low_readability_does_not_prevent_bounded_rule_match(self):
        self.item.probs = {"readable": .6, "unreadable": .4}
        self.engine._run(self.case)
        self.assertEqual(self.case.verdict["key"], "blocked")
        self.assertFalse(self.item.data["review_requirements"]["complete"])

    def test_full_supplement_requires_valid_answer_then_resumes_same_case(self):
        self.set_label(FULL)
        self.engine._run(self.case)
        self.assertEqual(self.case.status, "done")
        self.assertIsNone(self.case.pending)
        guard = self.item.data["review_requirements"]
        self.assertTrue(guard["complete"])
        self.assertEqual(guard["confirmation_by"], "code")
        self.assertTrue(guard["evidence_id"])
        self.assertTrue(self.case.data["self_answers"][0]["evidence_lines"])
        self.assertEqual(self.case.verdict["key"], "blocked")

    def test_low_confidence_unknown_origin_and_other_product_cannot_be_confirmed(self):
        for extra in ({"source": "ocr", "mean_conf": .2}, {"source": "ocr", "mean_conf": None},
                      {"source_url": None}, {"source_url": "test://another-product"},
                      {"source": "dsld"}, {"identity": "conflict"}):
            with self.subTest(extra=extra):
                self.set_label(FULL, **extra)
                status = self.item.spec.completion_guard(self.case, self.item)
                self.item.data["confirmed_evidence_id"] = status["evidence_id"]
                status = self.item.spec.completion_guard(self.case, self.item)
                self.assertFalse(status["ready_for_confirmation"])
                self.assertFalse(status["complete"])
                self.assertNotIn("enough", self.item.spec.next_options(self.case, self.item))

    def test_changed_evidence_invalidates_pending_yes(self):
        self.set_label(FULL)
        old = self.item.spec.completion_guard(self.case, self.item)
        self.assertEqual(old["confirmation_by"], "code")
        self.set_label(PARTIAL)
        current = self.item.spec.completion_guard(self.case, self.item)
        self.assertNotEqual(current["evidence_id"], old["evidence_id"])
        self.assertFalse(current["complete"])
        self.assertIsNone(current["confirmation_by"])

    def test_refusal_and_low_model_score_remain_incomplete(self):
        self.set_label(FULL)
        self.item.probs = {"readable": .6, "unreadable": .4}
        self.assertFalse(self.item.spec.completion_guard(self.case, self.item)["complete"])
        self.set_label(PARTIAL)
        self.item.probs = {"readable": .99, "unreadable": .01}
        self.engine._run(self.case)
        self.assertEqual(self.case.items[-1].status, "handoff")
        self.assertEqual(self.case.verdict["key"], "blocked")
        self.assertFalse(self.item.data["review_requirements"]["complete"])

    def test_previous_confirmation_does_not_survive_source_replacement(self):
        self.set_label(FULL)
        old = self.item.spec.completion_guard(self.case, self.item)
        self.item.data["confirmed_evidence_id"] = old["evidence_id"]
        self.set_label(PARTIAL + ["Other ingredients: Gelatin."])
        current = self.item.spec.completion_guard(self.case, self.item)
        self.assertNotEqual(current["evidence_id"], old["evidence_id"])
        self.assertTrue(current["complete"])
        self.assertEqual(current["confirmation_by"], "code")
        self.assertEqual(self.item.data["self_confirmed_evidence_id"], current["evidence_id"])
        self.assertIn("Gelatin", str(self.case.data["self_answers"][-1]["evidence_lines"]))

    def test_clear_ingredient_card_cannot_authorize_sale_of_blocked_product(self):
        self.engine._run(self.case)
        other = supplement.Item("ing:stearic", self.plugin.ING, "성분: Stearic Acid",
                                data={"name": "Stearic Acid", "candidates": ["deanol"], "decided_by": "zoro"})
        other.probs, other.n_zoro = {"deanol": .068, "none": .932}, 1
        self.case.items.append(other)
        unsafe_recommendation = "차단 근거가 없으며, 판매를 진행합니다."
        with patch.object(supplement, "explain", return_value={"text": unsafe_recommendation, "by": "zoro-1"}):
            self.engine._close(self.case, other, "resolved", "ingredient classification", "clear")
        card = other.data["card"]
        self.assertEqual(other.result, "clear")
        self.assertEqual(self.plugin.verdict(self.case)[0], "blocked")
        self.assertEqual(card["why_by"], "code")
        self.assertIn("Stearic Acid", card["why"])
        self.assertNotIn(unsafe_recommendation, card["why"])
        self.assertIsNone(card["matched_entry"])

    def test_unknown_scope_retains_no_match_without_full_completion_or_sale_approval(self):
        self.set_label(["Ingredients: Rice Flour."])
        self.engine._run(self.case)
        self.assertIsNone(self.case.pending)
        self.assertEqual(self.case.verdict["key"], "not_found")
        self.assertEqual(self.case.verdict["label"], "읽은 범위에서 찾지 못함")
        guard = self.item.data["review_requirements"]
        self.assertTrue(guard["complete"])
        self.assertEqual(guard["confirmation_by"], "code")
        self.assertTrue(guard["evidence_id"])
        self.set_label(["Supplement Facts", "Serving Size 1 capsule", "Rice Flour 100 mg"])
        self.assertFalse(self.item.spec.completion_guard(self.case, self.item)["complete"])

    def test_stale_capture_cannot_mutate_new_pending_question(self):
        from nextask.core.web import app as web
        self.case.pending = (self.item, Question("old-capture", "old", kind="capture_auto"))
        scheduled = []
        with patch.dict(web.CASES, {self.case.id: self.case}), patch.object(web, "_run_bg", side_effect=lambda cid, fn: scheduled.append(fn)):
            web.capture(self.case.id, {"question_id": "old-capture", "source_url": "test://wrong"})
            self.case.pending = (self.item, Question("new-capture", "new", kind="capture_auto"))
            with self.assertRaises(RuntimeError):
                scheduled[0]()
        self.assertNotIn("capture_source_url", self.case.data)
        self.assertEqual(self.case.pending[1].id, "new-capture")



if __name__ == "__main__":
    unittest.main()
