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

    def test_competitor_best_is_matched_by_order_item_id_only(self):
        result = {"items": [{"position": 1, "order_item_id": 11}, {"position": 2, "order_item_id": 12},
                            {"position": 3, "order_item_id": 13}, {"position": 4}]}
        order = {"orderItems": [
            {"id": 11, "bestOfferItem": " Скотч 48мм ", "companiesWithOffersCount": 3},
            {"id": 12, "bestOfferItem": None, "companiesWithOffersCount": 0},
            {"id": 13, "bestOfferItem": "Чужой", "companiesWithOffersCount": 2}]}
        prices = {11: {"bestPrice": {"price": 29.7, "priceInDefaultCurrency": 29.7, "currency": "₽"}},
                  12: {"bestPrice": None}, 14: {"bestPrice": {"price": 5, "priceInDefaultCurrency": 5}}}
        out = podbor_page.competitor_best(result, order, prices)
        self.assertEqual(out, {"p1": {"price": 29.7, "name": "Скотч 48мм", "offers": 3},
                               "p2": {"price": None, "name": None, "offers": 0}})  # p3: no price; p4: no id; id 14 is not ours

    def test_best_endpoint_gives_prices_names_and_never_breaks_the_page(self):
        result = json.loads(json.dumps(self.result))
        result["items"][0]["order_item_id"] = 77
        with pipeline._connect() as conn:
            conn.execute("UPDATE automation_jobs SET result_json=? WHERE id=?", (json.dumps(result), self.job))
        data, _ = self.data()
        self.assertTrue(data["best_url"].endswith("/podbor/best"))
        row = {77: {"bestPrice": {"price": 240.75, "priceInDefaultCurrency": 240.75, "currency": "₽"}}}
        live = {"orderItems": [{"id": 77, "bestOfferItem": "Грунтовка БС-321", "companiesWithOffersCount": 4}]}
        with patch("browser_prices.fetch_best_prices", return_value={"ok": True, "enabled": True, "prices": row, "errors": []}) as fetch, \
             patch("main.fetch_order_by_id", return_value=live):
            body = self.client.get(self.url + "/best")
        self.assertEqual(body.status_code, 200)
        self.assertEqual(fetch.call_args.args[0], [77])
        self.assertEqual(body.json()["items"], {"p1": {"price": 240.75, "name": "Грунтовка БС-321", "offers": 4}})
        # live order unavailable: the price still shows, without a name
        with patch("browser_prices.fetch_best_prices", return_value={"ok": True, "enabled": True, "prices": row, "errors": []}), \
             patch("main.fetch_order_by_id", side_effect=RuntimeError("down")):
            body = self.client.get(self.url + "/best").json()
        self.assertEqual(body["items"]["p1"], {"price": 240.75, "name": None, "offers": None})
        self.assertFalse(body["names"])
        # source not configured / session expired: a note, no prices, no error
        with patch("browser_prices.fetch_best_prices", return_value={"ok": False, "enabled": False, "prices": {}, "errors": ["not_configured"]}):
            body = self.client.get(self.url + "/best").json()
        self.assertEqual((body["ok"], body["items"]), (False, {}))
        self.assertIn("не настроен", body["note"])

    def test_summary_table_has_competitor_price_difference_columns(self):
        _, page = self.data()
        for title in ("Лучшая цена конкурента за ед.", "Разница, %", "Разница, ₽", "Товар конкурента по лучшей цене"):
            self.assertIn(title, page.text)
        # difference = our price per request unit minus the competitor's, never guessed without both prices
        self.assertIn("function vsCompetitor(pos,o)", page.text)
        self.assertIn("c.price==null", page.text)
        self.assertIn("o.unitPrice==null", page.text)

    def test_position_title_is_pinned_to_the_top_of_the_screen(self):
        _, page = self.data()
        self.assertIn(".pos-work>.req{position:sticky;top:0", page.text)
        # a scrolling wrapper around the summary table would stop the sticky title on wide screens
        self.assertIn("@media (min-width:900px){.tablewrap.summary-wrap{overflow:visible}}", page.text)
        self.assertIn("tablewrap summary-wrap", page.text)

    def test_clean_button_shows_request_without_brands(self):
        data, page = self.data()
        self.assertIn("makita", data["brands"])  # the app's own brand list goes to the page
        self.assertIn("function cleanName(name,extra)", page.text)
        self.assertIn("'Очистка'", page.text)
        self.assertIn("Заявка без брендов", page.text)
        # a brand is removed only from the lists or from what the person typed; the rest is only suggested
        self.assertIn("function possibleBrands(names,extra)", page.text)
        self.assertIn("Свои бренды (через запятую)", page.text)

    def test_navigation_row_has_only_the_checkbox_with_a_hint(self):
        _, page = self.data()
        # the text is a hint, the request line is not repeated next to the buttons
        self.assertIn("title:'После выбора — закрыть и открыть следующую. Поставьте галочку заранее", page.text)
        self.assertNotIn("render();}}),'После выбора — закрыть и открыть следующую')", page.text)
        self.assertIn("r.pos.example?null:el('button'", page.text)

    def test_other_product_type_is_never_close_and_same_type_is(self):
        _, page = self.data()
        # valve instead of a tee: critical difference, goes to «Остальные» and is not recommended
        self.assertIn("const typeDiff=dReq.some(x=>x.f==='Тип товара')", page.text)
        self.assertIn("a.typeDiff-b.typeDiff", page.text)
        self.assertIn("const isNear=r=>!r.typeDiff&&r.dReq.length<=2&&(r.same.length>=2||r.same.some(x=>x.f==='Тип товара'))", page.text)
        # the group, the filter counts and the recommendation use the same rule
        self.assertEqual(page.text.count("isNear("), 3)
        self.assertIn("ranked.find(isNear)", page.text)

    def test_words_glued_by_slash_hyphen_or_dot_are_split_when_one_is_a_known_characteristic(self):
        _, page = self.data()
        # «Тройник/чугун», «чугун-ТФ», «ВЧШГ-чугун»: the parts are separate words; abbreviations and models stay whole
        self.assertIn("if(/^[a-zа-яё]+(?:[./-][a-zа-яё]+)+$/i.test(m[0]))", page.text)
        self.assertIn("parts.some(q=>q[0].length>=3&&lookup(", page.text)

    def test_supplier_rubrics_reach_the_page_as_a_filter(self):
        # rubrics: breadcrumbs without «Главная» and the product's own name, last three; else the price-list category; nothing given -> none
        self.assertEqual(podbor_page.rubric({"name": "Тройник 1", "breadcrumbs": ["Главная", "Сантехника", "Фитинги", "Тройники", "Тройник 1"]}),
                         ["Сантехника", "Фитинги", "Тройники"])
        self.assertEqual(podbor_page.rubric({"name": "x", "breadcrumbs": ["А", "Б", "В", "Г"]}), ["Б", "В", "Г"])
        self.assertEqual(podbor_page.rubric({"name": "x", "breadcrumbs": "Каталог"}), [])
        self.assertEqual(podbor_page.rubric({"name": "x", "category": "Тройники"}), ["Тройники"])
        self.assertEqual(podbor_page.rubric({"name": "x", "category": "Не определена"}), [])
        self.assertEqual(podbor_page.rubric({"name": "x"}), [])
        result = json.loads(json.dumps(self.result))
        item = result["items"][0]
        for candidate in [item.get("selected")] + list(item.get("candidates") or []) + list(item.get("rejected_candidates") or []) + list(item.get("catalog_candidates") or []):
            if candidate:
                candidate["breadcrumbs"] = ["Главная", "Сантехника", "Тройники"]
        with pipeline._connect() as conn:
            conn.execute("UPDATE automation_jobs SET result_json=? WHERE id=?", (json.dumps(result), self.job))
        data, page = self.data()
        self.assertIn(["Сантехника", "Тройники"], [p[3]["cat"] for p in data["positions"][0]["products"]])
        # the page turns them into the rubric «Раздел каталога» (✓/✕ like the others), but never into a rubric for the AI to fill from names
        self.assertIn("field:'Раздел каталога',value:String(v)", page.text)
        self.assertIn("FIELDS.filter(f=>f!=='Раздел каталога').join('; ')", page.text)

    def test_pinned_card_holds_navigation_and_title_without_order_line_or_ai_notice(self):
        _, page = self.data()
        # navigation row goes into the pinned title card (above the name), so both stay on screen together
        self.assertIn("function renderPosition(pos,nav){", page.text)
        self.assertIn("el('div',{class:'req'},\n    nav||null,\n    el('b',{},pos.name)", page.text)
        self.assertIn("renderPosition(r.pos,nav)", page.text)
        self.assertIn("r.pos.products.length?null:nav,work", page.text)
        self.assertIn(".req>.posnav{flex:0 0 100%;margin:0}", page.text)
        # the card shows name and quantity only: no «Заявка Закупай № … · позиция N» line there
        self.assertNotIn("pos.example?el('span',{class:'note'},pos.example)", page.text)
        # the «Подбор ИИ» bar is not drawn at all while AI is not connected and nothing was saved; no key-missing notice in it
        self.assertIn("if(!sampleFn&&!saved)return null;", page.text)
        self.assertIn("const aiPick=aiPickPanel(pos,M); if(aiPick)main.append(aiPick);", page.text)

    def test_site_column_with_fill_down(self):
        _, page = self.data()
        # own checkbox → extra column; typing a site shows «↓», which puts it into the positions below
        self.assertIn("Сайт для поиска", page.text)
        self.assertIn("function siteCell(r,shown)", page.text)
        self.assertIn("for(const x of shown.slice(shown.indexOf(r)+1))state.sites[x.pos.id]=v;", page.text)
        self.assertIn("state.siteCol?siteCell(r,shown):null", page.text)
        self.assertIn("colspan:state.siteCol?13:12", page.text)
        # «Яндекс» button: site:домен + название позиции без брендов; a site given as a name goes in as a word
        self.assertIn("function yandexUrl(pos,site)", page.text)
        self.assertIn("'https://yandex.ru/search/?text='+encodeURIComponent((isDomain?'site:'+host:typed)+' '+(cleanName(pos.name,state.brands).text||pos.name))", page.text)

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
