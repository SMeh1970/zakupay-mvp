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


def post_check(client, job):
    return client.post(f'/dashboard/automation/jobs/{job}/actuality', headers={'X-Requested-With': 'order-actuality'})


def test_expired_record_opens_at_once_and_page_checks_exact_id_and_preserves_invoice(panel):
    client, job, fetch, _, order = panel
    expire()
    fetch.return_value = order
    response = client.get(f'/dashboard/automation/jobs/{job}/open', follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'].endswith('/review')
    fetch.assert_not_called()  # opening never waits for Zakupay
    page = client.get(f'/dashboard/automation/jobs/{job}/review')
    assert "id='actuality-banner'" in page.text and "data-fresh='0'" in page.text
    assert "connect-src 'self'" in page.headers['content-security-policy']
    with closing(p._connect()) as conn:
        before = dict(conn.execute('SELECT * FROM automation_jobs WHERE id=?',(job,)).fetchone())
    assert post_check(client, job).json()['state'] == 'active'  # what the banner script does
    fetch.assert_called_once_with(123, force=True)
    with closing(p._connect()) as conn:
        after = dict(conn.execute('SELECT * FROM automation_jobs WHERE id=?',(job,)).fetchone())
    assert before == after
    fresh_page = client.get(f'/dashboard/automation/jobs/{job}/review')
    assert "data-fresh='1'" in fresh_page.text and 'Актуальна' in fresh_page.text


@pytest.mark.parametrize('value,reason', [
    (None, 'не вернул заявку'),
    ({'id': 999, 'actualityStatus': 'ACTUAL'}, 'другим номером'),
    ({'id': 123, 'actualityStatus': 'WEIRD', 'orderItems': [{'actualityStatus': 'X'}]}, 'без распознанного статуса'),
])
def test_unrecognized_answer_is_unknown_explained_and_keeps_last_real_answer(panel, value, reason):
    client, job, fetch, _, _ = panel
    expire()
    fetch.return_value = value
    response = client.get(f'/dashboard/automation/jobs/{job}/open', follow_redirects=False)
    assert response.status_code == 303  # still opens: the saved version stays reachable
    result = post_check(client, job).json()
    assert result['state'] == 'unknown' and 'не подтверждена' in result['label']
    assert reason in result['reason']
    assert 'Последний ответ Закупай: Актуальна' in result['label']
    page = client.get(f'/dashboard/automation/jobs/{job}/review')
    shown = page.text.split("id='actuality-banner'", 1)[1].split('</div>', 1)[0]
    # The failed check did not erase the last real answer, and nothing claims the order is closed.
    assert 'Актуальна · требуется повторная проверка' in shown and 'неактуальна' not in shown.lower().replace('проверяется', '')


def test_failure_reasons_are_reported_and_logged(panel, caplog):
    from fastapi import HTTPException
    client, job, fetch, _, _ = panel
    fetch.side_effect = HTTPException(502, 'Ошибка соединения с Закупай: connect timeout')
    with caplog.at_level('WARNING'):
        result = post_check(client, job).json()
    assert result['state'] == 'unknown' and 'HTTP 502' in result['reason'] and 'connect timeout' in result['reason']
    assert any('actuality check' in r.message and 'HTTP 502' in r.message for r in caplog.records)
    fetch.side_effect = None
    import time
    with patch.object(a, 'CHECK_TIMEOUT', 0.05):
        fetch.side_effect = lambda *args, **kwargs: time.sleep(0.3)
        assert 'не ответил за' in post_check(client, job).json()['reason']
    time.sleep(0.4)


def test_first_failed_check_without_any_previous_answer_is_stored_as_unknown(panel):
    client, job, fetch, _, _ = panel
    with closing(p._connect()) as conn, conn:
        conn.execute('DELETE FROM order_actuality')
    fetch.return_value = None
    assert post_check(client, job).json()['state'] == 'unknown'
    with closing(p._connect()) as conn:
        assert conn.execute('SELECT state FROM order_actuality WHERE order_id=123').fetchone()['state'] == 'unknown'


def test_explicit_closure_is_shown_and_fresh_closure_stops_opening_without_api(panel):
    client, job, fetch, _, order = panel
    expire()
    fetch.return_value = {**order, 'actualityStatus':'CLOSED'}
    assert post_check(client, job).json()['state'] == 'inactive'
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


def test_late_zakupay_answer_is_kept_for_the_next_open(panel):
    import time
    client, job, fetch, _, order = panel
    expire()

    def slow(order_id, force=False):
        time.sleep(0.5)
        return order
    fetch.side_effect = slow
    with patch.object(a, 'CHECK_TIMEOUT', 0.1):
        assert post_check(client, job).json()['state'] == 'unknown'
    time.sleep(0.8)  # the late answer arrives in the background and is stored
    again = client.get(f'/dashboard/automation/jobs/{job}/open', follow_redirects=False)
    assert again.status_code == 303 and fetch.call_count == 1
    assert "data-fresh='1'" in client.get(f'/dashboard/automation/jobs/{job}/review').text


def test_late_closed_answer_is_kept_and_stops_opening(panel):
    import time
    client, job, fetch, _, order = panel
    expire()

    def slow(order_id, force=False):
        time.sleep(0.4)
        return {**order, 'actualityStatus': 'CLOSED'}
    fetch.side_effect = slow
    with patch.object(a, 'CHECK_TIMEOUT', 0.1):
        post_check(client, job)
    time.sleep(0.6)
    again = client.get(f'/dashboard/automation/jobs/{job}/open', follow_redirects=False)
    assert again.status_code == 200 and 'Неактуальна' in again.text


def test_actuality_filter_uses_last_known_answer_and_keeps_manual_out(panel):
    client, job, fetch, _, order = panel
    p.process_api_order({**order, 'id': 124, 'name': 'Second'})
    p.process_api_order({**order, 'id': 125, 'name': 'Third'})
    with closing(p._connect()) as conn, conn:
        conn.execute("UPDATE order_actuality SET state='inactive' WHERE order_id=124")
        conn.execute("DELETE FROM order_actuality WHERE order_id=125")
    p.save_manual_order('Ручная', [{'goodName': 'Болт', 'count': 1}], reference='M-1')

    def shown(value):
        text = client.get('/dashboard/automation?actuality=' + value).text
        return {n for n in (123, 124, 125, 'M-1') if f'Заявка №{n}' in text or (n == 'M-1' and 'M-1' in text)}
    assert shown('all') == {123, 124, 125, 'M-1'}
    assert shown('active') == {123}
    assert shown('inactive') == {124}
    assert shown('unknown') == {125}  # never checked is not "actual"
    assert shown('garbage') == {123, 124, 125, 'M-1'}  # unknown value falls back to "all"
    assert "name='actuality'" in client.get('/dashboard/automation').text
