"""Explicit local decision/tool fixtures; not live-model or image accuracy measurements."""
import asyncio
import base64
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


from nextask.core import caselog, entry, recovery, store
from nextask.core.engine import Case, Engine
from nextask.core.matching import Entry, Registry
from nextask.core.plugin import Item
from nextask.core.plugins import supplement
from nextask.core.web import app as web
from nextask.core.zoro import Zoro

FULL = ["Supplement Facts", "Serving Size 1 Capsule", "DMAE 250 mg", "Other ingredients: Rice Flour."]


def post_json(path, payload):
    """Exercise the actual ASGI HTTP route without an undeclared httpx dependency."""
    async def invoke():
        events = []
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                 "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "",
                 "query_string": b"", "headers": [(b"content-type", b"application/json")],
                 "client": ("127.0.0.1", 12345), "server": ("testserver", 80)}

        async def receive():
            return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}

        async def send(event):
            events.append(event)

        await web.app(scope, receive, send)
        return SimpleNamespace(status_code=next(e["status"] for e in events if e["type"] == "http.response.start"))

    return asyncio.run(invoke())


def generated(action, tokens=12):
    return {"text": json.dumps({"action": action}), "tokens": tokens}


def lookup(query, canonical, aliases=()):
    return {"query": query, "canonical": canonical, "aliases": list(aliases), "source": "explicit local lookup fixture"}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in [patch.dict(os.environ, {"NEXTASK_AGENT_RECOVERY": "1", "NEXT_POLICY": "agent", "FIXED_PIPELINE": ""}),
                  patch.object(caselog, "LOG_DIR", self.tmp.name),
                  patch.object(store, "PATH", str(Path(self.tmp.name) / "store.json")),
                  patch.object(supplement, "REG", Registry([Entry("deanol", en="Deanol", etc=["DMAE"])], source="test registry")),
                  patch.object(supplement, "DICTS", []),
                  patch.object(supplement.PUBCHEM, "lookup", return_value=None),
                  patch.object(supplement, "i2715_lookup", return_value={"status": "none", "rows": [],
                               "source": "explicit local fixture", "reason": "No matching product history in fixture"})]:
            p.start()
            self.addCleanup(p.stop)
        previous = getattr(caselog.CURRENT, "case_id", None)
        self.addCleanup(setattr, caselog.CURRENT, "case_id", previous)
        self.plugin = supplement.Supplement()
        self.plugin.ING, self.plugin.LABEL = supplement.Ingredient(), supplement.LabelSource()
        self.engine = Engine(self.plugin, zoro=Zoro(mode="mock"))
        self.engine.zoro.recordings = {}
        self.photo_probability = .99
        self.head_action = "invent_source"
        self.judge = Mock(side_effect=self.decide)
        p = patch.object(self.engine.zoro, "decide", self.judge)
        p.start()
        self.addCleanup(p.stop)
        self.case = Case(self.plugin, case_id=self._testMethodName)
        self.case.data["input"] = {"url": "test://current", "title": "Example supplement"}
        self.item = Item("ing:1", self.plugin.ING, "ingredient", data={"name": "Mystery ingredient", "line": "Mystery ingredient 100 mg",
                            "candidates": ["deanol"], "decided_by": "zoro"}, probs={"deanol": .6, "none": .4})
        self.case.items = [self.item]

    def decide(self, request, mock_fn=None, reasoning=False):
        if "main" in request["questions"] and "candidate_names" in request.get("state", {}):
            options = request["questions"]["main"]["criteria"]
            return {"answers": {"main": {"choice": self.head_action,
                    "probabilities": {k: float(k == self.head_action) for k in options}}}}, {"source": "explicit-local-fixture"}
        if "same" in request["questions"]:
            probs = {"same": {"c1": .97, "none": .03}}
        else:
            assert mock_fn is not None
            probs = mock_fn(request)
            if "photo" in probs:
                probs["photo"] = {"usable": self.photo_probability, "unusable": 1 - self.photo_probability}
        return {"answers": {k: {"probabilities": p} for k, p in probs.items()}}, {
            "system": 1, "source": "explicit-local-fixture", "latency_ms": 0}

    def run_actions(self, actions, resolver=None):
        resolver = resolver or (lambda name: lookup(name, "Deanol", ["Mystery ingredient"]))
        with patch.object(recovery, "generate", side_effect=actions) as gen, \
                patch.object(supplement.ALIAS, "lookup", side_effect=resolver):
            self.engine._item_loop(self.case, self.item)
        return gen

    def test_unconfirmed_ingredient_generation_lookup_rejudgment_then_code_block(self):
        self.run_actions([generated("alias")])
        self.assertEqual((self.item.status, self.item.result), ("resolved", "blocked"))
        self.assertEqual(self.item.tools_done, ["ingredient_alias_lookup"])
        self.assertTrue(self.item.spec.evidence(self.item)[0])
        self.assertEqual(self.item.data["_generation_steps"], 1)
        events = caselog.read_case(self.case.id)
        stages = [e["stage"] for e in events if e["event"] == "agent_stage"]
        self.assertIn("generation", stages)
        self.assertIn("judgment", stages)
        self.assertEqual(stages[-1], "code_check")
        self.assertEqual(self.case.data["agent_state"]["missing"], [])
        self.assertEqual(web.to_sse(next(e for e in events if e["event"] == "agent_stage"))[0], "step")

    def test_candidate_only_lookup_never_becomes_identity_proof(self):
        resolver = lambda name: lookup(name, "Deanol" if name == "Deanol" else None)
        self.run_actions([generated("alias"), generated("stop")], resolver)
        self.assertEqual(self.item.status, "handoff")
        self.assertIsNone(self.item.result)
        self.assertFalse(self.item.spec.evidence(self.item)[0])

    def test_observed_alternate_requires_two_way_rooted_identity(self):
        def resolver(name):
            if name == "Mystery ingredient":
                return lookup(name, "Observed alternate", ["Mystery ingredient"])
            if name == "Observed alternate":
                return lookup(name, "Deanol", ["Mystery ingredient"])
            return lookup(name, "Deanol")
        self.run_actions([generated("alias"), generated("alias_alternate")], resolver)
        self.assertEqual((self.item.status, self.item.result), ("resolved", "blocked"))
        self.assertEqual(self.item.tools_done, ["ingredient_alias_lookup", "ingredient_alias_alternate"])
        self.assertEqual(self.item.data["label_alias_hits"][-1]["query"], "Observed alternate")

    def test_alternate_without_reverse_link_cannot_confirm(self):
        def resolver(name):
            return lookup(name, "Observed alternate" if name == "Mystery ingredient" else "Deanol")
        self.run_actions([generated("alias"), generated("alias_alternate"), generated("stop")], resolver)
        self.assertEqual(self.item.status, "handoff")
        self.assertFalse(self.item.spec.evidence(self.item)[0])

    def test_invalid_action_has_only_one_repair_then_handoff(self):
        gen = self.run_actions([generated("approve_sale"), generated("invent_source")])
        self.assertEqual(gen.call_count, 1)
        self.assertEqual(self.item.status, "handoff")
        self.assertEqual(self.item.tools_done, [])

    def test_single_invalid_action_can_recover_to_real_tool(self):
        self.head_action = "alias"
        self.run_actions([generated("invent_source")])
        self.assertEqual((self.item.status, self.item.result), ("resolved", "blocked"))
        self.assertTrue(self.item.data["_generation_reasked"])
        self.assertEqual(self.item.data["_generation_steps"], 2)

    def test_cap_hit_even_valid_json_is_not_executed_or_repaired(self):
        gen = self.run_actions([generated("alias", 64)])
        self.assertEqual(gen.call_count, 1)
        self.assertEqual(self.item.tools_done, [])
        self.assertEqual(self.item.status, "handoff")
        self.assertIn("completion_unverified", self.item.reason)

    def test_resumed_exhausted_budget_cannot_generate_again(self):
        self.item.data["_generation_steps"] = 4
        gen = self.run_actions([])
        gen.assert_not_called()
        self.assertEqual(self.item.status, "handoff")

    def test_already_attempted_action_is_rejected_without_tool_repetition(self):
        self.item.data["_recovery_actions"] = ["alias"]
        gen = self.run_actions([generated("alias"), generated("alias")])
        self.assertEqual(gen.call_count, 1)
        self.assertEqual(self.item.tools_done, [])
        self.assertEqual(self.item.status, "handoff")

    def test_generation_can_request_seller_but_does_not_answer_for_them(self):
        self.run_actions([generated("ask")])
        self.assertEqual(self.item.status, "waiting_user")
        assert self.case.pending is not None
        self.assertEqual(self.case.pending[1].id, "q_label_alias")
        self.assertNotIn("seller_alias_confirmation", self.item.data)
        self.assertIsNone(self.item.result)

    def test_failed_coverage_does_not_stop_bounded_comparison_or_certify_full_scope(self):
        self.case.data["label"] = {"source": "dom", "source_url": "test://current",
                                   "lines": [{"text": t} for t in FULL]}
        self.item = Item("label", self.plugin.LABEL, "label", probs={"readable": .99, "unreadable": .01})
        self.case.items = [self.item]
        self.item.data["coverage_probs"] = {"complete": .2, "incomplete": .8}
        guard = self.item.spec.completion_guard(self.case, self.item)
        self.item.data["confirmed_evidence_id"] = guard["evidence_id"]
        self.run_actions([generated("stop")])
        self.assertEqual((self.item.status, self.item.result), ("resolved", "readable"))
        self.assertFalse(self.item.spec.completion_guard(self.case, self.item)["complete"])
        self.assertFalse(self.item.spec.recovery_supported)

    def supplied_photo(self, actions):
        image = Path(self.tmp.name) / "transport-fixture.png"
        image.write_bytes(b"Explicit local image transport fixture, not an image accuracy test")
        raw = {"engine": "fixture", "lines": [{"text": t, "conf": .99} for t in FULL],
               "mean_conf": .99, "repeat_ratio": 0}
        with patch.object(recovery, "generate", side_effect=actions), \
                patch.object(supplement.OCR, "read", return_value=copy.deepcopy(raw)), \
                patch.object(supplement, "read_dom", side_effect=AssertionError("seller image cannot use DOM")):
            return self.engine.start(image_path=str(image), inputs={"url": "test://current", "title": "DMAE Capsules",
                                     "image_origin": "seller", "page_text": "unrelated DOM text"})

    def test_seller_photo_uses_image_judgment_and_requires_scope_answer(self):
        case = self.supplied_photo([])
        self.assertEqual(case.status, "done")
        self.assertIsNone(case.pending)
        self.assertEqual(case.data["label"]["source_description"], "판매자가 준 사진")
        gate = case.items[0]
        self.assertGreaterEqual(gate.data["photo_probs"]["usable"], .85)
        guard = self.plugin.LABEL.completion_guard(case, gate)
        self.assertTrue(guard["complete"])
        self.assertEqual(guard["confirmation_by"], "code")
        self.assertTrue(guard["evidence_id"])
        self.assertTrue(case.data["self_answers"][0]["evidence_lines"])
        self.assertTrue(any("image" in call.args[0] and "photo" in call.args[0]["questions"] for call in self.judge.call_args_list))
        self.assertEqual(case.verdict["key"], "blocked")

    def test_unusable_seller_photo_does_not_complete_despite_good_ocr(self):
        self.photo_probability = .1
        case = self.supplied_photo([])
        self.assertEqual(case.status, "waiting_user")
        self.assertEqual(case.pending[1].kind, "capture_region")
        self.engine.answer(case, None)
        self.assertEqual(case.status, "done")
        self.assertFalse(self.plugin.LABEL.completion_guard(case, case.items[0])["complete"])
        assert case.verdict is not None
        self.assertEqual(case.verdict["key"], "review")

    def test_direct_official_alias_skips_generation_even_if_model_says_none(self):
        self.item.data["name"] = "DMAE"
        self.item.probs = {"deanol": .01, "none": .99}
        gen = self.run_actions([])
        gen.assert_not_called()
        self.assertEqual((self.item.status, self.item.result), ("resolved", "blocked"))

    def test_invalid_action_only_hands_off_current_ingredient(self):
        known = Item("ing:known", self.plugin.ING, "DMAE", status="resolved", result="blocked",
                     data={"name": "DMAE", "entry_id": "deanol", "candidates": ["deanol"]},
                     probs={"deanol": 1.0, "none": 0.0})
        self.case.items.append(known)
        self.run_actions([generated("2023070077")])
        self.assertEqual(self.item.status, "handoff")
        self.assertEqual(known.result, "blocked")
        self.assertEqual(self.plugin.verdict(self.case)[0], "blocked")

    def test_disabled_feature_keeps_existing_item_loop(self):
        self.case.data["agent_recovery_enabled"] = False
        with patch.object(recovery, "generate", side_effect=AssertionError("must not generate")), \
                patch.object(supplement.ALIAS, "lookup", side_effect=lambda n: lookup(n, "Deanol", ["Mystery ingredient"])):
            self.engine._item_loop(self.case, self.item)
        self.assertNotIn("_generation_steps", self.item.data)
        self.assertEqual(self.item.result, "blocked")


