"""Invoice deadlines must release the website without retrying a live offer."""

import asyncio
import copy
import html
import threading
import unittest
from unittest.mock import Mock, patch

import httpx
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from offer_panel_safe import install_offer_panel


class OfferSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.order = {"id": 37463885, "orderItems": [{"id": 10, "goodName": "Товар", "count": 2}]}
        self.context = {
            "job_id": 456, "order": self.order,
            "result": {
                "customer": {"name": "ООО Покупатель", "inn": "7716997861", "kpp": "770801001"},
                "items": [{"order_item_id": 10, "quantity": 2, "unit": "шт", "decision": "approved"}],
            },
        }
        self.fetch = Mock(return_value=copy.deepcopy(self.order))
        self.claim = Mock(return_value={"claimed": True, "id": 1, "status": "sending"})
        self.submission = None
        self.finish = Mock(side_effect=self._finish)
        self.invoice = Mock(return_value=b"invoice")
        self.form = {
            "confirm_send": "SEND", "producer_offer_number": "278", "producer_offer_date": "2026-10-22",
            "destination_account_id": "bank", "use_10": "1", "qty_10": "2", "price_10": "100",
            "unit_10": "шт", "provider_name_10": "Товар", "vat_rate": "0.22",
        }
        self.app = FastAPI()

        @self.app.get("/probe")
        async def probe():
            return {"ok": True}

        install_offer_panel(
            self.app, Mock(), lambda: {}, "https://example.invalid", lambda v: html.escape(str(v)),
            fetch_order_by_id=self.fetch, load_offer_context=lambda oid: copy.deepcopy(self.context),
            begin_submission=self.claim, finish_submission=self.finish,
            latest_submission=lambda job_id: self.submission,
            order_hash=lambda order: str(order["orderItems"]), build_invoice=self.invoice,
        )
        self.url = "/dashboard/order/37463885/offer/submit"
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def _finish(self, submission_id, status, **kwargs):
        self.submission = {"id": submission_id, "status": status, **kwargs}

    @staticmethod
    def response(data, status=200):
        return Mock(status_code=status, ok=status < 400, headers={"content-type": "application/json"}, json=lambda: data)

    def upload(self):
        return self.response({"file": {"id": "file-123"}})

    def check_deadline(self, stage, post):
        """The blocked network thread cannot resume the rest of the submission."""
        started, release, completed = threading.Event(), threading.Event(), threading.Event()

        def slow_reply(*args, **kwargs):
            started.set()
            release.wait(2)
            completed.set()
            return copy.deepcopy(self.order) if stage == "order_check" else self.response({"id": "late-id"})

        if stage == "order_check":
            self.fetch.side_effect = slow_reply
            deadline_name = "ORDER_CHECK_DEADLINE_SECONDS"
        elif stage == "file_upload":
            post.side_effect = slow_reply
            deadline_name = "UPLOAD_DEADLINE_SECONDS"
        else:
            post.side_effect = [self.upload(), Mock()]

            def dispatch(*args, **kwargs):
                return self.upload() if "files" in kwargs else slow_reply(*args, **kwargs)

            post.side_effect = dispatch
            deadline_name = "OFFER_POST_DEADLINE_SECONDS"

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
                pending = asyncio.create_task(client.post(self.url, data=self.form))
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 1))
                    # This must work while the upstream request is still blocked.
                    probe = await asyncio.wait_for(client.get("/probe"), timeout=0.3)
                    self.assertEqual(probe.status_code, 200)
                    self.assertFalse(release.is_set())
                    response = await asyncio.wait_for(pending, timeout=0.5)
                    if stage == "offer_post":
                        self.assertEqual(response.status_code, 409)
                        self.assertEqual(self.submission["status"], "unknown")
                        again = await client.post(self.url, data=self.form)
                        self.assertEqual(again.status_code, 409)
                        self.assertEqual(post.call_count, 2)  # One upload and exactly one offer POST.
                    else:
                        self.assertEqual(response.status_code, 503)
                        self.assertIn("Счёт не отправлен", response.text)
                        self.claim.assert_not_called()
                finally:
                    release.set()
                    await asyncio.to_thread(completed.wait, 1)
                    if not pending.done():
                        await pending

        try:
            with patch("offer_panel_safe." + deadline_name, 0.05):
                asyncio.run(scenario())
        finally:
            release.set()
        self.assertEqual(post.call_count, {"order_check": 0, "file_upload": 1, "offer_post": 2}[stage])

    @patch("offer_panel_safe.requests.post")
    def test_slow_preflight_leaves_site_responsive_and_late_reply_never_sends(self, post):
        self.check_deadline("order_check", post)
        self.invoice.assert_not_called()

    @patch("offer_panel_safe.requests.post")
    def test_slow_upload_never_continues_to_offer_post(self, post):
        self.check_deadline("file_upload", post)

    @patch("offer_panel_safe.requests.post")
    def test_slow_offer_post_blocks_retries_even_after_late_reply(self, post):
        self.check_deadline("offer_post", post)

    @patch("offer_panel_safe.requests.post")
    def test_success_confirms_offer_only_after_id_is_received(self, post):
        post.side_effect = [self.upload(), self.response({"id": "offer-123"})]
        response = self.client.post(self.url, data=self.form)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Предложение создано", response.text)
        self.assertEqual(self.submission["status"], "confirmed")
        self.assertEqual(self.submission["offer_id"], "offer-123")
        self.assertEqual(post.call_count, 2)

    @patch("offer_panel_safe.requests.post")
    def test_network_failure_during_post_is_unknown_and_never_retried(self, post):
        post.side_effect = [self.upload(), requests.Timeout("slow")]
        self.assertEqual(self.client.post(self.url, data=self.form).status_code, 409)
        self.assertEqual(self.submission["status"], "unknown")
        self.assertEqual(self.client.post(self.url, data=self.form).status_code, 409)
        self.assertEqual(post.call_count, 2)

    @patch("offer_panel_safe.requests.post")
    def test_upstream_server_error_does_not_permit_duplicate_offer(self, post):
        post.side_effect = [self.upload(), self.response({"error": "upstream"}, 502)]
        self.assertEqual(self.client.post(self.url, data=self.form).status_code, 409)
        self.assertEqual(self.submission["status"], "unknown")

    @patch("offer_panel_safe.requests.post")
    def test_success_without_offer_id_is_not_reported_as_confirmed(self, post):
        for data in ({"result": True}, {"result": False, "id": "not-an-offer"}):
            with self.subTest(data=data):
                self.submission = None
                post.side_effect = [self.upload(), self.response(data)]
                response = self.client.post(self.url, data=self.form)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(self.submission["status"], "unknown")

    @patch("offer_panel_safe.requests.post")
    def test_invalid_form_is_rejected_before_upload(self, post):
        response = self.client.post(self.url, data={**self.form, "destination_account_id": ""})
        self.assertEqual(response.status_code, 400)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
