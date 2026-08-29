import json
import requests
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, JSONResponse


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace('\xa0', ' ').replace('₽', '').replace('руб.', '').replace('руб', '').strip()
        s = s.replace(' ', '').replace(',', '.')
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _walk(obj, prefix=''):
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f'{prefix}.{k}' if prefix else k
            yield path, v
            yield from _walk(v, path)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            path = f'{prefix}[{i}]'
            yield path, v
            yield from _walk(v, path)


def price_candidates(item):
    out = []
    for path, value in _walk(item):
        n = _num(value)
        if n is None or n <= 0:
            continue
        leaf = path.split('.')[-1].lower()
        p = path.lower()
        score = None
        if leaf in {'bestprice', 'bestofferprice', 'minprice', 'minimumprice', 'price', 'unitprice', 'offerprice', 'priceperunit'}:
            score = 0
        elif 'best' in leaf and 'price' in leaf:
            score = 1
        elif 'min' in leaf and 'price' in leaf:
            score = 2
        elif 'price' in leaf or 'cost' in leaf:
            score = 3
        elif ('corridor' in p or 'range' in p) and ('min' in leaf or 'best' in leaf):
            score = 4
        if score is None:
            continue
        if any(x in leaf for x in ('total', 'amount', 'sum')):
            score += 5
        out.append({'path': path, 'value': n, 'score': score})
    out.sort(key=lambda x: (x['score'], len(x['path'])))
    return out


def extract_best_price(item):
    candidates = price_candidates(item)
    return candidates[0] if candidates else None


def _best_offer_name(item):
    bo = item.get('bestOfferItem')
    if isinstance(bo, str):
        return bo
    if isinstance(bo, dict):
        for key in ('goodName', 'name', 'productName', 'offerName', 'title', 'description'):
            v = bo.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return '—'


def _get_order(fetch_all_orders, order_id):
    orders = fetch_all_orders()
    order = next((x for x in orders if x.get('id') == order_id), None)
    if not order:
        orders = fetch_all_orders(force=True)
        order = next((x for x in orders if x.get('id') == order_id), None)
    if not order:
        raise HTTPException(status_code=404, detail='Заявка не найдена среди актуальных')
    return order


def install_analysis_detail(app, fetch_all_orders, zakupay_headers, zakupay_base_url, esc):
    @app.get('/dashboard/analysis/order/{order_id}', response_class=HTMLResponse)
    def analysis_order_detail(order_id: int):
        order = _get_order(fetch_all_orders, order_id)
        customer = order.get('customer') or {}
        region = order.get('region') or {}
        rows = ''
        for n, item in enumerate(order.get('orderItems') or [], 1):
            pc = extract_best_price(item)
            price = pc['value'] if pc else None
            qty = _num(item.get('count')) or 0
            total = round(price * qty, 2) if price is not None else None
            rows += (
                f"<tr><td>{n}</td><td>{esc(item.get('goodName'))}</td>"
                f"<td>{esc((item.get('category') or {}).get('name'))}</td>"
                f"<td>{esc(item.get('count'))}</td><td>{esc((item.get('unit') or {}).get('name'))}</td>"
                f"<td>{esc(item.get('companiesWithOffersCount'))}</td>"
                f"<td>{esc(_best_offer_name(item))}</td>"
                f"<td><b>{('—' if price is None else f'{price:,.2f} ₽'.replace(',', ' '))}</b></td>"
                f"<td>{('—' if total is None else f'{total:,.2f} ₽'.replace(',', ' '))}</td></tr>"
            )
        delay = order.get('delay')
        payment = 'Предоплата / без отсрочки' if delay == 0 else (f'{delay} дней' if delay is not None else '—')
        html = f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Заявка {order_id}</title><style>body{{font-family:Arial;margin:24px;background:#f5f5f5;color:#222}}.card{{background:#fff;border-radius:12px;padding:18px;margin-bottom:18px}}table{{width:100%;border-collapse:collapse;font-size:13px}}th{{background:#eee;text-align:left;padding:9px}}td{{padding:9px;border-bottom:1px solid #eee;vertical-align:top}}a{{color:#4c39d4;font-weight:bold;text-decoration:none}}.meta{{display:grid;grid-template-columns:170px 1fr;gap:7px}}</style></head><body>
<p><a href='/dashboard/analysis'>← Анализ заявок</a> · <a target='_blank' href='/analysis/raw-order/{order_id}'>Сырой JSON позиции</a> · <a target='_blank' href='/analysis/price-source/{order_id}'>Проверить API цен</a></p>
<div class='card'><h1>{esc(order.get('name'))}</h1><div class='meta'><b>ID</b><div>{order_id}</div><b>Заказчик</b><div>{esc(customer.get('shortName') or customer.get('name'))}</div><b>Регион</b><div>{esc(region.get('name'))}</div><b>Оплата</b><div>{esc(payment)}</div><b>Срок поставки</b><div>{esc(order.get('finishDate'))}</div><b>Адрес</b><div>{esc(order.get('deliveryAddress'))}</div></div></div>
<div class='card'><table><thead><tr><th>№</th><th>Наименование</th><th>Категория</th><th>Кол-во</th><th>Ед.</th><th>Конкурентов</th><th>Лучшее предложение</th><th>Лучшая цена</th><th>Сумма</th></tr></thead><tbody>{rows}</tbody></table></div>
</body></html>"""
        return HTMLResponse(html)

    @app.get('/analysis/raw-order/{order_id}')
    def raw_order_price_fields(order_id: int):
        order = _get_order(fetch_all_orders, order_id)
        items = []
        for item in order.get('orderItems') or []:
            items.append({
                'id': item.get('id'), 'goodName': item.get('goodName'),
                'bestOfferItem': item.get('bestOfferItem'),
                'companiesWithOffersCount': item.get('companiesWithOffersCount'),
                'best_price': extract_best_price(item),
                'price_candidates': price_candidates(item), 'raw': item,
            })
        return JSONResponse({'order_id': order_id, 'items': items})

    @app.get('/analysis/price-source/{order_id}')
    def probe_price_source(order_id: int):
        _get_order(fetch_all_orders, order_id)
        url = f"{zakupay_base_url}/api/v1/offers"
        probes = [
            {'orderId': order_id, 'page': 1, 'pageSize': 100},
            {'order': order_id, 'page': 1, 'pageSize': 100},
            {'order_id': order_id, 'page': 1, 'pageSize': 100},
            {'requestId': order_id, 'page': 1, 'pageSize': 100},
        ]
        results = []
        for params in probes:
            try:
                r = requests.get(url, headers=zakupay_headers(), params=params, timeout=20)
                try:
                    body = r.json()
                except ValueError:
                    body = r.text[:2000]
                results.append({'params': params, 'status': r.status_code, 'body': body})
            except requests.RequestException as exc:
                results.append({'params': params, 'error': str(exc)})
        return JSONResponse({'order_id': order_id, 'endpoint': '/api/v1/offers', 'probes': results})
