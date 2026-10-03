import json
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
    "customer": {"name": 'ООО «Тестовый покупатель»', "inn": "7716997861", "kpp": "770801001"},
    "delay": 0,
    "orderItems": [{"id": 10, "goodName": "Маркер черный 1 мм", "count": 10, "unit": {"name": "шт"}}],
}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        pipeline.DB_PATH = os.path.join(self.tmp.name, "automation.db")
        pipeline.DATABASE_URL = ""
        pipeline._initialized_database_key = None
        pipeline._last_api_poll_started = 0.0

    def tearDown(self):
        self.tmp.cleanup()

    def test_schema_initialization_is_cached_for_same_database(self):
        with pipeline._connect() as conn:
            conn.execute(
                "INSERT INTO automation_jobs "
                "(dedupe_key, order_id, event_type, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("once", 1, "test", "received", "now", "now"),
            )
        initialized_key = pipeline._initialized_database_key

        with pipeline._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM automation_jobs").fetchone()[0]

        self.assertEqual(initialized_key, pipeline._initialized_database_key)
        self.assertEqual(count, 1)

    def test_search_variants_expand_russian_catalog_word_forms(self):
        variants = pipeline._search_variants("Нарукавники брезентовые")
        self.assertIn("нарукавники брезентовые", variants)
        self.assertIn("брезентовые нарукавники", variants)
        self.assertIn("нарукавник брезентовый", variants)
        self.assertIn("брезентовый нарукавник", variants)
        self.assertIn("нарукавники", variants)
        self.assertIn("36641496", variants)

    def test_supplier_search_returns_position_diagnostics(self):
        class Supplier:
            code = "test"
            name = "Тестовый поставщик"

            def search(self, query, limit=8):
                return [SupplierQuote(
                    supplier="Тестовый поставщик",
                    sku="sku-1",
                    article="a-1",
                    name="Маркер черный 1 мм",
                    price=10.0,
                    unit="шт",
                )]

        quotes, diagnostics, timed_out = pipeline._search_candidates(
            Supplier(), "Маркер черный 1 мм", time_budget_seconds=5,
        )

        self.assertFalse(timed_out)
        self.assertEqual(len(quotes), 1)
        self.assertEqual(diagnostics[0]["supplier"], "Тестовый поставщик")
        self.assertGreaterEqual(diagnostics[0]["queries"], 1)
        self.assertEqual(diagnostics[0]["candidates"], 1)
        self.assertIn("seconds", diagnostics[0])

    def test_purchase_label_explains_pack_and_unit_price_basis(self):
        packed = pipeline._purchase_label(
            {"name": "Саморезы, 1000 шт.", "price": 1339.0, "unit": "Упаковка"},
            "шт",
        )
        single = pipeline._purchase_label(
            {"name": "Защитные очки", "price": 76.0, "unit": "шт"},
            "шт",
        )
        self.assertIn("закупка ВИ: 1339 ₽ за упаковку 1000 шт.", packed)
        self.assertIn("закупка ВИ: 76 ₽ за 1 шт", single)

    def test_refresh_can_rebuild_order_from_saved_job(self):
        order = pipeline._order_from_saved_result(
            {"order_id": 37247138, "subject": "Заявка из письма"},
            {
                "order_name": "Брезентовые изделия",
                "items": [{
                    "order_item_id": 1,
                    "requested_name": "Нарукавники брезентовые",
                    "quantity": 25,
                    "unit": "пара",
                }],
            },
        )
        self.assertEqual(order["source"], "saved_automation_job")
        self.assertEqual(order["orderItems"][0]["goodName"], "Нарукавники брезентовые")
        self.assertEqual(order["orderItems"][0]["unit"]["name"], "пара")

    def test_refresh_prefers_full_saved_order_snapshot(self):
        row = {
            "id": 89,
            "order_id": 37247138,
            "subject": "Заявка из письма",
            "order_json": json.dumps({
                "id": 37247138,
                "deliveryAddress": "Сохранённый адрес",
                "orderItems": [{"goodName": "Нарукавники брезентовые", "count": 25}],
            }, ensure_ascii=False),
        }
        order = pipeline._saved_order_snapshot(row, {"items": []})
        self.assertEqual(order["source"], "saved_order_snapshot")
        self.assertEqual(order["snapshotStorage"], "local_database")
        self.assertEqual(order["deliveryAddress"], "Сохранённый адрес")

    @patch.object(pipeline.VseinstrumentiAdapter, "search")
    def test_broad_catalog_variant_recovers_product(self, search):
        order = {
            "id": 37299999,
            "orderItems": [{
                "id": 1,
                "goodName": "Нарукавники брезентовые",
                "count": 10,
                "unit": {"name": "пар"},
            }],
        }

        def results(query, limit=8):
            if query == "нарукавники":
                return [SupplierQuote(
                    supplier="ВИ",
                    name="Нарукавники брезентовые ФАЕР РЕЗИСТ п.420гр",
                    sku="36641496",
                    price=175,
                    stock=100,
                )]
            return []

        search.side_effect = results
        item = pipeline.build_vi_draft(order)["items"][0]
        self.assertEqual(item["selected"]["sku"], "36641496")
        self.assertEqual(item["decision"], "review")

    @patch.object(pipeline.VseinstrumentiAdapter, "search")
    def test_imports_email_without_price_search_and_deduplicates_message(self, search):
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
        self.assertEqual(first["status"], "pending_search")
        self.assertEqual(first["result"]["items"], [])
        self.assertIsNone(first["result"]["invoice_number"])
        self.assertIsNone(second["result"]["invoice_number"])
        self.assertNotIn("gmail_draft", first)
        search.assert_not_called()
        self.assertFalse(first["result"]["live_offer_created"])
        with pipeline._connect() as conn:
            row = pipeline._execute(
                conn, "SELECT order_json FROM automation_jobs WHERE id=?", (first["job_id"],)
            ).fetchone()
        saved_order = json.loads(row["order_json"])
        self.assertEqual(saved_order["orderItems"][0]["goodName"], "Маркер черный 1 мм")

    @patch.object(pipeline.VseinstrumentiAdapter, "search")
    def test_email_import_does_not_reserve_invoice_numbers(self, search):
        search.return_value = [SupplierQuote(
            supplier="ВсеИнструменты.ру", name="Маркер черный 1 мм",
            sku="123", price=100, stock=50,
        )]
        fetch = lambda order_id, force=False: ORDER
        first = pipeline.process_email(email(message_id="sequence-1"), fetch)
        second = pipeline.process_email(email(message_id="sequence-2"), fetch)
        self.assertIsNone(first["result"]["invoice_number"])
        self.assertIsNone(second["result"]["invoice_number"])
        search.assert_not_called()

    def test_missing_order_is_recorded_as_failed(self):
        with self.assertRaisesRegex(RuntimeError, "Заявка не получена"):
            pipeline.process_email(email(message_id="mail-2"), lambda *_args, **_kwargs: None)

    @patch.object(pipeline.VseinstrumentiAdapter, "search")
    def test_partial_invoice_contains_only_confirmed_rows(self, search):
        order = {
            "id": 37299999,
            "delay": 0,
            "customer": ORDER["customer"],
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
        imported = pipeline.process_email(email(message_id="partial"), lambda *_args, **_kwargs: order)
        search.assert_not_called()
        result = pipeline.build_vi_draft(order, invoice_number=240)
        self.assertEqual(result["status"], "ready_for_review")
        self.assertEqual(result["summary"]["included_in_invoice"], 1)
        self.assertEqual(result["summary"]["excluded_from_invoice"], 1)
        self.assertIn("attachment_base64", pipeline._gmail_draft_payload(result, imported["job_id"]))

        workbook = load_workbook(BytesIO(pipeline.build_invoice_xlsx(result)))
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
        self.assertIsNone(item["selected"])
        self.assertEqual(item["match_status"], "не соответствует")
        self.assertIn("не совпадает модель/артикул", item["rejected_candidates"][0]["hard_conflicts"])

    @patch.object(pipeline.VseinstrumentiAdapter, "search")
    def test_different_product_type_is_rejected_even_when_number_matches(self, search):
        order = {
            "id": 37299999,
            "orderItems": [{"id": 1, "goodName": "Мешки 230 л", "count": 30, "unit": {"name": "шт"}}],
        }
        search.return_value = [SupplierQuote(
            supplier="ВИ",
            name="Компрессор Вихрь КМП-230/24",
            sku="12345",
            price=10048,
            stock=5,
        )]
        item = pipeline.build_vi_draft(order)["items"][0]
        self.assertIsNone(item["selected"])
        self.assertEqual(item["match_status"], "не соответствует")
        self.assertTrue(any("не совпадает тип товара" in x for x in item["rejected_candidates"][0]["hard_conflicts"]))

    @patch.object(pipeline.VseinstrumentiAdapter, "search")
    def test_manual_repeat_search_excludes_previously_shown_candidate(self, search):
        search.return_value = [
            SupplierQuote(supplier="ВИ", name="Мешки для мусора 230 л", sku="old", price=100, stock=50),
            SupplierQuote(supplier="ВИ", name="Мешки строительные 230 л", sku="new", price=120, stock=50),
        ]
        item = {"id": 1, "goodName": "Мешки 230 л", "count": 30, "unit": {"name": "шт"}}
        excluded = {pipeline._candidate_key({"supplier": "ВИ", "sku": "old"})}
        row = pipeline._build_match_row(1, item, [pipeline.VseinstrumentiAdapter()], excluded)
        self.assertEqual(row["selected"]["sku"], "new")
        self.assertIn(excluded.pop(), row["excluded_candidate_keys"])

    def test_manual_import_saves_order_without_supplier_search(self):
        outcome = pipeline.save_api_order_for_manual_start(ORDER)
        self.assertFalse(outcome["duplicate"])
        with pipeline._connect() as conn:
            row = pipeline._execute(conn, "SELECT * FROM automation_jobs WHERE id=?", (outcome["job_id"],)).fetchone()
        result = json.loads(row["result_json"])
        self.assertEqual(row["status"], "pending_search")
        self.assertEqual(result["summary"]["positions"], 1)
        self.assertEqual(result["items"], [])

    def test_order_selection_keeps_only_checked_positions(self):
        order = {
            "id": 77,
            "orderItems": [
                {"goodName": "Первая"},
                {"goodName": "Вторая"},
                {"goodName": "Третья"},
            ],
        }
        selected = pipeline._order_with_selected_positions(order, {1, 3})
        self.assertEqual([item["goodName"] for item in selected["orderItems"]], ["Первая", "Третья"])
        self.assertEqual(len(order["orderItems"]), 3)

    def test_operator_approved_row_is_included_in_partial_invoice(self):
        draft = {
            "invoice_number": 240,
            "order_id": 1,
            "customer": ORDER["customer"],
            "items": [
                {"decision": "approved", "requested_name": "Товар 1", "selected": {"name": "Аналог 1"}, "quantity": 2, "unit": "шт", "proposed_unit_price": 100},
                {"decision": "excluded", "requested_name": "Товар 2", "selected": {"name": "Аналог 2"}, "quantity": 1, "unit": "шт", "proposed_unit_price": 200},
            ],
        }
        workbook = load_workbook(BytesIO(pipeline.build_invoice_xlsx(draft)))
        values = [cell.value for row in workbook.active.iter_rows() for cell in row]
        self.assertIn("Аналог 1", values)
        self.assertNotIn("Аналог 2", values)

    @patch.object(pipeline.VseinstrumentiAdapter, "search")
    def test_api_order_is_persisted_and_deduplicated_by_order_id(self, search):
        search.return_value = [SupplierQuote(
            supplier="ВИ", name="Маркер черный 1 мм", sku="123", price=100, stock=50,
        )]
        first = pipeline.process_api_order(ORDER)
        second = pipeline.process_api_order(ORDER)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertIsNone(first["result"]["invoice_number"])
        self.assertEqual(first["status"], "pending_search")
        search.assert_not_called()
        context = pipeline.load_automation_offer_context(ORDER["id"])
        self.assertEqual(context["job_id"], first["job_id"])
        self.assertEqual(context["order"]["orderItems"][0]["id"], 10)

        pipeline.mark_automation_offer_created(first["job_id"], offer_id="offer-1", file_id="file-1")
        context = pipeline.load_automation_offer_context(ORDER["id"])
        self.assertTrue(context["result"]["live_offer_created"])
        self.assertEqual(context["result"]["live_offer_id"], "offer-1")

    @patch.object(pipeline.VseinstrumentiAdapter, "search")
    def test_missing_line_ids_are_enriched_once_and_persisted(self, search):
        search.return_value = [SupplierQuote(
            supplier="ВИ", name="Маркер черный 1 мм", sku="123", price=100, stock=50,
        )]
        order_without_ids = {
            "id": ORDER["id"], "name": "Тест", "delay": 0,
            "orderItems": [{"goodName": "Маркер черный 1 мм", "count": 10, "unit": {"name": "шт"}}],
        }
        created = pipeline.process_api_order(order_without_ids)
        context = pipeline.enrich_automation_offer_context(ORDER["id"], ORDER)
        self.assertEqual(context["job_id"], created["job_id"])
        self.assertEqual(context["result"]["items"], [])
        self.assertTrue(context["result"]["order_id_enrichment_found"])
        self.assertEqual(context["order"]["orderItems"][0]["id"], 10)

    def test_line_ids_accept_documented_payload_variants(self):
        self.assertEqual(pipeline._order_item_id({"orderItemId": 41}), 41)
        self.assertEqual(pipeline._order_item_id({"itemId": "42"}), "42")
        self.assertEqual(pipeline._order_item_id({"orderItem": {"id": 43}}), 43)
        merged = pipeline._merge_line_ids(
            {"orderItems": [{"goodName": "Анкер-клин 6x60"}]},
            {"orderItems": [{"goodName": "Анкер-клин 6x60", "orderItemId": 44}]},
        )
        self.assertEqual(merged["orderItems"][0]["id"], 44)

    @patch.object(pipeline, "build_vi_draft")
    def test_failed_api_order_can_be_retried(self, build):
        first = pipeline.process_api_order(ORDER)
        with pipeline._connect() as conn:
            conn.execute("UPDATE automation_jobs SET status='failed', result_json=NULL WHERE id=?", (first["job_id"],))
        retried = pipeline.process_api_order(ORDER)
        self.assertFalse(retried["duplicate"])
        self.assertEqual(retried["status"], "pending_search")
        self.assertEqual(retried["job_id"], first["job_id"])
        build.assert_not_called()

    def test_commercial_hash_ignores_unrelated_metadata_but_detects_quantity(self):
        saved = dict(ORDER, irrelevantServerField="one")
        same = dict(ORDER, irrelevantServerField="two")
        changed = dict(ORDER)
        changed["orderItems"] = [dict(ORDER["orderItems"][0], count=11)]
        self.assertEqual(pipeline.commercial_order_hash(saved), pipeline.commercial_order_hash(same))
        self.assertNotEqual(pipeline.commercial_order_hash(saved), pipeline.commercial_order_hash(changed))
        self.assertIn(
            "изменились позиции, количества или единицы измерения",
            pipeline.commercial_order_changes(saved, changed),
        )

    def test_manual_order_is_saved_without_zakupay_and_can_be_reviewed(self):
        saved = pipeline.save_manual_order(
            "Заявка клиента № 154",
            [{"goodName": "Анкер-клин 6x60", "count": "25", "unit": "шт"}],
            reference="154",
            customer_name="Тестовый заказчик",
            customer_inn="1234567890",
        )
        self.assertLess(saved["order_id"], 0)
        context = pipeline.load_automation_offer_context(saved["order_id"])
        self.assertEqual(context["result"]["source_type"], "manual")
        self.assertEqual(context["result"]["manual_reference"], "154")
        self.assertEqual(context["order"]["source"], "manual_entry")
        self.assertEqual(context["order"]["orderItems"][0]["goodName"], "Анкер-клин 6x60")

    def test_manual_order_rejects_empty_or_invalid_positions(self):
        with self.assertRaisesRegex(ValueError, "хотя бы одну позицию"):
            pipeline.save_manual_order("Пустая", [])
        with self.assertRaisesRegex(ValueError, "количество больше нуля"):
            pipeline.save_manual_order(
                "Ошибка",
                [{"goodName": "Товар", "count": "0", "unit": "шт"}],
            )

    @patch.object(pipeline.VseinstrumentiAdapter, "search")
    def test_offer_submission_claim_is_atomic_and_unknown_blocks_retry(self, search):
        search.return_value = [SupplierQuote(
            supplier="ВИ", name="Маркер черный 1 мм", sku="123", price=100, stock=50,
        )]
        created = pipeline.process_api_order(ORDER)
        arguments = (
            created["job_id"], ORDER["id"], "attempt-1", "guid-1",
            pipeline.commercial_order_hash(ORDER), "payload-1", "240", "file-1", {"offer": 1},
        )
        first = pipeline.begin_offer_submission(*arguments)
        second = pipeline.begin_offer_submission(*arguments)
        self.assertTrue(first["claimed"])
        self.assertFalse(second["claimed"])
        self.assertEqual(second["status"], "sending")

        pipeline.finish_offer_submission(first["id"], "unknown", error="timeout")
        third = pipeline.begin_offer_submission(*arguments)
        self.assertFalse(third["claimed"])
        self.assertEqual(third["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
