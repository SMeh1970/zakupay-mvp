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
            rows = [{"id": params.get("ids", 99), "customer": "PRIVATE CUSTOMER", "orderItems": [{"price": "PRIVATE PRICE"}]}]
            return Mock(status_code=200, json=lambda: {"result": True, "orders": rows})

        get = Mock(side_effect=reply)
        with self.assertLogs("order_diagnostics", level="WARNING") as captured:
            diagnose_order_lookup(37463885, "https://example.invalid", {"ZakupayToken": "PRIVATE TOKEN"}, get=get)
        output = "\n".join(captured.output)
        self.assertNotIn("PRIVATE", output)
        self.assertLessEqual(len(get.call_args_list), 8)
        self.assertEqual(get.call_args_list[1].kwargs["params"]["ids"], 37463885)
        self.assertEqual(get.call_args_list[2].kwargs["params"]["pageSize"], 1)
        self.assertEqual(get.call_args_list[3].kwargs["params"]["ids"], 99)
        self.assertEqual(get.call_args_list[4].kwargs["params"]["ids"], 99)
        self.assertNotIn("ignoreKeywordFilter", get.call_args_list[4].kwargs["params"])
        self.assertNotIn("headers", get.call_args_list[0].kwargs)
        self.assertIn('"found": true', output)

    def test_cabinet_credentials_are_never_sent_to_another_origin(self):
        get = Mock(return_value=Mock(status_code=200, content=b"{}", json=lambda: {"orders": []}))
        cookie_factory = Mock(return_value="PRIVATE COOKIE")
        with self.assertLogs("order_diagnostics", level="WARNING"):
            diagnose_order_lookup(1, "https://example.invalid", {}, get=get, cookie_factory=cookie_factory)
        cookie_factory.assert_not_called()
        self.assertTrue(all("cookies" not in call.kwargs for call in get.call_args_list))

    def test_cabinet_checks_are_get_only_and_do_not_log_session(self):
        get = Mock(return_value=Mock(status_code=200, content=b"{}", json=lambda: {"orders": []}))
        with self.assertLogs("order_diagnostics", level="WARNING") as captured:
            diagnose_order_lookup(1, "https://prodavay.sel-be.ru", {"ZakupayToken": "PRIVATE TOKEN"},
                                  get=get, cookie_factory=lambda: "PRIVATE COOKIE")
        self.assertNotIn("PRIVATE", "\n".join(captured.output))
        self.assertEqual(sum("cookies" in call.kwargs for call in get.call_args_list), 2)
        self.assertTrue(all(call.kwargs["allow_redirects"] is False for call in get.call_args_list))

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
