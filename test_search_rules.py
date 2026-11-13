"""Search by size and product type (plan, section 9, 13.11.2026)."""
from datetime import date, timedelta
import unittest

import automation_pipeline as pipeline
import price_lists as prices
import search_rules as rules
from supplier_adapters import SupplierAdapter, SupplierQuote
import test_price_lists_catalog as base
from test_price_lists_catalog import settings, workbook

PROFILES = [
    ("Профиль потолочный ПП 60х27 3м (0,4мм)", 141.8),
    ("Профиль потолочный ПП 60х27 3м (0,5мм)", 162.15),
    ("Профиль потолочный ПП 60х27 4м (0,5мм)", 216.2),
    ("КНАУФ Профиль потолочный ПП 60х27 (3м)", 317.2),
]


class SizeAndTypeRules(unittest.TestCase):
    def test_every_spelling_is_one_size(self):
        for text in ("60*27", "60×27", "60x27", "60х27", "60/27", "60 x 27", "60Х27мм"):
            self.assertEqual(rules.sizes("профиль " + text), [("60", "27")], text)
        self.assertEqual(rules.sizes("саморез 4,2*16"), [("4.2", "16")])
        self.assertEqual(rules.normalize_sizes("профиль 60*27"), "профиль 60х27")

    def test_type_is_the_head_word(self):
        self.assertEqual(rules.product_type("профиль 60*27"), "профиль")
        self.assertEqual(rules.product_type("ПП 60*27 профиль"), "профиль")
        self.assertEqual(rules.product_type("Алюминиевый профиль 60х27"), "профиль")
        self.assertEqual(rules.product_type("КНАУФ Профиль потолочный"), "профиль")
        self.assertIsNone(rules.product_type("60*27"))

    def test_type_status(self):
        status = lambda name: rules.type_status("профиль", name)
        self.assertEqual(status("Профиль потолочный ПП 60х27 3м"), "match")
        self.assertEqual(status("Профиль-капельник 10мм"), "match")
        self.assertEqual(status("Профиль алюм стандарт"), "match")
        self.assertEqual(status("Грильято 100х100мм профиль L=1200мм"), "doubtful")
        for other in ("Соединитель профилей 60х27", "Подвес для профиля 60х27",
                      "Сверло по металлу с профилем", "Ремень приводной"):
            self.assertEqual(status(other), "other", other)

    def test_sizes_compare_as_pairs(self):
        wanted = rules.sizes("60*27")
        self.assertEqual(rules.compare_sizes(wanted, "ПП 60х27")["confirmed"], [("60", "27")])
        self.assertEqual(rules.compare_sizes(wanted, "ПП 60x30")["conflicts"], [("60", "27")])
        self.assertEqual(rules.compare_sizes(wanted, "Профиль")["missing"], [("60", "27")])

    def test_relevance_gate_only_drops_names_with_no_common_word(self):
        self.assertFalse(rules.shares_word("профиль 60*27", "Ремень приводной 60"))
        self.assertTrue(rules.shares_word("профиль 60*27", "Профиль ПП"))
        self.assertTrue(rules.shares_word("4607", "Любое название"))


class SearchOnPriceList(unittest.TestCase):
    setUp = base.PriceListsCatalogTests.setUp
    tearDown = base.PriceListsCatalogTests.tearDown

    def load(self, names):
        rows = [["Наименование", "Цена", "Артикул", "Ед. изм.", "Остаток"]]
        rows += [[name, price, f"A{i}", "шт", None] for i, (name, price) in enumerate(names)]
        lid = prices.stage_file(workbook(rows), "test.xlsx", "Сатурн", "Основной",
                                (date.today() - timedelta(days=1)).isoformat())
        prices.activate_list(lid, settings())

    def load_saturn_like(self):
        self.load([
            *PROFILES,
            ("Профиль потолочный ПП 60x27 3м (латиница)", 150.0),
            ("Профиль потолочный ПП 60*27 3м (звёздочка)", 151.0),
            ("Профиль потолочный ПП 60/27 4м (дробь)", 152.0),
            ("Профиль потолочный ПП 27х28 3м", 90.0),
            ("Профиль маячковый 6мм 3м", 30.2),
            ("Грильято 100х100мм профиль L=600мм", 41.0),
            ("Соединитель профилей 60х27", 20.0),
            ("Подвес для профиля 60х27", 12.0),
            ("Ремень крепления груза 38мм", 936.0),
            ("Сверло по металлу с профилем 6мм", 70.0),
            ("Пластины крепёжные 60х27", 10.0),
        ])

    def test_every_profile_60x27_is_found_in_any_spelling(self):
        self.load_saturn_like()
        for query in ("профиль 60*27", "профиль 60×27", "профиль 60x27", "профиль 60х27", "профиль 60/27"):
            found = prices.search_prices(query)
            first = [q.name for q in found if q.search_level == 1]
            self.assertEqual(len(first), 7, (query, first))
            self.assertTrue(all("60" in n and "27" in n for n in first), first)
            # level 1 goes first, cheapest first inside it
            self.assertEqual([q.search_level for q in found], sorted(q.search_level for q in found))
            level_one_prices = [q.price for q in found if q.search_level == 1]
            self.assertEqual(level_one_prices, sorted(level_one_prices))

    def test_junk_and_accessories_are_not_profiles(self):
        self.load_saturn_like()
        names = [q.name for q in prices.search_prices("профиль 60*27")]
        for junk in ("Ремень", "Сверло", "Соединитель", "Подвес", "Пластины"):
            self.assertFalse(any(junk in n for n in names), (junk, names))

    def test_other_size_is_a_difference_and_unknown_size_is_unspecified(self):
        self.load_saturn_like()
        by_name = {q.name: q for q in prices.search_prices("профиль 60*27")}
        self.assertEqual(by_name["Профиль потолочный ПП 27х28 3м"].search_level, 3)
        self.assertEqual(by_name["Профиль маячковый 6мм 3м"].search_level, 2)
        self.assertEqual(by_name["Грильято 100х100мм профиль L=600мм"].search_level, 3)

    def test_no_limit_of_eight_per_supplier(self):
        self.load([(f"Профиль потолочный ПП 60х27 {n}м (вариант {n})", 100 + n) for n in range(1, 31)])
        found = prices.search_prices("профиль 60*27")
        self.assertEqual(len(found), 30)
        self.assertTrue(all(q.search_level == 1 for q in found))

    def test_sizes_are_not_taken_for_articles(self):
        # A size must not make every name that contains it an "exact article" hit.
        self.load([("Ремень 60х27", 5.0), ("Профиль потолочный ПП 60х27 3м", 141.8)])
        self.assertEqual([q.name for q in prices.search_prices("профиль 60х27")],
                         ["Профиль потолочный ПП 60х27 3м"])


class ApiJunk(unittest.TestCase):
    def test_api_result_without_common_word_is_dropped(self):
        class Fake(SupplierAdapter):
            code, name = "fake", "Fake"
            def search(self, query, limit=8):
                return [SupplierQuote(supplier="ВИ", name="Ремень приводной 60", sku="1", price=10, stock=5),
                        SupplierQuote(supplier="ВИ", name="Профиль потолочный ПП 60х27", sku="2", price=150, stock=5)]
        row = pipeline._build_match_row(1, {"goodName": "профиль 60*27", "count": 1, "unit": {"name": "шт"}}, [Fake()])
        shown = [c["name"] for c in pipeline._review_candidates(row)]
        self.assertEqual(shown, ["Профиль потолочный ПП 60х27"])


if __name__ == "__main__":
    unittest.main()
