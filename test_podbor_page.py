import json
import os
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import automation_pipeline as pipeline
import podbor_page
import security
import supplier_comparison as comparison
from test_supplier_comparison import quote, result_with

H = {"X-Requested-With": "podbor"}


class PodborPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.multiple(pipeline, DB_PATH=os.path.join(self.tmp.name, "test.db"), DATABASE_URL="", _initialized_database_key=None)
        self.patch.start()
        self.app = FastAPI()
        pipeline.install_automation_pipeline(self.app, Mock(side_effect=AssertionError("no live fetch")))
        comparison.install_supplier_comparison(self.app)
        podbor_page.install(self.app)
        self.session = patch.object(security, "_valid_session", return_value=True)
        self.session.start()
        self.client = TestClient(self.app)
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        self.a, self.b = quote("А", name="Труба 89х4 мм", price_date=today), {**quote("Б", name="Труба 89х5 мм", price_date=today), "price": 90}
        self.result = result_with(self.a, self.b)
        with pipeline._connect() as conn:
            self.job = conn.execute("INSERT INTO automation_jobs (dedupe_key,order_id,event_type,status,result_json,created_at,updated_at) "
                                    "VALUES ('podbor',123,'zakupay_order','needs_review',?,'now','now')", (json.dumps(self.result),)).lastrowid
        self.url = f"/dashboard/automation/jobs/{self.job}/podbor"

    def tearDown(self):
        self.client.close(); self.session.stop(); self.patch.stop(); self.tmp.cleanup()

    def data(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.text)
        return json.loads(re.search(r'id="podbor-data">(.*?)</script>', response.text, re.S)[1]), response

    def test_page_serves_the_stand_with_request_positions(self):
        data, response = self.data()
        self.assertTrue(response.text.startswith("<!doctype html>"))
        self.assertIn("script-src 'nonce-", response.headers["content-security-policy"])
        self.assertNotIn("localStorage", response.text)
        position = data["positions"][0]
        self.assertEqual((position["id"], position["name"], position["qty"], position["unit"]), ("p1", "Труба 89х4 мм", 60, "м"))
        self.assertEqual([p[0] for p in position["products"]], ["Труба 89х4 мм", "Труба 89х5 мм"])
        self.assertEqual(position["products"][1][1:3], [90, "м"])
        self.assertEqual(position["products"][0][3]["key"], comparison.quote_key(self.a))
        self.assertEqual(data["chosen"], {})  # auto draft («auto_ready») is not a choice
        self.assertFalse(data["ai"])
        with patch.object(security, "_valid_session", return_value=False):
            self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_state_is_saved_on_server_and_returned(self):
        state = {"marks": {"p1": {"89х4": "accepted"}}, "positions": [{"id": "own1", "name": "Своя", "products": []}]}
        self.assertEqual(self.client.post(self.url + "/state", json={"state": state}).status_code, 403)
        response = self.client.post(self.url + "/state", json={"state": state}, headers=H)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.data()[0]["state"], state)
        self.client.post(self.url + "/state", json={"state": {"marks": {}}}, headers=H)
        self.assertEqual(self.data()[0]["state"], {"marks": {}})
        self.assertEqual(self.client.post(f"/dashboard/automation/jobs/999/podbor/state", json={"state": {}}, headers=H).status_code, 404)

    def test_choose_goes_to_invoice_and_back(self):
        data, _ = self.data()
        key = data["positions"][0]["products"][0][3]["key"]
        response = self.client.post(data["action_url"], headers=H, json={
            "action": "value_accept_select", "position": 1, "key": key, "csrf": data["csrf"], "revision": data["revision"]})
        self.assertEqual(response.status_code, 200, response.text)
        after, _ = self.data()
        self.assertEqual(after["chosen"], {"p1": "Труба 89х4 мм"})
        response = self.client.post(data["action_url"], headers=H, json={
            "action": "value_unselect", "position": 1, "csrf": after["csrf"], "revision": after["revision"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.data()[0]["chosen"], {})

    def test_ai_needs_key_and_returns_json(self):
        self.assertEqual(self.client.post(self.url + "/ai", json={"mode": "json", "prompt": "x"}, headers=H).status_code, 503)
        reply = Mock(ok=True, status_code=200)
        reply.json.return_value = {"content": [{"type": "text", "text": 'Вот: {"items":[{"i":0,"verdict":"подходит"}]}'}]}
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), patch.object(podbor_page.requests, "post", return_value=reply) as post:
            self.assertTrue(self.data()[0]["ai"])
            response = self.client.post(self.url + "/ai", json={"mode": "json", "prompt": "оцени"}, headers=H)
            self.assertEqual(response.json(), {"json": {"items": [{"i": 0, "verdict": "подходит"}]}})
            response = self.client.post(self.url + "/ai", json={"mode": "text", "turns": [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]}, headers=H)
            self.assertEqual(response.json(), {"text": 'Вот: {"items":[{"i":0,"verdict":"подходит"}]}'})
            self.assertEqual(post.call_args.kwargs["json"]["messages"], [{"role": "user", "content": "a\n\nb"}])
            self.assertEqual(self.client.post(self.url + "/ai", json={"mode": "x"}, headers=H).status_code, 400)


if __name__ == "__main__":
    unittest.main()
