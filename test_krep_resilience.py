import os
import time
import unittest
from unittest.mock import Mock, patch

import requests

from supplier_adapters import KrepKompAdapter
from test_krep_diagnostics import ITEM, ok, reset_krep_state

NEW_ITEM = {"Код": "002", "Наименование": "Гайка М8", "ПолноеНаименование": "Гайка М8 оцинкованная",
            "Артикул": "g8", "Категория": "Гайки"}


def wait_for_refresh(timeout=3.0):
    deadline = time.time() + timeout
    while KrepKompAdapter._refreshing and time.time() < deadline:
        time.sleep(0.01)
    assert not KrepKompAdapter._refreshing, "background refresh did not finish"


class KrepResilienceTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"KREP_KOMP_API_USERNAME": "u", "KREP_KOMP_API_PASSWORD": "p",
                                           "KREP_KOMP_STORAGE_NAMES": ""}, clear=False)
        self.env.start()
        self.saved_row = None  # (items, expires_at) returned for allow_expired reads
        self.valid_row = None

        def load(_key, now, allow_expired=False):
            if self.valid_row is not None and self.valid_row[1] > now:
                return self.valid_row
            return self.saved_row if allow_expired else None

        self.cache_load = patch("supplier_catalog_cache.load", side_effect=load)
        self.cache_save = patch("supplier_catalog_cache.save")
        self.cache_load.start()
        self.save = self.cache_save.start()
        self.get = patch("supplier_adapters.requests.get", return_value=ok("jwt")).start()
        self.post = patch("supplier_adapters.requests.post").start()
        reset_krep_state()

    def tearDown(self):
        wait_for_refresh()
        reset_krep_state()
        patch.stopall()
        self.env.stop()

    def test_expired_saved_catalogue_is_served_at_once_while_the_api_is_down(self):
        self.saved_row = ([ITEM], time.time() - 3 * 3600)
        self.post.side_effect = requests.exceptions.ConnectTimeout("down")
        adapter = KrepKompAdapter()
        with self.assertLogs("supplier_adapters", "WARNING") as logs:
            items = adapter.prepare_catalog()
            wait_for_refresh()
        self.assertEqual(items, [ITEM])
        self.assertTrue(KrepKompAdapter.catalog_is_stale())
        text = "\n".join(logs.output)
        self.assertIn("krep catalogue stale served source=stale_db records=1", text)
        self.assertIn("krep catalogue background refresh failed", text)
        calls = self.post.call_count
        # Inside the cooldown further orders neither wait nor start another refresh.
        self.assertEqual(adapter.prepare_catalog(), [ITEM])
        time.sleep(0.05)
        self.assertEqual(self.post.call_count, calls)

    def test_background_refresh_replaces_the_stale_catalogue_and_saves_it(self):
        self.saved_row = ([ITEM], time.time() - 3600)

        def call(_url, json, headers, timeout):
            return ok({"success": True, "data": [ITEM, NEW_ITEM], "meta": {"last_page": 1, "page": 1}})

        self.post.side_effect = call
        adapter = KrepKompAdapter()
        self.assertEqual(adapter.prepare_catalog(), [ITEM])
        wait_for_refresh()
        self.assertFalse(KrepKompAdapter.catalog_is_stale())
        self.assertEqual([x["Код"] for x in KrepKompAdapter._catalog], ["001", "002"])
        self.assertEqual(KrepKompAdapter._catalog_source, "api")
        self.save.assert_called_once()

    def test_catalogue_too_old_is_not_used(self):
        self.saved_row = ([ITEM], time.time() - 400 * 3600)
        self.post.side_effect = requests.exceptions.ConnectTimeout("down")
        with self.assertRaises(requests.exceptions.ConnectTimeout):
            KrepKompAdapter().prepare_catalog()

    def test_cold_start_failure_is_remembered_and_not_retried_during_cooldown(self):
        self.post.side_effect = requests.exceptions.ConnectTimeout("down")
        adapter = KrepKompAdapter()
        with self.assertRaises(requests.exceptions.ConnectTimeout):
            adapter.prepare_catalog()
        calls = self.post.call_count
        with self.assertRaises(RuntimeError) as caught:
            adapter.prepare_catalog()
        self.assertIn("каталог временно недоступен", str(caught.exception))
        self.assertEqual(self.post.call_count, calls)

    def test_network_failure_makes_following_api_calls_fail_fast(self):
        self.post.side_effect = requests.exceptions.ConnectTimeout("down")
        adapter = KrepKompAdapter()
        with self.assertRaises(requests.exceptions.ConnectTimeout):
            adapter._get_data("get_price", parametrs={"СписокКодов": ["1"]})
        calls = self.post.call_count
        with self.assertRaises(RuntimeError):
            adapter._get_data("get_price", parametrs={"СписокКодов": ["1"]})
        self.assertEqual(self.post.call_count, calls)

    def test_a_success_or_an_http_error_does_not_trip_the_breaker(self):
        failing = Mock()
        failing.status_code = 500
        failing.raise_for_status.side_effect = requests.exceptions.HTTPError("500")
        self.post.return_value = failing
        adapter = KrepKompAdapter()
        with self.assertRaises(requests.exceptions.HTTPError):
            adapter._get_data("get_price", parametrs={"СписокКодов": ["1"]})
        self.assertEqual(KrepKompAdapter._api_down_until, 0.0)

    def test_missing_last_page_reads_until_an_empty_page(self):
        pages = {1: [ITEM], 2: [NEW_ITEM], 3: []}

        def call(_url, json, headers, timeout):
            return ok({"success": True, "data": pages[json["page"]], "meta": {}})

        self.post.side_effect = call
        items = KrepKompAdapter().prepare_catalog()
        self.assertEqual([x["Код"] for x in items], ["001", "002"])

    def test_missing_last_page_and_ignored_paging_stops_instead_of_looping(self):
        self.post.side_effect = lambda _url, json, headers, timeout: ok(
            {"success": True, "data": [ITEM], "meta": {}})
        with self.assertRaises(RuntimeError) as caught:
            KrepKompAdapter().prepare_catalog()
        self.assertIn("постраничное чтение не поддерживается", str(caught.exception))
        self.assertLess(self.post.call_count, 5)
        self.assertIsNone(KrepKompAdapter._catalog)


if __name__ == "__main__":
    unittest.main()


class StaleCatalogueNoticeTests(unittest.TestCase):
    def test_draft_reports_that_a_saved_catalogue_is_used(self):
        import automation_pipeline as pipeline

        class FakeKrep:
            enabled = True
            code = "krep_komp"
            name = "КРЕП-КОМП"

            def __init__(self, stale):
                self.stale = stale

            def prepare_catalog(self):
                return []

            def catalog_is_stale(self):
                return self.stale

        for stale in (True, False):
            with self.subTest(stale=stale), patch.object(pipeline, "KrepKompAdapter", lambda: FakeKrep(stale)):
                draft = pipeline.build_vi_draft({"id": 1, "orderItems": []})
                notices = draft["supplier_notices"]
                self.assertEqual(bool(notices), stale)
                if stale:
                    self.assertIn("ранее сохранённый каталог", notices[0])
                self.assertEqual(draft["deferred_suppliers"], [])
