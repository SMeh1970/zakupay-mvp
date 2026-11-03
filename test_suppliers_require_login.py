import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import security
import supplier_panel


class SuppliersRequireLoginTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        security.install_security(self.app)
        supplier_panel.install_supplier_panel(self.app, lambda value: value)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()

    def test_supplier_routes_refuse_requests_without_login_and_call_no_supplier(self):
        with patch.object(security, "_valid_session", return_value=False), \
                patch.object(supplier_panel, "compare_suppliers") as search, \
                patch.object(supplier_panel, "krep_komp_diagnostic") as krep, \
                patch.object(supplier_panel, "vseinstrumenti_diagnostic") as vi, \
                patch.object(supplier_panel, "supplier_statuses") as statuses:
            for path in ("/suppliers/search?q=болт", "/suppliers/status",
                         "/suppliers/debug/krep-komp", "/suppliers/debug/vseinstrumenti?q=болт"):
                with self.subTest(path=path):
                    response = self.client.get(path, follow_redirects=False)
                    self.assertEqual(response.status_code, 401)
                    self.assertNotIn("болт", response.text)
            search.assert_not_called()
            krep.assert_not_called()
            vi.assert_not_called()
            statuses.assert_not_called()

    def test_supplier_routes_work_for_a_logged_in_session(self):
        with patch.object(security, "_valid_session", return_value=True), \
                patch.object(supplier_panel, "compare_suppliers", return_value={"quotes": []}) as search, \
                patch.object(supplier_panel, "krep_komp_diagnostic", return_value={"ok": True}) as krep:
            self.assertEqual(self.client.get("/suppliers/search?q=болт").status_code, 200)
            self.assertEqual(self.client.get("/suppliers/debug/krep-komp").json(), {"ok": True})
            search.assert_called_once()
            krep.assert_called_once()

    def test_public_paths_stay_public(self):
        with patch.object(security, "_valid_session", return_value=False):
            self.assertEqual(self.client.post("/automation/api/poll").status_code, 404)
            response = self.client.get("/login")
            self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
