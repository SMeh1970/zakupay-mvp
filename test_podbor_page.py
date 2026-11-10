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
        self.assertFalse(data.get("local"))  # the app page keeps decisions on the server
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

    def test_supplier_photo_and_same_name_from_two_suppliers(self):
        same = {**self.a, "supplier": "Б", "sku": "Б1", "price": 80, "image_url": "https://img.example/1.jpg"}
        bad = {**self.a, "supplier": "В", "sku": "В1", "image_url": "javascript:alert(1)"}
        result = result_with(self.a, same, bad)
        with pipeline._connect() as conn:
            conn.execute("UPDATE automation_jobs SET result_json=? WHERE id=?", (json.dumps(result), self.job))
        data, _ = self.data()
        metas = [p[3] for p in data["positions"][0]["products"]]
        self.assertEqual([m["supplier"] for m in metas], ["А", "Б", "В"])
        self.assertEqual([m["image_url"] for m in metas], ["", "https://img.example/1.jpg", ""])
        key = metas[1]["key"]
        response = self.client.post(data["action_url"], headers=H, json={
            "action": "value_accept_select", "position": 1, "key": key, "csrf": data["csrf"], "revision": data["revision"]})
        self.assertEqual(response.status_code, 200, response.text)
        after, _ = self.data()
        self.assertEqual(after["chosen"], {"p1": "Труба 89х4 мм"})
        self.assertEqual(after["chosenKey"], {"p1": key})

    def test_choose_is_refused_when_page_and_invoice_prices_differ(self):
        data, _ = self.data()
        key = data["positions"][0]["products"][0][3]["key"]
        wrong = {"unit_price": 0.4, "buy": 1, "total": 100}
        response = self.client.post(data["action_url"], headers=H, json={
            "action": "value_accept_select", "position": 1, "key": key, "csrf": data["csrf"], "revision": data["revision"], "page": wrong})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("Цена не сходится", response.json()["detail"])
        self.assertIn("6 000,00 ₽", response.json()["detail"])
        self.assertEqual(self.data()[0]["chosen"], {})  # nothing went to the invoice
        right = {"unit_price": 100, "buy": 60, "total": 6000}
        response = self.client.post(data["action_url"], headers=H, json={
            "action": "value_accept_select", "position": 1, "key": key, "csrf": data["csrf"], "revision": data["revision"], "page": right})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.data()[0]["chosen"], {"p1": "Труба 89х4 мм"})

    def test_request_page_and_stages(self):
        import request_steps
        request_steps.install(self.app)
        response = self.client.get(f"/dashboard/automation/jobs/{self.job}/request")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Труба 89х4 мм", response.text)
        self.assertIn(f"/dashboard/automation/jobs/{self.job}/podbor#p1", response.text)
        for title in ("Заявка", "Мониторинг", "Счёт", "Оплата", "Отгрузка"):
            self.assertIn(f"<b>{title}</b>", response.text)
        _, page = self.data()
        self.assertIn("aria-label='Этапы заявки'", page.text)
        self.assertIn("Повторить поиск у поставщиков", page.text)

    def test_page_can_choose_a_product_the_server_comparison_does_not_list(self):
        from datetime import datetime, timezone
        offer = dict(supplier="ВсеИнструменты.ру", name="Удлинитель силовой iTOK КГтп-ХЛ 5x2,5 мм 16А 380В IP67 ГОСТ 25 м", sku="x",
                     unit="Штука", price=20949, price_date=datetime.now(timezone.utc).date().isoformat())
        result = dict(order_id=1, markup=.05, items=[dict(position=1, requested_name="Переноска 25м 380В", quantity=2, unit="шт",
                                                          selected=None, candidates=[offer], decision="auto_ready")])
        with pipeline._connect() as conn:
            conn.execute("UPDATE automation_jobs SET result_json=? WHERE id=?", (json.dumps(result), self.job))
        data, _ = self.data()
        key = data["positions"][0]["products"][0][3]["key"]
        response = self.client.post(data["action_url"], headers=H, json={
            "action": "value_accept_select", "position": 1, "key": key, "csrf": data["csrf"], "revision": data["revision"],
            "page": {"unit_price": 20949, "buy": 2, "total": 41898}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.data()[0]["chosen"], {"p1": offer["name"]})
        with pipeline._connect() as conn:
            item = json.loads(conn.execute("SELECT result_json FROM automation_jobs WHERE id=?", (self.job,)).fetchone()["result_json"])["items"][0]
        self.assertTrue(comparison.selected_allowed(item))   # it stays in the invoice
        self.assertEqual((item["purchase_units"], item["decision"]), (2, "approved"))

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



class StandBuildTests(unittest.TestCase):
    def test_stand_is_built_from_the_app_page(self):
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))
        import build_podbor_stand
        html = build_podbor_stand.build()
        data = json.loads(re.search(r'id="podbor-data">(.*?)</script>', html, re.S)[1])
        self.assertTrue(data["local"])
        self.assertGreater(len(data["positions"]), 2)
        page = podbor_page.ASSET.read_text(encoding="utf-8")
        self.assertEqual(html.replace(re.search(r'<script type="application/json" id="podbor-data">.*?</script>', html, re.S)[0], ""), page)


if __name__ == "__main__":
    unittest.main()
