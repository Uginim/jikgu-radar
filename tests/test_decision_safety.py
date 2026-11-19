"""Narrow offline regressions: incomplete reasoning cannot finalize a product decision."""
import copy
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from nextask.core import caselog
from nextask.core.engine import Engine
from nextask.core.matching import Entry, zoro_request
from nextask.core.plugin import Item, Plugin, Tool
from nextask.core.zoro import Zoro, ZoroIncomplete, request_key


class DecisionPlugin(Plugin):
    options_map = {"blocked": "same blocked ingredient", "clear": "none of the candidates"}
    labels_map = {"blocked": "차단", "clear": "해당 없음"}
    max_questions = 0


def response(p, **meta):
    return {"answers": {"main": {"probabilities": {"blocked": p, "clear": 1 - p}}}, **meta}


class DecisionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"FIXED_PIPELINE": "", "MFDS_LIVE": "0", "ZORO_MODE": "mock", "NEXTASK_SYSTEM2": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.logs = patch.object(caselog, "LOG_DIR", self.tmp.name)
        self.logs.start()
        self.addCleanup(self.logs.stop)
        previous = getattr(caselog.CURRENT, "case_id", None)
        self.addCleanup(setattr, caselog.CURRENT, "case_id", previous)

    def run_case(self, second, plugin=None, after_tool=None):
        zoro = Zoro(mode="remote")
        replies = [response(.7, answers_source="direct"), second]
        if after_tool:
            replies.append(after_tool)
        with patch.object(zoro.client, "_post", side_effect=replies):
            return Engine(plugin or DecisionPlugin(), zoro=zoro).start("label")

    def test_cut_and_legacy_s2_never_replace_s1_or_finalize(self):
        for meta in ({"answers_source": "direct", "cut": "length", "reasoning_stats": {"finish_reason": "length"}},
                     {"answers_source": "reasoned", "cut": "length", "reasoning": "unfinished", "reasoning_stats": {"finish_reason": "length"}},
                     {"answers_source": "reasoned", "reasoning": "", "reasoning_stats": {"finish_reason": "stop"}},
                     {}):
            with self.subTest(meta=meta):
                c = self.run_case(response(.99, **meta))
                self.assertEqual(c.items[0].status, "handoff")
                self.assertEqual(c.items[0].probs["blocked"], .7)
                self.assertEqual(c.stats["model_calls_system2"], 0)
                self.assertEqual(c.stats["reasoning_attempts"], 1)
                self.assertEqual(c.stats["reasoning_incomplete"], 1)
                self.assertNotIn("system2", [c.items[0].data["ended_by"]])
                self.assertIn("S2 완료 미확인", caselog.path_line(caselog.read_case(c.id)))

    def test_verified_s2_can_finalize_and_is_counted(self):
        c = self.run_case(response(.99, answers_source="reasoned", reasoning="Complete reasoning.",
                                   reasoning_stats={"finish_reason": "stop", "completion_tokens": 8}))
        self.assertEqual(c.items[0].result, "blocked")
        self.assertEqual(c.stats["model_calls_system2"], 1)
        self.assertEqual(c.items[0].data["ended_by"], "system2")

    def test_cut_uses_tool_then_labels_new_direct_answer_s1(self):
        plugin = DecisionPlugin()
        plugin.tools = [Tool("lookup", lambda c, it: {"fact": "verified new evidence"})]
        c = self.run_case(response(.99, answers_source="direct", cut="length"), plugin,
                          response(.02, answers_source="direct"))
        self.assertEqual(c.items[0].result, "clear")
        self.assertEqual(c.items[0].tools_done, ["lookup"])
        self.assertEqual(c.items[0].data["_last_decision"]["system"], 1)
        self.assertEqual(c.stats["model_calls_system2"], 0)

    def test_legacy_recording_does_not_become_verified_s2(self):
        zoro = Zoro(mode="mock")
        req = {"state": "label", "questions": {}}
        zoro.recordings[request_key({**req, "_system": 2})] = response(.99)
        with self.assertRaises(ZoroIncomplete):
            zoro.decide(req, reasoning=True)

    def test_none_option_and_list_evidence_guard(self):
        from nextask.core.plugins import supplement
        reg = SimpleNamespace(by_id={"wrong": Entry("wrong", en="Tianeptine"),
                                     "right": Entry("right", en="Yohimbine", etc=["Yohimbe"])})
        for candidates in ([], ["wrong"]):
            req = zoro_request("2-Aminoisoheptane", reg, candidates)
            self.assertIn("none", req["questions"]["same"]["criteria"])
        spec = supplement.Ingredient()
        it = Item("ingredient", spec, "ingredient", data={"name": "2-Aminoisoheptane"},
                  probs={"wrong": .99, "none": .01})
        with patch.object(supplement, "REG", reg):
            self.assertFalse(spec.resolvable(it, "blocked"))
            it.probs = {"right": .99, "none": .01}
            it.data["name"] = "Yohimbe"
            self.assertTrue(spec.resolvable(it, "blocked"))

    def test_review_candidate_is_not_recommended_as_unblocked(self):
        from nextask.core import altsearch
        c = SimpleNamespace(status="done", data={}, items=[], id="review-case",
                            verdict={"key": "review", "label": "확인 필요"})
        engine = SimpleNamespace(start=lambda **kwargs: copy.deepcopy(c),
                                 zoro=SimpleNamespace(decide=lambda *a, **kw: (
                                     {"answers": {"fit": {"probabilities": {"matches": .99, "no": .01}}}}, {})))
        result = altsearch.rank(engine, {}, [{"title": "Unverified product"}])
        self.assertEqual(result["top"], [])
        self.assertEqual(result["excluded"][0]["excluded_by"], "확인 필요")

    def test_partial_no_match_candidate_is_not_recommended_as_fully_reviewed(self):
        from nextask.core import altsearch
        c = SimpleNamespace(status="done", data={}, items=[], id="partial-case",
                            verdict={"key": "not_found", "label": "읽은 범위에서 찾지 못함"})
        engine = SimpleNamespace(start=lambda **kwargs: copy.deepcopy(c),
                                 zoro=SimpleNamespace(decide=lambda *a, **kw: (
                                     {"answers": {"fit": {"probabilities": {"matches": .99, "no": .01}}}}, {})))
        result = altsearch.rank(engine, {}, [{"title": "Partial product"}])
        self.assertEqual(result["top"], [])
        self.assertFalse(result["excluded"][0]["scope_complete"])


if __name__ == "__main__":
    unittest.main()
