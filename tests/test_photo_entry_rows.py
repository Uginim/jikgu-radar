import unittest

from nextask.core.entry import photo_declared_names
from nextask.core.plugins.supplement import parse_ingredients


def ocr(*lines):
    return [{"text": text, "conf": .95} for text in lines]


class PhotoEntryRows(unittest.TestCase):
    def test_cropped_quantity_keeps_verbatim_ingredient(self):
        rows = ocr("Supplement facts", "Serving Size: 1 Capsule", "Servings Per Container: 60",
                   "Amount Per Serving", "Vitamin C (as Ascorbic Acid)")
        self.assertEqual(parse_ingredients(rows), ["Vitamin C (as Ascorbic Acid)"])
        self.assertEqual(rows[-1], {"text": "Vitamin C (as Ascorbic Acid)", "conf": .95})

    def test_front_warning_and_low_confidence_are_not_identified_ingredients(self):
        self.assertEqual(photo_declared_names(ocr("Vitamin C", "Made with Fruit", "Vitamin D3")), [])
        self.assertEqual(parse_ingredients(ocr("NITROTEST", "Naturally & Artificially Flavored",
                                               "Net Wt. 16.72 oz. (474g)")), [])
        rows = ocr("Supplement Facts", "Amount Per Serving", "Biotin", "Warnings:", "Vitamin C")
        rows[2]["conf"] = .3
        self.assertEqual(photo_declared_names(ocr("Supplement Facts", "Amount Per Serving",
                                                 "Vitamin C (not included)")), [])
        self.assertEqual(photo_declared_names(rows), [])

    def test_no_duplicate_when_amount_occupies_the_next_line(self):
        rows = ocr("Supplement Facts", "Amount Per Serving", "Vitamin C (as Ascorbic Acid)", "1,000 mg")
        self.assertEqual(parse_ingredients(rows), ["Vitamin C (as Ascorbic Acid)"])
