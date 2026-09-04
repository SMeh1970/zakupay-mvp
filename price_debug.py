import json
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
        # Strongly prefer explicit price/cost fields; allow corridor/min/max/best price variants.
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


def install_price_debug(app, fetch_all_orders, esc):
    @app.get('/analysis/raw-order/{order_id}')
    def raw_order_price_fields(order_id: int):
        orders = fetch_all_orders()
        order = next((x for x in orders if x.get('id') == order_id), None)
        if not order:
            orders = fetch_all_orders(force=True)
            order = next((x for x in orders if x.get('id') == order_id), None)
        if not order:
            raise HTTPException(status_code=404, detail='Заявка не найдена среди актуальных')
        items = []
        for item in order.get('orderItems') or []:
            items.append({
                'id': item.get('id'),
                'goodName': item.get('goodName'),
                'bestOfferItem': item.get('bestOfferItem'),
                'companiesWithOffersCount': item.get('companiesWithOffersCount'),
                'best_price': extract_best_price(item),
                'price_candidates': price_candidates(item),
                'raw': item,
            })
        return JSONResponse({'order_id': order_id, 'items': items})

    @app.get('/analysis/order/{order_id}', response_class=HTMLResponse)
    def enriched_order(order_id: int):
        orders = fetch_all_orders()
        order = next((x for x in orders if x.get('id') == order_id), None)
        if not order:
            orders = fetch_all_orders(force=True)
            order = next((x for x in orders if x.get('id') == order_id), None)
        if not order:
            raise HTTPException(status_code=404, detail='Заявка не найдена среди актуальных')

        rows = ''
        for n, item in enumerate(order.get('orderItems') or [], 1):
            pc = extract_best_price(item)
            price = pc['value'] if pc else None
            qty = _num(item.get('count')) or 0
            total = round(price * qty, 2) if price is not None else None
            source = pc['path'] if pc else ''
            rows += (
                f"<tr><td>{n}</td><td>{esc(item.get('goodName'))}</td>"
                f"<td>{esc((item.get('category') or {}).get('name'))}</td>"
                f"<td>{esc(item.get('count'))}</td><td>{esc((item.get('unit') or {}).get('name'))}</td>"
                f"<td>{esc(item.get('companiesWithOffersCount'))}</td>"
                f"<td>{esc(_best_offer_name(item))}</td>"
                f"<td><b>{('—' if price is None else f'{price:,.2f} ₽'.replace(',', ' '))}</b>"
                f"<div style='font-size:11px;color:#777'>{esc(source)}</div></td>"
                f"<td>{('—' if total is None else f'{total:,.2f} ₽'.replace(',', ' '))}</td></tr>"
            )
        raw_link = f'/analysis/raw-order/{order_id}'
        html = f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><title>Заявка {order_id}</title>
<style>body{{font-family:Arial;margin:24px;background:#f5f5f5;color:#222}}.card{{background:#fff;border-radius:12px;padding:18px;margin-bottom:18px}}table{{width:100%;border-collapse:collapse;font-size:13px}}th{{background:#eee;text-align:left;padding:9px}}td{{padding:9px;border-bottom:1px solid #eee;vertical-align:top}}a{{color:#4c39d4;font-weight:bold;text-decoration:none}}</style></head><body>
<p><a href='/dashboard/analysis'>← Анализ заявок</a> · <a target='_blank' href='{raw_link}'>Сырой JSON цен</a></p>
<div class='card'><h1>{esc(order.get('name'))}</h1><div>ID: {order_id}</div></div>
<div class='card'><table><thead><tr><th>№</th><th>Наименование</th><th>Категория</th><th>Кол-во</th><th>Ед.</th><th>Конкурентов</th><th>Лучшее предложение</th><th>Лучшая цена</th><th>Сумма</th></tr></thead><tbody>{rows}</tbody></table></div>
</body></html>"""
        return HTMLResponse(html)
