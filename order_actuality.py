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
                    order = fetch_order(int(order_id), force=True)
                    if isinstance(order, dict) and str(order.get('id')) == str(order_id):
                        known = classify(order)
                        if known != 'unknown':
                            # Zakupay answered after the caller stopped waiting: keep the answer,
                            # so the next click opens at once instead of asking Zakupay again.
                            try:
                                with connect() as conn:
                                    store(conn, execute, order_id, known)
                            except Exception:
                                pass
                    return order
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
        if not fresh(value) or value['state'] == 'active':
            # Open at once. The order page asks Zakupay itself and shows the answer in a banner,
            # so a silent Zakupay never keeps the person waiting on a blank tab.
            return RedirectResponse(target, 303, headers={'Cache-Control': 'no-store'})
        message = ('Закупай сообщил, что заявка неактуальна. Подбор не запускался.' if value['state'] == 'inactive' else
                   'Не удалось подтвердить актуальность заявки в Закупай. Это не означает, что она закрыта. Подбор не запускался.')
        return HTMLResponse("<!doctype html><html lang='ru'><meta charset='utf-8'><title>Актуальность заявки</title>"
            "<body style='font-family:Arial;margin:32px'><h1>" + html.escape(label(value)) + '</h1><p>' + message + '</p>'
            + f"<p><a href='{target}'>Открыть сохранённую версию</a></p><p><a href='/dashboard/automation'>Вернуться к списку</a></p></body></html>",
            headers={'Cache-Control': 'no-store'})


BANNER_SCRIPT = """
(() => {
 const box = document.getElementById('actuality-banner');
 if (!box || box.dataset.job === undefined) return;
 const paint = (state, text) => {
  box.dataset.state = state;
  box.textContent = text;
  const colors = {active:['#e6f4ea','#137333'], inactive:['#fce8e6','#b3261e'], unknown:['#fff7df','#8a5a00']};
  const pair = colors[state] || colors.unknown;
  box.style.background = pair[0]; box.style.color = pair[1];
 };
 const run = async () => {
  paint('unknown', 'Актуальность проверяется в Закупай…');
  let value;
  try {
   const response = await fetch(`/dashboard/automation/jobs/${box.dataset.job}/actuality`, {
    method:'POST', headers:{'X-Requested-With':'order-actuality'}, signal:AbortSignal.timeout(20000)
   });
   if (!response.ok) throw new Error('check failed');
   value = await response.json();
  } catch { value = {state:'unknown', label:'Актуальность не подтверждена'}; }
  const note = {inactive:' Закупай сообщил, что заявка неактуальна: не отправляйте счёт, пока не уточните.',
                unknown:' Закупай не ответил. Это не означает, что заявка закрыта; сведения ниже сохранены ранее.'}[value.state] || '';
  paint(value.state, value.label + '.' + note);
  if (value.state !== 'active') {
   const again = document.createElement('button');
   again.type = 'button'; again.textContent = 'Проверить ещё раз'; again.style.marginLeft = '12px';
   again.addEventListener('click', run);
   box.appendChild(again);
  }
 };
 if (box.dataset.fresh !== '1') run();
})();
"""


def banner(order_id, job_id, nonce, cached):
    """Status line for the order page; rechecks in the browser when the saved answer is old."""
    value = dict(cached) if cached else None
    is_fresh = fresh(value) and value['state'] == 'active'
    colour = {'active': ('#e6f4ea', '#137333'), 'inactive': ('#fce8e6', '#b3261e')}.get(value['state'] if value else '', ('#fff7df', '#8a5a00'))
    text = label(value) + ('' if is_fresh else '. Проверяется в Закупай…')
    return (f"<div id='actuality-banner' role='status' data-job='{int(job_id)}' data-fresh='{1 if is_fresh else 0}' "
            f"style='padding:12px;border-radius:8px;margin:10px 0;background:{colour[0]};color:{colour[1]}'>{html.escape(text)}</div>"
            f"<script nonce='{html.escape(nonce, quote=True)}'>{BANNER_SCRIPT}</script>")


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
