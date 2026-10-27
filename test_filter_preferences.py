import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import security
import test_saved_dashboard as fixtures


class FilterPreferencesTests(unittest.TestCase):
    tearDown = fixtures.SavedDashboardTests.tearDown

    def setUp(self):
        fixtures.SavedDashboardTests.setUp(self)
        self.client.close()
        self.client = TestClient(self.app, base_url="https://testserver")

    def token(self):
        self.assertEqual(self.client.get('/dashboard/automation').status_code, 200)
        return self.client.cookies.get('panel_filter_csrf')

    def post(self, **values):
        return self.client.post('/dashboard/automation/filters', data={'csrf': self.token(), **values})

    def test_saved_regions_and_parameters_survive_new_browser_session(self):
        response = self.post(regions=['Москва', 'Казань'], payment='prepayment', enabled='on')
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"/jobs/{self.first['job_id']}/review", response.text)
        self.assertNotIn(f"class='title' target='_blank' rel='noopener' href='/dashboard/automation/jobs/{self.second['job_id']}/review", response.text)
        self.client.get('/logout')
        self.client.cookies.clear()
        response = self.client.get('/dashboard/automation')
        self.assertIn("name='enabled' checked", response.text)
        self.assertIn("value='Москва' checked", response.text)
        self.assertIn("value='Казань' checked", response.text)
        self.assertIn("value='prepayment' selected", response.text)

    def test_toggle_preserves_settings_and_restores_results(self):
        self.post(regions=['Москва'], enabled='on')
        disabled = self.post(action='toggle')
        self.assertNotIn("name='enabled' checked", disabled.text)
        self.assertIn("value='Москва' checked", disabled.text)
        self.assertIn(f"/jobs/{self.second['job_id']}/review", disabled.text)
        enabled = self.post(action='toggle', enabled='on')
        self.assertIn("name='enabled' checked", enabled.text)
        self.assertNotIn(f"class='title' target='_blank' rel='noopener' href='/dashboard/automation/jobs/{self.second['job_id']}/review", enabled.text)

    def test_multiple_regions_match_either_and_temporary_bypass_keeps_profile(self):
        result = self.post(regions=['Москва', 'Казань'], enabled='on')
        for job in (self.first, self.second):
            self.assertIn(f"/jobs/{job['job_id']}/review", result.text)
        bypass = self.client.get('/dashboard/automation?unfiltered=1')
        self.assertNotIn("name='enabled' checked", bypass.text)
        self.assertIn("name='enabled' checked", self.client.get('/dashboard/automation').text)

    def test_preferences_require_session_and_csrf(self):
        self.assertEqual(self.client.post('/dashboard/automation/filters', data={'enabled': 'on'}).status_code, 403)
        with patch.object(security, '_valid_session', return_value=False):
            response = self.client.post('/dashboard/automation/filters', follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertTrue(response.headers['location'].startswith('/login?'))

    def test_real_session_logout_login_restores_account_preferences(self):
        self.session.stop()
        with patch.object(security, '_valid_credentials', return_value=True):
            self.client.post('/login', data={'username': security.PANEL_USERNAME, 'password': 'test-only'})
            self.assertEqual(self.post(regions=['Москва'], enabled='on').status_code, 200)
            self.client.get('/logout')
            self.assertNotIn(security.SESSION_COOKIE, self.client.cookies)
            self.assertIn('Вход', self.client.get('/dashboard/automation').text)
            self.client.cookies.clear()
            restored = self.client.post('/login', data={'username': security.PANEL_USERNAME, 'password': 'test-only'})
            self.assertIn("name='enabled' checked", restored.text)
            self.assertIn("value='Москва' checked", restored.text)

    def test_invalid_save_keeps_previous_preferences(self):
        self.post(regions=['Москва'], enabled='on')
        self.assertEqual(self.post(quantity_min='10', enabled='on').status_code, 422)
        self.assertIn("value='Москва' checked", self.client.get('/dashboard/automation').text)
