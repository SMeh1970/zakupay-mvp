import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from order_diagnostics import _order_parameters, diagnose_order_lookup, start_configured_diagnostic


class OrderDiagnosticsTests(unittest.TestCase):
    def test_probes_only_one_order_and_never_logs_credentials_or_payloads(self):
        schema = {"paths": {"/api/v1/orders": {"get": {"parameters": [
            {"name": "fromAllCompanies", "in": "query", "type": "boolean"},
            {"name": "ignoreKeywordFilter", "in": "query", "type": "boolean", "description": "Поиск независимо от ключевых слов"},
        ]}}}}
        def reply(url, **kwargs):
            if url.endswith(".yaml"):
                return Mock(status_code=200, content=json.dumps(schema, ensure_ascii=False).encode("utf-8"))
            params = kwargs["params"]
            rows = [{"id": 37463885, "customer": "PRIVATE CUSTOMER", "orderItems": [{"price": "PRIVATE PRICE"}]}] if "ids" in params else [{"id": 99}]
            return Mock(status_code=200, json=lambda: {"result": True, "orders": rows})

        get = Mock(side_effect=reply)
        with self.assertLogs("order_diagnostics", level="WARNING") as captured:
            diagnose_order_lookup(37463885, "https://example.invalid", {"ZakupayToken": "PRIVATE TOKEN"}, get=get)
        output = "\n".join(captured.output)
        self.assertNotIn("PRIVATE", output)
        self.assertLessEqual(len(get.call_args_list), 8)
        for call in get.call_args_list[1:-1]:
            self.assertEqual(call.kwargs["params"]["ids"], 37463885)
        self.assertEqual(get.call_args_list[-1].kwargs["params"]["pageSize"], 1)
        self.assertNotIn("headers", get.call_args_list[0].kwargs)
        self.assertIn('"found": true', output)

    def test_supplier_parameter_variant_is_preferred_to_buyer_schema(self):
        def operation(name):
            return {"get": {"parameters": [{"name": name, "in": "query"}]}}
        schema = {"paths": {"/api/v1/orders (заказчик)": operation("buyer"),
                            "/api/v1/orders (поставщик)": operation("supplier")}}
        self.assertEqual([p["name"] for p in _order_parameters(schema)], ["supplier"])

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
