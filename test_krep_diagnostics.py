import os
import unittest
from unittest.mock import Mock, patch

import requests

from supplier_adapters import KrepKompAdapter


def ok(payload):
    result = Mock()
    result.status_code = 200
    result.ok = True
    result.json.return_value = payload
    result.raise_for_status.side_effect = None
    return result


ITEM = {"Код": "001", "Наименование": "Болт М8х30", "ПолноеНаименование": "Болт М8х30 оцинкованный",
        "Артикул": "b830", "Категория": "Болты"}


class KrepDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "KREP_KOMP_API_USERNAME": "secret-user", "KREP_KOMP_API_PASSWORD": "secret-password",
            "KREP_KOMP_STORAGE_NAMES": "",
        }, clear=False)
        self.env.start()
        self.cache_load = patch("supplier_catalog_cache.load", return_value=None)
        self.cache_save = patch("supplier_catalog_cache.save")
        self.cache_load.start()
        self.cache_save.start()
        for name, value in (("_token", None), ("_token_expires_at", 0), ("_catalog", None),
                            ("_catalog_expires_at", 0), ("_storage_ids", None), ("_storage_ids_expires_at", 0)):
            setattr(KrepKompAdapter, name, value)

    def tearDown(self):
        self.cache_load.stop()
        self.cache_save.stop()
        self.env.stop()

    def text(self, logs):
        return "\n".join(logs.output)

    @patch("supplier_adapters.requests.get", side_effect=requests.exceptions.ConnectTimeout("boom"))
    def test_token_failure_names_the_operation_and_error_type(self, _get):
        with self.assertLogs("supplier_adapters", "WARNING") as logs, self.assertRaises(requests.exceptions.ConnectTimeout):
            KrepKompAdapter().prepare_catalog()
        text = self.text(logs)
        self.assertIn("operation=generate_token", text)
        self.assertIn("error=ConnectTimeout", text)
        self.assertIn("krep catalogue prepare failed", text)

    @patch("supplier_adapters.requests.post")
    @patch("supplier_adapters.requests.get")
    def test_catalogue_failure_on_a_later_page_reports_page_and_records(self, get, post):
        get.return_value = ok("jwt-token-value")

        def call(_url, json, headers, timeout):
            if json["page"] == 1:
                return ok({"success": True, "data": [ITEM], "meta": {"last_page": 3, "page": 1}})
            raise requests.exceptions.ConnectTimeout("boom")

        post.side_effect = call
        with self.assertLogs("supplier_adapters", "WARNING") as logs, self.assertRaises(requests.exceptions.ConnectTimeout):
            KrepKompAdapter().prepare_catalog()
        text = self.text(logs)
        self.assertIn("operation=get_items page=2", text)
        self.assertIn("krep catalogue load failed page=2 last_page=3 records_loaded=1", text)
        self.assertIn("error=ConnectTimeout", text)
        self.assertIsNone(KrepKompAdapter._catalog)

    @patch("supplier_adapters.requests.post")
    @patch("supplier_adapters.requests.get")
    def test_successful_load_and_search_log_source_counts_and_step_timings(self, get, post):
        get.return_value = ok("jwt-token-value")

        def call(_url, json, headers, timeout):
            operation = headers["Operation"]
            if operation == "get_items":
                return ok({"success": True, "data": [ITEM], "meta": {"last_page": 1, "page": 1}})
            if operation == "get_price":
                return ok({"success": True, "data": [{"Код": "001", "Цена": "12.5", "ЦенаБазовая": "15"}]})
            return ok({"success": True, "data": [{"Код": "001", "Остаток": "4", "Склад": "A"}]})

        post.side_effect = call
        adapter = KrepKompAdapter()
        with self.assertLogs("supplier_adapters", "WARNING") as logs:
            adapter.prepare_catalog()
            quotes = adapter.search("болт м8х30")
            adapter.prepare_catalog()
        text = self.text(logs)
        self.assertEqual(len(quotes), 1)
        self.assertIn("krep catalogue first page records=1 meta_keys=['last_page', 'page'] last_page_reported=1", text)
        self.assertIn("krep catalogue ready source=api pages=1 records=1", text)
        self.assertIn("cache_saved=True", text)
        self.assertIn("krep catalogue prepared source=api records=1", text)
        self.assertIn("krep catalogue prepared source=memory records=1", text)
        for field in ("catalog_ms=", "rank_ms=", "raw_candidates=1", "priced=1", "price_ms=", "stocks_ms=", "total_ms="):
            self.assertIn(field, text)

    @patch("supplier_adapters.requests.post")
    @patch("supplier_adapters.requests.get")
    def test_logs_never_contain_credentials_tokens_or_prices(self, get, post):
        get.return_value = ok("jwt-token-value")

        def call(_url, json, headers, timeout):
            operation = headers["Operation"]
            if operation == "get_items":
                return ok({"success": True, "data": [ITEM], "meta": {"last_page": 1, "page": 1}})
            if operation == "get_price":
                return ok({"success": True, "data": [{"Код": "001", "Цена": "12345.67"}]})
            raise requests.exceptions.ConnectTimeout("boom")

        post.side_effect = call
        adapter = KrepKompAdapter()
        with self.assertLogs("supplier_adapters", "WARNING") as logs:
            adapter.search("болт м8х30")
        text = self.text(logs)
        for secret in ("secret-user", "secret-password", "jwt-token-value", "Bearer", "Basic", "12345.67"):
            self.assertNotIn(secret, text)
        self.assertIn("operation=get_stocks", text)
        self.assertIn("krep search failed", text)

    @patch("supplier_adapters.requests.post")
    @patch("supplier_adapters.requests.get")
    def test_cache_restore_is_logged_with_source_and_expiry(self, get, post):
        import time
        get.return_value = ok("jwt-token-value")
        expires = time.time() + 7200
        with patch("supplier_catalog_cache.load", return_value=([ITEM], expires)), \
                self.assertLogs("supplier_adapters", "WARNING") as logs:
            KrepKompAdapter().prepare_catalog()
        text = self.text(logs)
        self.assertIn("krep catalogue restored source=db_cache records=1", text)
        self.assertIn("krep catalogue prepared source=db_cache records=1", text)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
