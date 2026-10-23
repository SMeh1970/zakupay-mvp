import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from order_diagnostics import diagnose_order_lookup, start_configured_diagnostic


class OrderDiagnosticsTests(unittest.TestCase):
    def test_probes_only_one_order_and_never_logs_credentials_or_payloads(self):
        schema = {"paths": {"/api/v1/orders": {"get": {"parameters": [
            {"name": "fromAllCompanies", "in": "query", "type": "boolean"},
            {"name": "ignoreKeywordFilter", "in": "query", "type": "boolean", "description": "Поиск независимо от ключевых слов"},
        ]}}}}
        get = Mock(side_effect=[
            Mock(status_code=200, content=json.dumps(schema, ensure_ascii=False).encode("utf-8")),
            Mock(status_code=200, json=lambda: {"result": True, "orders": []}),
            Mock(status_code=200, json=lambda: {"result": True, "orders": []}),
            Mock(status_code=200, json=lambda: {"result": True, "orders": [
                {"id": 37463885, "customer": "PRIVATE CUSTOMER", "orderItems": [{"price": "PRIVATE PRICE"}]}]}),
            Mock(status_code=200, json=lambda: {"result": True, "orders": []}),
        ])
        with self.assertLogs("order_diagnostics", level="WARNING") as captured:
            diagnose_order_lookup(37463885, "https://example.invalid", {"ZakupayToken": "PRIVATE TOKEN"}, get=get)
        output = "\n".join(captured.output)
        self.assertNotIn("PRIVATE", output)
        self.assertEqual(len(get.call_args_list), 5)
        for call in get.call_args_list[1:]:
            self.assertEqual(call.kwargs["params"]["ids"], 37463885)
        self.assertEqual(get.call_args_list[3].kwargs["params"]["fromAllCompanies"], "true")
        self.assertEqual(get.call_args_list[2].kwargs["params"]["count"], 1)
        self.assertNotIn("headers", get.call_args_list[0].kwargs)
        self.assertIn('"found": true', output)

    @patch("order_diagnostics.threading.Thread")
    def test_expired_or_unbounded_flag_never_starts_network_work(self, thread):
        for delta in (-1, 1801):
            config = {"order_id": 37463885, "until": (datetime.now(timezone.utc) + timedelta(seconds=delta)).isoformat()}
            with patch.dict("os.environ", {"ZAKUPAY_ORDER_DIAGNOSTIC": json.dumps(config)}):
                start_configured_diagnostic("https://example.invalid", Mock())
        thread.assert_not_called()

    @patch("order_diagnostics.threading.Thread")
    def test_diagnostics_are_disabled_without_explicit_flag(self, thread):
        with patch.dict("os.environ", {"ZAKUPAY_ORDER_DIAGNOSTIC": ""}):
            start_configured_diagnostic("https://example.invalid", Mock())
        thread.assert_not_called()
