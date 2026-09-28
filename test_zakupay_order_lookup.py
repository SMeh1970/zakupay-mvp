import os
import unittest
from unittest.mock import Mock, patch

import requests

os.environ.setdefault("ZAKUPAY_API_KEY", "test-token")

import main


class ZakupayOrderLookupTests(unittest.TestCase):
    @patch("main.time.sleep")
    def test_retries_temporary_timeout(self, sleep):
        call = Mock(side_effect=[requests.Timeout("slow"), Mock(status_code=200)])

        response = main._zakupay_request(call, "https://example.invalid")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(call.call_count, 2)
        sleep.assert_called_once_with(1)

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
        post.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
