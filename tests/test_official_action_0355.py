"""Safety invariants; model accuracy evidence is in the separate remote run logs."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from nextask.core.matching import Entry, Registry
from nextask.core.plugins import supplement as s
from nextask.core.recovery import choose_action_head

class OfficialActionTests(unittest.TestCase):
    def registry(self, echinacea='Echinacea spp.', kr='에키네시아속'):
        return Registry([Entry('2023070077', en=echinacea, kr=kr), Entry('2023070264', en='Gymnema Sylvestre', kr='당살초')])

    def test_verified_species_matches_genus_scope_only(self):
        with patch.object(s,'REG',self.registry()):
            links=s.verified_taxon_links('Echinacea (Echinacea purpurea (aerial) and Echinacea angustifolia (root))')
            self.assertEqual({x['registry_id'] for x in links},{'2023070077'})
            self.assertEqual(len(links),2)
            self.assertEqual(s.verified_taxon_links('Echinacea Complex'),[])
            self.assertEqual(s.verified_taxon_links('Burdock (Arctium lappa) (root)'),[])

    def test_changed_species_or_part_scope_cannot_be_inferred(self):
        for en,kr in [('Echinacea pallida','에키네시아'),('Echinacea spp. root only','에키네시아속'),('Echinacea spp.','뿌리에 한함')]:
            with self.subTest(en=en,kr=kr),patch.object(s,'REG',self.registry(en,kr)):
                self.assertEqual(s.verified_taxon_links('Echinacea purpurea (aerial)'),[])

    def test_gurmar_not_guar_or_isolated_constituent(self):
        with patch.object(s,'REG',self.registry()):
            self.assertEqual(s.verified_taxon_links('Organic Gurmar Powder')[0]['registry_id'],'2023070264')
            for name in ['Guar powder','gurmarin','Gymnema inodorum']:
                self.assertEqual(s.verified_taxon_links(name),[])

    def head(self,choice,probs):
        decide=Mock(return_value=({'answers':{'main':{'choice':choice,'probabilities':probs}}},{'source':'explicit-unit-fixture'}))
        return SimpleNamespace(zoro=SimpleNamespace(decide=decide))

    def test_head_candidate_id_is_not_action(self):
        req={'questions':{'main':{'criteria':{'alias':'lookup','stop':'handoff'}}}}
        with self.assertRaises(ValueError):
            choose_action_head(self.head('2023070077',{'alias':.9,'stop':.1}),req)

    def test_head_missing_or_nonfinite_probabilities_fail_closed(self):
        req={'questions':{'main':{'criteria':{'alias':'lookup','stop':'handoff'}}}}
        for probs in [{'alias':1.0},{'alias':float('nan'),'stop':.1},{'alias':-.1,'stop':1.1}]:
            with self.subTest(probs=probs),self.assertRaises(ValueError):
                choose_action_head(self.head('alias',probs),req)

    def test_head_valid_action_is_only_a_tool_choice(self):
        req={'questions':{'main':{'criteria':{'alias':'lookup','stop':'handoff'}}}}
        engine=self.head('alias',{'alias':.9,'stop':.1})
        self.assertEqual(choose_action_head(engine,req)[0],'alias')
        self.assertEqual(engine.zoro.decide.call_count,1)
