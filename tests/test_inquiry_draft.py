import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from nextask.core import inquiry
from nextask.core.web import app as web


class InquiryDraftTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {"input": {"title": "Sample blend\nBcc: header", "barcode": "0123456789012"},
                         "items": [{"id": "ing:1", "status": "resolved", "card": {"ingredient": "Lime"}},
                                   {"id": "ing:2", "status": "handoff", "reason": "same ingredient unknown",
                                    "card": {"ingredient": "Botanical Blend", "matched_entry": "Example candidate"}}]}
        self.facts = inquiry.collect_facts(self.snapshot)

    def generate(self, text):
        with patch.dict(os.environ, {"ZORO_MODE": "remote"}), patch.object(inquiry.ZoroClient, "generate", return_value={"text": text, "request_id": "test-request"}):
            return inquiry.make_draft(self.facts)

    def test_letter_asks_only_for_remaining_blend_and_does_not_claim_an_unconfirmed_candidate(self):
        result, audit = self.generate("Would you share @1?\n@1을 알려 주시겠습니까?")
        draft = result["draft"]
        self.assertEqual(result["source"], "zoro")
        self.assertEqual(self.facts["requests"], [{"kind": "blend", "name": "Botanical Blend", "reason": "same ingredient unknown"}])
        self.assertIn("Would you share the component names and amounts per serving for “Botanical Blend”?", draft["body_en"])
        self.assertIn("‘Botanical Blend’의 세부 원료명과 1회 제공량당 각 함량을 알려 주시겠습니까?", draft["body_ko"])
        self.assertIn("0123456789012", draft["body_en"])
        self.assertNotIn("\nBcc:", draft["body_en"])
        for outside in ("Lime", "Example candidate", "@1"):
            self.assertNotIn(outside, draft["body_en"] + draft["body_ko"])
        self.assertNotIn("Botanical Blend", audit["prompt"])

    def test_normalize_only_the_last_punctuation_of_a_complete_known_request(self):
        for end in ("?", ".", ""):
            with self.subTest(end=end):
                result, _ = self.generate(f"Please provide @1{end}\n@1을 제공해 주세요.")
                self.assertEqual(result["source"], "zoro")
                self.assertIn("per serving for “Botanical Blend”.", result["draft"]["body_en"])
        result, _ = self.generate("Please provide @1? It contains lead.\n@1을 제공해 주세요.")
        self.assertEqual(result["source"], "template")

    def test_untrusted_clause_wrong_translation_and_missing_clause_are_not_published(self):
        for text in ("Would you share @1?\n@1을 알려 주시겠습니까?\nThis product contains lead.",
                     "Would you share @1?\n@2을 알려 주시겠습니까?", "Would you share @1?"):
            with self.subTest(text=text):
                result, audit = self.generate(text)
                self.assertEqual(result["validation"]["status"], "outside_grounded_grammar")
                self.assertEqual(result["source"], "template")
                self.assertIn("Botanical Blend", result["draft"]["body_en"])
                self.assertNotIn("contains lead", str(result))
                self.assertEqual(audit["raw_generation"], text)

    def test_new_label_during_generation_invalidates_old_draft(self):
        case_id = "inquiry-stale"
        case = SimpleNamespace(status="done", snapshot=lambda: self.snapshot)
        new_label = {"checks": {"ingredient_sections": False}, "evidence_id": "new-label"}

        def mutate(_facts):
            self.snapshot["extras"] = {"label_evidence": {"review_requirements": new_label}}
            return {}, {"status": "accepted"}

        with patch.dict(web.CASES, {case_id: case}), patch.dict(web.LOCKS, {}), patch.object(inquiry, "make_draft", side_effect=mutate), patch.object(web.caselog, "log_event") as log:
            with self.assertRaises(web.HTTPException) as exc:
                web.inquiry_draft(case_id)
        self.assertEqual(exc.exception.status_code, 409)
        self.assertEqual(log.call_args.args[:2], (case_id, "inquiry_draft_stale"))
