import copy
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import automation_pipeline as pipeline
import security
import tile_autopilot as auto
from test_porcelain import ORDER, archive
import porcelain as tiles


class TileAutopilotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        settings=patch.multiple(pipeline,DB_PATH=os.path.join(self.tmp.name,'auto.db'),
                                DATABASE_URL='',_initialized_database_key=None)
        settings.start();self.addCleanup(settings.stop)
        self.order=copy.deepcopy(ORDER)
        self.order.update(creationDate='2026-09-01T12:00:00+0300',finishDate='2026-09-10')
        self.job=pipeline.save_api_order_for_manual_start(self.order)['job_id']
        tiles.import_catalog(archive())
        self.fetch=Mock(side_effect=AssertionError('No expired order lookup'))
        self.headers=Mock(side_effect=AssertionError('No price request for expired order'))
        app=FastAPI()
        auto.install_tile_autopilot(app,fetch_order=self.fetch,headers=self.headers,
                                   base='https://prodavay.sel-be.ru',has_offer=Mock(return_value=False))
        security.install_security(app)
        auth=patch.object(security,'_valid_session',return_value=True)
        auth.start();self.addCleanup(auth.stop)
        self.client=TestClient(app);self.addCleanup(self.client.close)

    @patch.object(auto.managed,'configured',return_value=False)
    def test_read_page_never_triggers_price_search_and_expired_queue_persists(self,_):
        self.assertEqual(self.client.get(auto.URL).status_code,200)
        self.fetch.assert_not_called()
        result=auto.check_queue(self.fetch,self.headers,'https://prodavay.sel-be.ru',Mock())
        self.assertEqual(result['checked'],1)
        rows=auto.saved_reviews();self.assertEqual(len(rows),1)
        report=json.loads(rows[0]['report_json'])
        self.assertEqual(report['status'],'blocked')
        self.assertIn('Истёк срок заявки',report['blockers'])
        self.fetch.assert_not_called();self.headers.assert_not_called()
        self.assertEqual(auto.check_queue(self.fetch,self.headers,'https://prodavay.sel-be.ru',Mock())['checked'],0)
        page=self.client.get(auto.URL)
        self.assertIn('Истёк срок заявки',page.text)
        self.assertIn('автоматическая отправка ещё не подключена',page.text)

    def test_protected_page_and_cross_origin_post(self):
        with patch.object(security,'_valid_session',return_value=False):
            self.assertNotEqual(self.client.get(auto.URL,follow_redirects=False).status_code,200)
        self.assertEqual(self.client.post(auto.URL+'/check',headers={'Origin':'https://other.example'}).status_code,403)
        self.fetch.assert_not_called()

    @patch.object(auto.managed,'configured',return_value=False)
    def test_duplicate_order_saved_jobs_still_have_single_review(self,_):
        # Older job IDs never create a second automatic submission for one order.
        auto.check_queue(self.fetch,self.headers,'https://prodavay.sel-be.ru',Mock())
        self.assertEqual(len(auto.saved_reviews()),1)
        self.assertEqual(auto.saved_reviews()[0]['job_id'],self.job)

    @patch.object(auto.managed,'configured',return_value=False)
    @patch.object(auto,'fetch_prices',return_value=({},'Источник лучших цен Закупай: HTTP 401'))
    def test_provider_probe_retains_denial_and_runs_once_per_day(self,fetch,_):
        auto.check_queue(self.fetch,self.headers,'https://prodavay.sel-be.ru',Mock())
        auto.probe_provider_once(self.headers,'https://prodavay.sel-be.ru')
        report=json.loads(auto.saved_reviews()[0]['report_json'])
        self.assertIn('401',report['provider_result'])
        self.assertEqual(report['status'],'blocked')
        auto.probe_provider_once(self.headers,'https://prodavay.sel-be.ru')
        self.assertEqual(fetch.call_count,1)

    @patch.object(auto.managed,'configured',return_value=False)
    @patch.object(auto,'fetch_prices',return_value=({},'HTTP 401'))
    def test_replaced_session_invalidates_diagnostic_without_saving_cookie(self,fetch,_):
        auto.check_queue(self.fetch,self.headers,'https://prodavay.sel-be.ru',Mock())
        with patch.dict(os.environ,{'ZAKUPAY_PLAY_SESSION':'first-test-session'}):
            auto.probe_provider_once(self.headers,'https://prodavay.sel-be.ru')
            auto.probe_provider_once(self.headers,'https://prodavay.sel-be.ru')
        self.assertEqual(fetch.call_count,1)
        with patch.dict(os.environ,{'ZAKUPAY_PLAY_SESSION':'replaced-test-session'}):
            auto.probe_provider_once(self.headers,'https://prodavay.sel-be.ru')
        self.assertEqual(fetch.call_count,2)
        saved=auto.saved_reviews()[0]['report_json']
        self.assertNotIn('first-test-session',saved)
        self.assertNotIn('replaced-test-session',saved)

    @patch.object(auto.managed,'configured',return_value=False)
    @patch.object(auto,'fetch_prices',return_value=({},'HTTP 401'))
    def test_queue_refresh_preserves_provider_diagnostic_cooldown(self,fetch,_):
        auto.check_queue(self.fetch,self.headers,'https://prodavay.sel-be.ru',Mock())
        auto.probe_provider_once(self.headers,'https://prodavay.sel-be.ru')
        report=json.loads(auto.saved_reviews()[0]['report_json'])
        report['checked_at']='2026-01-01T00:00:00+00:00'
        auto.persist(self.job,report)
        auto.check_queue(self.fetch,self.headers,'https://prodavay.sel-be.ru',Mock())
        auto.probe_provider_once(self.headers,'https://prodavay.sel-be.ru')
        self.assertEqual(fetch.call_count,1)
        self.assertIn('401',json.loads(auto.saved_reviews()[0]['report_json'])['provider_result'])

    @patch.object(auto.managed,'configured',return_value=False)
    @patch.object(auto,'fetch_prices',return_value=({},'No price'))
    def test_diagnostic_prefers_order_with_competing_tile_offer(self,fetch,_):
        for item in self.order['orderItems']:
            item.update(bestOfferItem=None,companiesWithOffersCount='0')
        pipeline.save_api_order_for_manual_start(self.order)
        other=copy.deepcopy(self.order);other['id']=int(other['id'])+1
        other['orderItems'][0].update(bestOfferItem='Керамогранит конкурента',companiesWithOffersCount=1)
        pipeline.save_api_order_for_manual_start(other)
        auto.check_queue(self.fetch,self.headers,'https://prodavay.sel-be.ru',Mock())
        auto.probe_provider_once(self.headers,'https://prodavay.sel-be.ru')
        self.assertEqual(fetch.call_args.args[0]['id'],other['id'])


if __name__=='__main__':unittest.main()
