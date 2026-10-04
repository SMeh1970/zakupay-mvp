import copy
import json
import os
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import automation_pipeline as pipeline
import security
from saved_dashboard import listing_query


ORDER = {
    "id": 123, "name": "Трубы для школы", "creationDate": "2026-09-28T21:30:00Z",
    "finishDate": "2026-10-10", "delay": 0, "region": {"name": "Москва"},
    "customer": {"name": "ООО Строитель", "inn": "7716997861"},
    "orderItems": [{"goodName": "Труба стальная", "count": 2, "unit": {"name": "шт"},
                    "category": {"name": "Металлопрокат"}, "companiesWithOffersCount": 2}],
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
        self.tmp.cleanup()

    def ids(self, **params):
        response = self.client.get("/dashboard/automation", params=params)
        self.assertEqual(response.status_code, 200)
        return re.findall(r"<a class='title'[^>]*href='/dashboard/automation/jobs/(\d+)/review'", response.text)

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
                  "region": "моск", "category": "Металл", "payment": "prepayment", "min_positions": "1", "max_competitors": "2"}, self.first),
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
        with TestClient(main_ai.app) as client, patch.object(main_ai, "_valid_session", return_value=True), \
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
