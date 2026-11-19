"""Controlled next-action probabilities and HTTP failures; no model or OCR service calls."""
import io
import json
import os
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from nextask.core import caselog
from nextask.core._infra_client import ZoroClient
from nextask.core.engine import Case, Engine
from nextask.core.ocr import ChainOCR, RemoteOCR
from nextask.core.plugin import Item, Tool
from nextask.core.plugins import supplement
from nextask.core.web import app as web


FULL = ["Supplement Facts", "Serving Size 1 Capsule", "Vitamin C 100 mg", "Other ingredients: Rice Flour."]


class LabelAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in [patch.object(caselog, "LOG_DIR", self.tmp.name),
                  patch.dict(os.environ, {"NEXT_POLICY": "agent", "NEXTASK_AGENT_RECOVERY": "0"})]:
            p.start()
            self.addCleanup(p.stop)
        self.plugin = supplement.Supplement()
        self.spec = supplement.LabelSource()
        self.plugin.LABEL = self.spec
        self.case = Case(self.plugin, case_id=self._testMethodName)
        self.case.data["input"] = {"url": "test://current-product", "title": "Example Vitamin C", "role": "consumer"}
        self.case.data["label"] = {"source": "dom", "source_url": "test://current-product",
                                   "lines": [{"text": s, "conf": None} for s in FULL]}
        self.case.items = self.plugin.make_items(self.case)
        self.item = self.case.items[0]
        self.item.probs = {"readable": .99, "unreadable": .01}
        self.decide = Mock()
        self.engine = Engine(self.plugin, zoro=SimpleNamespace(decide=self.decide, mode="mock"))

    def test_existing_label_skips_capture_and_generated_next_action(self):
        self.decide.side_effect = AssertionError("label sequencing must not consult model")
        self.engine._item_loop(self.case, self.item)
        self.assertEqual((self.item.status, self.item.result), ("resolved", "readable"))
        self.assertTrue(self.spec.completion_guard(self.case, self.item)["complete"])
        self.assertEqual(self.item.data["review_requirements"]["confirmation_by"], "code")
        self.assertIsNone(self.case.pending)

    def test_failed_supplied_image_is_retried_once_then_requests_region(self):
        from nextask.core.ocr import OCRUnavailable
        self.case.data.pop("label")
        self.case.image_path = str(Path(self.tmp.name) / "transport-only.png")
        self.decide.side_effect = AssertionError("unavailable OCR cannot cause model action selection")
        with patch.object(supplement.OCR, "read", side_effect=OCRUnavailable("explicit local failure")) as read:
            self.engine._run(self.case)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(self.case.pending[1].kind, "capture_region")
        self.assertEqual([a["status"] for a in self.item.data["_acquisitions"]], ["ocr_unavailable", "ocr_unavailable"])
        self.engine.answer(self.case, None)
        self.assertEqual(self.case.verdict["key"], "review")
        self.assertFalse(self.spec.completion_guard(self.case, self.item)["complete"])

    def test_stale_acquisition_choice_cannot_promote_low_quality_to_readable(self):
        self.case.data["label"].update(source="ocr", mean_conf=.2)
        self.spec.next_execute(self.engine, self.case, self.item, "compare")
        self.assertEqual(self.item.status, "handoff")
        self.assertIsNone(self.item.result)



class OCRAttemptLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(caselog, "LOG_DIR", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)
        self.image = Path(self.tmp.name) / "transport-fixture.png"
        self.image.write_bytes(b"Explicit transport-only fixture, not an OCR accuracy image")
        self.case = Case(supplement.Supplement(), case_id=self._testMethodName)
        self.case.image_path = str(self.image)
        self.item = Item("label", supplement.LabelSource(), "label")
        self.case.items = [self.item]
        self.engine = Engine(self.case.plugin)
        self.remote = RemoteOCR.__new__(RemoteOCR)
        self.remote.client = ZoroClient(urls=["http://ocr-fixture.invalid"], api_key="", retries=2)

    @staticmethod
    def http_failure(status):
        return HTTPError("http://ocr-fixture.invalid/v1/ocr", status, "injected failure", Message(),
                         io.BytesIO(json.dumps({"error": "explicit injected gateway failure"}).encode()))

    def run_ocr(self, responses):
        with patch.object(self.remote.client, "_post", side_effect=responses), \
                patch("nextask.core._infra_client.time.sleep"), \
                patch.object(supplement, "OCR", ChainOCR([self.remote])):
            result = self.engine._run_tool(self.case, self.item, Tool("ocr_capture", supplement.ocr_capture, retries=0))
        assert result is not None
        event = next(e for e in caselog.read_case(self.case.id) if e["event"] == "tool_call")
        self.assertEqual(event["attempt"], 1)  # Engine invocation is separate from HTTP attempts.
        self.assertEqual(event["result"]["attempts"], result["attempts"])
        self.assertEqual(event["result"]["attempt_count"], len(result["attempts"]))
        self.assertEqual(self.item.data["_acquisitions"][-1]["attempts"], result["attempts"])
        return result

    def test_gateway_failure_then_success_preserves_both_attempts(self):
        raw = {"engine": "explicit-ocr-response-fixture", "text": "\n".join(FULL),
               "mean_conf": .99, "request_id": "fixture-after-504"}
        result = self.run_ocr([self.http_failure(504), raw])
        self.assertEqual(result["status"], "read")
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(result["attempts"][0]["status"], 504)
        self.assertEqual(result["attempts"][0]["error"], "explicit injected gateway failure")
        self.assertTrue(result["attempts"][1]["ok"])
        self.assertEqual(result["response"]["request_id"], "fixture-after-504")

    def test_exhausted_retries_remain_failed_with_actual_reasons(self):
        result = self.run_ocr([self.http_failure(504), TimeoutError("explicit second failure")])
        self.assertEqual(result["status"], "ocr_failed")
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(result["attempts"][0]["status"], 504)
        self.assertIn("TimeoutError: explicit second failure", result["attempts"][1]["error"])
        self.assertFalse(any(a.get("ok") for a in result["attempts"]))
        self.assertNotIn("lines", result)
        self.assertNotIn("label", self.case.data)

    def test_nonretryable_error_keeps_one_failed_attempt(self):
        result = self.run_ocr([self.http_failure(400)])
        self.assertEqual(result["status"], "ocr_failed")
        self.assertEqual(result["attempt_count"], 1)
        self.assertEqual(result["attempts"][0]["status"], 400)


if __name__ == "__main__":
    unittest.main()
