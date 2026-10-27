import json
import unittest
from unittest.mock import patch

import automation_pipeline as pipeline
import price_lists as prices
import supplier_comparison as comparison
from supplier_adapters import PriceListAdapter
import test_price_lists_catalog as base
from test_price_lists_catalog import settings, workbook


class PriceSearchRecallTests(unittest.TestCase):
    setUp = base.PriceListsCatalogTests.setUp
    tearDown = base.PriceListsCatalogTests.tearDown

    def load(self, names):
        rows = [["Наименование", "Цена", "Артикул", "Ед. изм.", "Остаток"]]
        rows += [[name, price, article, "шт", None] for name, price, article in names]
        lid = prices.stage_file(workbook(rows), "test.xlsx", "Сатурн", "Основной", "2026-10-22")
        prices.activate_list(lid, settings())

    def row(self, query):
        return pipeline._build_match_row(1, dict(goodName=query, count=1, unit={"name": "шт"}), [PriceListAdapter()])

    def test_missing_attributes_are_visible_but_not_exact(self):
        self.load([("Фанера ламинированная", 100, "F1")])
        found = prices.search_prices("Фанера ламинированная толщина 21 мм")
        self.assertEqual(len(found), 1)
        self.assertTrue(any("Толщина" in x for x in found[0].characteristic_missing))
        row = self.row("Фанера ламинированная толщина 21 мм")
        self.assertNotEqual(row["match_status"], "точное")
        self.assertNotEqual(row["decision"], "auto_ready")
        view = comparison.offer_view(row, pipeline._review_candidates(row)[0])
        self.assertTrue(any(f["field"] == "Проверка прайса" and f["state"] == "pending" for f in view["features"]))

    def test_wrong_size_survives_only_as_reviewable_difference(self):
        self.load([("Фанера ламинированная толщина 18 мм", 100, "F18")])
        row = self.row("Фанера ламинированная толщина 21 мм")
        self.assertIsNone(row["selected"])
        shown = pipeline._review_candidates(row)
        self.assertEqual(len(shown), 1)
        self.assertTrue(shown[0]["hard_conflicts"])
        self.assertIn("Толщина", pipeline._review_candidate_details(row, shown[0]))

    def test_numbers_and_wrong_product_type_do_not_fill_shortlist(self):
        self.load([("Саморез черный 200 мм", 1, "S1"), ("Маркер черный 200 мм", 2, "M1")])
        found = prices.search_prices("Маркер черный 200 мм")
        self.assertEqual([q.article for q in found], ["M1"])
        self.assertEqual(prices.search_prices("Неизвестный товар 200 мм"), [])

    def test_synonym_expansion_is_one_index_query_and_original_requirements_remain(self):
        self.load([("Пила кольцевая 32 мм", 100, "P32"), ("Пила кольцевая 60 мм", 1, "P60")])
        calls = []
        original = prices.execute
        def counted(conn, sql, params=()):
            calls.append(sql)
            return original(conn, sql, params)
        with patch.object(prices, "execute", side_effect=counted):
            found = prices.search_prices("Коронка 32 мм")
        self.assertTrue(found)
        self.assertEqual(found[0].article, "P32")
        self.assertEqual(sum("supplier_price_tokens" in sql for sql in calls), 1)
        row = self.row("Коронка 32 мм")
        self.assertFalse(row["selected"] and row["selected"]["article"] == "P60")

    def test_exact_article_wins_over_cheap_inexact_variants(self):
        self.load([("Маркер черный 1 мм", 100, "ART123"), ("Маркер черный 2 мм", 1, "OTHER")])
        self.assertEqual(prices.search_prices("Маркер черный 1 мм ART123")[0].article, "ART123")

    def test_numeric_only_hits_skip_expensive_scoring(self):
        import vi_order_match
        self.load([("Маркер черный 200 мм", 100, "M1"),
                   *[(f"Шуруп {n} 200 мм", 1, f"S{n}") for n in range(40)]])
        with patch.object(vi_order_match, '_score_details', wraps=vi_order_match._score_details) as score:
            found = prices.search_prices("Маркер черный 200 мм")
        self.assertEqual([q.article for q in found], ["M1"])
        self.assertEqual(score.call_count, 1)

    def test_direct_price_search_shows_names_and_warnings(self):
        self.load([("Фанера ламинированная толщина 18 мм", 100, "F18")])
        response = self.client.get('/dashboard/prices', params={"q": "Фанера ламинированная толщина 21 мм"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Фанера ламинированная толщина 18 мм", response.text)
        self.assertIn("Отличия / уточнить", response.text)


class PriceSearchDiagnosticTests(unittest.TestCase):
    def test_disabled_expired_and_invalid_config_cannot_start(self):
        from datetime import datetime, timezone, timedelta
        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        for raw in ["", "bad json", json.dumps({"until": expired, "queries": ["Маркер"]}),
                    json.dumps({"until": future, "queries": ["Маркер"] * 9}),
                    json.dumps({"until": future, "queries": [12]})]:
            with patch.dict('os.environ', {"PRICE_SEARCH_DIAGNOSTIC": raw}), patch('threading.Thread') as thread:
                prices.start_search_diagnostic()
                thread.assert_not_called()

    def test_valid_probe_reads_only_and_logs_no_raw_query_or_price(self):
        from datetime import datetime, timezone, timedelta
        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        config = json.dumps({"until": future, "queries": ["Private product name"]})
        def immediate(**kwargs):
            class Worker:
                def start(self): kwargs["target"]()
            return Worker()
        row = {"match_status": "сомнительное", "decision": "review", "candidates": [
            {"name": "Private product name", "price": 123456.78, "clarification_reasons": ["Check size"]}]}
        with patch.dict('os.environ', {"PRICE_SEARCH_DIAGNOSTIC": config}), patch('threading.Thread', side_effect=immediate), \
             patch.object(pipeline, '_build_match_row', return_value=row) as build, \
             self.assertLogs('zakupay.price_search', level='WARNING') as logs:
            prices.start_search_diagnostic()
        self.assertEqual(build.call_count, 1)
        self.assertEqual([s.code for s in build.call_args.args[2]], ['price_lists'])
        self.assertIn('"candidates": 1', logs.output[0])
        self.assertNotIn('Private product name', logs.output[0])
        self.assertNotIn('123456', logs.output[0])


if __name__ == '__main__':
    unittest.main()
