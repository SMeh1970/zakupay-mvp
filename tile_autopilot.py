"""Persistent preflight queue for the user's automatic tile offer policy.

No outbound commercial POST is made until the price/delivery/account providers
are verified. The queue records the concrete missing inputs instead of silently
substituting a floor price or approving a fuzzy match.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

import requests
from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

import keramahall_catalog as managed
import browser_prices
import porcelain as tiles
from price_lists import database, execute
from porcelain_panel import page, esc
from tile_offer_policy import POLICY_VERSION, competitor_prices, due_at, evaluate, relevant_items, number

URL = '/dashboard/tiles/automatic'
_lock = threading.Lock()
logger = logging.getLogger('zakupay.tile_auto')


def initialize_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS tile_auto_reviews (
        order_id BIGINT PRIMARY KEY, job_id BIGINT NOT NULL, status TEXT NOT NULL,
        due_at TEXT, report_json TEXT NOT NULL, checked_at TEXT NOT NULL)''')


def fetch_prices(order, base, headers):
    ids = [item['id'] for _, item in relevant_items(order) if item.get('id') is not None]
    if not ids:
        return {}, 'Нет ID подходящих позиций'
    # The cabinet uses a browser session rather than the integration token.
    # Reuse the existing server-side provider without exporting its cookie.
    browser_status = 'сессия кабинета на сервере не настроена'
    if browser_prices.browser_price_source_enabled():
        browser_status = 'сессия кабинета не вернула подтверждённых числовых цен'
        try:
            result = browser_prices.fetch_best_prices(ids, force=True)
            if result.get('ok'):
                rows = [row for key, row in result.get('prices', {}).items()
                        if str(key) in {str(value) for value in ids}]
                prices = competitor_prices(rows, datetime.now(timezone.utc).isoformat())
                if any(price.get('price') is not None for price in prices.values()):
                    return prices, ''
                fields = sorted({key for row in rows if isinstance(row.get('bestPrice'), dict)
                                 for key in ('price','priceInDefaultCurrency','currencyCode','unitName')
                                 if key in row['bestPrice']})
                browser_status = ('сессия кабинета: HTTP 200; позиций в ответе: ' + str(len(rows))
                    + '; числовых лучших цен нет; поля цены: ' + (', '.join(fields) or 'нет'))
            # Never persist raw upstream bodies, exceptions, or session values.
            statuses = sorted({str(error['status']) for error in result.get('errors', [])
                               if isinstance(error, dict) and isinstance(error.get('status'), int)})
            if statuses:
                browser_status = 'сессия кабинета: HTTP ' + ', '.join(statuses)
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            browser_status = 'не удалось проверить сессию кабинета'
    url = base.rstrip('/') + '/core/supplier/getoffersdeviationpercent'
    try:
        response = requests.post(url, headers={**headers(), 'X-Requested-With': 'XMLHttpRequest',
                                              'Accept': 'application/json'},
                                 json=ids, timeout=15, allow_redirects=False)
        if response.status_code != 200:
            error = 'Источник лучших цен Закупай: HTTP ' + str(response.status_code)
            if response.status_code in (401, 403):
                try:
                    control = requests.get(base.rstrip('/') + '/api/v1/orders', headers=headers(),
                        params={'page':1, 'pageSize':1, 'isoDate':'true'}, timeout=15, allow_redirects=False)
                    error += '; контроль API заявок: HTTP ' + str(control.status_code)
                except requests.RequestException:
                    error += '; контроль API заявок: нет ответа'
                try:
                    comparison = requests.get(base.rstrip('/') + '/api/v1/orders/' + str(int(order['id']))
                        + '/offer-compare-report', headers=headers(), timeout=15, allow_redirects=False)
                    error += '; официальный отчёт сравнения: HTTP ' + str(comparison.status_code)
                except requests.RequestException:
                    error += '; официальный отчёт сравнения: нет ответа'
            return {}, error + '; ' + browser_status
        prices = competitor_prices(response.json(), datetime.now(timezone.utc).isoformat())
        return prices, '' if prices else 'Источник Закупай не вернул числовые лучшие цены; ' + browser_status
    except (requests.RequestException, ValueError, TypeError):
        return {}, 'Не удалось проверить числовые лучшие цены Закупай; ' + browser_status


def saved_reviews():
    with database() as conn:
        rows = execute(conn, 'SELECT * FROM tile_auto_reviews ORDER BY checked_at DESC LIMIT 100').fetchall()
    return [dict(row) for row in rows]


def persist(job_id, report):
    with database() as conn:
        execute(conn, '''INSERT INTO tile_auto_reviews(order_id,job_id,status,due_at,report_json,checked_at)
            VALUES (?,?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET
            job_id=excluded.job_id,status=excluded.status,due_at=excluded.due_at,
            report_json=excluded.report_json,checked_at=excluded.checked_at''',
            (report['order_id'], job_id, report['status'], report['due_at'],
             json.dumps(report, ensure_ascii=False, allow_nan=False), report['checked_at']))


def probe_provider_once(headers, base):
    """One authenticated read diagnoses the separate numeric-price provider.

    Even an expired request can establish whether this integration token is
    accepted. It cannot authorize a sale. No response bodies/credentials logged.
    """
    rows = saved_reviews()
    if not rows: return
    from tile_offer_policy import timestamp
    clock = datetime.now(timezone.utc)
    revision = browser_prices.session_revision()
    for entry in rows:
        saved_report = json.loads(entry['report_json'])
        previous = timestamp(saved_report.get('provider_checked_at'))
        if (saved_report.get('provider_probe_version') == 7
                and saved_report.get('provider_session_revision') == revision
                and previous and 0 <= (clock - previous).total_seconds() < 86400): return
    # An order without competitors cannot verify that numeric prices are read.
    # Prefer a saved tile order with an actual competing offer; read at most ten.
    with database() as conn:
        candidates = execute(conn, '''SELECT r.order_id,r.job_id,r.report_json,j.order_json
            FROM tile_auto_reviews r JOIN automation_jobs j ON j.id=r.job_id
            ORDER BY r.checked_at DESC LIMIT 10''').fetchall()
    if not candidates: return
    def score(candidate):
        order = json.loads(candidate['order_json'])
        offers = sum(bool(item.get('bestOfferItem')) or (number(item.get('companiesWithOffersCount')) or 0) > 0
                     for _, item in relevant_items(order))
        return offers, str(order.get('finishDate') or ''), int(candidate['order_id'])
    row = max(candidates, key=score)
    report = json.loads(row['report_json'])
    prices, error = fetch_prices(json.loads(row['order_json']), base, headers)
    count = sum(1 for price in prices.values() if price.get('price') is not None)
    report['provider_checked_at'] = clock.isoformat()
    report['provider_probe_version'] = 7
    report['provider_session_revision'] = revision
    report['provider_result'] = error or ('Числовых цен получено: ' + str(count)
        + '. Единицы, НДС и доставка проверяются отдельно.')
    persist(row['job_id'], report)


def check_queue(fetch_order, headers, base, has_offer, *, limit=5):
    """Bounded reads; one entry per order, latest saved job, no supplier search."""
    if not _lock.acquire(blocking=False): return {'status': 'busy', 'checked': 0}
    checked = 0
    try:
        clock = datetime.now(timezone.utc)
        if managed.configured(): managed.sync_catalog(force=False)
        catalog = tiles.load_catalog()
        if not catalog or not managed.is_managed(catalog['data']): catalog = None
        seller = next((p for p in tiles.sellers().values() if p.get('inn') == '5638073633'), None)
        with database() as conn:
            rows = execute(conn, '''SELECT j.id,j.order_id,j.order_json FROM automation_jobs j
                LEFT JOIN tile_auto_reviews r ON r.order_id=j.order_id
                WHERE j.order_json IS NOT NULL AND j.status != 'offer_created'
                AND j.id=(SELECT MAX(k.id) FROM automation_jobs k WHERE k.order_id=j.order_id)
                ORDER BY COALESCE(r.checked_at,''),j.id DESC LIMIT 500''').fetchall()
        for row in rows:
            original = json.loads(row['order_json'])
            if original.get('source') == 'manual_entry' or not list(relevant_items(original)): continue
            with database() as conn:
                prior = execute(conn, 'SELECT checked_at,report_json FROM tile_auto_reviews WHERE order_id=?', (row['order_id'],)).fetchone()
            if prior:
                from tile_offer_policy import timestamp
                previous = timestamp(prior['checked_at'])
                if previous and (clock - previous).total_seconds() < 3600: continue
            due = due_at(original)
            # Mature/active orders are re-read before matching. No stale saved
            # list can authorize an external offer.
            source_error, prices = '', {}
            order = original
            if due and clock >= due and str(original.get('finishDate') or '')[:10] >= clock.date().isoformat():
                try:
                    order = fetch_order(int(row['order_id']), force=True)
                    if not order: raise ValueError('missing')
                    if has_offer(order):
                        source_error = 'Предложение уже есть в Закупай'
                    else:
                        prices, source_error = fetch_prices(order, base, headers)
                except Exception:
                    source_error = 'Не удалось проверить актуальную заявку Закупай'
                    order = original
            report = evaluate(order, catalog, prices, clock=clock, seller=seller)
            if prior:
                # Queue refreshes must not erase the provider's daily cooldown.
                previous_report = json.loads(prior['report_json'])
                report.update({key: value for key, value in previous_report.items()
                               if key.startswith('provider_')})
            if source_error: report['blockers'].insert(0, source_error)
            # These providers have not been configured/validated in this project.
            # Surface that fact even when all product lines happen to match.
            report['blockers'].append('Для автоотправки требуется настройка НДС СВ и проверка счёта СВ в Закупай')
            if report['status'] != 'waiting': report['status'] = 'blocked'
            persist(row['id'], report)
            checked += 1
            if checked >= limit: break
        return {'status': 'checked', 'checked': checked}
    finally:
        _lock.release()


def install_tile_autopilot(app, *, fetch_order, headers, base, has_offer):
    def check():
        return check_queue(fetch_order, headers, base, has_offer)

    @app.get(URL)
    def automatic_page():
        cards = []
        for row in saved_reviews():
            r = json.loads(row['report_json'])
            reasons = ''.join('<li>' + esc(value) + '</li>' for value in r['blockers'])
            lines = ''.join('<tr><td>' + esc(line['position']) + '</td><td>' + esc(line['requested_name'])
                            + '</td><td>' + esc((line.get('product') or {}).get('name')) + '</td><td>'
                            + esc(line.get('best_price')) + '</td><td>' + esc(line.get('target_price'))
                            + '</td><td>' + esc(line.get('floor')) + '</td></tr>' for line in r['lines'])
            from zoneinfo import ZoneInfo
            from tile_offer_policy import timestamp
            due = timestamp(row['due_at'])
            due_label = due.astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M МСК') if due else 'Не определено'
            cards.append(f"<section><h2>Заявка №{row['order_id']}</h2><p>"
                         + ('Ожидание 24 часов' if row['status'] == 'waiting' else 'Отправка остановлена')
                         + f" · Не ранее: {esc(due_label)}</p><p>{esc(r.get('provider_result') or '')}</p><ul>{reasons}</ul>"
                         + '<div class="scroll"><table><tr><th>№</th><th>Заявка</th><th>Подбор</th><th>Лучшая цена</th><th>−5%</th><th>Минимум</th></tr>'
                         + lines + f"</table></div><a href='/dashboard/automation/jobs/{row['job_id']}/tiles'>Проверить подбор и доставку</a></section>")
        return page('<h1>Автоматические предложения: проверка готовности</h1>'
                    + '<p>Обычные заявки — не раньше 24 часов после публикации; срочные — без задержки. '
                    + 'Цена на 5% ниже лучшей цены Закупай. Минимум товара: 60×60 — 920 ₽/м²; 60×120 — 1440 ₽/м². '
                    + 'При включённой доставке минимум проверяется после вычета её доли.</p>'
                    + '<p class="notice">Проверка работает в пробном режиме. Реальная автоматическая отправка ещё не подключена: '
                    + 'нужны проверенный числовой источник лучших цен, доставка по адресу и коммерческие настройки СВ.</p>'
                    + f"<form method='post' action='{URL}/check'><button>Проверить следующие 5 заявок</button></form>"
                    + ''.join(cards or ['<section>Проверок пока нет. Нажмите кнопку выше.</section>']))

    @app.post(URL + '/check')
    async def run_check(request: Request):
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            raise HTTPException(403, 'Неверный источник запроса')
        try:
            await run_in_threadpool(check)
        except Exception:
            logger.warning('Tile automatic preflight unavailable; no commercial writes')
            raise HTTPException(503, 'Проверка временно недоступна. Предложения не отправлены.') from None
        return RedirectResponse(URL, 303)

    # No continuous timer until a usable numeric best-price provider and the
    # commercial settings exist. This avoids waking Neon to repeat known blockers.
    app.state.tile_auto_check = check

    def initial_check():
        try:
            check()
            probe_provider_once(headers, base)
        except Exception:
            logger.warning('Initial tile preflight unavailable; no commercial writes')

    app.router.on_startup.append(lambda: threading.Thread(
        target=initial_check, name='tile-offer-preflight', daemon=True).start())
