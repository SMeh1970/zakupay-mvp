import copy
import csv
import io
import json
import os
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import automation_pipeline as pipeline
import catalog_comparison as bridge
import prepared_catalog
import price_lists
import security
import supplier_comparison as comparison


class CatalogueComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.multiple(pipeline, DB_PATH=os.path.join(self.tmp.name, "catalog.db"),
            DATABASE_URL="", _initialized_database_key=None)
        self.db.start()
        self.session = patch.object(security, "_valid_session", return_value=True)
        self.session.start()
        app = FastAPI()
        self.external = Mock(side_effect=AssertionError("Local catalogue comparison must not call suppliers"))
        pipeline.install_automation_pipeline(app, self.external)
        price_lists.install_price_lists(app)
        comparison.install_supplier_comparison(app)
        self.client = TestClient(app)
        self.job = pipeline.save_manual_order("Трубы", [dict(goodName="Труба 89х4 мм", count=60, unit="м")])["job_id"]
        self.url = f"/dashboard/automation/jobs/{self.job}/comparison"

    def tearDown(self):
        self.client.close()
        self.session.stop()
        self.db.stop()
        self.tmp.cleanup()

    def catalogue(self, supplier="Поставщик А", name="Труба 89х4 мм L=6м", article="P1", price=600,
                  stock=100, unit="шт", rows=None):
        out = io.StringIO()
        writer = csv.writer(out, delimiter=";")
        writer.writerow(["Наименование", "Цена", "Артикул", "Единица", "Остаток"])
        writer.writerows(rows or [[name, price, article, unit, stock]])
        lid = price_lists.stage_file(out.getvalue().encode(), "prices.csv", supplier, "Основной", "")
        price_lists.activate_list(lid, dict(catalog_only=True, sheet="CSV", header_row=1,
            mapping=dict(name=0, price=1, article=2, unit=3, stock=4),
            source_provenance=dict(message_id="mail-test", email_received_at="2026-10-09T12:00:00+03:00")))
        return lid

    def saved(self):
        with pipeline._connect() as conn:
            row = pipeline._execute(conn, "SELECT order_json,result_json FROM automation_jobs WHERE id=?", (self.job,)).fetchone()
        return json.loads(row["result_json"]), row["order_json"]

    def model(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.text[:500])
        return json.loads(re.search(r'id="comparison-data">(.*?)</script>', response.text, re.S)[1])

    def post(self, action, **values):
        model = self.model()
        response = self.client.post(self.url + "/action", json=dict(action=action,
            csrf=model["csrf"], revision=model["revision"], **values))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def offer(self):
        return self.model()["rows"][0]["offers"][0]

    def accept_features(self, key):
        for f in next(o for o in self.model()["rows"][0]["offers"] if o["key"] == key)["features"]:
            if f["state"] == "pending":
                self.post("feature", position=1, feature=f["id"], decision="accepted")

    def terms(self, key, **overrides):
        values = dict(position=1, key=key, price=600, currency="RUB", unit="шт", vat_mode="excluded",
            vat_rate=22, price_date="2026-10-22", note="Условия подтверждены поставщиком")
        values.update(overrides)
        return self.post("confirm_terms", **values)

    def test_get_does_not_search_or_write_pending_request(self):
        before = self.saved()
        with patch.object(prepared_catalog, "search", side_effect=AssertionError("GET searched catalogue")):
            model = self.model()
        self.assertEqual(model["rows"][0]["quantity"], 60)
        self.assertEqual(model["suppliers"], [])
        self.assertEqual(before, self.saved())
        self.external.assert_not_called()

    def test_explicit_search_loads_evidence_and_blocks_unconfirmed_data(self):
        lid = self.catalogue()
        original = self.saved()[1]
        self.post("load_catalog")
        offer = self.offer()
        self.assertFalse(offer["eligible"])
        self.assertIsNone(offer["price"])
        self.assertIsNone(offer["currency"])
        self.assertEqual(offer["raw_price"], 600)
        self.assertTrue(offer["quantity_unconfirmed"])
        self.assertEqual(offer["document_stock"], 100)
        self.assertEqual(offer["source_row"], 2)
        self.assertIn(str(lid), offer["original_url"])
        wall = next(f for f in offer["features"] if f["field"] == "Толщина стенки")
        self.assertEqual(wall["value"], "4 мм")
        self.assertIn("89х4 мм", wall["evidence"])
        self.assertEqual(self.saved()[1], original)
        self.external.assert_not_called()

    def test_selection_quantity_and_terms_are_separate_and_invoice_uses_partial_quantity(self):
        from invoice_generator import build_invoice_xlsx
        from openpyxl import load_workbook
        self.catalogue()
        self.post("load_catalog")
        key = self.offer()["key"]
        self.accept_features(key)
        self.assertFalse(self.offer()["eligible"])
        self.terms(key)
        self.assertFalse(self.offer()["eligible"])
        self.post("confirm_proposal", position=1, key=key, quantity=9, note="Подтверждены девять труб по 6 м")
        view = self.offer()
        self.assertEqual((view["factor"], view["converted_quantity"], view["shortage"]), (6, 54, 6))
        self.assertFalse(view["eligible"])
        self.post("quantity", position=1, key=key, decision="accepted")
        self.assertTrue(self.offer()["eligible"])
        self.post("select", position=1, key=key)
        result, original = self.saved()
        row = result["items"][0]
        self.assertEqual(row["comparison_source"]["quantity"], 60)
        self.assertEqual((row["quantity"], row["purchase_units"]), (54, 9))
        self.assertEqual(row["purchase_price"], 732)
        self.assertEqual(row["proposed_unit_price"], 128.1)
        self.assertTrue(pipeline._included(row))
        result["customer"] = dict(name="Покупатель", inn="7716997861", kpp="770801001")
        book = load_workbook(io.BytesIO(build_invoice_xlsx(result)), data_only=True)
        values = [cell.value for line in book.active for cell in line]
        self.assertIn(54, values)
        self.assertEqual(json.loads(original)["orderItems"][0]["count"], 60)

    def test_thickness_rejection_propagates_to_api_and_catalogue_only_in_same_position(self):
        self.catalogue(name="Труба 89х2 мм", unit="м")
        self.catalogue(supplier="Поставщик Б", name="Труба 89x2,0 мм", unit="м")
        self.post("load_catalog")
        result, _ = self.saved()
        row = result["items"][0]
        row["candidates"].append(dict(supplier="API", name="Труба 89х2 мм", sku="API-2", price=10, unit="м"))
        result["items"].append({**copy.deepcopy(row), "position": 2, "comparison": {}})
        feature = next(f for f in comparison.offer_view(row, comparison.candidates(row)[0])["features"] if f["field"] == "Толщина стенки")
        comparison.mutate(result, dict(action="feature", position=1, feature=feature["id"], decision="rejected"))
        for c in comparison.candidates(row):
            self.assertTrue(any(f["state"] == "rejected" for f in comparison.offer_view(row, c)["features"] if f["field"] == "Толщина стенки"))
        self.assertFalse(any(f["state"] == "rejected" for c in comparison.candidates(result["items"][1]) for f in comparison.offer_view(result["items"][1], c)["features"]))

    def test_unknown_units_are_not_shared_acceptances(self):
        for supplier in ("А", "Б"):
            self.catalogue(supplier=supplier, name="Труба 89х4", unit="м")
        self.post("load_catalog")
        offers = self.model()["rows"][0]["offers"]
        a, b = [next(f for f in o["features"] if f["field"] == "Толщина стенки") for o in offers]
        self.assertTrue(a["ambiguous"])
        self.assertNotEqual(a["id"], b["id"])
        self.post("feature", position=1, feature=a["id"], decision="accepted")
        self.assertEqual(next(f for f in self.model()["rows"][0]["offers"][1]["features"] if f["id"] == b["id"])["state"], "pending")

    def test_new_file_resets_financial_and_quantity_confirmations_but_keeps_rejection(self):
        self.catalogue(name="Труба 89х2 мм", unit="м")
        self.post("load_catalog")
        offer = self.offer()
        key = offer["key"]
        wall = next(f for f in offer["features"] if f["field"] == "Толщина стенки")
        self.post("feature", position=1, feature=wall["id"], decision="rejected")
        self.terms(key, unit="м")
        self.post("confirm_proposal", position=1, key=key, quantity=56, note="56 м в предложении")
        self.post("quantity", position=1, key=key, decision="accepted")
        self.catalogue(name="Труба 89х2 мм", unit="м", price=700)
        self.post("load_catalog")
        view = self.offer()
        self.assertTrue(view["quantity_unconfirmed"])
        self.assertFalse(view["pricing_ready"])
        self.assertTrue(any(f["id"] == wall["id"] and f["state"] == "rejected" for f in view["features"]))

    def test_product_correction_is_separate_from_accepting_deviation(self):
        self.catalogue()
        self.post("load_catalog")
        view = self.offer()
        pid = int(view["product_url"].split("/")[-1])
        wall = next(f for f in view["features"] if f["field"] == "Толщина стенки")
        self.post("feature", position=1, feature=wall["id"], decision="rejected")
        with price_lists.database() as conn:
            self.assertEqual(price_lists.execute(conn, "SELECT overrides_json FROM price_product_cards WHERE id=?", (pid,)).fetchone()[0], "{}")
        prepared_catalog.correct_product(pid, "Толщина стенки", 3, "мм", "Проверено по сертификату")
        self.post("load_catalog")
        corrected = next(f for f in self.offer()["features"] if f["field"] == "Толщина стенки")
        self.assertEqual((corrected["value"], corrected["origin"]), ("3 мм", "manual"))
        self.assertEqual(corrected["state"], "pending")

    def test_duplicate_articles_are_distinct_warehouse_rows(self):
        self.catalogue(unit="м", rows=[["Труба 89х4 мм", 10, "DUP", "м", 1], ["Труба 89х4 мм", 11, "DUP", "м", 3]])
        self.post("load_catalog")
        offers = self.model()["rows"][0]["offers"]
        self.assertEqual(len(offers), 2)
        self.assertNotEqual(offers[0]["key"], offers[1]["key"])

    def test_changed_sales_unit_invalidates_quantity_and_fractional_packs_are_rejected(self):
        self.catalogue()
        self.post("load_catalog")
        key = self.offer()["key"]
        self.terms(key)
        self.post("confirm_proposal", position=1, key=key, quantity=10, note="10 штук")
        self.terms(key, unit="м")
        self.assertTrue(self.offer()["quantity_unconfirmed"])
        result, _ = self.saved()
        candidate = comparison.candidates(result["items"][0])[0]
        state = comparison.settings(result["items"][0])
        bridge.confirm_terms(state, key, candidate, dict(price=10, currency="RUB", vat_mode="exempt", vat_rate=0,
            unit="упак", price_date="2026-10-22", note="Упаковка"))
        with self.assertRaises(ValueError):
            bridge.confirm_proposal(state, key, candidate, dict(quantity=2.5, note="Дробная упаковка"))

    def test_refresh_preserves_catalogue_rules_without_a_new_search(self):
        self.catalogue()
        self.post("load_catalog")
        result, _ = self.saved()
        new = dict(items=[dict(position=1, requested_name="Труба 89х4 мм", quantity=60, unit="м")])
        with patch.object(prepared_catalog, "search", side_effect=AssertionError("Unexpected catalogue query")):
            comparison.preserve_comparison(new, result)
        self.assertEqual(len(new["items"][0]["catalog_candidates"]), 1)

    def test_alternatives_need_shared_characteristic_evidence(self):
        self.catalogue(rows=[
            ["Труба ORIO гофрированная 40х50 мм", 100, "DRAIN", "шт", 3],
            ["Труба 89х2 мм L=6м", 500, "STEEL", "шт", 10]])
        found = prepared_catalog.search("Труба 89х4 мм", include_conflicts=True)
        self.assertEqual([c["article"] for c in found["candidates"]], ["STEEL"])
        self.assertEqual(found["candidates"][0]["match"]["status"], "conflict")


if __name__ == "__main__":
    unittest.main()
