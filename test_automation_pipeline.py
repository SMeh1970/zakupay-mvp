import os
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import load_workbook
from io import BytesIO

import automation_pipeline as pipeline
from supplier_adapters import SupplierQuote


def email(order_id=37299999, message_id="mail-1"):
    return f"""From: zakupay@sel-be.ru
To: test@example.com
Message-ID: <{message_id}@example.com>
Subject: Запрос предложения по заявке {order_id}
Content-Type: text/plain; charset=utf-8

Новая заявка {order_id}
""".encode()


ORDER = {
    "id": 37299999,
    "name": "Тест",
    "orderItems": [{"id": 10, "goodName": "Маркер черный 1 мм", "count": 10, "unit": {"name": "шт"}}],
}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        pipeline.DB_PATH = os.path.join(self.tmp.name, "automation.db")

    def tearDown(self):
        self.tmp.cleanup()

    @patch.object(pipeline.VseinstrumentiAdapter, "search")
    def test_builds_draft_and_deduplicates_message(self, search):
        search.return_value = [SupplierQuote(
            supplier="ВсеИнструменты.ру", name="Маркер черный 1 мм",
            sku="123", price=100, stock=50,
        )]
        fetch = lambda order_id, force=False: ORDER if order_id == 37299999 else None
        first = pipeline.process_email(email(), fetch)
        second = pipeline.process_email(email(), fetch)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["result"]["summary"]["positions"], 1)
        self.assertEqual(first["result"]["items"][0]["proposed_unit_price"], 105.0)
        self.assertEqual(first["result"]["invoice_number"], 240)
        self.assertEqual(second["result"]["invoice_number"], 240)
        self.assertEqual(first["result"]["vat_rate"], 0.22)
        self.assertEqual(first["result"]["prepayment_percent"], 100.0)
        self.assertTrue(first["result"]["delivery_included"])
        self.assertFalse(first["result"]["live_offer_created"])

    @patch.object(pipeline.VseinstrumentiAdapter, "search")
    def test_assigns_sequential_invoice_numbers(self, search):
        search.return_value = [SupplierQuote(
            supplier="ВсеИнструменты.ру", name="Маркер черный 1 мм",
            sku="123", price=100, stock=50,
        )]
        fetch = lambda order_id, force=False: ORDER
        first = pipeline.process_email(email(message_id="sequence-1"), fetch)
        second = pipeline.process_email(email(message_id="sequence-2"), fetch)
        self.assertEqual(first["result"]["invoice_number"], 240)
        self.assertEqual(second["result"]["invoice_number"], 241)

    def test_missing_order_is_recorded_as_failed(self):
        with self.assertRaisesRegex(RuntimeError, "Заявка не получена"):
            pipeline.process_email(email(message_id="mail-2"), lambda *_args, **_kwargs: None)

    @patch.object(pipeline.VseinstrumentiAdapter, "search")
    def test_partial_invoice_contains_only_confirmed_rows(self, search):
        order = {
            "id": 37299999,
            "orderItems": [
                {"id": 1, "goodName": "Маркер черный 1 мм", "count": 2, "unit": {"name": "шт"}},
                {"id": 2, "goodName": "Ковер 100x100 см", "count": 1, "unit": {"name": "шт"}},
            ],
        }

        def results(query, limit=8):
            if "Маркер" in query:
                return [SupplierQuote(supplier="ВИ", name="Маркер черный 1 мм", price=100, stock=10)]
            return [SupplierQuote(supplier="ВИ", name="Ковер 40x60 см", price=200, stock=10)]

        search.side_effect = results
        result = pipeline.process_email(email(message_id="partial"), lambda *_args, **_kwargs: order)
        self.assertEqual(result["status"], "ready_for_review")
        self.assertEqual(result["result"]["summary"]["included_in_invoice"], 1)
        self.assertEqual(result["result"]["summary"]["excluded_from_invoice"], 1)
        self.assertIn("attachment_base64", result["gmail_draft"])

        workbook = load_workbook(BytesIO(pipeline.build_invoice_xlsx(result["result"])))
        values = [cell.value for row in workbook.active.iter_rows() for cell in row]
        self.assertIn("Маркер черный 1 мм", values)
        self.assertNotIn("Ковер 40x60 см", values)

    @patch.object(pipeline.VseinstrumentiAdapter, "search")
    def test_pack_quantity_is_converted_before_stock_check(self, search):
        order = {
            "id": 37299999,
            "orderItems": [{
                "id": 1,
                "goodName": "Дюбель Tech-Krep 10x180 мм 130103",
                "count": 800,
                "unit": {"name": "шт"},
            }],
        }
        search.return_value = [SupplierQuote(
            supplier="ВИ",
            name="Дюбель Tech-Krep 10x180 мм, 200 шт 130103",
            article="130103",
            price=4000,
            stock=20,
            unit="Упаковка",
        )]
        draft = pipeline.build_vi_draft(order)
        item = draft["items"][0]
        self.assertEqual(item["pack_size"], 200)
        self.assertEqual(item["purchase_units"], 4)
        self.assertTrue(item["stock_confirmed"])
        self.assertEqual(item["decision"], "auto_ready")
        self.assertEqual(item["proposed_unit_price"], 21.0)

    @patch.object(pipeline.VseinstrumentiAdapter, "search")
    def test_delivery_date_allows_exact_item_without_numeric_stock(self, search):
        order = {
            "id": 37299999,
            "orderItems": [{"id": 1, "goodName": "Замок IEK YZK10-18-20-40", "count": 5, "unit": {"name": "шт"}}],
        }
        search.return_value = [SupplierQuote(
            supplier="ВИ",
            name="Замок IEK 18-20/40 YZK10-18-20-40",
            article="YZK10-18-20-40",
            price=1000,
            stock=None,
            courier_date="2026-09-18T03:00:00Z",
        )]
        item = pipeline.build_vi_draft(order)["items"][0]
        self.assertEqual(item["decision"], "auto_ready")
        self.assertEqual(item["availability_status"], "доступно к заказу, количество не подтверждено")

    @patch.object(pipeline.VseinstrumentiAdapter, "search")
    def test_conflicting_identifier_never_auto_accepted(self, search):
        order = {
            "id": 37299999,
            "orderItems": [{"id": 1, "goodName": "Контактор EKF km-2-32-20", "count": 1, "unit": {"name": "шт"}}],
        }
        search.return_value = [SupplierQuote(
            supplier="ВИ",
            name="Контактор EKF km-3-32-40",
            article="km-3-32-40",
            price=3000,
            stock=10,
        )]
        item = pipeline.build_vi_draft(order)["items"][0]
        self.assertNotEqual(item["decision"], "auto_ready")
        self.assertIn("не совпадает модель/артикул", item["replacement_details"])


if __name__ == "__main__":
    unittest.main()
