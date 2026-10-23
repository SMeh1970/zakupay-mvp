"""Adapter for the user's compare_clean.html; comparison never submits an invoice."""
import hashlib
import json


def matrix_model(result):
    from supplier_comparison import candidates, number, offer_view, quote_key, safe_url, settings, supplier_enabled
    items, offers, variants, meta = [], {}, {}, {}
    enabled = {}
    for item in result.get('items') or []:
        source = item.get('comparison_source') or item
        pos = item['position']
        items.append(dict(id=pos, name=source.get('requested_name') or '', qty=number(source.get('quantity')), unit=source.get('unit') or ''))
        rejected = {quote_key(q) for q in item.get('rejected_candidates') or [] if q.get('name')}
        selected_key = quote_key(item['selected']) if item.get('selected') else None
        grouped = {}
        for candidate in candidates(item):
            key = quote_key(candidate)
            # A search rejection is not an offer. Never revive it as an alternative.
            if key in rejected and not (key == selected_key and item.get('decision') in {'approved','auto_ready'}):
                continue
            view = offer_view(item, candidate)
            name = view['supplier']
            explicit = number(candidate.get('offered_quantity', candidate.get('qty')))
            amount = explicit if explicit is not None else view['quantity']
            state = settings(item)
            on = state.get('cells', {}).get(key, item.get('decision') != 'excluded')
            if any(f['state'] == 'rejected' for f in view['features']) or view['unit_state']=='rejected' or view['quantity_state']=='rejected':
                on = False
            quote = dict(key=key, name=candidate['name'], qty=amount, unit=candidate.get('unit') or '',
                price=number(candidate.get('price')), enabled=bool(on), anomaly=bool(candidate.get('anomaly')),
                url=safe_url(candidate.get('url') or candidate.get('source_url')),
                quantity_note='' if explicit is not None else 'Количество для расчёта по потребности и доступному остатку; не подтверждение наличия.',
                source_date=candidate.get('price_date'))
            if candidate.get('catalog_prepared') and not view.get('pricing_ready'):
                quote['price'] = None
            grouped.setdefault(name, []).append((key == selected_key, number(candidate.get('score')) or 0, quote))
            meta.setdefault(name, dict(delivery=dict(included=False,amount=None,note='Не подтверждена'), date=candidate.get('price_date'), note='Наличие требует подтверждения'))
            enabled[name] = supplier_enabled(result, name)
        for name, values in grouped.items():
            values.sort(key=lambda v: (-int(v[0]), -v[1]))
            variants.setdefault(name, {})[pos] = [v[2] for v in values]
            offers.setdefault(name, {})[pos] = values[0][2]
    names = sorted(offers)
    offers = {name:offers[name] for name in names}
    signature = hashlib.sha256(json.dumps([items, variants],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    stored = result.get('prototype_comparison') or {}
    state = stored.get('state') if stored.get('signature') == signature else None
    if state is None:
        state = dict(supplierOn=[enabled[n] for n in names], cellOn={}, diffState={}, choices={}, mode='sum')
    return dict(items=items, offers=offers, variants=variants, supplierMeta=meta, supplierNames=names, signature=signature, state=state)


def save_state(result, payload):
    matrix = matrix_model(result)
    if payload.get('signature') != matrix['signature']:
        raise ValueError('Предложения изменились. Обновите страницу.')
    state = payload.get('state')
    if not isinstance(state, dict) or set(state) != {'supplierOn','cellOn','diffState','choices','mode'}:
        raise ValueError('Некорректные решения сравнения')
    if state['mode'] not in {'sum','price'} or not isinstance(state['supplierOn'],list) or len(state['supplierOn']) != len(matrix['supplierNames']) or any(type(v) is not bool for v in state['supplierOn']):
        raise ValueError('Некорректные переключатели')
    allowed = {f"{item['id']}_{si}" for item in matrix['items'] for si in range(len(matrix['supplierNames']))}
    for name in ['cellOn','diffState','choices']:
        if not isinstance(state[name],dict):
            raise ValueError('Некорректное решение')
    for key,value in state['cellOn'].items():
        if key not in allowed or type(value) is not bool:
            raise ValueError('Некорректная ячейка')
    for key,value in state['choices'].items():
        if key not in allowed:
            raise ValueError('Некорректное предложение')
        pos,si=key.rsplit('_',1)
        quotes=matrix['variants'].get(matrix['supplierNames'][int(si)],{}).get(int(pos),[])
        if value not in {q['key'] for q in quotes}:
            raise ValueError('Предложение изменилось')
    # Word indices are bounded by the actual supplier wording, not free-form keys.
    import re
    for key,value in state['diffState'].items():
        match=re.fullmatch(r'(\d+)_(\d+)_(unit|qty|nameword_(\d+))',key)
        if not match or f'{match[1]}_{match[2]}' not in allowed or value not in {'accepted','rejected'}:
            raise ValueError('Некорректное отличие')
        pos,si=int(match[1]),int(match[2])
        quotes=matrix['variants'].get(matrix['supplierNames'][si],{}).get(pos,[])
        chosen=state['choices'].get(f'{pos}_{si}')
        quote=next((q for q in quotes if q['key']==chosen),quotes[0] if quotes else None)
        if quote is None or match[4] and int(match[4])>=len(quote['name'].split()):
            raise ValueError('Отличие не найдено')
    result['prototype_comparison']=dict(signature=matrix['signature'], state=state)