class EntryContractTests(unittest.TestCase):
    def setUp(self):
        self.context = {"url": "test://current", "case_id": "fixture", "case_status": "done", "same_product": True, "has_image": True}

    def choose(self, route, confidence=.98, outside=.01, context=None):
        probabilities = {k: (1 - confidence) / (len(entry.ROUTES) - 1) for k in entry.ROUTES}
        probabilities[route] = confidence
        raw = {"answers": {"route": {"probabilities": probabilities},
                           "boundary": {"probabilities": {"inspection": 1 - outside, "outside": outside}}}}
        engine = SimpleNamespace(zoro=SimpleNamespace(decide=lambda req: (raw, {"source": "explicit-local-fixture"})))
        return entry.choose(engine, "controlled request fixture", context or self.context)[0]

    def test_supported_routes_are_closed_choices_not_generated_answers(self):
        for route in ("start", "capture_region", "photo", "explain", "recheck"):
            with self.subTest(route=route):
                result = self.choose(route)
                self.assertEqual(result["route"], route)
                self.assertTrue(result["ready"])
                self.assertNotIn("answer", result)

    def test_outside_authority_blocks_even_high_start_score(self):
        result = self.choose("start", outside=.99)
        self.assertEqual(result["route"], "unsupported")
        self.assertFalse(result["ready"])

    def test_low_score_and_uncertain_authority_do_not_start(self):
        for confidence, outside in ((.6, .01), (.98, .4)):
            result = self.choose("start", confidence, outside)
            self.assertEqual(result["route"], "clarify")
            self.assertFalse(result["ready"])

    def test_missing_photo_is_an_upload_request_not_reading_success(self):
        result = self.choose("photo", context={**self.context, "has_image": False})
        self.assertEqual(result["route"], "photo")
        self.assertFalse(result["ready"])
        self.assertIn("붙여", result["answer"])

    def test_previous_product_result_cannot_authorize_recheck_or_explanation(self):
        for route in ("recheck", "explain"):
            result = self.choose(route, context={**self.context, "same_product": False})
            self.assertEqual(result["route"], "clarify")
            self.assertFalse(result["ready"])

    def test_transport_failure_and_invalid_probabilities_are_not_actions(self):
        for result in (RuntimeError("fixture outage"), ({"answers": {}}, {"source": "fixture"})):
            decide = Mock(side_effect=result if isinstance(result, Exception) else None,
                          return_value=result if not isinstance(result, Exception) else None)
            value, observation = entry.choose(SimpleNamespace(zoro=SimpleNamespace(decide=decide)), "inspect", self.context)
            self.assertEqual(value["route"], "clarify")
            self.assertFalse(value["ready"])
            self.assertIn("error", observation)

    def test_photo_api_preserves_origin_and_requires_page_link(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(web, "UPLOAD_DIR", tmp), \
                patch.object(web, "_run_bg") as run, patch.object(web.caselog, "subscribe"), \
                patch.dict(web.CASES, {}, clear=True):
            image = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nexplicit transport fixture").decode()
            bad = post_json("/cases", {"images": [image], "image_origin": "seller"})
            self.assertEqual(bad.status_code, 400)
            run.assert_not_called()
            response = post_json("/cases", {"url": "test://current", "images": [image], "image_origin": "seller"})
            self.assertEqual(response.status_code, 200)
            with patch.object(web.ENGINE, "start", return_value=SimpleNamespace()) as start:
                run.call_args.args[1]()
                self.assertEqual(start.call_args.kwargs["inputs"]["image_origin"], "seller")

    def test_seller_photo_rejects_remote_url_bad_signature_and_oversize(self):
        with patch.object(web, "_run_bg") as run:
            for image in ("https://example.invalid/photo.png", "data:image/png;base64,ZmFrZQ=="):
                response = post_json("/cases", {"url": "test://current", "images": [image], "image_origin": "seller"})
                self.assertEqual(response.status_code, 400)
            with patch.object(web, "SELLER_IMAGE_MAX_BYTES", 4):
                response = post_json("/cases", {"url": "test://current", "image_origin": "seller",
                    "images": ["data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()]})
                self.assertEqual(response.status_code, 413)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
