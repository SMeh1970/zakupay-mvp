"""Read-only Zakupay actuality checks, separate from invoice and matching snapshots."""
import html
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger(__name__)
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


def status_detail(order):
    """The raw statuses Zakupay returned, so a verdict can be compared with what the Zakupay site shows."""
    if not isinstance(order, dict):
        return ''
    items = [item for item in (order.get('orderItems') or []) if isinstance(item, dict)]
    counts = {}
    for item in items:
        key = str(item.get('actualityStatus'))
        counts[key] = counts.get(key, 0) + 1
    return (f"заявка: {order.get('actualityStatus')!r}; позиции: "
            + (', '.join(f'{key} × {number}' for key, number in sorted(counts.items())) or 'нет данных'))


def unknown_reason(order, order_id):
    """Say why an answer could not be classified, so 'не подтверждена' is never a mystery."""
    if not isinstance(order, dict):
        return 'Закупай не вернул заявку с этим номером'
    if str(order.get('id')) != str(order_id):
        return 'Закупай вернул заявку с другим номером'
    items = order.get('orderItems') or []
    seen = sorted({str(item.get('actualityStatus')) for item in items if isinstance(item, dict)})
    return ('Закупай вернул заявку без распознанного статуса актуальности '
            f"(заявка: {order.get('actualityStatus')!r}; позиции: {', '.join(seen) or 'нет данных'})")


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
        state, reason, detail = 'unknown', '', ''
        if not fetch_order:
            reason = 'получение заявок из Закупай не настроено'
        elif not _slots.acquire(blocking=False):
            reason = 'сейчас идёт слишком много проверок одновременно'
        else:
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
                reason = 'не удалось запустить проверку'
            if future:
                try:
                    order = future.result(timeout=CHECK_TIMEOUT)
                    state = classify(order) if isinstance(order, dict) and str(order.get('id')) == str(order_id) else 'unknown'
                    if state == 'unknown':
                        reason = unknown_reason(order, order_id)
                    else:
                        detail = status_detail(order)
                except TimeoutError:
                    reason = f'Закупай не ответил за {CHECK_TIMEOUT} с'
                except HTTPException as exc:
                    reason = f'ошибка обращения к Закупай: HTTP {exc.status_code}, {str(exc.detail)[:160]}'
                except Exception as exc:
                    reason = f'ошибка обращения к Закупай: {type(exc).__name__} {str(exc)[:160]}'
        if state == 'unknown':
            # Timeouts, API failures and absence do not prove closure.
            logger.warning('actuality check order=%s unknown reason=%s', order_id, reason)
        elif state == 'inactive':
            logger.warning('actuality check order=%s inactive %s', order_id, detail)
        with connect() as conn:
            if state == 'unknown':
                previous = execute(conn, 'SELECT state,checked_at FROM order_actuality WHERE order_id=?', (int(order_id),)).fetchone()
                if previous and previous['state'] != 'unknown':
                    # A failed check must not erase the last real answer from Zakupay.
                    return dict(state='unknown', checked_at=datetime.now(timezone.utc).isoformat(),
                                last_known=dict(previous), reason=reason)
            return {**store(conn, execute, order_id, state), 'reason': reason, 'detail': detail}

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
        text = (label(value) if 'last_known' not in value else
                'Актуальность не подтверждена. Последний ответ Закупай: ' + label(value['last_known']))
        return {**value, 'label': text}

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
                unknown:' Это не означает, что заявка закрыта; сведения ниже сохранены ранее.'}[value.state] || '';
  const why = value.state === 'unknown' && value.reason ? ' Причина: ' + value.reason + '.'
            : (value.state === 'inactive' && value.detail ? ' Ответ Закупай: ' + value.detail + '.' : '');
  paint(value.state, value.label + '.' + note + why);
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
