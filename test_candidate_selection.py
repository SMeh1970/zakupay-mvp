import unittest
from unittest.mock import patch

import automation_pipeline as pipeline
from supplier_adapters import SupplierQuote
from vi_order_match import _requested_brands


class CandidateSelectionTests(unittest.TestCase):
    def match(self, name, quotes):
        item = {"goodName": name, "count": 2, "unit": {"name": "шт"}}
        with patch.object(pipeline, "_search_candidates", return_value=(quotes, [], False)):
            return pipeline._build_match_row(1, item, [])

    def test_brandless_request_selects_cheapest_compatible_option_not_highest_score(self):
        result = self.match("Маркер черный", [
            SupplierQuote("ВИ", "Маркер черный", sku="expensive", price=100, stock=10),
            SupplierQuote("КРЕП-КОМП", "Маркер черный тонкий", sku="cheap", price=40, stock=10),
        ])
        self.assertEqual(result["selected"]["sku"], "cheap")
        self.assertLess(result["selected"]["match_score"], result["candidates"][1]["match_score"])
        self.assertEqual(result["selection_rule"], "lowest_unit_price")
        self.assertEqual(result["proposed_unit_price"], 42)

    def test_price_is_compared_per_requested_piece_not_per_pack(self):
        result = self.match("Маркер черный", [
            SupplierQuote("ВИ", "Маркер черный", sku="single", price=15, unit="шт", stock=10),
            SupplierQuote("КРЕП-КОМП", "Маркер черный 10 шт", sku="pack", price=100, unit="упаковка", stock=10),
        ])
        self.assertEqual(result["selected"]["sku"], "pack")
        self.assertEqual(result["pack_size"], 10)
        self.assertEqual(result["proposed_unit_price"], 10.5)

    def test_named_brand_is_not_replaced_by_cheaper_other_brand(self):
        result = self.match("Маркер черный Gigant", [
            SupplierQuote("ВИ", "Маркер черный Gigant", sku="wanted", brand="Gigant", price=100, stock=10),
            SupplierQuote("ВИ", "Маркер черный Matrix", sku="wrong", brand="Matrix", price=1, stock=10),
        ])
        self.assertEqual(result["selected"]["sku"], "wanted")
        self.assertEqual(result["requested_brands"], ["gigant"])
        self.assertEqual(result["selection_rule"], "brand_match")
        self.assertIn("не совпадает указанный в заявке бренд", result["rejected_candidates"][0]["hard_conflicts"])

    def test_unknown_brand_is_recognized_from_supplier_metadata(self):
        result = self.match("Маркер черный New Brand", [
            SupplierQuote("ВИ", "Маркер черный New Brand", sku="wanted", brand="New Brand", price=100),
            SupplierQuote("ВИ", "Маркер черный", sku="cheap", brand="Other Brand", price=1),
        ])
        self.assertEqual(result["selected"]["sku"], "wanted")
        self.assertEqual(result["requested_brands"], ["new brand"])

    def test_brand_matches_whole_words_case_insensitively(self):
        self.assertEqual(_requested_brands("Коронка MATRIX", ["Matrix"]), {"matrix"})
        self.assertEqual(_requested_brands("Коронка MatrixPro", ["Matrix"]), set())
        self.assertEqual(_requested_brands("Коронка New Brand", ["New Brand", "Brand"]), {"new brand"})

    def test_named_brand_can_be_read_from_product_name_without_brand_field(self):
        result = self.match("Маркер черный Gigant", [
            SupplierQuote("ВИ", "Маркер черный Gigant", sku="wanted", price=100),
            SupplierQuote("ВИ", "Маркер черный", sku="cheap", price=1),
        ])
        self.assertEqual(result["selected"]["sku"], "wanted")

    def test_wrong_dimensions_and_unrelated_product_are_not_cheapest_choices(self):
        result = self.match("Сверло по бетону 10x160 мм", [
            SupplierQuote("ВИ", "Сверло по бетону 10x160 мм", sku="wanted", price=100),
            SupplierQuote("ВИ", "Сверло по бетону 12x160 мм", sku="wrong-size", price=1),
            SupplierQuote("ВИ", "Бур по бетону 10x160 мм", sku="wrong-type", price=2),
        ])
        self.assertEqual(result["selected"]["sku"], "wanted")
        self.assertEqual(len(result["rejected_candidates"]), 2)

    def test_missing_zero_or_negative_price_never_wins_over_valid_price(self):
        result = self.match("Маркер черный", [
            SupplierQuote("ВИ", "Маркер черный", sku="valid", price=10),
            SupplierQuote("ВИ", "Маркер черный", sku="missing", price=None),
            SupplierQuote("ВИ", "Маркер черный", sku="zero", price=0),
            SupplierQuote("ВИ", "Маркер черный", sku="negative", price=-1),
        ])
        self.assertEqual(result["selected"]["sku"], "valid")

    def test_zero_price_is_not_auto_ready(self):
        result = self.match("Маркер черный", [SupplierQuote("ВИ", "Маркер черный", price=0, stock=10)])
        self.assertEqual(result["decision"], "manual")

    def test_cheapest_option_without_stock_still_needs_availability_confirmation(self):
        result = self.match("Маркер черный", [
            SupplierQuote("ВИ", "Маркер черный", sku="cheap", price=10, stock=0),
            SupplierQuote("ВИ", "Маркер черный", sku="available", price=20, stock=10),
        ])
        self.assertEqual(result["selected"]["sku"], "cheap")
        self.assertEqual(result["decision"], "manual")
        self.assertFalse(result["stock_confirmed"])
