"""The review table shows clarification reasons and close variants (read-only)."""
import copy
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import automation_pipeline as pipeline
from test_invoice_customer import ITEM, SELLER


class ClarificationDisplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.multiple(pipeline, DB_PATH=os.path.join(self.tmp.name, "jobs.db"), DATABASE_URL="", _initialized_database_key=None)
        self.db_patch.start()
        order = {"id": 37482415, "orderItems": [{"id": 10, "goodName": "Товар", "count": 2, "unit": {"name": "шт"}}]}
        item = {**copy.deepcopy(ITEM), "decision": "review", "match_status": "сомнительное",
                "candidates": [{"name": "Товар", "price": 100, "supplier": "ВИ"}],
                "clarification_reasons": ["диапазон размеров отличается: в заявке 56-64, в товаре 60-64 — требуется подтверждение"],
                "near_candidates": [{"name": "Хомут СВК <b>60-64</b>", "match_score": 0.463,
                                     "clarification_reasons": ["в названии не указан признак «прорезиненный» — нужно уточнить"]}]}
        result = {"order_id": order["id"], "invoice_number": 262, "seller": copy.deepcopy(SELLER), "items": [item],
                  "summary": {"positions": 1, "auto_ready": 0, "approved": 0, "included_in_invoice": 0}, "status": "ready_for_review"}
        with pipeline._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO automation_jobs (dedupe_key,order_id,event_type,status,order_json,result_json,created_at,updated_at,invoice_number) VALUES (?,?,?,?,?,?,?,?,?)",
                ("clar", order["id"], "api_order", "ready_for_review", json.dumps(order), json.dumps(result), "now", "now", 262),
            )
            self.job_id = cursor.lastrowid
        self.app = FastAPI()
        pipeline.install_automation_pipeline(self.app, Mock(return_value=order))
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_review_page_lists_reasons_and_close_variants_escaped(self):
        page = self.client.get(f"/dashboard/automation/jobs/{self.job_id}/review")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Уточнить: диапазон размеров отличается", page.text)
        self.assertIn("Близкие варианты (подтвердить вручную)", page.text)
        self.assertIn("не указан признак", page.text)
        self.assertNotIn("<b>60-64</b>", page.text)  # supplier text is escaped

    def test_old_results_without_clarification_fields_still_render(self):
        with pipeline._connect() as conn:
            row = conn.execute("SELECT result_json FROM automation_jobs WHERE id=?", (self.job_id,)).fetchone()
            result = json.loads(row[0])
            for key in ("clarification_reasons", "near_candidates"):
                result["items"][0].pop(key, None)
            conn.execute("UPDATE automation_jobs SET result_json=? WHERE id=?", (json.dumps(result), self.job_id))
        page = self.client.get(f"/dashboard/automation/jobs/{self.job_id}/review")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("Близкие варианты", page.text)
