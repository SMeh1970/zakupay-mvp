"""Describe saved search evidence without changing matching or invoice eligibility."""


def search_states(item, result, views, comparison):
    diagnostics = item.get('search_diagnostics') or []
    failures = result.get('supplier_search_errors') or []
    # Legacy snapshots kept catalogue failures only in the banner.
    for message in result.get('deferred_suppliers') or []:
        if ':' in message:
            name, detail = message.split(':', 1)
            if 'ошибка' in detail.lower():
                failures = [*failures, dict(supplier=name, errors=[detail.strip()])]
            elif 'загружается' in detail.lower():
                failures = [*failures, dict(supplier=name, timed_out=True)]
    raw = [item.get('selected')] + list(item.get('candidates') or []) + list(item.get('rejected_candidates') or [])
    names = {v['supplier'] for v in views}
    names.update(d['supplier'] for d in diagnostics + failures if d.get('supplier'))
    names.update(q['supplier'] for q in raw if q and q.get('supplier'))
    states = {}
    for name in sorted(names):
        ds = [d for d in diagnostics + failures if d.get('supplier') == name]
        errors = list(dict.fromkeys([str(e) for d in ds for e in d.get('errors', [])] +
                                   [str(q['error']) for q in raw if q and q.get('supplier') == name and q.get('error')]))
        offers = [v for v in views if v['supplier'] == name]
        comparable = [v for v in comparison['offers'] if v['supplier'] == name and v['comparable']]
        incomplete = any(d.get('timed_out') or (d.get('paging') and not d['paging'].get('complete')) for d in ds)
        if errors:
            state, label = 'error', 'Ошибка интеграции' if not offers else 'Результаты получены частично: ошибка интеграции'
        elif incomplete:
            state, label = 'incomplete', 'Поиск не завершён'
        elif comparable:
            state, label = 'found', 'Есть допустимые варианты'
        elif offers:
            # The label must match what the supplier dialog lets the operator choose: products of
            # another kind, without a recalculated price or short of quantity are not choices.
            primary = [v for v in comparison['offers'] if v['supplier'] == name and choosable(v)]
            if not primary:
                state, label = 'rejected', 'Найденные товары не подходят'
            else:
                state, label = 'review', 'Найдены варианты: выберите характеристики'
        elif ds and any(d.get('queries', 0) > 0 for d in ds):
            state, label = 'empty', 'Товары не найдены' if not any(d.get('candidates', 0) for d in ds) else 'Найденные товары отклонены'
        else:
            state, label = 'unknown', 'Нет сохранённой диагностики поиска'
        states[name] = dict(state=state, label=label, errors=errors, found=len(offers),
                            diagnostics=ds,
                            rejected=[dict(key=v['key'], name=v['name'], price=v['price'], url=v.get('product_url') or v.get('url'),
                                           reasons=next((c['comparison_reasons'] for c in comparison['offers'] if c['key'] == v['key']),
                                                        ['Не совпадают или не подтверждены основные параметры']))
                                      for v in offers if not any(c['key'] == v['key'] for c in comparable)])
    return states


def choosable(offer):
    """Can be chosen with «Принять отличия и выбрать»: same kind, a price per request unit, enough quantity."""
    from supplier_comparison import IDENTITY_CHECK_FIELDS
    if any(reason.startswith('Не разрешено: ') and reason[len('Не разрешено: '):] in IDENTITY_CHECK_FIELDS
           for reason in offer.get('comparison_reasons') or []):
        return False
    return offer.get('unit_price') is not None and not (offer.get('shortage') or 0) > 0 and bool(offer.get('quantity'))
