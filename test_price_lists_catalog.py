import copy
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

import automation_pipeline as pipeline
import own_catalog as catalog
import price_lists as prices
from invoice_generator import build_invoice_xlsx
from supplier_adapters import PriceListAdapter, OwnCatalogAdapter, SupplierQuote


TODAY = "2026-10-01"
NAME = "Маркер черный 1 мм"
CUSTOMER = {"name": "Тестовый покупатель", "inn": "7716997861", "kpp": "770801001"}


def workbook(rows):
    book = Workbook()
    book.active.title = "Прайс"
    for row in rows:
        book.active.append(row)
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


def settings(**overrides):
    result = {"sheet": "Прайс", "header_row": 1, "mapping": {"name": 0, "price": 1, "article": 2,
        "unit": 3, "stock": 4, "brand": None, "pack_size": None, "url": None},
        "default_unit": "шт", "vat_mode": "included", "vat_rate": 22}
    result.update(overrides)
    return result


class PriceListsCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.multiple(pipeline, DB_PATH=os.path.join(self.tmp.name, "prices.db"), DATABASE_URL="",
                                    _initialized_database_key=None)
        self.patch.start()
        self.app = FastAPI()
        prices.install_price_lists(self.app)
        catalog.install_own_catalog(self.app)
        pipeline.install_automation_pipeline(self.app, lambda *a, **kw: None)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.patch.stop()
        self.tmp.cleanup()

    def stage(self, price=40, supplier="Поставщик прайса", name=NAME, unit="шт", article="M-1234"):
        content = workbook([["Наименование", "Цена", "Артикул", "Ед. изм.", "Остаток"],
                            [name, price, article, unit, 999]])
        return prices.stage_file(content, "price.xlsx", supplier, "Основной", TODAY), content

    def import_price(self, **kwargs):
        lid, _ = self.stage(**kwargs)
        prices.activate_list(lid, settings())
        return lid

    def manual(self, **overrides):
        values = dict(name=NAME, supplier="Поставщик каталога", article="M-1234", brand="", unit="шт",
                      purchase_price="35,50", selling_price="42", price_date=TODAY,
                      competitors_count="7", found_method="Прайс по email", source_url="https://example.com/product")
        values.update(overrides)
        return catalog.add_manual_product(values)

    def test_uploaded_file_requires_mapping_and_is_durable(self):
        lid, content = self.stage()
        self.assertEqual(prices.search_prices(NAME), [])
        preview = self.client.get(f"/dashboard/prices/{lid}/preview")
        self.assertEqual(preview.status_code, 200)
        self.assertIn("Соответствие столбцов", preview.text)
        self.assertEqual(self.client.get(f"/dashboard/prices/{lid}/file").content, content)
        prices.activate_list(lid, settings())
        quote = prices.search_prices(NAME)[0]
        self.assertEqual(quote.price, 40)
        self.assertIsNone(quote.stock)
        self.assertEqual(quote.price_list_stock, 999)
        self.assertEqual((quote.source_list_id, quote.source_sheet, quote.source_row, quote.price_date), (lid, "Прайс", 2, TODAY))
        with patch.object(prices, "table_rows", side_effect=AssertionError("Search must not re-read files")):
            self.assertEqual(prices.search_prices(NAME)[0].price, 40)

    def test_update_replaces_only_same_catalogue_and_preserves_previous_version(self):
        old = self.import_price(price=10)
        other = self.import_price(price=15, supplier="Другой поставщик")
        new = self.import_price(price=70)
        self.assertEqual(prices.get_list(old)["status"], "superseded")
        self.assertEqual(prices.get_list(other)["status"], "active")
        self.assertEqual(prices.get_list(new)["status"], "active")
        self.assertEqual(sorted(q.price for q in prices.search_prices(NAME)), [15, 70])

    def test_invalid_replacement_cannot_remove_working_prices(self):
        old = self.import_price(price=10)
        broken, _ = self.stage(price="не цена")
        with self.assertRaisesRegex(ValueError, "Строка 2"):
            prices.activate_list(broken, settings())
        self.assertEqual(prices.get_list(old)["status"], "active")
        self.assertEqual(prices.get_list(broken)["status"], "pending")
        self.assertEqual(prices.search_prices(NAME)[0].price, 10)

    def test_duplicate_and_disabled_prices(self):
        old = self.import_price(price=10)
        duplicate, _ = self.stage(price=10)
        self.assertTrue(prices.activate_list(duplicate, settings())["duplicate"])
        self.assertEqual(len(prices.search_prices(NAME)), 1)
        self.client.post(f"/dashboard/prices/{old}/toggle")
        self.assertEqual(prices.search_prices(NAME), [])
        self.client.post(f"/dashboard/prices/{old}/toggle")
        self.assertEqual(len(prices.search_prices(NAME)), 1)

    def test_csv_cp1251_decimal_price_and_vat_conversion(self):
        content = "Наименование;Цена;Артикул;Единица измерения\nМаркер черный 1 мм;1 234,50;M-1234;шт\n".encode("cp1251")
        lid = prices.stage_file(content, "price.csv", "CSV поставщик", "Основной", TODAY)
        cfg = settings(sheet="CSV", vat_mode="excluded")
        cfg["mapping"]["stock"] = None
        prices.activate_list(lid, cfg)
        quote = prices.search_prices(NAME)[0]
        self.assertEqual(quote.price, 1506.09)
        self.assertEqual(quote.original_price, 1234.5)

    def test_shared_matching_cheapest_compatible_and_historical_stock_not_confirmed(self):
        self.import_price(price=40)
        self.manual(purchase_price=35)
        class API:
            name = "API поставщик"
            code = "krep_komp"
            def search(self, query, limit=8):
                return [SupplierQuote(supplier=self.name, name=NAME, sku="api", unit="шт", price=50, stock=100)]
        row = pipeline._build_match_row(1, {"goodName": NAME, "count": 10, "unit": {"name": "шт"}},
                                        [OwnCatalogAdapter(), PriceListAdapter(), API()])
        self.assertEqual(row["selected"]["supplier"], "Поставщик каталога")
        self.assertEqual(row["proposed_unit_price"], 36.75)
        self.assertFalse(row["stock_confirmed"])
        self.assertNotEqual(row["decision"], "auto_ready")
        self.assertEqual({item["supplier"] for item in row["candidates"]}, {"API поставщик", "Поставщик каталога", "Поставщик прайса"})

    def test_brand_dimension_and_price_unit_conflicts_are_not_selected(self):
        self.import_price(price=2, name="Маркер черный Bosch 2 мм")
        self.import_price(price=3, name="Маркер черный Matrix 1 мм", supplier="Вторая фирма")
        self.import_price(price=4, name="Маркер черный Bosch 1 мм", supplier="Неверная единица", unit="кг")
        self.import_price(price=20, name="Маркер черный Bosch 1 мм", supplier="Подходит")
        row = pipeline._build_match_row(1, {"goodName": "Маркер черный Bosch 1 мм", "count": 1, "unit": {"name": "шт"}}, PriceListAdapter())
        self.assertEqual(row["selected"]["supplier"], "Подходит")
        self.assertEqual(row["requested_brands"], ["bosch"])

    def test_pack_price_and_per_piece_price_are_compared_correctly(self):
        lid, _ = self.stage(price=100, name="Маркер черный 1 мм, 10 шт.", unit="упак")
        prices.activate_list(lid, settings())
        self.import_price(price=15, supplier="Поштучно", name="Маркер черный 1 мм, 10 шт.")
        row = pipeline._build_match_row(1, {"goodName": NAME, "count": 25, "unit": {"name": "шт"}}, PriceListAdapter())
        self.assertEqual(row["selected"]["supplier"], "Поставщик прайса")
        self.assertEqual(row["pack_size"], 10)
        self.assertEqual(row["purchase_units"], 3)
        self.assertEqual(row["proposed_unit_price"], 10.5)
        single = next(item for item in row["candidates"] if item["supplier"] == "Поштучно")
        self.assertEqual(pipeline._unit_candidate_price(single, "шт"), 15)

    def test_area_volume_and_length_price_units_cannot_be_confused(self):
        candidate = {"source_type": "price_list", "unit": "м", "name": "Материал", "price": 10}
        self.assertIsNotNone(pipeline._price_unit_conflict(candidate, "м²"))
        self.assertIsNotNone(pipeline._price_unit_conflict(candidate, "м³"))
        candidate["unit"] = "кв. м"
        self.assertIsNone(pipeline._price_unit_conflict(candidate, "м²"))

    def test_catalog_and_original_price_files_require_login(self):
        import security
        security.install_security(self.app)
        with patch.object(security, "_valid_session", return_value=False):
            for path in ("/dashboard/prices", "/dashboard/prices/1/file", "/dashboard/catalog", "/dashboard/catalog/1"):
                response = self.client.get(path, follow_redirects=False)
                self.assertEqual(response.status_code, 303)
                self.assertTrue(response.headers["location"].startswith("/login?"))

    def test_manual_catalog_latest_price_not_old_cheapest_with_history(self):
        result = self.manual(purchase_price=10)
        update = self.manual(purchase_price=80, price_date="2026-10-02", competitors_count="")
        self.assertEqual(result["product_id"], update["product_id"])
        found = catalog.search_catalog(NAME)[0]
        self.assertEqual(found.price, 80)
        self.assertIsNone(found.competitors_count)
        history = self.client.get(f"/dashboard/catalog/{result['product_id']}")
        self.assertIn("10.00", history.text)
        self.assertIn("80.00", history.text)
        self.assertIn("Прайс по email", history.text)

    def test_sale_requires_explicit_confirmation_deduplicates_and_freezes_snapshot(self):
        self.import_price(price=40)
        row = pipeline._build_match_row(1, {"goodName": NAME, "count": 10, "unit": {"name": "шт"}}, PriceListAdapter())
        row["decision"] = "approved"
        result = {"order_id": 333, "invoice_number": 240, "customer": CUSTOMER, "items": [row], "markup": .05, "vat_rate": .22}
        job = pipeline.save_manual_order("Тест", [dict(goodName=NAME, count=10, unit="шт")], customer_name=CUSTOMER["name"], customer_inn=CUSTOMER["inn"])
        job["id"] = job["job_id"]
        with pipeline._connect() as conn:
            pipeline._execute(conn, "UPDATE automation_jobs SET result_json=? WHERE id=?", (json.dumps(result), job["id"]))
        original = copy.deepcopy(result)
        denied = self.client.post(f"/dashboard/automation/jobs/{job['id']}/catalog-sale", data={"sale_date": TODAY}, follow_redirects=False)
        self.assertEqual(denied.status_code, 303)
        self.assertEqual(catalog.search_catalog(NAME), [])
        accepted = self.client.post(f"/dashboard/automation/jobs/{job['id']}/catalog-sale", data={"confirmed_sale": "1", "sale_date": TODAY, "competitors_1": "6"})
        self.assertEqual(accepted.status_code, 200)
        found = catalog.search_catalog(NAME)[0]
        self.assertEqual((found.price, found.selling_price, found.competitors_count), (40, 42, 6))
        self.assertEqual(catalog.record_confirmed_sale(job["id"], TODAY), 0)
        with pipeline._connect() as conn:
            saved = json.loads(pipeline._execute(conn, "SELECT result_json FROM automation_jobs WHERE id=?", (job["id"],)).fetchone()["result_json"])
            count = pipeline._execute(conn, "SELECT COUNT(*) FROM own_catalog_observations").fetchone()[0]
        self.assertEqual(saved, original)
        self.assertEqual(count, 1)
        self.assertTrue(build_invoice_xlsx(saved))

    def test_ui_upload_import_and_catalog_add(self):
        content = workbook([["Наименование", "Цена", "Артикул", "Ед. изм.", "Остаток"], [NAME, 40, "M-1234", "шт", 999]])
        uploaded = self.client.post("/dashboard/prices/upload", data={"supplier": "UI фирма", "catalog_name": "Основной", "price_date": TODAY},
                                    files={"file": ("price.xlsx", content)}, follow_redirects=False)
        self.assertEqual(uploaded.status_code, 303)
        lid = int(uploaded.headers["location"].split("/")[-2])
        form = {"sheet": "Прайс", "header_row": "1", "vat_mode": "included", "vat_rate": "22", "default_unit": "шт"}
        form.update({f"column_{key}": str(value) if value is not None else "" for key, value in settings()["mapping"].items()})
        self.assertIn("Импортировано позиций: 1", self.client.post(f"/dashboard/prices/{lid}/import", data=form).text)
        self.assertEqual(prices.search_prices(NAME)[0].supplier, "UI фирма")
        self.assertEqual(self.client.get("/dashboard/catalog").status_code, 200)


if __name__ == "__main__":
    unittest.main()
