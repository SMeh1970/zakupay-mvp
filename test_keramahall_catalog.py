import copy
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import automation_pipeline as pipeline
import keramahall_catalog as api
import porcelain as tiles
import porcelain_panel as panel
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_porcelain import PRODUCT, ORDER, archive


class ManagedCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        settings = patch.multiple(pipeline, DB_PATH=os.path.join(self.tmp.name, "catalog.db"), DATABASE_URL="", _initialized_database_key=None)
        settings.start(); self.addCleanup(settings.stop)
        self.old_id = tiles.import_catalog(archive())
        self.image = b"\xff\xd8managed-photo\xff\xd9"
        product = {**copy.deepcopy(PRODUCT), "stock_m2": 0, "price_rub_m2": 2390,
                   "stock_updated_at": "2026-10-17T13:40:00+03:00", "price_updated_at": "2026-10-21T07:13:31Z",
                   "image_url": api.ORIGIN + "/api/v1/catalog/images/" + PRODUCT["id"],
                   "image_sha256": hashlib.sha256(self.image).hexdigest()}
        self.source = {"schema_version": api.SCHEMA, "source": {"project_id": api.PROJECT_ID, "url": api.ORIGIN},
                       "content_sha256": "a" * 64, "revision": 0, "products": [product],
                       "stock_as_of": "2026-10-17T13:40:00+03:00", "catalog_updated_at": "2026-10-21T07:13:31Z",
                       "retrieved_at": "2026-10-22T09:00:00Z", "shipping": {}, "currency": "RUB"}

    def response(self, url, maximum, image=False):
        return self.image if image else json.dumps(self.source).encode()

    def test_daily_refresh_persists_success_and_skips_restarts(self):
        with patch.object(api, "_get", side_effect=self.response) as get:
            first = api.sync_catalog(force=False)
            self.assertTrue(first["changed"])
            status = api.sync_status()
            self.assertTrue(status["last_success"])
            get.reset_mock()
            self.assertEqual(api.sync_catalog(force=False)["reason"], "not_due")
            get.assert_not_called()
            # A persisted timestamp older than a day permits the next read.
            from price_lists import database, execute
            with database() as conn:
                execute(conn, "UPDATE tile_catalog_sync SET last_success=?,last_attempt=? WHERE id=1", ("2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"))
            self.assertFalse(api.sync_catalog(force=False)["changed"])
            self.assertEqual(get.call_count, 1)
            self.assertIsNone(api.sync_status()["last_error"])

    def test_daily_failure_keeps_catalog_and_success_timestamp(self):
        with patch.object(api, "_get", side_effect=self.response):
            api.sync_catalog()
        saved = tiles.load_catalog()["id"]
        success = api.sync_status()["last_success"]
        with patch.object(api, "_get", side_effect=ValueError("API недоступен")):
            with self.assertRaisesRegex(ValueError, "недоступен"):
                api.sync_catalog()
        self.assertEqual(tiles.load_catalog()["id"], saved)
        self.assertEqual(api.sync_status()["last_success"], success)
        self.assertEqual(api.sync_status()["last_error"], "API недоступен")
        with patch.object(api, "_get") as get:
            api.sync_catalog(force=False)
            get.assert_not_called()

    def test_managed_values_dates_and_photos_are_versioned_without_refreshing_stock(self):
        with patch.object(api, "_get", side_effect=self.response) as get:
            imported = api.sync_catalog()
            self.assertTrue(imported["changed"])
            self.assertEqual(get.call_count, 2)
            current = tiles.load_catalog()
            self.assertTrue(api.is_managed(current["data"]))
            product = current["data"]["products"][0]
            self.assertEqual(product["stock_m2"], 0)
            self.assertEqual(product["price_rub_m2"], 2390)
            self.assertEqual(product["stock_as_of"], "2026-10-17T13:40:00+03:00")
            self.assertNotEqual(product["stock_as_of"], current["data"]["retrieved_at"])
            self.assertEqual(tiles.catalogue_image(current["id"], product["id"]), self.image)
            self.assertEqual(tiles.load_catalog(self.old_id)["data"]["products"][0]["price_rub_m2"], None)
            get.reset_mock()
            self.assertFalse(api.sync_catalog()["changed"])
            self.assertEqual(get.call_count, 1, "unchanged catalogue must not download photos")
            get.reset_mock()
            self.source["content_sha256"] = "b" * 64
            self.source["products"][0]["price_rub_m2"] = 2500
            self.assertTrue(api.sync_catalog()["changed"])
            self.assertEqual(get.call_count, 1, "unchanged photo hash reuses immutable photo")

    def test_unknown_dates_remain_unknown_and_bad_response_cannot_replace_catalogue(self):
        with patch.object(api, "_get", side_effect=self.response):
            self.source["products"][0]["stock_updated_at"] = None
            self.source["stock_as_of"] = None
            api.sync_catalog()
            current = tiles.load_catalog()
            self.assertIsNone(current["data"]["products"][0]["stock_as_of"])
            self.assertIsNone(current["data"]["stock_as_of"])
            self.source["content_sha256"] = "b" * 64
            self.source["products"][0]["stock_updated_at"] = "not a date"
            with self.assertRaisesRegex(ValueError, "дату"):
                api.sync_catalog()
            self.assertEqual(tiles.load_catalog()["id"], current["id"])
            self.source["products"][0]["stock_updated_at"] = None
            self.source["products"][0]["image_url"] = "https://other.example/photo.jpg"
            with self.assertRaisesRegex(ValueError, "адрес фото"):
                api.sync_catalog()
            self.assertEqual(tiles.load_catalog()["id"], current["id"])

    def test_catalogue_price_prefills_quote_but_explicit_blank_is_not_silently_replaced(self):
        catalog = tiles.load_catalog()
        catalog["data"]["products"][0]["price_rub_m2"] = 2390
        form = {"product_1": PRODUCT["id"], "area_1": "40"}
        quote = tiles.make_quote(ORDER, catalog, form)
        self.assertEqual(quote["lines"][0]["proposed_unit_price"], 2390)
        self.assertIsNone(tiles.make_quote(ORDER, catalog, {**form, "price_1": ""})["lines"][0]["proposed_unit_price"])

    def test_configured_application_starts_and_only_current_source_can_prepare_new_quotes(self):
        with patch.dict(os.environ, {"KERAMAHALL_CATALOG_API_KEY": "test-key"}):
            with patch.object(api, "sync_catalog") as sync, patch.object(panel.threading, "Thread") as thread, patch("apscheduler.schedulers.background.BackgroundScheduler") as scheduler:
                app = FastAPI()
                panel.install_porcelain(app, lambda: [])
                with TestClient(app):
                    thread.assert_called_once()
                    thread.return_value.start.assert_called_once()
                sync.assert_not_called()
                scheduler.return_value.add_job.assert_called_once()
                self.assertEqual(scheduler.return_value.add_job.call_args.kwargs["hours"], 1)
                scheduler.return_value.shutdown.assert_called_once()
            with self.assertRaises(HTTPException):
                panel.required_proposal_catalog(self.old_id)
            # Historical snapshots remain readable.
            self.assertEqual(panel.required_catalog(self.old_id)["id"], self.old_id)
            with patch.object(api, "_get", side_effect=self.response):
                current = api.sync_catalog()["id"]
            self.assertEqual(panel.required_proposal_catalog(current)["id"], current)
            with self.assertRaises(HTTPException):
                panel.required_proposal_catalog(self.old_id)


if __name__ == "__main__":
    unittest.main()
