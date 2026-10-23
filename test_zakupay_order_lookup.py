import os
import unittest
from unittest.mock import Mock, patch

import requests

os.environ.setdefault("ZAKUPAY_API_KEY", "test-token")

import main


class ZakupayOrderLookupTests(unittest.TestCase):
    def setUp(self):
        # main may already be imported by other tests before the env default.
        token = patch.object(main, "ZAKUPAY_API_KEY", "test-token")
        token.start()
        self.addCleanup(token.stop)

    @patch("main.time.sleep")
    def test_retries_temporary_timeout(self, sleep):
        call = Mock(side_effect=[requests.Timeout("slow"), Mock(status_code=200)])

        response = main._zakupay_request(call, "https://example.invalid")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(call.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch("main.requests.get")
    def test_page_size_does_not_hide_orders_with_fewer_positions(self, get):
        short_order = {"id": 1, "orderItems": [{"id": 10}]}

        def page(*args, **kwargs):
            params = kwargs["params"]
            rows = [short_order] if int(params.get("count", 0)) <= 1 else []
            return Mock(status_code=200, ok=True, json=lambda: {"result": True, "orders": rows})

        get.side_effect = page
        result = main.request_orders_page(page=2, page_size=25)
        self.assertEqual(result["orders"], [short_order])
        self.assertEqual(get.call_args.kwargs["params"]["pageSize"], 25)
        self.assertEqual(get.call_args.kwargs["params"]["page"], 2)

    @patch("main.requests.post")
    @patch("main.requests.get")
    def test_uses_ids_filter_and_returns_order_items(self, get, post):
        get.return_value = Mock(
            status_code=200,
            ok=True,
            json=lambda: {
                "orders": [
                    {
                        "id": 37217190,
                        "orderItems": [{"id": 10, "goodName": "Диск отрезной"}],
                    }
                ]
            },
        )

        order = main.fetch_order_by_id(37217190)

        self.assertEqual(order["id"], 37217190)
        self.assertEqual(len(order["orderItems"]), 1)
        self.assertEqual(get.call_args.kwargs["params"]["ids"], 37217190)
        self.assertEqual(get.call_args.kwargs["params"]["ignoreKeywordFilter"], "true")
        post.assert_not_called()

    @patch("main.fetch_all_orders")
    @patch("main.requests.get")
    def test_explicit_order_lookup_is_not_hidden_by_keyword_preferences(self, get, all_orders):
        order = {"id": 37463885, "orderItems": [{"id": 1, "isPassKeywordFilter": False}]}

        def lookup(*args, **kwargs):
            orders = [order] if kwargs["params"].get("ignoreKeywordFilter") == "true" else []
            return Mock(status_code=200, ok=True, json=lambda: {"result": True, "orders": orders})

        get.side_effect = lookup
        self.assertEqual(main.fetch_order_by_id(37463885), order)
        all_orders.assert_not_called()

    @patch("main.fetch_all_orders")
    @patch("main.requests.get")
    def test_api_failure_in_http_200_is_not_reported_as_missing_order(self, get, all_orders):
        get.return_value = Mock(status_code=200, ok=True, json=lambda: {"result": False, "orders": []})
        with self.assertRaises(main.HTTPException) as caught:
            main.fetch_order_by_id(37463885)
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn("сообщил об ошибке", caught.exception.detail)
        all_orders.assert_not_called()

    @patch("main.request_orders_page")
    def test_empty_official_collection_stays_empty(self, page):
        page.return_value = {"orders": []}
        main._orders_cache.update({"ts": 0.0, "key": "", "orders": []})

        orders = main.fetch_all_orders(force=True)

        self.assertEqual(orders, [])

    @patch("main.request_orders_page")
    def test_official_api_timeout_is_reported_without_registry_fallback(self, page):
        page.side_effect = main.HTTPException(status_code=502, detail="timeout")
        main._orders_cache.update({"ts": 0.0, "key": "", "orders": []})

        with self.assertRaises(main.HTTPException) as context:
            main.fetch_all_orders(force=True)

        self.assertEqual(context.exception.status_code, 503)
        self.assertIn("Официальный API Закупай", context.exception.detail)

    @patch("main.fetch_all_orders")
    @patch("main.requests.get")
    def test_missing_single_order_does_not_crawl_registry(self, get, all_orders):
        get.return_value = Mock(status_code=200, ok=True, json=lambda: {"orders": []})
        self.assertIsNone(main.fetch_order_by_id(37463885, force=True))
        self.assertEqual(get.call_count, 1)
        all_orders.assert_not_called()

    @patch("main.fetch_all_orders")
    @patch("main.requests.get")
    def test_invalid_single_order_response_does_not_crawl_registry(self, get, all_orders):
        get.return_value = Mock(status_code=200, ok=True)
        get.return_value.json.side_effect = ValueError("invalid JSON")
        self.assertIsNone(main.fetch_order_by_id(37463885, force=True))
        all_orders.assert_not_called()

    @patch("main.fetch_all_orders")
    @patch("main.requests.get")
    def test_single_order_http_error_does_not_crawl_registry(self, get, all_orders):
        get.return_value = Mock(status_code=400, ok=False)
        with self.assertRaises(main.HTTPException):
            main.fetch_order_by_id(37463885, force=True)
        all_orders.assert_not_called()


if __name__ == "__main__":
    unittest.main()
