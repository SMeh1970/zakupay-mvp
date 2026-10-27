import copy
import gc
import json
import os
import re
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
import html
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import automation_pipeline as pipeline
import security
from saved_dashboard import listing_query, order_region
from zakupay_categories import load_categories, save_categories


ORDER = {
    "id": 123, "actualityStatus": "ACTUAL", "name": "Трубы для школы", "creationDate": "2026-09-28T21:30:00Z",
    "finishDate": "2026-10-10", "delay": 0, "region": {"name": "Москва"},
    "customer": {"name": "ООО Строитель", "inn": "7716997861"},
    "orderItems": [{"goodName": "Труба стальная", "count": 2, "unit": {"name": "шт"},
                    "category": {"id": 7, "name": "Металлопрокат"}, "companiesWithOffersCount": 2}],
}


class SavedDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = patch.multiple(pipeline, DB_PATH=os.path.join(self.tmp.name, "jobs.db"),
                                      DATABASE_URL="", _initialized_database_key=None)
        self.settings.start()
        self.fetch = Mock(side_effect=AssertionError("GET must not fetch live requests"))
        self.fetch_all = Mock(return_value=[])
        self.app = FastAPI()
        pipeline.install_automation_pipeline(self.app, self.fetch, fetch_all_orders=self.fetch_all)
        security.install_security(self.app)
        self.session = patch.object(security, "_valid_session", return_value=True)
        self.session.start()
        self.client = TestClient(self.app)
        self.first = pipeline.save_api_order_for_manual_start(copy.deepcopy(ORDER))
        self.second = pipeline.save_api_order_for_manual_start({**copy.deepcopy(ORDER), "id": 124,
                         "name": "Крепёж для склада", "creationDate": "2026-09-25", "delay": 30,
                         "region": {"name": "Казань"}, "customer": {"name": "Другой покупатель"}})
        self.manual = pipeline.save_manual_order("Ручная <заявка>", [{"goodName": "Болт", "count": 1}],
                                                reference="M-7", customer_name="Частный клиент")

    def tearDown(self):
        self.client.close()
        self.session.stop()
        self.settings.stop()
        gc.collect()  # Release SQLite handles before Windows removes the test directory.
        self.tmp.cleanup()

    def ids(self, **params):
        response = self.client.get("/dashboard/automation", params=params)
        self.assertEqual(response.status_code, 200)
        return re.findall(r"<a class='title'[^>]*href='/dashboard/automation/jobs/(\d+)/open'", response.text)

    def test_region_import_storage_and_existing_card_display(self):
        order = {**copy.deepcopy(ORDER), "id": 999, "region": {"id": 50, "name": "Московская <область>"},
                 "city": "Химки", "deliveryAddress": "г. Химки"}
        with patch.object(pipeline, "build_vi_draft") as search:
            saved = pipeline.save_api_order_for_manual_start(order)
            with pipeline._connect() as conn:
                row = conn.execute("SELECT * FROM automation_jobs WHERE id=?", (saved["job_id"],)).fetchone()
                self.assertEqual(json.loads(row["order_json"]), order)
            response = self.client.get("/dashboard/automation", params={"order_id": "999"})
            self.assertIn("<b>Регион:</b> Московская &lt;область&gt;", response.text)
            self.assertNotIn("Город:", response.text)
            self.assertNotIn("Химки", response.text)
            search.assert_not_called()
            self.fetch.assert_not_called()

    def test_region_is_never_inferred_from_city_or_address(self):
        for value in (None, "", "  ", {}, {"id": 50}, 50, [], {"name": 50}):
            with self.subTest(value=value):
                self.assertEqual(order_region({"region": value, "city": "Химки", "deliveryAddress": "г. Химки"}), "")
        self.assertEqual(order_region({"region": "  Московская область  "}), "Московская область")
        response = self.client.get("/dashboard/automation", params={"source": "manual"})
        self.assertIn("<b>Регион:</b> не указан", response.text)

    def test_repeat_import_backfills_legacy_and_email_snapshots(self):
        with pipeline._connect() as conn:
            legacy = {"order_name": ORDER["name"], "items": [{"requested_name": "Труба", "quantity": 2, "unit": "шт"}]}
            conn.execute("UPDATE automation_jobs SET order_json=NULL,result_json=? WHERE id=?", (json.dumps(legacy), self.first["job_id"]))
            conn.execute("UPDATE automation_jobs SET order_id=123,event_type='new_order',dedupe_key='email-region' WHERE id=?", (self.second["job_id"],))
        pipeline.save_api_order_for_manual_start({**copy.deepcopy(ORDER), "region": {"name": "Московская область"}})
        with pipeline._connect() as conn:
            rows = conn.execute("SELECT order_json FROM automation_jobs WHERE order_id=123").fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            snapshot = json.loads(row["order_json"])
            self.assertEqual(snapshot["region"], {"name": "Московская область"})
            self.assertTrue(snapshot["orderItems"])

    def test_repeat_import_refreshes_region_without_changing_processed_job(self):
        with pipeline._connect() as conn:
            conn.execute("UPDATE automation_jobs SET status='ready_for_review', viewed_at='2026-10-27' WHERE id=?", (self.first["job_id"],))
            before = dict(conn.execute("SELECT * FROM automation_jobs WHERE id=?", (self.first["job_id"],)).fetchone())
        fresh = {**copy.deepcopy(ORDER), "region": {"name": "Московская область"}, "name": "Другое имя"}
        with patch.object(pipeline, "build_vi_draft") as search:
            imported = pipeline.save_api_order_for_manual_start(fresh)
            self.assertTrue(imported["duplicate"])
            with pipeline._connect() as conn:
                after = dict(conn.execute("SELECT * FROM automation_jobs WHERE id=?", (self.first["job_id"],)).fetchone())
            expected_order = json.loads(before["order_json"])
            expected_order["region"] = fresh["region"]
            self.assertEqual(json.loads(after["order_json"]), expected_order)
            for key in before.keys() - {"order_json", "updated_at"}:
                self.assertEqual(after[key], before[key], key)
            self.assertEqual(self.ids(region="Московская область"), [str(self.first["job_id"])])
            # Omitted metadata leaves the stored region; explicit null clears it.
            pipeline.save_api_order_for_manual_start({k: v for k, v in fresh.items() if k != "region"})
            self.assertEqual(self.ids(region="Московская область"), [str(self.first["job_id"])])
            pipeline.save_api_order_for_manual_start({**fresh, "region": None, "city": "Химки"})
            response = self.client.get("/dashboard/automation", params={"order_id": "123"})
            self.assertIn("<b>Регион:</b> не указан", response.text)
            search.assert_not_called()

    def test_sort_is_request_date_not_insertion_order_and_unknown_dates_are_last(self):
        with pipeline._connect() as conn:
            conn.execute("UPDATE automation_jobs SET order_json=? WHERE id=?", (json.dumps({**ORDER, "creationDate": "invalid"}), self.second["job_id"]))
        self.assertEqual(self.ids(), [str(self.manual["job_id"]), str(self.first["job_id"]), str(self.second["job_id"])])
        response = self.client.get("/dashboard/automation")
        self.assertIn("29.09.2026", response.text)
        self.assertIn("Ручная &lt;заявка&gt;", response.text)
        self.assertNotIn("0 точных", response.text)
        self.assertNotIn("card bad", response.text)

    def test_filters_combine_use_saved_snapshots_and_do_not_search_prices(self):
        with patch.object(pipeline, "build_vi_draft") as search:
            cases = [
                ({"keyword": "СТАЛЬНАЯ", "customer": "строитель", "inn": "7716", "source": "zakupay",
                  "region": "моск", "category_id": "7", "payment": "prepayment", "min_positions": "1", "max_competitors": "2"}, self.first),
                ({"title": "склада", "payment": "delay", "delayFrom": "20", "delayTo": "30"}, self.second),
                ({"creationDateFrom": "2026-09-29", "creationDateTo": "2026-09-29", "finishDateTo": "2026-10-10"}, self.first),
                ({"source": "manual", "order_id": "M-7"}, self.manual),
                ({"order_id": "123", "status": "pending_search", "viewed": "no", "offer": "unsent"}, self.first),
            ]
            for filters, expected in cases:
                with self.subTest(filters=filters):
                    self.assertEqual(self.ids(**filters), [str(expected["job_id"])])
            self.assertEqual(self.ids(max_competitors="1"), [])
            self.assertEqual(self.ids(min_positions="2"), [])
            self.fetch.assert_not_called()
            self.fetch_all.assert_not_called()
            search.assert_not_called()

    def test_categories_use_real_ids_and_reject_invented_or_partial_names(self):
        changed = copy.deepcopy(ORDER)
        changed["orderItems"][0]["category"] = {"id": 71, "name": "Металлопрокат специальный"}
        with pipeline._connect() as conn:
            conn.execute("UPDATE automation_jobs SET order_json=? WHERE id=?", (json.dumps(changed), self.second["job_id"]))
        self.assertEqual(self.ids(category_id="7"), [str(self.first["job_id"])])
        self.assertEqual(self.ids(category_id="71"), [str(self.second["job_id"])])
        self.assertEqual(self.ids(category="Металлопрокат"), [str(self.first["job_id"])])
        self.assertEqual(self.ids(category="Металл"), [])
        self.assertEqual(self.ids(category_id="12345678"), [])
        response = self.client.get("/dashboard/automation", params={"customer": "no matches", "category_id": "7"})
        self.assertIn("type='checkbox' name='category_id' value='7' checked", response.text)
        self.assertIn("type='checkbox' name='category_id' value='71'", response.text)
        self.assertNotIn("<select name='category_id'>", response.text)
        self.assertNotIn("<input type='text' name='category'", response.text)
        self.assertIn("пока только категории сохранённых заявок", response.text)
        self.fetch.assert_not_called()
        self.fetch_all.assert_not_called()

    def test_multiple_rubrics_include_either_branch_and_validate_every_selection(self):
        changed = copy.deepcopy(ORDER)
        changed["orderItems"][0]["category"] = {"id": 71, "name": "Сантехника"}
        with pipeline._connect() as conn:
            conn.execute("UPDATE automation_jobs SET order_json=? WHERE id=?", (json.dumps(changed), self.second["job_id"]))
            save_categories(conn, pipeline._execute, [
                {"id": 1, "name": "Металл"}, {"id": 7, "name": "Металлопрокат", "parent": {"id": 1}},
                {"id": 2, "name": "Инженерные системы"}, {"id": 71, "name": "Сантехника", "parent": {"id": 2}},
            ])
        both = {str(self.first["job_id"]), str(self.second["job_id"])}
        self.assertEqual(set(self.ids(category_id=["7", "71"])), both)
        self.assertEqual(set(self.ids(category_id=["1", "2"])), both)
        self.assertEqual(set(self.ids(category_id=["1", "71"])), both)
        self.assertEqual(self.ids(category_id=["7", "7", ""]), [str(self.first["job_id"])])
        self.assertEqual(self.ids(category_id=["7", "invented"]), [])
        response = self.client.get("/dashboard/automation", params={"category_id": ["7", "71"]})
        self.assertIn("value='7' checked", response.text)
        self.assertIn("value='71' checked", response.text)
        self.assertIn("Выбрано: 2", response.text)
        self.assertIn("id='category-search'", response.text)
        self.assertIn("type='button'>Снять выбор рубрик", response.text)
        nonce = re.search(r"<script nonce='([^']+)'", response.text).group(1)
        self.assertIn(f"script-src 'nonce-{nonce}'", response.headers["content-security-policy"])
        self.assertNotIn("onclick=", response.text)
        self.fetch.assert_not_called()
        self.fetch_all.assert_not_called()

    def test_multiple_rubrics_persist_in_pagination_and_no_selection_means_all(self):
        for number in range(27):
            order = copy.deepcopy(ORDER)
            order["id"] = 1000 + number
            order["orderItems"][0]["category"] = {"id": 71 if number % 2 else 7, "name": "Другая рубрика" if number % 2 else "Металлопрокат"}
            pipeline.save_api_order_for_manual_start(order)
        response = self.client.get("/dashboard/automation", params={"category_id": ["7", "71"], "page_size": "25", "source": "zakupay"})
        next_url = html.unescape(re.search(r"href='([^']+)'>Следующая", response.text).group(1))
        params = parse_qs(urlsplit(next_url).query)
        self.assertEqual(params["category_id"], ["7", "71"])
        self.assertEqual(params["source"], ["zakupay"])
        page2 = self.client.get(next_url)
        self.assertIn("value='7' checked", page2.text)
        self.assertIn("value='71' checked", page2.text)
        self.assertIn("По фильтрам: <b>29</b>", page2.text)
        # Submitting a form with every checkbox cleared sends no category_id.
        self.assertIn(str(self.manual["job_id"]), self.ids(source="manual"))

    def test_official_parent_includes_children_and_survives_failed_refresh(self):
        reference = [{"id": 1, "name": "Стройматериалы"},
                     {"id": 7, "name": "Металлопрокат", "parent": {"id": 1}},
                     {"id": 99, "name": "Крепёж <спец>", "parent": {"id": 1}}]
        with pipeline._connect() as conn:
            save_categories(conn, pipeline._execute, reference)
        self.assertEqual(set(self.ids(category_id="1")), {str(self.first["job_id"]), str(self.second["job_id"])})
        self.assertEqual(self.ids(category_id="99"), [])
        response = self.client.get("/dashboard/automation", params={"category_id": "99"})
        self.assertIn("Стройматериалы → Крепёж &lt;спец&gt;", response.text)
        self.assertIn("Категории загружены из справочника Закупай", response.text)
        # A refresh without a configured callback cannot erase the cache.
        failed = self.client.post("/dashboard/automation/categories/sync", follow_redirects=False)
        self.assertIn("categories_notice=unavailable", failed.headers["location"])
        with pipeline._connect() as conn:
            self.assertEqual(len(load_categories(conn)[0]), 3)

    def test_legacy_snapshots_without_category_ids_use_exact_known_names(self):
        order = copy.deepcopy(ORDER)
        order["orderItems"][0]["category"] = {"name": "Товар без ID"}
        with pipeline._connect() as conn:
            conn.execute("UPDATE automation_jobs SET order_json=? WHERE id=?", (json.dumps(order), self.first["job_id"]))
        self.assertEqual(self.ids(category="Товар без ID"), [str(self.first["job_id"])])
        self.assertEqual(self.ids(category_id="name:товар без id"), [str(self.first["job_id"])])
        self.assertEqual(self.ids(category="Товар"), [])

    def test_category_refresh_reads_reference_only_and_bad_response_keeps_cache(self):
        fetch_categories = Mock(return_value=[{"id": 1, "name": "Официальная категория"}])
        app = FastAPI()
        pipeline.install_automation_pipeline(app, self.fetch, fetch_all_orders=self.fetch_all, fetch_categories=fetch_categories)
        security.install_security(app)
        # Control the startup thread so this also verifies the cache warm-up once.
        with patch.object(pipeline.threading, "Thread") as thread:
            with TestClient(app) as client:
                thread.call_args.kwargs["target"]()
                self.assertEqual(fetch_categories.call_count, 1)
                thread.call_args.kwargs["target"]()
                self.assertEqual(fetch_categories.call_count, 1)
                self.assertEqual(client.get("/dashboard/automation").status_code, 200)
                self.assertEqual(client.get("/dashboard/automation/categories/sync", follow_redirects=False).status_code, 303)
                self.assertEqual(fetch_categories.call_count, 1)
                good = client.post("/dashboard/automation/categories/sync", follow_redirects=False)
                self.assertIn("categories_notice=updated", good.headers["location"])
                fetch_categories.return_value = []
                failed = client.post("/dashboard/automation/categories/sync", follow_redirects=False)
                self.assertIn("categories_notice=unavailable", failed.headers["location"])
        with pipeline._connect() as conn:
            self.assertEqual(load_categories(conn)[0][0]["name"], "Официальная категория")
        self.fetch.assert_not_called()
        self.fetch_all.assert_not_called()

    def test_list_title_opens_review_and_persists_first_view(self):
        page = self.client.get("/dashboard/automation").text
        self.assertNotIn(">Открыть заявку</a>", page)
        self.assertNotIn(">Открыть</a>", page)
        path = re.search(r"<a class='title'[^>]*href='([^']+)'", page).group(1)
        job_id = int(path.split("/")[-2])
        self.assertEqual(self.client.head(path).status_code, 200)
        with pipeline._connect() as conn:
            self.assertIsNone(conn.execute("SELECT viewed_at FROM automation_jobs WHERE id=?", (job_id,)).fetchone()[0])
        self.assertEqual(self.client.get(path).status_code, 200)
        with pipeline._connect() as conn:
            viewed_at = conn.execute("SELECT viewed_at FROM automation_jobs WHERE id=?", (job_id,)).fetchone()[0]
        self.assertTrue(viewed_at)
        self.assertEqual(self.client.get(path).status_code, 200)
        with pipeline._connect() as conn:
            self.assertEqual(conn.execute("SELECT viewed_at FROM automation_jobs WHERE id=?", (job_id,)).fetchone()[0], viewed_at)
        page = self.client.get("/dashboard/automation").text
        self.assertIn(".card.read .title{color:#5f6368}", page)
        self.assertRegex(page, rf"<section class='card [^']* read'><div><a class='title'[^>]*href='{path}'")
        self.assertEqual(self.ids(viewed="yes"), [str(job_id)])

    def test_comparison_requires_actual_offers_not_request_positions(self):
        job_id = self.first["job_id"]
        link = f"href='/dashboard/automation/jobs/{job_id}/review#review-table'"
        self.assertNotIn(link, self.client.get("/dashboard/automation").text)
        for field in ("selected", "candidates", "rejected_candidates", "catalog_candidates"):
            for offer in (None, {"name": "Труба", "error": "Поиск не удался"},
                          {"name": "Труба", "supplier": "Тест", "price": 100}):
                with self.subTest(field=field, offer=offer):
                    item = {field: offer if field == "selected" else ([offer] if offer else [])}
                    result = {"items": [item]}
                    with pipeline._connect() as conn:
                        conn.execute("UPDATE automation_jobs SET status='ready_for_review',result_json=? WHERE id=?",
                                     (json.dumps(result), job_id))
                    page = self.client.get("/dashboard/automation").text
                    self.assertEqual(link in page, bool(offer and not offer.get("error")))

    def test_viewed_and_offer_filters_track_actual_state(self):
        self.client.get(f"/dashboard/automation/jobs/{self.first['job_id']}/review")
        self.assertEqual(self.ids(viewed="yes"), [str(self.first["job_id"])])
        with pipeline._connect() as conn:
            conn.execute("UPDATE automation_jobs SET status='offer_created' WHERE id=?", (self.first["job_id"],))
        self.assertEqual(self.ids(offer="sent"), [str(self.first["job_id"])])
        self.assertEqual(self.ids(status="offer_created"), [str(self.first["job_id"])])
        self.assertNotIn(str(self.first["job_id"]), self.ids(only_without_my_offer="true"))

    def test_blank_numeric_invalid_ranges_and_html_are_handled_without_422(self):
        self.assertEqual(len(self.ids(min_positions="", max_competitors="", page="bad")), 3)
        self.assertEqual(self.ids(creationDateFrom="bad-date"), [])
        self.assertEqual(self.ids(creationDateFrom="2026-10-03", creationDateTo="2026-09-01"), [])
        self.assertEqual(self.ids(min_positions="5", max_positions="1"), [])
        response = self.client.get("/dashboard/automation", params={"customer": "'><script>alert(1)</script>"})
        self.assertNotIn("<script>alert(1)</script>", response.text)
        self.assertIn("&lt;script&gt;", response.text)

    def test_quantity_range_uses_one_line_unit_and_selected_rubric(self):
        order = copy.deepcopy(ORDER)
        order["id"] = 400
        order["orderItems"] = [
            {"count": 150, "unit": {"name": "шт."}, "category": {"id": 7, "name": "Металл"}},
            {"count": 5.25, "unit": {"name": "кв. м"}, "category": {"id": 71, "name": "Плитка"}},
            {"count": 60, "unit": {"name": "м²"}, "category": {"id": 71, "name": "Плитка"}},
            {"count": 60, "unit": {"name": "м2"}, "category": {"id": 71, "name": "Плитка"}},
        ]
        saved = pipeline.save_api_order_for_manual_start(order)
        expected = [str(saved["job_id"])]
        self.assertEqual(self.ids(quantity_min="5,25", quantity_max="5.25", quantity_unit="м²"), expected)
        self.assertEqual(self.ids(quantity_min="150", quantity_max="150", quantity_unit="шт"), expected)
        self.assertEqual(self.ids(quantity_min="100", quantity_unit="м²"), [])
        self.assertEqual(self.ids(quantity_max="6", quantity_unit="м²", category_id="7"), [])
        self.assertEqual(self.ids(quantity_max="6", quantity_unit="м²", category_id="71"), expected)
        self.assertEqual(self.ids(quantity_unit="кв.м"), expected)
        self.fetch.assert_not_called()
        self.fetch_all.assert_not_called()

    def test_quantity_validation_zero_unknowns_and_legacy_snapshot(self):
        result = pipeline._pending_search_result(ORDER)
        result["items"] = [{"requested_name": "Остаток", "quantity": 0, "unit": "кг"}]
        with pipeline._connect() as conn:
            conn.execute("UPDATE automation_jobs SET order_json=NULL,result_json=? WHERE id=?", (json.dumps(result), self.first["job_id"]))
        self.assertEqual(self.ids(quantity_min="0", quantity_max="0", quantity_unit="кг"), [str(self.first["job_id"])])
        self.assertEqual(self.ids(quantity_min="0.001", quantity_unit="кг"), [])
        for params in (
            {"quantity_min": "1"}, {"quantity_min": "3", "quantity_max": "2", "quantity_unit": "шт"},
            {"quantity_min": "-1", "quantity_unit": "шт"}, {"quantity_min": "nan", "quantity_unit": "шт"},
            {"quantity_max": "inf", "quantity_unit": "шт"}, {"quantity_unit": "выдуманная единица"},
        ):
            with self.subTest(params=params):
                response = self.client.get("/dashboard/automation", params=params)
                self.assertEqual(response.status_code, 200)
                self.assertIn("class='filter-error'", response.text)
                self.assertEqual(self.ids(**params), [])

    def test_quantity_and_product_text_cannot_match_different_lines(self):
        order = copy.deepcopy(ORDER)
        order.update(id=401, name="Керамогранит и материалы для фасада")
        order["orderItems"] = [
            {"goodName": "Керамогранит серый 600х600", "count": 430, "unit": {"name": "кв.м."},
             "category": {"id": 7, "name": "Материалы"}},
            {"goodName": "Утеплитель фасадный", "count": 6600, "unit": {"name": "м²"},
             "category": {"id": 7, "name": "Материалы"}},
        ]
        saved = pipeline.save_api_order_for_manual_start(order)
        for params in ({"keyword": "керамогранит"}, {"keyword": "керамогранит", "category_id": "7"}):
            self.assertEqual(self.ids(**params, quantity_min="1000", quantity_unit="м²"), [])
            self.assertEqual(self.ids(**params, quantity_min="400", quantity_max="430", quantity_unit="м²"), [str(saved["job_id"])])
        response = self.client.get("/dashboard/automation", params={"keyword": "керамогранит", "quantity_min": "400", "quantity_unit": "м²"})
        proof = re.search(r"<div class='meta quantity-match'>(.*?)</div>", response.text).group(1)
        self.assertIn("Керамогранит серый 600х600", proof)
        self.assertIn("430 м²", proof)
        self.assertNotIn("Утеплитель", proof)
        # A title or another row's comment must not lend its keyword to a large unrelated quantity.
        order["orderItems"][0]["goodName"] = "Плитка напольная"
        with pipeline._connect() as conn:
            conn.execute("UPDATE automation_jobs SET order_json=? WHERE id=?", (json.dumps(order), saved["job_id"]))
        self.assertEqual(self.ids(keyword="керамогранит", quantity_min="400", quantity_unit="м²"), [])
        self.assertEqual(self.ids(keyword="керамогранит"), [str(saved["job_id"])])
        self.fetch.assert_not_called()
        self.fetch_all.assert_not_called()

    def test_active_filters_render_orange_state_and_quantity_survives_pagination(self):
        for number in range(26):
            pipeline.save_api_order_for_manual_start({**copy.deepcopy(ORDER), "id": 500 + number})
        response = self.client.get("/dashboard/automation", params={
            "source": "zakupay", "quantity_min": "0", "quantity_max": "2", "quantity_unit": "шт",
            "category_id": "7", "max_competitors": "0",
        })
        for name in ("source", "quantity_min", "quantity_max", "quantity_unit", "max_competitors"):
            self.assertRegex(response.text, rf"<label class='filter-cell active-filter'[^>]*>[^<]*<(?:input|select)[^>]*name='{name}'")
        self.assertIn("class='category-picker active-filter'", response.text)
        self.assertIn("background:#fff3e0", response.text)
        self.assertIn("control.addEventListener('input', update)", response.text)
        self.assertIn("picker.classList.toggle('active-filter', selected > 0)", response.text)
        # Remove the zero-competitor restriction to obtain the second page.
        response = self.client.get("/dashboard/automation", params={
            "source": "zakupay", "quantity_min": "0", "quantity_max": "2", "quantity_unit": "шт", "category_id": "7",
        })
        next_url = html.unescape(re.search(r"href='([^']+)'>Следующая", response.text).group(1))
        params = parse_qs(urlsplit(next_url).query)
        self.assertEqual(params["quantity_min"], ["0"])
        self.assertEqual(params["quantity_max"], ["2"])
        self.assertEqual(params["quantity_unit"], ["шт"])
        self.assertEqual(self.client.get(next_url).status_code, 200)
        reset = self.client.get("/dashboard/automation")
        self.assertNotIn("class='filter-cell active-filter'", reset.text)
        self.assertNotIn("class='category-picker active-filter'", reset.text)

    def test_pagination_keeps_filters_and_newest_order_beyond_old_300_limit(self):
        with pipeline._connect() as conn:
            for number in range(300):
                order = {**copy.deepcopy(ORDER), "id": 1000 + number, "creationDate": "2026-09-01"}
                conn.execute("INSERT INTO automation_jobs (dedupe_key,order_id,event_type,status,order_json,result_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                             (f"bulk-{number}", order["id"], "api_order", "pending_search", json.dumps(order), json.dumps(pipeline._pending_search_result(order)), "2026-10-04", "2026-10-04"))
        first_page = self.ids(source="zakupay", page_size="25")
        self.assertEqual(len(first_page), 25)
        self.assertEqual(first_page[0], str(self.first["job_id"]))
        second_page = self.ids(source="zakupay", page_size="25", page="2")
        self.assertEqual(len(second_page), 25)
        self.assertFalse(set(first_page) & set(second_page))
        response = self.client.get("/dashboard/automation?source=zakupay&page=2")
        self.assertIn("source=zakupay&amp;page=3", response.text)
        self.assertIn("По фильтрам: <b>302</b>", response.text)

    def test_manual_import_does_not_drop_fresh_requests_after_first_300(self):
        self.fetch_all.return_value = [{"id": number, "delay": 30} for number in range(300)] + [
            {**copy.deepcopy(ORDER), "id": 999, "creationDate": "2026-10-04"}]
        with patch.object(pipeline, "build_vi_draft") as search:
            response = self.client.post("/dashboard/automation/sync", follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertIn("added=1", response.headers["location"])
            self.assertEqual(len(self.ids(order_id="999")), 1)
            search.assert_not_called()

    def test_filter_read_excludes_large_supplier_candidate_payloads(self):
        with pipeline._connect() as conn:
            result = pipeline._pending_search_result(ORDER)
            result["items"] = [{"candidates": [{"name": "SENSITIVE-CANDIDATE-PAYLOAD"}]}]
            conn.execute("UPDATE automation_jobs SET result_json=? WHERE id=?", (json.dumps(result), self.first["job_id"]))
            row = conn.execute(listing_query() + " AND id=?", (self.first["job_id"],)).fetchone()
        self.assertNotIn("SENSITIVE-CANDIDATE-PAYLOAD", row["result_json"])
        self.assertEqual(json.loads(row["result_json"])["customer"]["name"], ORDER["customer"]["name"])

    def test_older_jobs_without_source_snapshot_remain_searchable(self):
        result = pipeline._pending_search_result(ORDER)
        result["items"] = [{"requested_name": "Редкий товар из старой заявки"}]
        with pipeline._connect() as conn:
            conn.execute("UPDATE automation_jobs SET order_json=NULL,result_json=? WHERE id=?", (json.dumps(result), self.first["job_id"]))
        self.assertEqual(self.ids(keyword="Редкий товар", customer="Строитель", creationDateFrom="2026-09-29"), [str(self.first["job_id"])])

    def test_repeat_import_preserves_viewed_state_and_does_not_duplicate_saved_requests(self):
        self.client.get(f"/dashboard/automation/jobs/{self.first['job_id']}/review")
        self.fetch_all.return_value = [copy.deepcopy(ORDER), {**copy.deepcopy(ORDER), "id": 777}]
        first = self.client.post("/dashboard/automation/sync", follow_redirects=False)
        second = self.client.post("/dashboard/automation/sync", follow_redirects=False)
        self.assertIn("added=1&duplicates=1", first.headers["location"])
        self.assertIn("added=0&duplicates=2", second.headers["location"])
        self.assertEqual(self.ids(viewed="yes"), [str(self.first["job_id"])])
        self.assertEqual(len(self.ids(order_id="777")), 1)

    def test_old_lists_redirect_without_running_old_handlers_and_login_uses_same_list(self):
        @self.app.get("/dashboard")
        @self.app.get("/dashboard/analysis")
        def forbidden_old_list():
            raise AssertionError("Old list must not run")

        for url in ("/dashboard", "/dashboard/analysis", "/dashboard/analysis/", "/dashboard/automation/"):
            response = self.client.get(url + "?source=manual", follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/dashboard/automation?source=manual")
        self.assertIn('value="/dashboard/automation"', self.client.get("/login").text)
        for next_path in ("", "/dashboard", "/dashboard/analysis?source=manual", "https://evil.example"):
            with patch.object(security, "_valid_credentials", return_value=True), patch.object(security, "_login_allowed", return_value=True):
                response = self.client.post("/login", data={"username": "test", "password": "test", "next": next_path}, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertTrue(response.headers["location"].startswith("/dashboard/automation"))
        self.assertEqual(security._safe_next("/dashboard/order/123/offer"), "/dashboard/order/123/offer")

    def test_manual_form_scripts_are_allowed_by_its_policy_and_one_line_can_be_saved(self):
        response = self.client.get("/dashboard/automation/manual")
        nonce = re.search(r"<script nonce='([^']+)'", response.text).group(1)
        self.assertIn(f"script-src 'nonce-{nonce}'", response.headers["content-security-policy"])
        self.assertNotIn("onclick=", response.text)
        self.assertEqual(response.text.count("name='item_name' required"), 1)
        saved = self.client.post("/dashboard/automation/manual", data={"title": "Тест одной строки", "customer_name": "Новый клиент",
                    "item_name": ["Труба", "", ""], "item_quantity": ["2", "", ""], "item_unit": ["шт", "шт", "шт"]}, follow_redirects=False)
        self.assertEqual(saved.status_code, 303)
        self.assertEqual(len(self.ids(source="manual", customer="Новый клиент")), 1)
        self.fetch.assert_not_called()
        self.fetch_all.assert_not_called()


class ProductionNavigationTests(unittest.TestCase):
    def test_legacy_detail_uses_saved_review_and_root_opens_unified_list(self):
        import main_ai
        with patch.object(main_ai.app.router, "on_startup", []), TestClient(main_ai.app) as client, \
                patch.object(main_ai, "_valid_session", return_value=True), \
                patch.object(main_ai, "saved_job_id", return_value=42) as lookup, \
                patch.object(security, "_valid_session", return_value=True), \
                patch.object(main_ai, "fetch_all_orders", side_effect=AssertionError("No external fetch")):
            for path in ("/dashboard/order/123", "/dashboard/analysis/order/123/"):
                response = client.get(path, follow_redirects=False)
                self.assertEqual(response.headers["location"], "/dashboard/automation/jobs/42/review")
                self.assertEqual(response.status_code, 303)
            lookup.assert_called_with(123)
            with patch.object(main_ai, "saved_job_id", return_value=None):
                response = client.get("/dashboard/order/999", follow_redirects=False)
                self.assertEqual(response.headers["location"], "/dashboard/automation?order_id=999")
            self.assertEqual(client.get("/", follow_redirects=False).headers["location"], "/dashboard/automation")


if __name__ == "__main__":
    unittest.main()
