import io
import json
import unittest
from unittest.mock import patch

from openpyxl import Workbook

import prepared_catalog as prepared
import price_lists as prices
from product_characteristics import compare, extract
import test_price_lists_catalog as base
from test_price_lists_catalog import settings, workbook


class PreparedCatalogTests(unittest.TestCase):
    setUp = base.PriceListsCatalogTests.setUp
    tearDown = base.PriceListsCatalogTests.tearDown
    def catalog(self, rows, supplier="Каталог", **options):
        lid = prices.stage_file(workbook(rows), "price.xlsx", supplier, "Основной", "")
        cfg = settings(catalog_only=True, **options)
        prices.activate_list(lid, cfg)
        return lid

    def test_unconfirmed_prices_are_searchable_but_never_purchase_prices(self):
        lid = self.catalog([["Наименование", "Цена", "Артикул", "Ед. изм.", "Остаток"],
            ["Фанера 2440х1220х21мм ламинированная", 990, "A21", "лист", None],
            ["Фанера 2440х1220х18мм ламинированная", 500, "A18", "лист", 0]])
        result = prepared.search("Фанера ламинированная толщина 21 мм")
        self.assertEqual([c["article"] for c in result["candidates"]], ["A21"])
        self.assertEqual(result["candidates"][0]["match"]["status"], "matched")
        offer = result["candidates"][0]
        self.assertIsNone(offer["price"])
        self.assertIsNone(offer["currency"])
        self.assertIsNone(offer["price_date"])
        self.assertIsNone(offer["price_list_stock"])
        self.assertEqual(prices.search_prices("Фанера ламинированная толщина 21 мм"), [])
        self.assertIn("Характеристики", self.client.get("/dashboard/prices").text)
        self.assertIn("21.0", self.client.get(f"/dashboard/prices/prepared?list_id={lid}").text)

    def test_ambiguous_threads_and_star_never_become_matches(self):
        for text in ["Крепление М8/М10", "Крепление М10*", "Крепление М8"]:
            result = compare(extract("Крепление М10"), extract(text))
            self.assertNotEqual(result["status"], "matched", text)
        self.assertEqual(compare(extract("Крепление М10"), extract("Крепление М10"))["status"], "matched")
        self.assertFalse(any(a["parameter"] == "Марка стали" for a in extract("Отвод стальной крутоизогнутый Ду100")["attributes"]))

    def test_inventory_profile_preserves_unknown_units_and_on_order(self):
        book = Workbook()
        book.active.title = "Лист1"
        for row in [["Наименование", "Всего", "Свободно", "Склад"],
            ["Труба 12х1,5 ст.20 ТУ 14-3р-55-2001", .321, .321, "Под заказ до 10 дней"]]:
            book.active.append(row)
        out = io.BytesIO()
        book.save(out)
        lid = prices.stage_file(out.getvalue(), "pipe.xlsx", "Трубы", "Склад", "")
        prices.activate_list(lid, dict(catalog_only=True, profile="pipe"))
        c = prepared.search("Труба диаметр 12 мм стенка 1,5 мм ст.20")["candidates"][0]
        self.assertEqual(c["availability"], "on_order")
        self.assertIsNone(c["stock_unit"])
        self.assertIsNone(c["stock_as_of"])
        self.assertEqual(c["price_list_stock"], .321)
        self.assertEqual(c["match"]["status"], "review")
        self.assertEqual(c["source_row"], 2)

    def test_unique_sku_reuses_description_and_keeps_manual_override(self):
        rows = [["Наименование", "Цена", "Артикул", "Ед. изм.", "Остаток"],
            ["Фанера ламинированная 2440х1220х18мм", 10, "A", "лист", 3]]
        old = self.catalog(rows)
        pid = prepared.search("Фанера толщина 18 мм")["candidates"][0]["product_id"]
        prepared.correct_product(pid, "Толщина", 21, "мм", "Проверено по спецификации")
        self.assertEqual(prepared.search("Фанера толщина 18 мм")["candidates"], [])
        self.assertEqual(prepared.search("Фанера толщина 21 мм")["candidates"][0]["product_id"], pid)
        rows[1][1] = 20
        new = prices.stage_file(workbook(rows), "price.xlsx", "Каталог", "Основной", "")
        with patch.object(prepared, "extract", side_effect=AssertionError("Unchanged description reparsed")):
            result = prices.activate_list(new, settings(catalog_only=True))
        self.assertEqual(result["reused_descriptions"], 1)
        self.assertEqual(prices.get_list(old)["status"], "superseded")
        self.assertEqual(prepared.search("Фанера толщина 21 мм")["candidates"][0]["original_price"], 20)

    def test_duplicate_sku_and_no_sku_are_not_merged(self):
        self.catalog([["Наименование", "Цена", "Артикул", "Ед. изм.", "Остаток"],
            ["Фанера 2440х1220х21мм", 1, "A", "лист", 3],
            ["Фанера 2440х1220х21мм", 1, "A", "лист", 5],
            ["Фанера 2440х1220х21мм", 1, None, "лист", 0]])
        candidates = prepared.search("Фанера толщина 21 мм")["candidates"]
        self.assertEqual(len({c["product_id"] for c in candidates}), 3)
        self.assertTrue(all(c["identity_status"] == "separate_row" for c in candidates))

    def test_search_never_reads_original_and_filters_before_limit(self):
        rows = [["Наименование", "Цена", "Артикул", "Ед. изм.", "Остаток"]]
        rows += [["Фанера 2440х1220х18мм", 1, str(i), "лист", 1] for i in range(450)]
        rows += [["Фанера 2440х1220х21мм", 999, "wanted", "лист", None]]
        self.catalog(rows)
        with patch.object(prices, "table_rows", side_effect=AssertionError("Search read Excel")):
            c = prepared.search("Фанера толщина 21 мм")["candidates"]
        self.assertEqual([i["article"] for i in c], ["wanted"])

    def test_saved_real_order_shape_and_no_mutation(self):
        job = __import__("automation_pipeline").save_manual_order("Проверка", [dict(goodName="Фанера толщина 21 мм", count=3, unit="лист")])
        with prices.database() as conn:
            before = prices.execute(conn, "SELECT * FROM automation_jobs WHERE id=?", (job["job_id"],)).fetchone()
            snapshot = json.loads(before["order_json"])
            # Real Zakupay payloads use orderItems, manual payloads use items.
            snapshot["orderItems"] = snapshot.pop("items", []) or [dict(goodName="Фанера толщина 21 мм", count=3)]
            prices.execute(conn, "UPDATE automation_jobs SET order_json=? WHERE id=?", (json.dumps(snapshot), job["job_id"]))
        self.assertEqual(len(prepared.match_saved_order(job["job_id"])["items"]), 1)
        self.assertEqual(self.client.get(f"/dashboard/prices/match-order/{job['job_id']}").status_code, 200)

    def test_prepared_pages_require_authentication(self):
        import security
        security.install_security(self.app)
        with patch.object(security, "_valid_session", return_value=False):
            for path in ["/dashboard/prices/prepared", "/dashboard/prices/products/1", "/dashboard/prices/match-order/1"]:
                self.assertEqual(self.client.get(path, follow_redirects=False).status_code, 303)
