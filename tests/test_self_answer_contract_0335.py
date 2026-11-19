"""Code invariants only; fixtures are not evidence of real Zoro accuracy."""
import unittest
from nextask.core.plugins.supplement import photo_scope_complete, parse_ingredients

class SelfAnswerContract(unittest.TestCase):
    def label(self, other='Other Ingredients: Water, Alcohol.'):
        return {'lines':[{'text':s,'conf':.99} for s in ['Supplement Facts','Serving Size: 1 capsule','Lobelia Herb 250 mg',other]]}

    def test_cutoff_declaration_is_not_complete(self):
        self.assertFalse(photo_scope_complete(self.label('Other Ingredients: Water,')))
        self.assertFalse(photo_scope_complete(self.label('Other Ingredients: Water (')))

    def test_low_confidence_is_not_complete(self):
        lab=self.label();lab['lines'][2]['conf']=.6
        self.assertFalse(photo_scope_complete(lab))

    def test_wrapped_other_ingredients_complete(self):
        lab=self.label('Other Ingredients: Water, Organic')
        lab['lines'].append({'text':'Alcohol. Suggested Use: one capsule','conf':.99})
        self.assertTrue(photo_scope_complete(lab))

    def test_blend_contains_child_rows_preserved(self):
        rows=['Supplement Facts','Echinacea Complex 450 mg','Contains:','Echinacea','(Echinacea purpurea (aerial))','Burdock (Arctium lappa) (root)','Elderberry Concentrate (Sambucus nigra) (fruit)','**Daily Value not established.','Other Ingredients: Gelatin.']
        names=parse_ingredients([{'text':x} for x in rows])
        self.assertIn('Echinacea (Echinacea purpurea (aerial))',names)
        self.assertIn('Burdock (Arctium lappa) (root)',names)
        self.assertIn('Elderberry Concentrate (Sambucus nigra) (fruit)',names)
