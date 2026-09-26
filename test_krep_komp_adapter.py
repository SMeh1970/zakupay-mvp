import os
import unittest
from unittest.mock import Mock, patch

from supplier_adapters import KrepKompAdapter


def response(payload, status=200):
    result = Mock()
    result.status_code = status
    result.ok = 200 <= status < 300
    result.json.return_value = payload
    result.raise_for_status.side_effect = None
    return result


class KrepKompAdapterTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "KREP_KOMP_API_USERNAME": "test-user",
            "KREP_KOMP_API_PASSWORD": "test-password",
            "KREP_KOMP_STORAGE_NAMES": "КОЛЕДИНО",
        }, clear=False)
        self.env.start()
        KrepKompAdapter._token = None
        KrepKompAdapter._token_expires_at = 0
        KrepKompAdapter._catalog = None
        KrepKompAdapter._catalog_expires_at = 0
        KrepKompAdapter._storage_ids = None
        KrepKompAdapter._storage_ids_expires_at = 0

    def tearDown(self):
        self.env.stop()

    @patch("supplier_adapters.requests.post")
    @patch("supplier_adapters.requests.get")
    def test_search_uses_individual_price_and_selected_warehouse_stock(self, get, post):
        get.return_value = response("jwt-token")

        def api_call(_url, json, headers, timeout):
            operation = headers["Operation"]
            if operation == "get_items":
                return response({
                    "success": True,
                    "data": [{
                        "Код": "00010046536",
                        "Наименование": "Шуруп фасадный 7х145",
                        "ПолноеНаименование": "Шуруп фасадный 7х145 с шестигранной головой",
                        "Артикул": "шф7145д",
                        "ЕдиницаИзмерения": "2239",
                        "Категория": "Шурупы фасадные",
                    }],
                    "meta": {"last_page": 1, "page": 1},
                })
            if operation == "get_price":
                self.assertEqual(json["parametrs"]["СписокКодов"], ["00010046536"])
                return response({"success": True, "data": [{
                    "Код": "00010046536", "ЦенаБазовая": "8517.76", "Цена": "3662.64"
                }]})
            if operation == "get_storages":
                return response({"success": True, "data": [
                    {"Код": "warehouse-k", "Наименование": "КОЛЕДИНО"},
                    {"Код": "warehouse-x", "Наименование": "НОВОСИБИРСК"},
                ]})
            if operation == "get_stocks":
                self.assertEqual(json["parametrs"]["СписокСкладов"], ["warehouse-k"])
                return response({"success": True, "data": [{
                    "Код": "00010046536", "Склад": "КОЛЕДИНО", "Остаток": "37"
                }]})
            raise AssertionError(operation)

        post.side_effect = api_call
        quote = KrepKompAdapter().search("шуруп фасадный 7х145", limit=5)[0]

        self.assertEqual(quote.supplier, "КРЕП-КОМП")
        self.assertEqual(quote.sku, "00010046536")
        self.assertEqual(quote.article, "шф7145д")
        self.assertEqual(quote.price, 3662.64)
        self.assertEqual(quote.base_price, 8517.76)
        self.assertEqual(quote.stock, 37.0)
        self.assertEqual(quote.pickup_date, "КОЛЕДИНО")

    @patch("supplier_adapters.requests.get")
    def test_diagnostic_never_returns_credentials_or_token(self, get):
        get.side_effect = [response("jwt-secret"), response({"success": True, "Info": "Авторизация успешна"})]
        result = KrepKompAdapter().diagnose()
        rendered = str(result)
        self.assertTrue(result["ok"])
        self.assertNotIn("test-password", rendered)
        self.assertNotIn("jwt-secret", rendered)


if __name__ == "__main__":
    unittest.main()
