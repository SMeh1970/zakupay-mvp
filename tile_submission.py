"""Send one immutable tile quote only after explicit, version-bound approval."""
import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import requests
from fastapi import HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

import porcelain as tiles
from invoice_generator import (AVAILABILITY_CONFIRMATION_NOTE, build_invoice_xlsx,
                               customer_validation_error, normalize_customer, validate_seller)
from offer_responses import confirmed_offer_id
from price_lists import database, execute


def fingerprint(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def invoice_body(quote):
    if not quote.get('id'):
        return build_invoice_xlsx(tiles.invoice_draft(quote['snapshot']))
    revision = fingerprint(quote['snapshot'])
    with database() as conn:
        row = execute(conn, 'SELECT snapshot_hash,body FROM tile_invoice_files WHERE quote_id=?', (quote['id'],)).fetchone()
    if not row:
        body = build_invoice_xlsx(tiles.invoice_draft(quote['snapshot']))
        with database() as conn:
            execute(conn, 'INSERT INTO tile_invoice_files(quote_id,snapshot_hash,body) VALUES (?,?,?) ON CONFLICT DO NOTHING',
                    (quote['id'], revision, body))
            row = execute(conn, 'SELECT snapshot_hash,body FROM tile_invoice_files WHERE quote_id=?', (quote['id'],)).fetchone()
    if row['snapshot_hash'] != revision:
        raise ValueError('Сохранённый файл не соответствует версии предложения')
    return bytes(row['body'])


def dispatch_state(order_id):
    with database() as conn:
        row = execute(conn, 'SELECT * FROM tile_dispatch WHERE order_id=?', (order_id,)).fetchone()
    return dict(row) if row else None


def claim(quote, invoice_hash):
    with database() as conn:
        row = execute(conn, '''INSERT INTO tile_dispatch(order_id,quote_id,state,invoice_hash,updated_at)
            VALUES (?,?,'sending',?,?) ON CONFLICT DO NOTHING RETURNING order_id''',
            (quote['snapshot']['order_id'], quote['id'], invoice_hash, tiles.now())).fetchone()
    return bool(row)


def finish(order_id, state, *, file_id='', offer_id='', error=''):
    with database() as conn:
        execute(conn, '''UPDATE tile_dispatch SET state=?,file_id=?,offer_id=?,error=?,updated_at=?
            WHERE order_id=? AND state='sending' ''',
            (state, str(file_id), str(offer_id), error, tiles.now(), order_id))


def validate_quote(quote, fresh_order, catalog, *, clock=None):
    """Recheck live source, exact chosen goods and stale approvals before any upload."""
    from automation_pipeline import commercial_order_hash
    from order_actuality import classify
    from tile_offer_policy import line_match, timestamp
    from tile_pricing import discounted_price
    clock = clock or datetime.now(timezone.utc)
    s = quote['snapshot']
    errors = list(s.get('issues') or [])
    if not fresh_order or str(fresh_order.get('id')) != str(s['order_id']):
        return errors + ['Не удалось проверить исходную заявку Закупай']
    if fresh_order.get('source') == 'manual_entry' or fresh_order.get('isLimitedView'):
        errors.append('Заявка не доступна для отправки в Закупай')
    if classify(fresh_order) != 'active':
        errors.append('Актуальность заявки не подтверждена')
    end = str(fresh_order.get('finishDate') or '')[:10]
    if end and end < clock.astimezone(ZoneInfo('Europe/Moscow')).date().isoformat():
        errors.append('Истёк срок заявки')
    if commercial_order_hash(fresh_order) != commercial_order_hash(s['order_snapshot']):
        errors.append('Заявка изменилась: повторите подготовку предложения')
    customer = normalize_customer(s['customer'])
    fresh_customer = normalize_customer(fresh_order.get('customer'))
    if customer['inn'] != fresh_customer['inn'] or customer['kpp'] != fresh_customer['kpp']:
        errors.append('Реквизиты покупателя отличаются от свежей заявки')
    error = customer_validation_error(s['customer'])
    if error: errors.append(error)
    try:
        validate_seller(s['seller'])
    except ValueError as exc:
        errors.append(str(exc))
    approved_at = timestamp(s.get('created_at'))
    if not approved_at or not timedelta(0) <= clock - approved_at <= timedelta(hours=24):
        errors.append('Проверки наличия и доставки старше суток: пересохраните расчёт')
    if not catalog:
        return errors + ['Не удалось проверить текущий каталог']
    products = {p['id']: p for p in catalog['data']['products']}
    used = {}
    ids = set()
    for line in s['lines']:
        position = line['position']
        item = (s['order_snapshot'].get('orderItems') or [])[position-1]
        from saved_dashboard import normalize_unit
        unit=item.get('unit') or {}
        if normalize_unit(unit.get('name') if isinstance(unit,dict) else unit) != 'м²':
            errors.append(f'Позиция {position}: единица заявки не м²; пересчёт не подтверждён')
        iid = str(item.get('id') or '')
        if not iid or iid in ids:
            errors.append('Нет уникальных ID выбранных позиций')
        ids.add(iid)
        p = products.get(line['product']['id'])
        if not p or p != line['product']:
            errors.append(f'Позиция {position}: каталог изменился; пересчитайте предложение')
            continue
        if tiles.tile_kind(line['requested_name']) != 'tile':
            errors.append(f'Позиция {position}: фасонное изделие не заменяется плиткой')
        check = tiles.assess(str(item.get('goodName') or item.get('name') or '') + ' ' + str(item.get('comment') or ''), p)
        errors.extend(f'Позиция {position}: {reason}' for reason in check['conflicts'])
        # Strict automated checks may be resolved only by an explicit technical
        # review with a recorded document, never by a generic stock checkbox.
        problems = line_match(item, p)
        values = s['inputs']
        if problems and not (values.get(f'technical_{position}') == 'yes'
                             and str(values.get(f'technical_source_{position}') or '').strip()):
            errors.extend(f'Позиция {position}: {reason}' for reason in problems)
        if item.get('analogAllow') is False and any('Точная модель не найдена' in reason for reason in problems):
            errors.append(f'Позиция {position}: заказчик запретил замену модели')
        used[p['id']] = used.get(p['id'], 0) + line['quantity']
        if p.get('stock_m2') is None or used[p['id']] > p['stock_m2'] + 1e-6:
            errors.append(f'Позиция {position}: недостаточный остаток')
        if values.get('discount_pct') not in (None, ''):
            target, _ = discounted_price(line.get('price_evidence'), values['discount_pct'],
                                          values.get('vat'), clock=clock)
            if target != line['proposed_unit_price'] or target is None:
                errors.append(f'Позиция {position}: конкурентная цена устарела; пересчитайте предложение')
    return list(dict.fromkeys(errors))


def payload_for_quote(quote, file_id):
    """Official create-without-processing shape; delivery remains a separate row."""
    s = quote['snapshot']; seller = validate_seller(s['seller']); buyer = normalize_customer(s['customer'])
    rows = []
    for line in s['lines']:
        iid = s['order_snapshot']['orderItems'][line['position']-1]['id']
        rows.append(dict(orderItem={'id': int(iid)}, providerGoodName=line['product']['name'] + ' ' + str(line['product'].get('format') or ''),
                         count=line['quantity'], amount=line['amount'], vatRate={'rate': s['vat_rate']},
                         comment=AVAILABILITY_CONFIRMATION_NOTE, externalId=f"tile-{quote['id']}-{iid}"))
    rows.append(dict(providerGoodName='Доставка по адресу: ' + s['address'], count=1,
                     amount=s['delivery_price'], unit={'name': 'услуга'}, vatRate={'rate': s['vat_rate']}))
    if sum(Decimal(str(r['amount'])) for r in rows) != Decimal(str(s['total'])):
        raise ValueError('Сумма позиций не совпадает со счётом')
    return dict(supplierInn=seller['inn'], supplierKpp=seller.get('kpp') or '', payerInn=buyer['inn'],
                payerKpp=buyer['kpp'], producerOfferDate=tiles.invoice_draft(s)['invoice_date'], producerOfferNumber=s['invoice_number'],
                files=[{'id': int(file_id)}], currency={'id': 643}, deliveryIncluded=False, prepaidPercent=100,
                destinationAccount={'currentAccount': seller['account']}, hasVat=bool(s['vat_rate']), vat=s['vat_rate'],
                additionalData=json.dumps({'tile_quote_id': quote['id'], 'revision': fingerprint(s)}), offerItems=rows)


def install_tile_submission(app):
    from porcelain_panel import URL, page, esc, required_quote, field, select

    def gateway():
        value = getattr(app.state, 'tile_gateway', None)
        if not value: raise HTTPException(503, 'Отправка не подключена; комплект можно скачать')
        return value

    def checked(quote):
        from porcelain_panel import required_proposal_catalog
        import keramahall_catalog as managed
        g = gateway()
        fresh = g['fetch_order'](int(quote['snapshot']['order_id']), force=True)
        if managed.configured(): managed.sync_catalog(force=False)
        catalog = required_proposal_catalog(quote['catalog_id'])
        errors = validate_quote(quote, fresh, catalog)
        if g['has_offer'](fresh): errors.append('Предложение по заявке уже есть в Закупай')
        with database() as conn:
            attempt = execute(conn, "SELECT id FROM offer_submissions WHERE order_id=? AND status IN ('sending','unknown','confirmed') LIMIT 1", (quote['snapshot']['order_id'],)).fetchone()
        if attempt: errors.append('По этой заявке уже есть отправка или неподтверждённая попытка')
        if errors: raise HTTPException(409, '; '.join(errors))
        return g

    @app.get(URL+'/quotes/{quote_id}/invoice.xlsx')
    def invoice(quote_id: int):
        q = required_quote(quote_id)
        try: body = invoice_body(q)
        except ValueError as exc: raise HTTPException(409, str(exc))
        return Response(body, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        headers={'Content-Disposition': f'attachment; filename="tile-quote-{quote_id}.xlsx"', 'Cache-Control': 'no-store'})

    @app.get(URL+'/quotes/{quote_id}/send')
    def confirmation(quote_id: int):
        q = required_quote(quote_id); s = q['snapshot']
        state = dispatch_state(s['order_id'])
        if state:
            return page(f"<h1>Отправка: {esc(state['state'])}</h1><p>Версия {esc(state['quote_id'])}; предложение Закупай {esc(state['offer_id'])}.</p><p>Повторная отправка остановлена. При неизвестном результате сначала проверьте кабинет Закупай.</p>")
        errors = list(s.get('issues') or [])
        accounts = []
        try:
            g = checked(q)
            accounts, error = g['accounts']()
            if error: errors.append('Не удалось проверить банковские счета Закупай')
        except (HTTPException, ValueError, requests.RequestException) as exc:
            errors.append(str(exc.detail) if isinstance(exc, HTTPException) else 'Не удалось проверить актуальную заявку или каталог')
        try:
            body = invoice_body(q)
        except ValueError as exc:
            errors.append(str(exc))
        options = [('', 'Выберите банковский счёт')]+[(a['id'],a['label']) for a in accounts
            if str(a.get('accountNumber') or '') == str((s.get('seller') or {}).get('account') or '')
            and str(a.get('company_inn') or '') == str((s.get('seller') or {}).get('inn') or '')]
        if len(options)==1: errors.append('Нет подтверждённого банковского счёта выбранной фирмы в Закупай')
        seller = s.get('seller') or {}; buyer=normalize_customer(s.get('customer'))
        rows=''.join(f"<tr><td>{esc(l['product']['name'])}</td><td>{esc(l['quantity'])} м²</td><td>{esc(l['amount'])} ₽</td></tr>" for l in s['lines'])
        form = (f"<form method='post' action='{URL}/quotes/{quote_id}/send'><input type='hidden' name='revision' value='{fingerprint(s)}'>"
                + select('destination_account_id','Банковский счёт в Закупай',options)
                + "<label><input type='checkbox' name='confirm_send' value='SEND' required> Я проверил эту версию счёта, характеристики, наличие, реквизиты, доставку и подтверждаю внешнюю отправку в Закупай.</label><button>Отправить этот счёт в Закупай</button></form>") if not errors else ''
        return page(f"<h1>Подтверждение отправки версии №{quote_id}</h1><p>Получатель: {esc(buyer['name'])}, ИНН {esc(buyer['inn'])}. Заявка №{esc(s['order_id'])}.</p>"
                    + f"<p>Продавец: {esc(seller.get('name'))}, ИНН {esc(seller.get('inn'))}; р/с {esc(seller.get('account'))}.</p>"
                    + f"<p>Счёт №{esc(s['invoice_number'])}. Итого {esc(s['total'])} ₽; доставка {esc(s['delivery_price'])} ₽ отдельной строкой. НДС {esc((s['vat_rate'] or 0)*100)}%.</p><table>{rows}</table>"
                    + f"<p>{esc(AVAILABILITY_CONFIRMATION_NOTE)}</p><a href='{URL}/quotes/{quote_id}/invoice.xlsx'>Скачать именно этот счёт</a>"
                    + '<ul class="error">'+''.join('<li>'+esc(e)+'</li>' for e in errors)+'</ul>'+form)

    @app.post(URL+'/quotes/{quote_id}/send')
    async def send(quote_id: int, request: Request):
        from offer_panel_safe import _bounded_call
        origin=request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            raise HTTPException(403, 'Неверный источник запроса')
        form=dict(await request.form()); q=await run_in_threadpool(required_quote,quote_id); s=q['snapshot']
        if form.get('confirm_send') != 'SEND' or form.get('revision') != fingerprint(s):
            raise HTTPException(400, 'Подтвердите отправку конкретной версии счёта')
        if await run_in_threadpool(dispatch_state,s['order_id']):
            raise HTTPException(409, 'Попытка отправки уже зафиксирована; повтор запрещён')
        try:
            g=await _bounded_call(checked,q,deadline=50)
            accounts,error=await _bounded_call(g['accounts'],deadline=15)
            matched=next((a for a in accounts if str(a['id'])==str(form.get('destination_account_id'))
                          and str(a.get('accountNumber') or '')==s['seller']['account']
                          and str(a.get('company_inn') or '')==s['seller']['inn']),None)
            if error or not matched: raise ValueError('Принадлежность банковского счёта выбранной фирме не подтверждена')
            seller,account,vat=await _bounded_call(g['resolve'],
                {'seller_id':'saved','destination_account_id':form.get('destination_account_id'),'vat_rate':s['vat_rate']},
                {'seller':s['seller']},deadline=15)
        except ValueError as exc: raise HTTPException(409,str(exc))
        except (asyncio.TimeoutError,requests.RequestException):
            raise HTTPException(503,'Проверка не завершена; счёт не загружен и предложение не отправлено') from None
        if seller != validate_seller(s['seller']) or vat != s['vat_rate']:
            raise HTTPException(409,'Реквизиты или НДС изменились')
        body=await run_in_threadpool(invoice_body,q)
        # Claim BEFORE uploading: concurrent clicks and uncertain failures cannot
        # create another file/offer for this order, even from another quote.
        if not await run_in_threadpool(claim,q,hashlib.sha256(body).hexdigest()):
            raise HTTPException(409,'Отправка уже выполняется')
        file_id=''
        try:
            file_id,_=await _bounded_call(g['upload'],f'tile-quote-{quote_id}.xlsx',body,deadline=35)
            payload=payload_for_quote(q,file_id)
            response=await _bounded_call(requests.post,g['base'].rstrip('/')+'/api/v1/offers/create-without-processing',
                headers=g['headers'](),json=payload,timeout=(5,20),allow_redirects=False,deadline=35)
            data=response.json()
            offer_id=confirmed_offer_id(data,require_explicit_success=True)
            state='confirmed' if response.ok and offer_id else 'unknown'
            # Explicit rejection is recorded, but never retried automatically.
            if 400 <= response.status_code < 500 and response.status_code not in (408,429): state='rejected'
            await run_in_threadpool(finish,s['order_id'],state,file_id=file_id,offer_id=offer_id or '',error='' if state=='confirmed' else 'Создание счёта не подтверждено')
        except Exception:
            await run_in_threadpool(finish,s['order_id'],'unknown',file_id=file_id,error='Результат неизвестен; проверьте Закупай перед повтором')
        return await run_in_threadpool(confirmation,quote_id)
