"""Read-only Zakupay actuality checks, separate from invoice and matching snapshots."""
import html
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

SCHEMA = "CREATE TABLE IF NOT EXISTS order_actuality (order_id BIGINT PRIMARY KEY, state TEXT NOT NULL, checked_at TEXT NOT NULL)"
TTL_SECONDS = 600
CHECK_TIMEOUT = 12
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='actuality')
_slots = threading.BoundedSemaphore(4)


def classify(order):
    if not isinstance(order, dict):
        return 'unknown'
    states = {'ACTUAL': 'active', 'NOT_ACTUAL': 'inactive', 'CLOSED': 'inactive'}
    root = states.get(str(order.get('actualityStatus') or '').upper())
    if root:
        return root
    items = order.get('orderItems') or []
    values = [states.get(str(item.get('actualityStatus') or '').upper()) for item in items]
    if 'active' in values:
        return 'active'
    return 'inactive' if values and all(value == 'inactive' for value in values) else 'unknown'


def store(conn, execute, order_id, state):
    stamp = datetime.now(timezone.utc).isoformat()
    execute(conn, "INSERT INTO order_actuality(order_id,state,checked_at) VALUES(?,?,?) ON CONFLICT(order_id) DO UPDATE SET state=excluded.state, checked_at=excluded.checked_at", (int(order_id), state, stamp))
    return dict(state=state, checked_at=stamp)


def fresh(value):
    if not value or value['state'] == 'unknown':
        return False
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(value['checked_at'])).total_seconds()
        return 0 <= age < TTL_SECONDS
    except (ValueError, TypeError):
        return False


def label(value):
    if not value:
        return 'Актуальность не проверена'
    title = {'active': 'Актуальна', 'inactive': 'Неактуальна', 'unknown': 'Актуальность не подтверждена'}[value['state']]
    stamp = datetime.fromisoformat(value['checked_at'])
    minutes = max(0, int((datetime.now(timezone.utc) - stamp).total_seconds() // 60))
    return title + (' · требуется повторная проверка' if minutes >= 10 else '') + f' · проверено {minutes} мин. назад'


def install(app, connect, execute, fetch_order):
    def check(order_id):
        state = 'unknown'
        if fetch_order and _slots.acquire(blocking=False):
            def fetch():
                try:
                    return fetch_order(int(order_id), force=True)
                finally:
                    _slots.release()
            try:
                future = _pool.submit(fetch)
            except Exception:
                _slots.release()
                future = None
            if future:
                try:
                    order = future.result(timeout=CHECK_TIMEOUT)
                    if isinstance(order, dict) and str(order.get('id')) == str(order_id):
                        state = classify(order)
                except Exception:
                    # Timeout, API failures and absence do not prove closure.
                    pass
        with connect() as conn:
            return store(conn, execute, order_id, state)

    def job(job_id):
        with connect() as conn:
            row = execute(conn, 'SELECT order_id,event_type FROM automation_jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Заявка не найдена')
        return row

    @app.post('/dashboard/automation/jobs/{job_id}/actuality')
    def check_one(job_id: int, request: Request):
        if request.headers.get('x-requested-with') != 'order-actuality':
            raise HTTPException(403, 'Обновите страницу и повторите проверку')
        row = job(job_id)
        if row['event_type'] == 'manual_order':
            return {'state': 'manual', 'label': 'Ручная заявка'}
        value = check(row['order_id'])
        return {**value, 'label': label(value)}

    @app.head('/dashboard/automation/jobs/{job_id}/open')
    def open_head(job_id: int):
        job(job_id)
        return HTMLResponse('', headers={'Cache-Control': 'no-store'})

    @app.get('/dashboard/automation/jobs/{job_id}/open')
    def open_checked(job_id: int):
        row = job(job_id)
        target = f'/dashboard/automation/jobs/{job_id}/review'
        if row['event_type'] == 'manual_order':
            return RedirectResponse(target, 303)
        with connect() as conn:
            cached = execute(conn, 'SELECT state,checked_at FROM order_actuality WHERE order_id=?', (row['order_id'],)).fetchone()
        value = dict(cached) if cached else None
        if not fresh(value):
            value = check(row['order_id'])
        if value['state'] == 'active':
            return RedirectResponse(target, 303, headers={'Cache-Control': 'no-store'})
        message = ('Закупай сообщил, что заявка неактуальна. Подбор не запускался.' if value['state'] == 'inactive' else
                   'Не удалось подтвердить актуальность заявки в Закупай. Это не означает, что она закрыта. Подбор не запускался.')
        return HTMLResponse("<!doctype html><html lang='ru'><meta charset='utf-8'><title>Актуальность заявки</title>"
            "<body style='font-family:Arial;margin:32px'><h1>" + html.escape(label(value)) + '</h1><p>' + message + '</p>'
            + f"<p><a href='{target}'>Открыть сохранённую версию</a></p><p><a href='/dashboard/automation'>Вернуться к списку</a></p></body></html>",
            headers={'Cache-Control': 'no-store'})


SCRIPT = """
(() => {
 const button = document.getElementById('check-actuality');
 if (!button) return;
 const status = document.getElementById('actuality-progress');
 const jobs = JSON.parse(button.dataset.jobs);
 button.disabled = jobs.length === 0;
 button.addEventListener('click', async () => {
  button.disabled = true;
  let done = 0, unknown = 0, inactive = 0;
  for (const id of jobs) {
   status.textContent = `Проверка актуальности: ${done} из ${jobs.length}…`;
   let value;
   try {
    const response = await fetch(`/dashboard/automation/jobs/${id}/actuality`, {
     method:'POST', headers:{'X-Requested-With':'order-actuality'}, signal:AbortSignal.timeout(18000)
    });
    if (!response.ok) throw new Error('check failed');
    value = await response.json();
   } catch { value = {state:'unknown',label:'Актуальность не подтверждена'}; }
   const badge = document.querySelector(`[data-actuality-job="${id}"]`);
   if (badge) {badge.textContent = value.label; badge.dataset.state = value.state;}
   if (value.state === 'unknown') unknown++;
   if (value.state === 'inactive') inactive++;
   done++;
  }
  status.textContent = `Проверено: ${done}. Неактуальных: ${inactive}. Не удалось подтвердить: ${unknown}.`;
  button.disabled = jobs.length === 0;
 });
})();
"""
