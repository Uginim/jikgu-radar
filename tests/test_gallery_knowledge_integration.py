"""Recorded OCR text and explicit local tool results; no GPU/API calls."""
import copy
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from nextask.core import caselog, ingredient_knowledge, recovery
from nextask.core._infra_client import ZoroClient, ZoroUnavailable
from nextask.core.engine import Case, Engine
from nextask.core.matching import Entry, Registry
from nextask.core.plugin import Item, Question
from nextask.core.plugins import supplement as s
from nextask.core.web import app as web

FIXTURES = json.loads((Path(__file__).parent / "fixtures/gallery-ocr-lines.json").read_text())
FULL = ["Supplement Facts", "Serving Size 1 Capsule", "Vitamin D 40 mcg", "Other ingredients: Rice Flour."]


def lines(texts):
    return [{"text": text} for text in texts]


class GalleryKnowledgeIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.plugin = s.Supplement()
        self.registry = Registry([Entry("deanol", en="Deanol", etc=["DMAE"]), Entry("yohimbe", en="Yohimbe")], source="explicit local registry")
        for context in [patch.object(caselog, "LOG_DIR", str(self.tmp)), patch.object(s, "REG", self.registry),
                        patch.object(s, "DICTS", []), patch.dict(os.environ, {"NEXTASK_AGENT_RECOVERY": "1",
                        "NEXTASK_INGREDIENT_KB": "0", "NEXTASK_SYSTEM2": "0", "NEXT_POLICY": "agent", "FIXED_PIPELINE": ""})]:
            context.start()
            self.addCleanup(context.stop)
        prior = getattr(caselog.CURRENT, "case_id", None)
        self.addCleanup(setattr, caselog.CURRENT, "case_id", prior)
        self.case = Case(self.plugin, case_id=self._testMethodName)
        self.case.data["input"] = {"url": "test://current", "title": "Controlled product", "role": "consumer"}
        self.engine = Engine(self.plugin, zoro=SimpleNamespace(mode="mock"))

    def label(self, texts=FULL, source="dom", **extra):
        return {"source": source, "source_url": "test://current", "lines": lines(texts),
                "status": "read", "mean_conf": .99 if source == "ocr" else None, "repeat_ratio": 0, **extra}

    def gate(self):
        gate = Item("label", self.plugin.LABEL, "Label", probs={"readable": .99, "unreadable": .01},
                    data={"coverage_probs": {"complete": .99}, "photo_probs": {"usable": .99}})
        self.case.items = [gate]
        return gate

    def test_nested_units_do_not_cut_ingredient_names(self):
        texts = ["Vitamin A (as beta-carotene, acetate) (5,000 IU) 1,500 mcg",
                 "Vitamin B6 (as pyridoxine HCl) (5 mg) 10 mg", "L-시트룰린 0.04 oz (1,200 mg)"]
        self.assertEqual(s.parse_ingredients(lines(texts)), ["Vitamin A (as beta-carotene, acetate) (5,000 IU)",
                         "Vitamin B6 (as pyridoxine HCl) (5 mg)", "L-시트룰린"])

    def test_recorded_now_wrapped_list_keeps_nested_coating_and_final_ingredients(self):
        names = s.parse_ingredients(FIXTURES["B00KYKLPS2"]["lines"])
        self.assertEqual(names, ["L-Citrulline", "Microcrystalline Cellulose", "Stearic Acid (vegetable source)",
                         "Vegetarian Coating [hypromellose (cellulose), stearic acid (vegetable source), sunflower lecithin, triethyl citrate, sunflower oil]",
                         "Croscarmellose Sodium", "Silicon Dioxide"])

    def test_recorded_d3_preserves_shared_head_and_all_wrapped_ingredients(self):
        names = s.parse_ingredients(FIXTURES["B09T81PN2R"]["lines"])
        self.assertIn("fruit and vegetable juice (color)", names)
        self.assertIn("carnauba wax", names)
        self.assertIn("Vitamin D3 (as cholecalciferol)", names)
        self.assertNotIn("fruit", names)
        self.assertFalse(any("Distributed" in name or "Watsonville" in name for name in names))

    def test_recorded_omega_keeps_actual_total_but_not_cholesterol_sentence(self):
        names = s.parse_ingredients(FIXTURES["B0739KKHWL"]["lines"])
        self.assertIn("Total Omega-3s®", names)
        self.assertIn("purified deep sea fish oil (from anchovies, sardines, mackerel, and herring)", names)
        self.assertNotIn("Less than", names)
        self.assertFalse(any("Calories" in name or "Total Fat" in name for name in names))

    def test_allergen_statement_is_not_an_extra_ingredient(self):
        self.assertEqual(s.parse_ingredients(lines(["Other ingredients: Rice Flour, Steviol Glycosides.",
                         "CONTAINS: Soybeans, Fish (Anchovy)."])), ["Rice Flour", "Steviol Glycosides"])

    def test_korean_list_and_duplicates_keep_declared_ingredients(self):
        self.assertEqual(s.parse_ingredients(lines(["성분: L-시트룰린 및 타피오카시럽.",
                         "Ingredients: L-시트룰린, 타피오카시럽."])), ["L-시트룰린", "타피오카시럽"])

    def test_gallery_ocr_can_replace_one_unverified_dom_declaration_but_still_needs_seller(self):
        self.case.data["label"] = self.label(["Other ingredients: 유기농 타피오카 시럽"])
        self.case.data["input"]["image_origin"] = "gallery"
        self.case.data["gallery_selection"] = {"selected_index": 1}
        incoming = self.label(source="ocr", image_origin="gallery")
        for row in incoming["lines"]:
            row["conf"] = .99
        gate = self.gate()
        self.assertTrue(s.retain_label(self.case, gate, incoming))
        guard = self.plugin.LABEL.completion_guard(self.case, gate)
        self.assertTrue(guard["ready_for_confirmation"])
        self.assertTrue(guard["complete"])
        self.assertEqual(guard["confirmation_by"], "code")
        self.assertTrue(guard["evidence_id"])
        self.assertEqual(self.case.data["label_sources"][0]["label"]["lines"], lines(["Other ingredients: 유기농 타피오카 시럽"]))

    def test_gallery_exception_cannot_drop_a_known_official_block(self):
        self.case.data["label"] = self.label(["Ingredients: DMAE."])
        self.case.data["gallery_selection"] = {"selected_index": 1}
        self.assertFalse(s.retain_label(self.case, self.gate(), self.label(source="ocr", image_origin="gallery")))
        self.assertEqual(s.parse_ingredients(self.case.data["label"]["lines"]), ["DMAE"])

    def test_dose_conflict_vetoes_even_stale_seller_confirmation(self):
        self.case.data["label"] = self.label()
        gate = self.gate()
        s.retain_label(self.case, gate, self.label([text.replace("40 mcg", "50 mcg") for text in FULL], "ocr"))
        guard = self.plugin.LABEL.completion_guard(self.case, gate)
        gate.data["confirmed_evidence_id"] = guard["evidence_id"]
        self.assertFalse(self.plugin.LABEL.completion_guard(self.case, gate)["complete"])
        self.assertFalse(guard["ready_for_confirmation"])
        self.assertEqual(self.case.data["label_amount_conflicts"][0]["name"], "vitamin d")

    def test_unselected_gallery_requests_region_not_an_unrelated_capture(self):
        self.case.data["gallery_selection"] = {"next_action": "request_evidence", "selected_index": None}
        gate = self.gate()
        options = self.plugin.LABEL.next_options(self.case, gate)
        self.assertNotIn("capture", options)
        action = self.plugin.LABEL.next_code_action(self.case, gate, options)
        assert action is not None
        self.assertEqual(action["choice"], "ask")

    def test_readable_span_is_compared_before_low_model_coverage_can_stop_acquisition(self):
        self.case.data["label"] = self.label()
        gate = self.gate()
        gate.probs = {"readable": .7551, "unreadable": .2449}
        gate.data["coverage_probs"] = {"complete": .3}
        gate.n_zoro = 1
        with patch.object(self.engine, "_judge", side_effect=AssertionError("acquisition cannot invoke S2")), \
             patch.dict(os.environ, {"NEXTASK_SYSTEM2": "1"}), \
             patch.object(recovery, "advance", side_effect=AssertionError("label cannot choose generated actions")):
            self.engine._item_loop(self.case, gate)
        self.assertEqual((gate.status, gate.result), ("resolved", "readable"))
        self.assertFalse(self.plugin.LABEL.completion_guard(self.case, gate)["complete"])
        self.assertIsNone(getattr(self.case, "pending"))

    def test_declared_dom_span_does_not_select_or_read_gallery_first(self):
        with patch.object(web, "PLUGIN", self.plugin), \
             patch.object(web, "choose_gallery_image", side_effect=AssertionError("unnecessary gallery call")), \
             patch.object(web, "_run_bg", side_effect=lambda cid, fn: fn()), \
             patch.object(web.ENGINE, "start", return_value=self.case) as start, \
             patch.object(caselog, "subscribe"), patch.dict(web.CASES, {}, clear=True):
            web.create_case({"url": "test://current", "page_text": "\n".join(FULL),
                             "images": [{"url": "unused-gallery-image"}]})
        kwargs = start.call_args.kwargs
        self.assertIsNone(kwargs["image_path"])
        self.assertEqual(kwargs["acquisition"]["selection"]["next_action"], "compare")
        self.assertEqual(kwargs["acquisition"]["selection"]["model_calls"], 0)

    def test_gallery_telemetry_is_counted_before_case_finishes(self):
        selection = {"model_calls": 3, "model_latency_ms": 400, "selected_index": None,
                     "candidates": [], "next_action": "request_evidence", "source": "explicit fixture"}
        with patch.object(self.plugin, "pre_tools", []), patch.object(self.plugin, "make_items", return_value=[]), \
             patch.object(self.engine, "_run", side_effect=lambda case: case):
            case = self.engine.start(inputs=self.case.data["input"], acquisition={"started_at": time.perf_counter() - 1,
                                     "selection": selection})
        stats = self.engine.decision_stats(case)
        self.assertEqual(stats["model_calls"], 3)
        self.assertGreaterEqual(stats["total_ms"], 1000)
        self.assertEqual(case.snapshot()["extras"]["gallery_selection"]["selected_index"], None)

    def test_cases_passes_only_selected_gallery_path_without_pixel_payload_in_inputs(self):
        selection = {"model_calls": 2, "model_latency_ms": 20, "selected_index": 1,
                     "candidates": [{"index": 1, "asset": "https://example.test/back.jpg"}], "next_action": "ocr"}
        def choose(images, **kwargs):
            kwargs["observe"](selection)
            return "/controlled/back.jpg"
        with patch.object(web, "PLUGIN", self.plugin), patch.object(web, "choose_gallery_image", side_effect=choose), \
             patch.object(web, "_run_bg", side_effect=lambda cid, fn: fn()), patch.object(web.ENGINE, "start", return_value=self.case) as start, \
             patch.object(caselog, "subscribe"), patch.dict(web.CASES, {}, clear=True):
            web.create_case({"url": "test://current", "images": [{"url": "front", "data": "secret-pixels"}, {"url": "back"}], "role": "consumer"})
        kwargs = start.call_args.kwargs
        self.assertEqual(kwargs["image_path"], "/controlled/back.jpg")
        self.assertEqual(kwargs["inputs"]["image_origin"], "gallery")
        self.assertNotIn("secret-pixels", json.dumps(kwargs["inputs"]))
        self.assertEqual(kwargs["acquisition"]["selection"], selection)

    def test_page_reread_does_not_retain_gallery_pixels(self):
        gate = self.gate()
        setattr(self.case, "pending", (gate, Question("reread", "read", kind="page_reread")))
        self.case.data["input"]["images"] = [{"data": "old-pixels"}]
        with patch.dict(web.CASES, {self.case.id: self.case}), patch.object(web, "_run_bg", side_effect=lambda cid, fn: fn()), \
             patch.object(web.ENGINE, "answer"):
            web.capture(self.case.id, {"question_id": "reread", "source_url": "test://current",
                                      "page_text": "Ingredients: Rice Flour.", "images": [{"data": "new-pixels"}]})
        self.assertNotIn("images", self.case.data["input"])
        self.assertEqual(self.case.data["input"]["image_candidates_count"], 1)

    def test_reference_candidate_and_seller_answer_do_not_authorize_a_block(self):
        item = Item("ingredient", self.plugin.ING, "Unverified alias", probs={"deanol": .99, "none": .01},
                    data={"name": "Unverified alias", "candidates": ["deanol"], "seller_alias_confirmation": {
                          "name": "Unverified alias", "entry_id": "deanol", "answer": True}, "stages": {"kb_retrieval": {
                          "mode": "candidates", "candidates": [{"id": "deanol", "official": False, "scope_check": False}]}}})
        self.assertFalse(self.plugin.ING.resolvable(item, "blocked"))
        item.data["label_alias_hits"] = [{"query": "Unverified alias", "canonical": "Deanol", "aliases": [], "source": "independent fixture"}]
        self.assertTrue(self.plugin.ING.resolvable(item, "blocked"))
        item.data["stages"]["kb_retrieval"]["candidates"][0]["scope_check"] = True
        self.assertFalse(self.plugin.ING.resolvable(item, "blocked"))

    def test_common_hint_is_not_imported_as_alias_or_negative_proof(self):
        item = Item("ingredient", self.plugin.ING, "Cellulose", data={"name": "Cellulose", "candidates": []})
        result = {"hits": [], "knowledge": {"mode": "candidates", "status": "retrieved", "candidates": [],
                  "common_hint": {"status": "known_identity_no_indexed_collision", "authority": "reference_only"}}}
        self.assertFalse(self.plugin.ING.apply_tool(self.case, item, "ingredient_alias_lookup", result))
        self.assertNotIn("label_alias_hits", item.data)
        self.assertNotIn("alias_verification", item.data)
        self.assertEqual(item.status, "open")

    def test_verified_registry_retrieval_recovers_dmha_but_changed_registry_is_rejected(self):
        path = os.environ.get("MFDS_BLOCKLIST") or str(Path(s.ROOT) / "data/mfds/snapshot/registry_20261119.json")
        official = Registry.load(path)
        with patch.dict(os.environ, {"NEXTASK_INGREDIENT_KB": "1"}):
            result = ingredient_knowledge.retrieve(official, "2-Aminoisoheptane")
            assert result is not None
            self.assertEqual(result["status"], "retrieved")
            candidate = next(row for row in result["candidates"] if row["id"] == "2023070142")
            self.assertFalse(candidate["official"])
            changed = copy.deepcopy(official)
            changed.entries[0].en = "a different official registry revision"
            mismatch = ingredient_knowledge.retrieve(changed, "2-Aminoisoheptane")
            assert mismatch is not None
            self.assertEqual(mismatch["status"], "registry_mismatch")

    def test_initial_knowledge_candidates_do_not_become_an_automatic_block(self):
        path = os.environ.get("MFDS_BLOCKLIST") or str(Path(s.ROOT) / "data/mfds/snapshot/registry_20261119.json")
        with patch.object(s, "REG", Registry.load(path)), patch.dict(os.environ, {"NEXTASK_INGREDIENT_KB": "1"}):
            self.case.data["label"] = self.label(["Ingredients: 2-Aminoisoheptane."])
            gate = self.gate()
            self.plugin._add_ingredients(self.case, gate)
            item = next(item for item in self.case.items if item.spec is self.plugin.ING)
            self.assertIn("2023070142", item.data["candidates"])
            self.assertEqual(item.status, "open")
            self.assertFalse(self.plugin.ING.resolvable(item, "clear"))


    def test_busy_server_waits_then_recovers_with_failure_history(self):
        client = ZoroClient(urls=["http://fixture.invalid"], retries=3)
        errors = [HTTPError("http://fixture.invalid", 503, "busy", {}, io.BytesIO(b'{"error":"queue timeout"}'))
                  for _ in range(2)]
        with patch.object(client, "_post", side_effect=[*errors, {"answers": {"fixture": True}}]) as post, \
             patch("nextask.core._infra_client.time.sleep") as sleep:
            result = client.call("/v1/systemone", {})
        self.assertEqual(post.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])
        self.assertEqual([attempt.get("status") for attempt in result["attempts"]], [503, 503, None])
        self.assertEqual([attempt.get("retry_wait_s") for attempt in result["attempts"]], [2, 4, None])

    def test_timeout_exhaustion_reports_all_three_attempts(self):
        client = ZoroClient(urls=["http://fixture.invalid"], retries=3)
        with patch.object(client, "_post", side_effect=TimeoutError("controlled timeout")) as post, \
             patch("nextask.core._infra_client.time.sleep") as sleep:
            with self.assertRaises(ZoroUnavailable) as caught:
                client.call("/v1/generate", {"prompt": "controlled request"})
        self.assertEqual(post.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])
        self.assertTrue(all("TimeoutError" in attempt["error"] for attempt in caught.exception.attempts))
        self.assertEqual(len(caught.exception.attempts), 3)


if __name__ == "__main__":
    unittest.main()
