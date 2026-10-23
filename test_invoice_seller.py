"""Seller choices survive reload/search and agree with downloaded invoices."""
import copy
import json
import os
import tempfile
import unittest
from io import BytesIO
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook

import automation_pipeline as pipeline
from invoice_generator import SELLER


class SavedInvoiceSellerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = patch.multiple(pipeline, DB_PATH=os.path.join(self.tmp.name, 'jobs.db'), DATABASE_URL='', _initialized_database_key=None)
        db.start()
        self.addCleanup(db.stop)
        self.sv = {**SELLER, 'name': 'ООО «СВ»', 'inn': '5638073633', 'kpp': '561101001', 'account': '4' * 20}
        self.order = {'id': 42, 'orderItems': [{'id': 10, 'goodName': 'Товар', 'count': 2}]}
        self.result = {'order_id': 42, 'invoice_number': 278, 'customer': {'name': 'ООО Покупатель', 'inn': '7716997861', 'kpp': '770801001'},
                       'items': [{'position': 1, 'order_item_id': 10, 'decision': 'approved', 'selected': {'name': 'Товар'},
                                  'requested_name': 'Товар', 'quantity': 2, 'unit': 'шт', 'proposed_unit_price': 100}],
                       'summary': {'positions': 1, 'auto_ready': 1}, 'status': 'ready_for_review'}
        with pipeline._connect() as conn:
            self.job = conn.execute('INSERT INTO automation_jobs(dedupe_key,order_id,event_type,status,order_json,result_json,created_at,updated_at,invoice_number) VALUES(?,?,?,?,?,?,?,?,?)',
                                    ('seller-test', 42, 'api_order', 'ready_for_review', json.dumps(self.order), json.dumps(self.result), 'now', 'now', 278)).lastrowid
        app = FastAPI()
        pipeline.install_automation_pipeline(app, Mock())
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_saved_sv_is_used_by_both_download_routes_after_reload(self):
        pipeline.save_invoice_settings(self.job, self.sv, 'sv-account', 0.2)
        context = pipeline.load_automation_offer_context(42)
        self.assertEqual(context['result']['seller'], self.sv)
        self.assertEqual(context['result']['destination_account_id'], 'sv-account')
        for prefix in ('/automation', '/dashboard/automation'):
            response = self.client.get(f'{prefix}/jobs/{self.job}/invoice.xlsx')
            self.assertEqual(response.status_code, 200, response.text if response.status_code != 200 else '')
            sheet = load_workbook(BytesIO(response.content)).active
            self.assertIn(self.sv['inn'], sheet['A7'].value)
            self.assertNotIn(SELLER['inn'], sheet['A7'].value)
            self.assertIn(self.sv['inn'], response.headers['content-disposition'])

    def test_supplier_search_does_not_reset_selected_seller_or_tax(self):
        pipeline.save_invoice_settings(self.job, self.sv, 'sv-account', 0.2)
        with patch.object(pipeline, 'build_vi_draft', return_value=copy.deepcopy(self.result)):
            response = self.client.post(f'/dashboard/automation/jobs/{self.job}/refresh', follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        saved = pipeline.load_automation_offer_context(42)['result']
        self.assertEqual(saved['seller'], self.sv)
        self.assertEqual(saved['vat_rate'], 0.2)
        self.assertEqual(saved['destination_account_id'], 'sv-account')

    def test_existing_tile_quote_seller_is_recovered_only_for_same_job(self):
        with pipeline._connect() as conn:
            conn.execute('INSERT INTO tile_quotes(job_id,catalog_id,snapshot_json,created_at) VALUES(?,?,?,?)',
                         (self.job + 1, 1, json.dumps({'seller': self.sv, 'vat_rate': 0.2}), 'now'))
        self.assertNotIn('seller', pipeline.load_automation_offer_context(42)['result'])
        with pipeline._connect() as conn:
            conn.execute('INSERT INTO tile_quotes(job_id,catalog_id,snapshot_json,created_at) VALUES(?,?,?,?)',
                         (self.job, 1, json.dumps({'seller': self.sv, 'vat_rate': 0.2}), 'now'))
        response = self.client.get(f'/dashboard/automation/jobs/{self.job}/invoice.xlsx')
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.sv['inn'], load_workbook(BytesIO(response.content)).active['A7'].value)

    def test_seller_change_is_blocked_for_sent_and_uncertain_offers(self):
        pipeline.save_invoice_settings(self.job, self.sv, 'sv-account', 0.2)
        pipeline.begin_offer_submission(self.job, 42, 'attempt', 'guid', 'hash', 'payload', '278', 'file', {})
        with self.assertRaisesRegex(ValueError, 'заблокировано'):
            pipeline.save_invoice_settings(self.job, SELLER, 'avior-account', 0.22)
        self.assertEqual(pipeline.load_automation_offer_context(42)['result']['seller']['inn'], self.sv['inn'])


if __name__ == '__main__':
    unittest.main()
