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


if __name__ == "__main__":
    unittest.main()
