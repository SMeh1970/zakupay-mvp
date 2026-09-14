import os
import tempfile
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
