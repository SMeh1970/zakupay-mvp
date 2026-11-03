"""Search completeness: retrieval keeps every plausible offer, the strict check decides afterwards.

Each case builds a large catalogue of look-alike rows that all share the request's descriptive
words and differ in size, and hides one correct row among them. The correct row has fewer words
in common with the request than the decoys, so a small top-N window ranks it out.
"""
import os
import time
import unittest
from datetime import date, timedelta
from unittest.mock import Mock, patch

import automation_pipeline as pipeline
import own_catalog as catalog
import price_lists as prices
import supplier_adapters as sa
import test_price_lists_catalog as base
from supplier_adapters import KrepKompAdapter, SupplierQuote
from test_price_lists_catalog import settings, workbook

PRICE_DAY = (date.today() - timedelta(days=1)).isoformat()

# (request, decoy template with {n}, correct row name, number of decoys)
CATEGORIES = [
    ("Болт шестигранный оцинкованный М8х40 DIN 933", "Болт шестигранный оцинкованный М8х{n} DIN 933 кл.пр. 8.8", "Болт М8х40 DIN 933 шестигранный", 900),
    ("Труба профильная квадратная стальная 40х40х4", "Труба профильная квадратная стальная {n}х{n}х4 ГОСТ", "Труба профильная квадратная 40х40х4 ГОСТ 8639-82", 900),
    ("Фанера ламинированная влагостойкая 18 мм", "Фанера ламинированная влагостойкая берёзовая {n}х1250 сорт", "Фанера ламинированная 18 мм ФСФ", 900),
]


class PriceListCompletenessTests(unittest.TestCase):
    setUp = base.PriceListsCatalogTests.setUp
    tearDown = base.PriceListsCatalogTests.tearDown

    def load(self, names):
        rows = [["Наименование", "Цена", "Артикул", "Ед. изм.", "Остаток"]]
        rows += [[name, price, article, "шт", None] for name, price, article in names]
        lid = prices.stage_file(workbook(rows), "big.xlsx", "Сатурн", "Основной", PRICE_DAY)
        prices.activate_list(lid, settings())

    def test_correct_row_is_found_among_hundreds_of_lookalikes(self):
        for request, decoy, correct, count in CATEGORIES:
            with self.subTest(request=request):
                self.setUp()
                try:
                    rows = [(decoy.format(n=100 + i), 50 + i, f"D{i}") for i in range(count)]
                    self.load(rows + [(correct, 500, "RIGHT")])
                    found = prices.search_prices(request)
                    self.assertIn("RIGHT", [q.article for q in found])
                    # Row-level result: the right row is visible to the operator, never lost.
                    row = pipeline._build_match_row(1, dict(goodName=request, count=1, unit={"name": "шт"}),
                                                    [sa.PriceListAdapter()])
                    shown = [c.get("article") for c in (row["candidates"] + row["rejected_candidates"])]
                    self.assertIn("RIGHT", shown)
                finally:
                    self.tearDown()

    def test_many_valid_offers_of_one_supplier_are_all_kept(self):
        self.load([(f"Перчатки х/б с ПВХ точкой вариант {i}", 10 + i, f"G{i}") for i in range(25)])
        found = prices.search_prices("Перчатки х/б с ПВХ точкой")
        self.assertEqual(len(found), 25)

    def test_other_type_is_returned_flagged_and_explained(self):
        self.load([("Саморез черный 200 мм", 1, "S1"), ("Маркер черный 200 мм", 2, "M1")])
        row = pipeline._build_match_row(1, dict(goodName="Маркер черный 200 мм", count=1, unit={"name": "шт"}),
                                        [sa.PriceListAdapter()])
        self.assertEqual(row["selected"]["article"], "M1")
        reasons = {c["article"]: c.get("rejection_reason", "") for c in row["rejected_candidates"] if c.get("article")}
        self.assertIn("тип товара", reasons.get("S1", ""))

    def test_large_price_list_search_stays_fast(self):
        rows = [(f"Болт шестигранный оцинкованный М8х{i % 300} DIN 933 партия {i}", 5 + i % 90, f"B{i}") for i in range(6000)]
        self.load(rows)
        started = time.monotonic()
        found = prices.search_prices("Болт шестигранный оцинкованный М8х40 DIN 933")
        elapsed = time.monotonic() - started
        self.assertTrue(found)
        self.assertLess(elapsed, 8.0, f"search took {elapsed:.1f}s")


class OwnCatalogCompletenessTests(unittest.TestCase):
    setUp = base.PriceListsCatalogTests.setUp
    tearDown = base.PriceListsCatalogTests.tearDown

    def test_low_score_and_conflicting_rows_are_returned_flagged_not_dropped(self):
        for name, article in (("Маркер черный 1 мм", "A1"), ("Маркер черный 3 мм", "A2"), ("Маркер перманентный", "A3")):
            catalog.add_manual_product(dict(name=name, supplier="Поставщик каталога", article=article, brand="", unit="шт",
                                            purchase_price="35,50", selling_price="42", price_date=base.TODAY,
                                            competitors_count="7", found_method="Прайс по email",
                                            source_url="https://example.com/product"))
        found = catalog.search_catalog("Маркер черный 1 мм")
        self.assertEqual(found[0].article, "A1")
        self.assertGreaterEqual(len(found), 2)


