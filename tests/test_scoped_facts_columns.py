import unittest

from nextask.core.sources import label_lines


class ScopedFactsColumnsTests(unittest.TestCase):
    def test_archived_scoped_columns_keep_active_and_other_rows_without_inventing_serving_size(self):
        html = ('<p>Description Amount per Serving % Daily Value</p><p>Folate 170 mcg DFE (100 mcg folic acid) 43%</p>'
                '<p>Vitamin B-12 (as Cyanocobalamin) 1 mg (1,000 mcg) 41,667%</p><p>Other Ingredients: Xylitol.</p>')
        self.assertEqual(label_lines('Recommended: unrelated 99 mg', html), [
            'Description Amount per Serving % Daily Value', 'Folate 170 mcg DFE (100 mcg folic acid) 43%',
            'Vitamin B-12 (as Cyanocobalamin) 1 mg (1,000 mcg) 41,667%', 'Other ingredients: Xylitol'])

    def test_unscoped_column_words_or_advertising_do_not_create_facts(self):
        table = 'Description Amount per Serving % Daily Value\nUnrelated candidate 20 mg\nOther ingredients: Cellulose.'
        self.assertEqual(label_lines(table), ['Other ingredients: Cellulose'])
        html = '<p>Our description: amount per serving % daily value</p><p>Unrelated candidate 20 mg</p>'
        self.assertEqual(label_lines('', html), [])
