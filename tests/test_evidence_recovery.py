"""Local tool fixtures, not real OCR/model/site accuracy measurements."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nextask.core import caselog
from nextask.core.engine import Case, Engine
from nextask.core.ocr import ChainOCR, SidecarOCR
from nextask.core.plugin import Item
from nextask.core.plugins import supplement
from nextask.core.sources import DSLDSearch
from nextask.core.zoro import Zoro


class EvidenceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(caselog, "LOG_DIR", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)
        self.plugin = supplement.Supplement()
        self.engine = Engine(self.plugin, zoro=Zoro(mode="mock"))
        self.case = Case(self.plugin, case_id="evidence-regression")
        self.case.data["input"] = {"title": "Example Vitamin C", "brand": "Example", "barcode": "123456789012"}
        self.item = Item("label", self.plugin.LABEL, "label", data={"_pending_action": "capture"})
        self.case.items = [self.item]

    def search_fixture(self, upc="123456789012", brand="Example", off_market=0):
        search = DSLDSearch()
        hit = {"_id": "17", "_source": {"brandName": "Example", "fullName": "Example Vitamin C", "offMarket": 0}}
        label = {"id": 17, "brandName": brand, "fullName": "Example Vitamin C", "upcSku": upc,
                 "offMarket": off_market, "ingredientRows": [{"name": "Yohimbine", "quantity": []}]}
        search._get = lambda path: {"hits": [hit]} if path.startswith("/search-filter") else label
        return search

    def test_same_barcode_candidate_never_becomes_current_label(self):
        with patch.object(supplement, "DSLD", self.search_fixture()):
            self.plugin.LABEL.next_execute(self.engine, self.case, self.item, "dsld")
        self.assertNotIn("label", self.case.data)
        evidence = self.plugin.panel_extras(self.case)["label_evidence"]["acquisitions"][0]
        self.assertEqual(evidence["candidate"]["identity"], "barcode_match")
        self.assertFalse(evidence["candidate"]["version_verified"])
        self.assertEqual(evidence["status"], "candidate_unverified")
        self.item.status = "handoff"
        self.assertEqual(self.plugin.verdict(self.case)[0], "review")
        self.assertEqual(supplement._lab_ingredients(self.case), [])

    def test_explicit_identity_conflicts_are_retained_without_ingredients(self):
        result = self.search_fixture("999999999999", "Example Labs", 1).search("Example Vitamin C", "Example", "123456789012")
        self.assertEqual(set(result["candidate"]["conflicts"]), {"barcode_mismatch", "brand_mismatch", "off_market"})
        self.assertEqual(result["candidate"]["identity"], "conflict")
        self.assertNotIn("lines", result)

    def test_lookup_error_is_not_no_candidates(self):
        with patch.object(supplement.DSLD, "search", side_effect=RuntimeError("fixture outage")):
            self.plugin.LABEL.next_execute(self.engine, self.case, self.item, "dsld")
        self.assertEqual(self.item.data["_outcomes"][0]["status"], "lookup_failed")
        self.assertIn("fixture outage", self.item.data["_outcomes"][0]["result"])
        search = DSLDSearch()
        with patch.object(search, "_get", return_value={"hits": []}):
            self.assertEqual(search.search("Example Vitamin C")["status"], "no_candidates")

    def test_current_capture_sidecar_recovers_ingredients(self):
        image = Path(self.tmp.name) / "capture.png"
        image.write_bytes(b"explicit local capture identity fixture; not a photograph")
        Path(str(image) + ".ocr.json").write_text(json.dumps({"lines": [
            {"text": "Supplement Facts", "conf": .99}, {"text": "Vitamin C 100 mg", "conf": .99}]}))
        self.case.image_path = str(image)
        with patch.object(supplement, "OCR", ChainOCR([SidecarOCR([self.tmp.name])])):
            result = supplement._next_answer(self.engine, self.case, self.item, True)
        self.assertEqual(result, "rejudge")
        self.assertEqual(self.case.data["label"]["source"], "ocr")
        self.assertEqual(self.case.data["label"]["engine"], "sidecar")
        self.assertIn("Vitamin C", supplement._lab_ingredients(self.case))

    def test_absent_recording_is_unavailable_not_unreadable(self):
        image = Path(self.tmp.name) / "unrecorded.png"
        image.write_bytes(b"unrecorded capture fixture")
        self.case.image_path = str(image)
        with patch.object(supplement, "OCR", ChainOCR([SidecarOCR([self.tmp.name])])):
            result = supplement._next_answer(self.engine, self.case, self.item, True)
        self.assertEqual(result, "noop")
        self.assertEqual(self.item.data["_outcomes"][0]["status"], "ocr_unavailable")
        self.assertNotIn("label", self.case.data)
        self.item.status = "handoff"
        verdict = self.plugin.verdict(self.case)
        self.assertEqual(verdict[0], "review")
        self.assertIn("판독을 실행하지 않음", verdict[2])

    def test_missing_empty_and_execution_error_are_distinct(self):
        self.assertEqual(supplement.ocr_capture(self.case, self.item)["status"], "capture_missing")
        self.case.image_path = "fixture.png"
        with patch.object(supplement.OCR, "read", return_value={"lines": [], "mean_conf": 0}):
            self.assertEqual(supplement.ocr_capture(self.case, self.item)["status"], "read_empty")
        with patch.object(supplement.OCR, "read", side_effect=RuntimeError("decoder failed")):
            result = supplement.ocr_capture(self.case, self.item)
            self.assertEqual(result["status"], "ocr_failed")
            self.assertIn("decoder failed", result["reason"])
        self.assertFalse(self.plugin.LABEL.apply_tool(self.case, self.item, "ocr_capture", result))
        self.assertNotIn("label", self.case.data)

    def test_remote_ocr_retains_response_provenance(self):
        from types import SimpleNamespace
        from nextask.core.ocr import RemoteOCR
        image = Path(self.tmp.name) / "capture.png"
        image.write_bytes(b"explicit OCR transport fixture")
        raw = {"text": "Supplement Facts\nVitamin C 100 mg", "request_id": "fixture-ocr-1",
               "tokens": 12, "timing_ms": {"total": 42}}
        adapter = RemoteOCR.__new__(RemoteOCR)
        adapter.client = SimpleNamespace(ocr=lambda image: raw)
        result = adapter.read(str(image))
        self.assertEqual(result["response"], raw)
        self.assertEqual([x["text"] for x in result["lines"]], raw["text"].splitlines())


if __name__ == "__main__":
    unittest.main()
