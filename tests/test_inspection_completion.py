"""Controlled tool/HTTP inputs for 00:40 fixes; no live inference or provider requests."""
import copy
import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from nextask.core import caselog, recovery
from nextask.core._infra_client import ZoroClient, ZoroUnavailable
from nextask.core.engine import Case, Engine
from nextask.core.matching import Entry, Registry, SynDict
from nextask.core.plugin import Item
from nextask.core.plugins import supplement as s
from nextask.core.sources import PubChemSynonyms

FULL = ["Supplement Facts", "Serving Size 1 Capsule", "Vitamin C 100 mg", "Other ingredients: Rice Flour."]


class InspectionCompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for context in [patch.object(caselog, "LOG_DIR", self.tmp.name),
                        patch.dict(os.environ, {"NEXTASK_AGENT_RECOVERY": "1", "NEXT_POLICY": "agent", "FIXED_PIPELINE": ""}),
                        patch.object(s, "REG", Registry([Entry("yohimbe", en="Yohimbe"), Entry("deanol", en="Deanol", etc=["DMAE"])], source="explicit test registry")),
                        patch.object(s, "DICTS", [])]:
            context.start()
            self.addCleanup(context.stop)
        previous = getattr(caselog.CURRENT, "case_id", None)
        self.addCleanup(setattr, caselog.CURRENT, "case_id", previous)
        self.plugin = s.Supplement()
        self.case = Case(self.plugin, case_id=self._testMethodName)
        self.case.data["input"] = {"url": "test://current", "title": "Controlled product", "role": "consumer"}
        self.decide = Mock(return_value=({"answers": {"same": {"probabilities": {"c1": .01, "c2": .97, "none": .02}}}},
                                       {"source": "explicit local fixture", "system": 1, "latency_ms": 0}))
        self.engine = Engine(self.plugin, zoro=SimpleNamespace(decide=self.decide))

    def ingredient(self, name="Microcrystalline cellulose"):
        self.item = Item("ingredient", self.plugin.ING, name, data={"name": name, "line": name,
                         "candidates": ["yohimbe"], "decided_by": "zoro"}, probs={"yohimbe": .184, "none": .816})
        self.case.items = [self.item]
        return self.item

    def run_verification(self, response=None, error=None, actions=("verify_none",)):
        with patch.object(s.PUBCHEM, "lookup", return_value=response, side_effect=error) as lookup, \
             patch.object(recovery, "generate", side_effect=[{"text": json.dumps({"action": a}), "tokens": 12} for a in actions]):
            self.engine._item_loop(self.case, self.item)
        return lookup

    def record(self, canonical="Cellulose", aliases=None):
        return {"query": self.item.data["name"], "canonical": canonical,
                "aliases": aliases if aliases is not None else [self.item.data["name"], "Avicel"],
                "source": "explicit PubChem response fixture", "cid": 123, "lookup_complete": True}

    def test_low_none_needs_successful_identity_lookup_before_code_clear(self):
        self.ingredient()
        assert self.item.probs is not None
        original = dict(self.item.probs)
        self.run_verification(self.record())
        self.assertEqual((self.item.status, self.item.result), ("resolved", "clear"))
        self.assertEqual(self.item.probs, original)
        self.assertEqual(self.item.data["decided_by"], "alias_exclusion")
        self.assertEqual(self.item.spec.theta_hi, .85)
        self.assertEqual(self.item.tools_done, ["ingredient_alias_verify"])
        self.assertIn("코드", self.item.data["card"]["decided_by_label"])
        self.assertEqual(self.case.data["agent_state"]["missing"], [])

    def test_missing_empty_incomplete_and_wrong_identity_are_not_negative_evidence(self):
        for kind in ("missing", "empty", "incomplete", "wrong"):
            with self.subTest(kind=kind):
                self.ingredient("2-Aminoisoheptane")
                result = self.record()
                if kind == "missing": result = None
                elif kind == "empty": result["aliases"] = []
                elif kind == "incomplete": result.pop("lookup_complete")
                else: result["query"] = "unrelated"
                self.run_verification(result)
                self.assertEqual(self.item.status, "handoff")
                self.assertIsNone(self.item.result)

    def test_lookup_failure_is_not_retried_or_reclassified_as_clear(self):
        self.ingredient()
        lookup = self.run_verification(error=TimeoutError("explicit timeout fixture"))
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(self.item.status, "handoff")
        self.assertIsNone(self.item.result)

    def test_legacy_snapshot_without_completeness_cannot_prove_exclusion(self):
        self.ingredient()
        dictionary = SynDict()
        dictionary["microcrystalline cellulose"] = "Cellulose"
        dictionary.syns["Cellulose"] = {"cellulose", "microcrystalline cellulose"}
        with patch.object(s, "DICTS", [("PubChem", dictionary)]):
            self.run_verification(None)
        self.assertEqual(self.item.status, "handoff")
        self.assertIsNone(self.item.result)

    def test_new_alias_adds_unoffered_official_candidate_and_rejudges(self):
        self.ingredient("Fibre preparation")
        self.run_verification(self.record("Deanol"))
        self.assertIn("deanol", self.item.data["candidates"])
        self.assertEqual((self.item.status, self.item.result), ("resolved", "blocked"))
        self.assertTrue(self.item.spec.evidence(self.item)[0])
        self.assertEqual(self.decide.call_count, 1)

    def test_model_none_cannot_override_new_positive_alias_evidence(self):
        self.ingredient("Fibre preparation")
        self.decide.return_value = ({"answers": {"same": {"probabilities": {"c1": .005, "c2": .005, "none": .99}}}},
                                    {"source": "explicit local fixture", "system": 1, "latency_ms": 0})
        self.run_verification(self.record("Deanol"), actions=("verify_none", "stop"))
        self.assertEqual(self.item.status, "resolved")
        self.assertEqual(self.item.result, "blocked")

    def test_plain_ingredients_list_preserves_parenthesized_components(self):
        lines = [{"text": "Ingredients: Fish oil (anchovy, sardine), gelatin, glycerin."}]
        self.assertEqual(s.parse_ingredients(lines), ["Fish oil (anchovy, sardine)", "gelatin", "glycerin"])

    def label(self, lines=FULL, source="dom", url="test://current"):
        return {"source": source, "source_url": url, "status": "read", "mean_conf": .99,
                "lines": [{"text": text, "conf": .99} for text in lines]}

    def gate(self):
        self.case.data["label"] = self.label()
        gate = Item("label", self.plugin.LABEL, "Label", probs={"readable": .99, "unreadable": .01},
                    data={"coverage_probs": {"complete": .99}})
        self.case.items = [gate]
        return gate

    def test_actual_capture_answer_preserves_complete_dom_and_both_sources(self):
        gate = self.gate()
        original = copy.deepcopy(self.case.data["label"])
        gate.data["_pending_action"] = "capture"
        self.case.image_path = "controlled-capture.png"
        partial = self.label(FULL[:-1], source="ocr")
        with patch.object(self.engine, "_run_tool", return_value=partial):
            outcome = s._next_answer(self.engine, self.case, gate, True)
        self.assertEqual(outcome, "noop")
        self.assertEqual(self.case.data["label"], original)
        self.assertEqual([row["label"]["source"] for row in self.case.data["label_sources"]], ["dom", "ocr"])
        self.assertTrue(self.plugin.LABEL.completion_guard(self.case, gate)["ready_for_confirmation"])
        event = next(e for e in caselog.read_case(self.case.id) if e["event"] == "label_selection")
        self.assertIn("other", event["lost_sections"])
        self.assertFalse(event["selected"])

    def test_fuller_current_source_replaces_partial_without_reusing_seller_answer(self):
        gate = self.gate()
        self.case.data["label"] = self.label(FULL[:-1])
        gate.data["photo_probs"] = {"usable": .99}
        old = self.plugin.LABEL.completion_guard(self.case, gate)["evidence_id"]
        gate.data["confirmed_evidence_id"] = old
        self.assertTrue(gate.spec.apply_tool(self.case, gate, "ocr_capture", self.label(source="ocr")))
        current = gate.spec.completion_guard(self.case, gate)
        self.assertNotEqual(current["evidence_id"], old)
        self.assertTrue(current["complete"])
        self.assertEqual(current["confirmation_by"], "code")
        self.assertTrue(current["ready_for_confirmation"])

    def test_other_product_or_low_quality_cannot_replace_current_label(self):
        gate = self.gate()
        original = copy.deepcopy(self.case.data["label"])
        for incoming in (self.label(FULL + ["DMAE 100 mg"], url="test://other"),
                         {**self.label(FULL + ["DMAE 100 mg"], source="ocr"), "mean_conf": .2}):
            self.assertFalse(gate.spec.apply_tool(self.case, gate, "ocr_capture", incoming))
            self.assertEqual(self.case.data["label"], original)

    def completed_review(self, classification):
        gate = self.gate()
        gate.data["confirmed_evidence_id"] = gate.spec.completion_guard(self.case, gate)["evidence_id"]
        gate.status, gate.result = "resolved", "readable"
        ing = Item("ingredient", self.plugin.ING, "Rice Flour", data={"name": "Rice Flour", "line": "Rice Flour", "candidates": []},
                   probs={"none": 1.0}, status="resolved", result="clear")
        self.case.items = [gate, classification, ing]
        self.case.data["checks"] = s.run_checks(self.case, classification, ["Rice Flour"])

    def test_uncertain_category_is_advisory_but_drug_suspect_still_requires_review(self):
        for kind, verdict in (("unknown", "not_found"), ("cosmetic", "not_found"), ("drug_suspect", "review")):
            with self.subTest(kind=kind):
                classification = Item("classify", self.plugin.CLASSIFY, "Classification", probs={kind: .6, "general_food": .4},
                                      data={"form_code": "other", "answers": {}}, status="resolved", result=kind)
                self.completed_review(classification)
                self.assertEqual(self.plugin.verdict(self.case)[0], verdict)

    def test_classification_transport_failure_does_not_override_completed_ingredient_review(self):
        classification = Item("classify", self.plugin.CLASSIFY, "Classification", status="handoff",
                              data={"server_error": "explicit fixture timeout"}, reason="Classification unavailable")
        self.completed_review(classification)
        self.assertEqual(self.plugin.verdict(self.case)[0], "not_found")
        self.assertTrue(self.case.data["checks"][0]["advisory"])


