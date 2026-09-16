import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("ZAKUPAY_API_KEY", "test-token")

import main


class ZakupayOrderLookupTests(unittest.TestCase):
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

    @patch("main.requests.post")
    @patch("main.request_orders_page")
    def test_all_orders_uses_registry_when_public_collection_is_empty(self, page, post):
        page.return_value = {"orders": []}
        post.return_value = Mock(
            status_code=200,
            ok=True,
            json=lambda: [{"id": 37217190, "delay": 0, "orderItems": [{"id": 10}]}],
        )
        main._orders_cache.update({"ts": 0.0, "key": "", "orders": []})

        orders = main.fetch_all_orders(force=True)

        self.assertEqual([order["id"] for order in orders], [37217190])
        self.assertEqual(post.call_args.args[0], f"{main.ZAKUPAY_BASE_URL}/core/supplier/getorders")


if __name__ == "__main__":
    unittest.main()
