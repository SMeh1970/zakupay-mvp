import html
import os
import unittest
from unittest.mock import Mock, patch

import browser_prices as bp


class BestPriceTests(unittest.TestCase):
    def setUp(self):
        bp._cache.clear()
        self.env = patch.dict(os.environ, {'ZAKUPAY_PLAY_SESSION': 'test-session', 'ZAKUPAY_BROWSER_PRICES_ENABLED': 'true'})
        self.env.start()
        self.transport = patch.object(bp.requests, 'Session')
        self.session = self.transport.start().return_value.__enter__.return_value
        self.response = self.session.post.return_value
        self.response.status_code = 200
        self.response.json.return_value = [{'orderItemId': '2', 'bestPrice': {'price': 125}}]

    def tearDown(self):
        self.transport.stop()
        self.env.stop()
        bp._cache.clear()

    def test_explicit_opt_in_required(self):
        with patch.dict(os.environ, {'ZAKUPAY_BROWSER_PRICES_ENABLED': ''}):
            self.assertFalse(bp.browser_price_source_enabled())
            self.assertEqual(bp.fetch_best_prices([2])['prices'], {})
            self.session.post.assert_not_called()

    def test_regular_order_page_and_missing_access(self):
        import main
        order = {'id': 1, 'orderItems': [{'id': 2, 'goodName': 'Item'}]}
        with patch.object(main, 'fetch_all_orders', return_value=[order]):
            with patch.dict(os.environ, {'ZAKUPAY_PLAY_SESSION': ''}):
                content = main.dashboard_order(1).body.decode()
            self.assertIn('Источник лучшей цены не настроен', content)
            self.assertIn('Лучшая цена', content)
            content = main.dashboard_order(1).body.decode()
            self.assertIn('125.00 ₽', content)
            self.assertNotIn('test-session', content)

    def test_ids_not_position_or_name(self):
        result = bp.fetch_best_prices([1, '2', 1, True, 2.5, 'bad'])
        self.assertEqual(set(result['prices']), {2})
        self.assertEqual(self.session.post.call_args.kwargs['json'], [1, 2])
        self.assertFalse(self.session.post.call_args.kwargs['allow_redirects'])
        self.assertNotIn('ZakupayToken', self.session.post.call_args.kwargs['headers'])

    def test_no_credentials_no_request_or_cached_prices(self):
        bp.fetch_best_prices([2])
        with patch.dict(os.environ, {'ZAKUPAY_PLAY_SESSION': ''}):
            result = bp.fetch_best_prices([2])
        self.assertEqual(result['prices'], {})
        self.assertEqual(result['errors'], ['not_configured'])
        self.assertEqual(self.session.post.call_count, 1)

    def test_session_replacement_and_ttl(self):
        bp.fetch_best_prices([2])
        bp.fetch_best_prices([2])
        self.assertEqual(self.session.post.call_count, 1)
        with patch.dict(os.environ, {'ZAKUPAY_PLAY_SESSION': 'other'}):
            bp.fetch_best_prices([2])
        self.assertEqual(self.session.post.call_count, 2)
        with patch.object(bp, 'CACHE_TTL', 0):
            bp.fetch_best_prices([2])
        self.assertEqual(self.session.post.call_count, 3)

    def test_auth_denied_redirect_and_safe_errors(self):
        for status, error in [(401, 'unauthorized'), (403, 'forbidden'), (302, 'unauthorized'), (500, 'upstream_error')]:
            with self.subTest(status=status):
                self.response.status_code = status
                result = bp.fetch_best_prices([2], force=True)
                self.assertEqual(result['prices'], {})
                self.assertEqual(result['errors'], [error])
        self.response.status_code = 200
        self.response.json.side_effect = ValueError('secret body')
        self.assertEqual(bp.fetch_best_prices([2], force=True)['errors'], ['invalid_contract'])
        self.response.json.side_effect = bp.requests.Timeout('secret cookie')
        self.assertEqual(bp.fetch_best_prices([2], force=True)['errors'], ['connection_error'])

    def test_duplicates_unknown_ids_and_contract(self):
        self.response.json.return_value = [
            {'orderItemId': 2, 'bestPrice': {'price': 1}},
            {'orderItemId': '2', 'bestPrice': {'price': 5}},
            {'orderItemId': 999, 'bestPrice': {'price': 10}},
            {'orderItemId': 'bad'}, None]
        result = bp.fetch_best_prices([2])
        self.assertEqual(result['prices'], {})
        self.assertEqual(result['errors'], ['invalid_contract'])
        self.response.json.return_value = {'login': 'secret'}
        self.assertEqual(bp.fetch_best_prices([2])['errors'], ['invalid_contract'])

    def test_values_currency_and_corridor(self):
        for value in [True, False, -1, 0, 'NaN', float('inf'), {}, None]:
            self.assertIsNone(bp.best_price_value({'bestPrice': {'price': value}}))
        self.assertEqual(bp.best_price_value({'bestPrice': {'price': '12.5'}}), 12.5)
        self.assertIsNone(bp.best_price_value({'bestPrice': {'price': 5, 'currencyCode': 'USD'}}))
        self.assertEqual(bp.best_price_value({'bestPrice': {'priceInDefaultCurrency': 500, 'currencyCode': 'USD'}}), 500)
        self.assertIsNone(bp.best_price_value({'price': 100, 'secondPrice': {'price': 50}, 'offerDeviation': 10}))
        self.assertIn('только коридор', bp.price_state({'errors': []}, {'offerDeviation': 10}))

    def test_analysis_page_never_falls_back_to_unrelated_price(self):
        from fastapi import FastAPI
        from analysis_detail import install_analysis_detail
        app = FastAPI()
        order = {'id': 1, 'orderItems': [
            {'id': 1, 'goodName': 'First', 'price': 99999},
            {'id': '2', 'goodName': 'Second', 'count': 2}]}
        install_analysis_detail(app, lambda: [order], lambda: {}, '', lambda v: '' if v is None else html.escape(str(v)),
                                fetch_order_by_id=lambda *a, **kw: order)
        endpoint = next(r.endpoint for r in app.routes if r.path == '/dashboard/analysis/order/{order_id}')
        result = {'ok': True, 'enabled': True, 'errors': [], 'prices': {2: {'orderItemId': 2, 'bestPrice': {'price': 125}}}}
        with patch('analysis_detail.best_price_rows_for_order', return_value=result):
            content = endpoint(1).body.decode()
        self.assertNotIn('99 999', content)
        self.assertIn('125.00 ₽', content)
        self.assertIn('250.00 ₽', content)


if __name__ == '__main__':
    unittest.main()