class ProviderBoundaryTests(unittest.TestCase):
    def test_complete_pubchem_lookup_does_not_truncate_31st_alias(self):
        names = ["Canonical", "Current label"] + [f"Alias {n}" for n in range(30)] + ["DMAE"]
        response = {"InformationList": {"Information": [{"CID": 123, "Synonym": names}]}}
        with patch.dict(os.environ, {"PUBCHEM_LIVE": "1"}), \
             patch("nextask.core.sources.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())):
            result = PubChemSynonyms().lookup("Current label", complete=True)
        self.assertIsNotNone(result)
        self.assertIn("DMAE", result["aliases"])
        self.assertTrue(result["lookup_complete"])

    def test_ambiguous_or_unlinked_pubchem_record_is_not_complete_identity(self):
        for rows in ([{"CID": 123, "Synonym": ["Unrelated"]}],
                     [{"CID": 123, "Synonym": ["Current label"]}, {"CID": 456, "Synonym": ["Current label"]}]):
            with patch.dict(os.environ, {"PUBCHEM_LIVE": "1"}), \
                 patch("nextask.core.sources.urllib.request.urlopen", return_value=io.BytesIO(json.dumps({"InformationList": {"Information": rows}}).encode())):
                self.assertIsNone(PubChemSynonyms().lookup("Current label", complete=True))

    def test_s2_has_separate_read_timeout_without_changing_s1(self):
        with patch.dict(os.environ, {"ZORO_REASONING_TIMEOUT": "120"}):
            client = ZoroClient(urls=["http://fixture.invalid"], timeout=7, retries=3)
        with patch("nextask.core._infra_client.urlopen", side_effect=[io.BytesIO(b"{}"), io.BytesIO(b"{}")]) as post:
            client._post(client.urls[0], "/v1/decide", {"request": {}, "reasoning": True}, "fixture")
            client._post(client.urls[0], "/v1/systemone", {}, "fixture")
        self.assertEqual([call.kwargs["timeout"] for call in post.call_args_list], [120, 7])

    def test_s2_unknown_completion_does_not_duplicate_generation_after_timeout(self):
        client = ZoroClient(urls=["http://first.invalid", "http://second.invalid"], retries=3)
        with patch.object(client, "_post", side_effect=TimeoutError("explicit transport fixture")) as post:
            with self.assertRaises(ZoroUnavailable) as error:
                client.call("/v1/decide", {"request": {}, "reasoning": True})
        self.assertEqual(post.call_count, 1)
        self.assertIn(error.exception.attempts[0]["request_id"], str(error.exception))


if __name__ == "__main__":
    unittest.main()
