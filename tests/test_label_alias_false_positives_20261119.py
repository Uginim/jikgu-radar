"""Official-row regressions at the ingredient-to-product decision boundary (no model calls)."""
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from nextask.core import caselog
from nextask.core.engine import Case, Engine
from nextask.core.matching import Registry
from nextask.core.plugin import Item
from nextask.core.plugins import supplement
from nextask.core.zoro import Zoro


ROWS = Path(__file__).resolve().parents[1] / 'data/mfds/foodsafety_block_raw_irdnt_318_20261119.json'
TIANEPTINE, DEANOL, L_DOPA = '2023070025', '2023070259', '2023070011'


class LabelAliasRegressions(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        stack.enter_context(patch.object(caselog, 'LOG_DIR', tmp))
        stack.enter_context(patch.object(caselog.CURRENT, 'case_id', None, create=True))
        stack.enter_context(patch.dict(os.environ, {'FIXED_PIPELINE': ''}))
        self.rows = Registry.load(ROWS)
        self.plugin = supplement.Supplement()
        self.engine = Engine(self.plugin, zoro=Zoro(mode='mock'))
        self.case = Case(self.plugin)
        # Isolate the rule or finalist under test, independent of live enrichment and ranking.
        self.stack = stack
        stack.enter_context(patch.object(supplement, 'DICTS', []))

    def label(self, row_id, *lines):
        self.stack.enter_context(patch.object(supplement, 'REG', Registry([self.rows.by_id[row_id]])))
        gate = Item('label', self.plugin.LABEL, 'label')
        self.case.items = [gate]
        self.case.data['label'] = {'lines': [{'text': s} for s in lines]}
        self.plugin._add_ingredients(self.case, gate)
        return {it.data['name']: it for it in self.case.items if it.spec is self.plugin.ING}

    def propose_block(self, item, candidate):
        # Replay a high-confidence wrong option at the final guard, not the transport or a new inference.
        item.probs = {candidate: .94, 'none': .06}
        item.n_zoro = 1
        self.engine._close(self.case, item, 'resolved', 'candidate from model', 'blocked')

    def test_lookup_of_tianeptine_itself_cannot_identify_aminoisoheptane(self):
        name = '2-Aminoisoheptane'  # DSLD 209976; reported wrong finalist, not an oracle for another row.
        item = self.label(TIANEPTINE, name + ' 100 mg')[name]
        self.plugin.ING.apply_tool(self.case, item, 'ingredient_alias_lookup', {'hits': [
            {'query': 'Tianeptine', 'canonical': 'Tianeptine', 'aliases': [], 'source': 'candidate-only lookup'}]})
        self.propose_block(item, TIANEPTINE)
        self.assertEqual(self.plugin.verdict(self.case)[0], 'review')

    def test_other_ingredients_keep_their_own_matches(self):
        # Deliberate co-location: even a listed alias in Other ingredients must not implicate its neighbour.
        items = self.label(DEANOL, 'Vitamin C 100 mg', 'Other ingredients: Rice Flour, DMAE')
        self.assertEqual(items['DMAE'].data['card']['entry']['id'], DEANOL)
        self.propose_block(items['Rice Flour'], DEANOL)
        self.assertEqual(self.plugin.verdict(self.case)[0], 'blocked')
        blocked_names = {row['name'] for row in self.plugin.comparison(self.case)['rows'] if row['ours'] == 'blocked'}
        self.assertEqual(blocked_names, {'DMAE'})

    def test_mucuna_leaf_cannot_inherit_the_seed_alias(self):
        name = 'Mucuna pruriens leaf extract'  # Part-swapped control, not a claim about a marketed product.
        item = self.label(L_DOPA, name + ' 100 mg')[name]
        self.propose_block(item, L_DOPA)
        self.assertEqual(self.plugin.verdict(self.case)[0], 'review')

    def test_explicit_mucuna_seed_alias_retains_official_row(self):
        name = 'Mucuna pruriens seed powder'
        item = self.label(L_DOPA, name + ' 100 mg')[name]
        self.assertEqual(self.plugin.verdict(self.case)[0], 'blocked')
        self.assertEqual(item.data['card']['entry']['id'], L_DOPA)