class KrepWindowTests(unittest.TestCase):
    def test_window_reaches_beyond_the_first_few_ranked_items(self):
        # 60 items tie on text rank; the right one is not among the first 8 by rank order.
        items = [{"Код": f"{i:05d}", "Наименование": f"Шуруп фасадный 7х{100 + i}", "ПолноеНаименование": f"Шуруп фасадный 7х{100 + i}",
                  "Артикул": f"a{i}", "ЕдиницаИзмерения": "шт", "Категория": "Шурупы"} for i in range(60)]
        adapter = KrepKompAdapter()
        captured = {}
        adapter._enrich = lambda chosen: captured.setdefault("codes", [x["Код"] for x in chosen]) and []
        with patch.object(KrepKompAdapter, "enabled", new=True), patch.object(KrepKompAdapter, "_catalog_items", return_value=items):
            adapter.search("Шуруп фасадный", limit=8)
        self.assertGreater(len(captured["codes"]), 20)
        self.assertLessEqual(len(captured["codes"]), 40)


class SupplierTimeoutIsolationTests(unittest.TestCase):
    def test_overrun_of_one_source_does_not_skip_the_next(self):
        slow = Mock(code="vseinstrumenti", name="slow", enabled=True)
        slow.name = "Медленный"
        calls = {"slow": 0}

        def slow_search(query, limit=8):
            calls["slow"] += 1
            time.sleep(1.2)
            return []
        slow.search.side_effect = slow_search
        slow.search_pages = None
        later = Mock(code="krep_komp", enabled=True)
        later.name = "Следующий"
        later.search.return_value = [SupplierQuote(supplier="Следующий", name="Болт М8х40", price=10, unit="шт")]
        with patch.dict(os.environ, {"AUTO_MIN_SUPPLIER_SECONDS": "3"}):
            quotes, diagnostics, timed_out = pipeline._search_candidates([slow, later], "Болт М8х40", time_budget_seconds=1.0)
        self.assertTrue(timed_out)
        self.assertTrue(any(q.supplier == "Следующий" for q in quotes))
        self.assertEqual([d["queries"] > 0 for d in diagnostics], [True, True])
        self.assertTrue(diagnostics[0]["timed_out"])
        self.assertFalse(diagnostics[1]["timed_out"])


if __name__ == "__main__":
    unittest.main()


class CtrlFSearchTests(unittest.TestCase):
    """The price-list search must find what a person finds with Ctrl+F on the main word."""
    setUp = base.PriceListsCatalogTests.setUp
    tearDown = base.PriceListsCatalogTests.tearDown

    def load(self, names):
        rows = [["Наименование", "Цена", "Артикул", "Ед. изм.", "Остаток"]]
        rows += [[name, price, article, "шт", None] for name, price, article in names]
        lid = prices.stage_file(workbook(rows), "ctrlf.xlsx", "Сатурн", "Основной", PRICE_DAY)
        prices.activate_list(lid, settings())

    def test_word_forms_with_different_endings_are_found(self):
        # Only the main noun is shared, and only its ending differs (the index stems -а/-и but not -о).
        for request, name in (("Сверло 10 мм", "Сверла HSS 10 мм"),
                              ("Ведро 10 л", "Ведра хозяйственные 10 л"),
                              ("Полотно 300 мм", "Полотна ножовочные 300 мм")):
            with self.subTest(request=request):
                self.setUp()
                try:
                    self.load([(name, 10, "X1"), ("Совсем другой товар 1 шт", 1, "Y1")])
                    found = prices.search_prices(request)
                    self.assertEqual([q.article for q in found][:1], ["X1"])
                finally:
                    self.tearDown()


class StagedQueriesTests(unittest.TestCase):
    def test_short_queries_follow_a_long_name(self):
        variants = pipeline._search_variants("Монолитный поликарбонат 8 мм прозрачный 2050×3050")
        self.assertIn("поликарбонат", variants)
        self.assertIn("поликарбонат 8 мм", variants)
        self.assertIn("поликарбонат монолитный", variants)
        self.assertEqual(variants.index("Монолитный поликарбонат 8 мм прозрачный 2050×3050") < variants.index("поликарбонат"), True)


class VocabularyTests(unittest.TestCase):
    setUp = base.PriceListsCatalogTests.setUp
    tearDown = base.PriceListsCatalogTests.tearDown

    def test_prefix_pass_uses_the_word_vocabulary_and_backfills_old_lists(self):
        rows = [["Наименование", "Цена", "Артикул", "Ед. изм.", "Остаток"], ["Сверла HSS 10 мм", 10, "X1", "шт", None]]
        lid = prices.stage_file(workbook(rows), "v.xlsx", "Сатурн", "Основной", PRICE_DAY)
        prices.activate_list(lid, settings())
        with prices.database() as conn:
            self.assertIsNotNone(prices.execute(conn, "SELECT 1 FROM supplier_price_vocab WHERE token='сверл'").fetchone())
            # A list imported before the vocabulary existed: the table is empty and is filled on first use.
            prices.execute(conn, "DELETE FROM supplier_price_vocab")
        self.assertEqual([q.article for q in prices.search_prices("Сверло 10 мм")], ["X1"])
        with prices.database() as conn:
            self.assertIsNotNone(prices.execute(conn, "SELECT 1 FROM supplier_price_vocab WHERE token='сверл'").fetchone())
