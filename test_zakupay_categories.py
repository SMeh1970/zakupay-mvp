import unittest
from unittest.mock import Mock, patch

import main
from zakupay_categories import normalize_categories


class CategoryApiTests(unittest.TestCase):
    def test_reference_uses_official_endpoint_and_existing_token(self):
        response = Mock(status_code=200)
        response.json.return_value = {"result": True, "categories": [{"id": 62, "name": "Сигнализация"}]}
        with patch.object(main, "_zakupay_request", return_value=response) as request, \
                patch.object(main, "ZAKUPAY_API_KEY", "test-token"):
            rows = main.fetch_category_reference()
        self.assertEqual(rows[0]["id"], "62")
        self.assertEqual(request.call_args.args[1], main.ZAKUPAY_BASE_URL.rstrip('/') + "/api/v1/refbooks/categories")
        self.assertEqual(request.call_args.kwargs["headers"]["ZakupayToken"], "test-token")
        self.assertFalse(request.call_args.kwargs["allow_redirects"])

    def test_unsuccessful_payload_and_cycles_are_rejected(self):
        response = Mock(status_code=200)
        response.json.return_value = {"result": False, "categories": [{"id": 1, "name": "Wrong"}]}
        with patch.object(main, "_zakupay_request", return_value=response), \
                patch.object(main, "ZAKUPAY_API_KEY", "test-token"), self.assertRaises(ValueError):
            main.fetch_category_reference()
        with self.assertRaises(ValueError):
            normalize_categories([{"id": 1, "name": "A", "parent": {"id": 2}},
                                  {"id": 2, "name": "B", "parent": {"id": 1}}])


if __name__ == "__main__":
    unittest.main()
