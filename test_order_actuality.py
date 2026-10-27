import copy
import gc
import json
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
import automation_pipeline as p
import order_actuality as a


@pytest.fixture
def panel(tmp_path):
    with patch.multiple(p, DB_PATH=str(tmp_path/'jobs.db'), DATABASE_URL='', _initialized_database_key=None):
        fetch = Mock()
        fetch_all = Mock(return_value=[])
        app = FastAPI()
        p.install_automation_pipeline(app, fetch, fetch_all_orders=fetch_all)
        order = {'id': 123, 'name': 'Test', 'delay': 0, 'actualityStatus': 'ACTUAL',
                 'orderItems': [{'goodName': 'Bolt', 'count': 2}]}
        saved = p.process_api_order(order)
        with TestClient(app) as client:
            yield client, saved['job_id'], fetch, fetch_all, order
        gc.collect()


@pytest.mark.parametrize('order,expected', [
    ({'actualityStatus': 'ACTUAL'}, 'active'),
    ({'actualityStatus': 'CLOSED'}, 'inactive'),
    ({'actualityStatus': 'NOT_ACTUAL'}, 'inactive'),
    ({'orderItems': [{'actualityStatus': 'ACTUAL'}, {'actualityStatus': 'CLOSED'}]}, 'active'),
    ({'orderItems': [{'actualityStatus': 'CLOSED'}, {'actualityStatus': 'NOT_ACTUAL'}]}, 'inactive'),
    ({'orderItems': [{'actualityStatus': 'CLOSED'}, {}]}, 'unknown'),
    ({'actualityStatus': 'NEW_UNKNOWN_VALUE'}, 'unknown'),
    ({'finishDate': '2000-01-01'}, 'unknown'), (None, 'unknown')])
def test_status_evidence(order, expected):
    assert a.classify(order) == expected


def expire():
    with closing(p._connect()) as conn, conn:
        conn.execute('UPDATE order_actuality SET checked_at=?', ((datetime.now(timezone.utc)-timedelta(minutes=11)).isoformat(),))


def test_fresh_import_opens_without_api_or_search(panel):
    client, job, fetch, _, _ = panel
    with patch.object(p, '_search_candidates', side_effect=AssertionError('No search')):
        response = client.get(f'/dashboard/automation/jobs/{job}/open', follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'].endswith('/review')
    fetch.assert_not_called()
    page = client.get('/dashboard/automation')
    assert 'Актуальна' in page.text and 'Проверить актуальность заявок' in page.text
    assert f"jobs/{job}/open" in page.text


def test_expired_record_checks_exact_id_and_preserves_invoice(panel):
    client, job, fetch, _, order = panel
    expire()
    fetch.return_value = order
    with closing(p._connect()) as conn:
        before = dict(conn.execute('SELECT * FROM automation_jobs WHERE id=?',(job,)).fetchone())
    response = client.get(f'/dashboard/automation/jobs/{job}/open', follow_redirects=False)
    assert response.status_code == 303
    fetch.assert_called_once_with(123, force=True)
    with closing(p._connect()) as conn:
        after = dict(conn.execute('SELECT * FROM automation_jobs WHERE id=?',(job,)).fetchone())
    assert before == after


@pytest.mark.parametrize('value', [None, {'id': 999, 'actualityStatus': 'ACTUAL'}, {'id': 123}])
def test_missing_or_unrecognized_is_unknown_and_never_closed(panel, value):
    client, job, fetch, _, _ = panel
    expire()
    fetch.return_value = value
    response = client.get(f'/dashboard/automation/jobs/{job}/open')
    assert 'Актуальность не подтверждена' in response.text
    assert 'Открыть сохранённую версию' in response.text
    assert 'Закупай сообщил, что заявка неактуальна' not in response.text


def test_explicit_closure_stops_opening_and_cached_closure_skips_api(panel):
    client, job, fetch, _, order = panel
    expire()
    fetch.return_value = {**order, 'actualityStatus':'CLOSED'}
    response = client.get(f'/dashboard/automation/jobs/{job}/open')
    assert 'Неактуальна' in response.text and 'Открыть сохранённую версию' in response.text
    client.get(f'/dashboard/automation/jobs/{job}/open')
    assert fetch.call_count == 1


def test_api_error_and_timeout_never_mark_closed(panel):
    client, job, fetch, _, _ = panel
    fetch.side_effect = RuntimeError('API down')
    response = client.post(f'/dashboard/automation/jobs/{job}/actuality', headers={'X-Requested-With':'order-actuality'})
    assert response.json()['state'] == 'unknown'
    fetch.side_effect = None
    with patch.object(a._pool, 'submit', return_value=Mock(result=Mock(side_effect=a.TimeoutError))):
        # Test the timeout path without waiting or leaving a real request running.
        try:
            response = client.post(f'/dashboard/automation/jobs/{job}/actuality', headers={'X-Requested-With':'order-actuality'})
            assert response.json()['state'] == 'unknown'
        finally:
            a._slots.release()


def test_mass_check_endpoint_always_rechecks_and_manual_is_local(panel):
    client, job, fetch, _, order = panel
    fetch.return_value = {**order, 'actualityStatus':'NOT_ACTUAL'}
    assert client.post(f'/dashboard/automation/jobs/{job}/actuality').status_code == 403
    response = client.post(f'/dashboard/automation/jobs/{job}/actuality', headers={'X-Requested-With':'order-actuality'})
    assert response.json()['state'] == 'inactive'
    fetch.assert_called_once()
    with closing(p._connect()) as conn, conn:
        conn.execute("UPDATE automation_jobs SET event_type='manual_order' WHERE id=?", (job,))
    assert client.get(f'/dashboard/automation/jobs/{job}/open', follow_redirects=False).status_code == 303
    client.post(f'/dashboard/automation/jobs/{job}/actuality', headers={'X-Requested-With':'order-actuality'})
    assert fetch.call_count == 1


def test_sync_updates_duplicate_actuality_without_overwriting_snapshot(panel):
    client, job, fetch, fetch_all, order = panel
    with closing(p._connect()) as conn:
        before = conn.execute('SELECT result_json FROM automation_jobs WHERE id=?',(job,)).fetchone()[0]
    fetch_all.return_value = [{**order, 'actualityStatus':'CLOSED', 'orderItems':[]}]
    client.post('/dashboard/automation/sync', follow_redirects=False)
    with closing(p._connect()) as conn:
        assert conn.execute('SELECT state FROM order_actuality WHERE order_id=123').fetchone()[0] == 'inactive'
        assert conn.execute('SELECT result_json FROM automation_jobs WHERE id=?',(job,)).fetchone()[0] == before
    fetch.assert_not_called()
