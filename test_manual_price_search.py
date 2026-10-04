import copy
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import automation_pipeline as pipeline
from supplier_adapters import SupplierQuote


ORDER = {"id": 123456, "name": "Заявка для просмотра", "delay": 0,
         "customer": {"name": "Тестовый заказчик", "inn": "7716997861", "kpp": "770801001"},
         "orderItems": [{"goodName": "Маркер черный", "count": 2, "unit": {"name": "шт"}}]}


class InlineThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


class ManualPriceSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = patch.multiple(pipeline, DB_PATH=os.path.join(self.tmp.name, "jobs.db"),
                                      DATABASE_URL="", _initialized_database_key=None,
                                      _last_api_poll_started=0.0, API_POLL_ENABLED=True)
        self.settings.start()
        self.fetch = Mock()
        self.fetch_all = Mock(return_value=[copy.deepcopy(ORDER)])
        self.app = FastAPI()
        pipeline.install_automation_pipeline(self.app, self.fetch, fetch_all_orders=self.fetch_all)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.settings.stop()
        self.tmp.cleanup()

    def poll(self):
        with patch.object(pipeline, "_authorized_automation_call", return_value=True):
            return self.client.post("/automation/api/poll")

    def test_opening_sync_returns_to_list_without_import_or_price_search(self):
        with patch.object(pipeline, "build_vi_draft") as build:
            response = self.client.get("/dashboard/automation/sync")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.url.path, "/dashboard/automation")
            self.assertIn("Для загрузки заявок нажмите", response.text)
            self.fetch_all.assert_not_called()
            self.fetch.assert_not_called()
            build.assert_not_called()

    def test_sync_post_lost_during_login_returns_to_list_and_can_be_retried(self):
        import security

        security.install_security(self.app)
        with patch.object(security, "_valid_session", return_value=False):
            response = self.client.post("/dashboard/automation/sync", follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertIn("next=%2Fdashboard%2Fautomation%2Fsync", response.headers["location"])
            self.fetch_all.assert_not_called()

        with patch.object(security, "_valid_credentials", return_value=True), \
                patch.object(security, "_login_allowed", return_value=True), \
                patch.object(security, "_valid_session", return_value=True), \
                patch.object(pipeline, "build_vi_draft") as build:
            response = self.client.post("/login", data={
                "username": "test", "password": "test", "next": "/dashboard/automation/sync",
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.url.path, "/dashboard/automation")
            self.assertIn("Для загрузки заявок нажмите", response.text)
            self.fetch_all.assert_not_called()
            response = self.client.post("/dashboard/automation/sync", follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.fetch_all.assert_called_once_with(force=True)
            with pipeline._connect() as conn:
                job = conn.execute("SELECT status,invoice_number FROM automation_jobs").fetchone()
            self.assertEqual(job["status"], "pending_search")
            self.assertIsNone(job["invoice_number"])
            build.assert_not_called()

    def test_hourly_poll_sync_and_page_views_never_search_prices_or_issue_invoice(self):
        with patch.object(pipeline, "build_vi_draft") as build:
            response = self.poll()
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["price_search"], "manual_only")
            self.assertEqual(data["processed"][0]["status"], "pending_search")
            self.assertIsNone(data["processed"][0]["invoice_number"])
            job_id = data["processed"][0]["job_id"]
            self.assertIn("поиск цен не запускался", self.client.get("/dashboard/automation").text)
            review = self.client.get(f"/dashboard/automation/jobs/{job_id}/review")
            self.assertIn("Маркер черный", review.text)
            self.assertIn("Поиск цен ещё не запускался", review.text)
            self.assertIn("Найти цены по выбранным позициям", review.text)
            self.assertEqual(self.client.get(f"/dashboard/automation/jobs/{job_id}/invoice.xlsx").status_code, 409)
            self.client.post("/dashboard/automation/sync", follow_redirects=False)
            build.assert_not_called()
            self.fetch.assert_not_called()  # Missing line IDs do not trigger an extra order lookup at intake.

    def test_price_search_and_invoice_numbering_begin_only_after_explicit_start(self):
        first = pipeline.process_api_order(copy.deepcopy(ORDER))
        second = pipeline.process_api_order({**copy.deepcopy(ORDER), "id": 123457})
        quotes = [SupplierQuote("ВИ", "Маркер черный", price=10, stock=10)]
        real_thread = pipeline.threading.Thread
        def search_thread(*args, **kwargs):
            if str(kwargs.get("name") or "").startswith("supplier-search-"):
                return InlineThread(*args, **kwargs)
            return real_thread(*args, **kwargs)
        with patch.object(pipeline, "_search_candidates", return_value=(quotes, [], False)) as search, \
                patch.object(pipeline.threading, "Thread", search_thread), \
                patch("offer_panel_safe.requests.post") as external_post:
            for expected_number, imported in ((240, first), (241, second)):
                job_id = imported["job_id"]
                self.assertIsNone(imported["result"]["invoice_number"])
                self.client.get(f"/dashboard/automation/jobs/{job_id}/review")
                response = self.client.post(f"/dashboard/automation/jobs/{job_id}/start", data={"selected_position": "1"}, follow_redirects=False)
                self.assertEqual(response.status_code, 303)
                with pipeline._connect() as conn:
                    row = conn.execute("SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
                result = json.loads(row["result_json"])
                self.assertEqual(row["invoice_number"], expected_number)
                self.assertEqual(result["items"][0]["proposed_unit_price"], 10.5)
                self.assertFalse(result["live_offer_created"])
                self.assertEqual(self.client.get(f"/dashboard/automation/jobs/{job_id}/invoice.xlsx").status_code, 200)
            self.assertEqual(search.call_count, 2)
            external_post.assert_not_called()

    def test_start_without_selected_positions_does_not_search(self):
        imported = pipeline.process_api_order(copy.deepcopy(ORDER))
        with patch.object(pipeline, "build_vi_draft") as build:
            response = self.client.post(f"/dashboard/automation/jobs/{imported['job_id']}/start", data={})
            self.assertEqual(response.status_code, 400)
            build.assert_not_called()

    def test_repeated_intake_preserves_previously_reviewed_result(self):
        imported = pipeline.process_api_order(copy.deepcopy(ORDER))
        approved = {**imported["result"], "status": "ready_for_review", "invoice_number": 250,
                    "items": [{"requested_name": "Маркер черный", "decision": "approved"}],
                    "customer": {"name": "Подтверждённый плательщик"}}
        with pipeline._connect() as conn:
            conn.execute("UPDATE automation_jobs SET status='ready_for_review', invoice_number=250, result_json=? WHERE id=?",
                         (json.dumps(approved), imported["job_id"]))
        with patch.object(pipeline, "build_vi_draft") as build:
            response = self.poll()
            self.assertEqual(response.json()["already_processed"], 1)
            duplicate = pipeline.process_api_order(copy.deepcopy(ORDER))
            self.assertEqual(duplicate["result"], approved)
            build.assert_not_called()
