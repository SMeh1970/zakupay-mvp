"""Position-level value filters and comparable prices, independent of invoice approval."""
from datetime import date, datetime, timezone


def build(item, views, enabled=lambda supplier: True, today=None, respect_row_exclusion=True):
    from supplier_comparison import feature_id, settings, lexical_tokens, norm
    today = today or datetime.now(timezone.utc).date()
    from product_identity import kind
    source = item.get('comparison_source') or item
    requested_kind = kind(source.get('requested_name'))
    all_views = views
    if requested_kind:
        views = [v for v in views if kind(v['name']) == requested_kind]
    else:
        # Unknown kind (e.g. «компенсатор»): a product must at least carry one of the leading
        # product words of the request, otherwise it is another product (a garland, a plug).
        stems = request_stems(source.get('requested_name'))
        if stems:
            views = [v for v in views if any(stem in str(v['name'] or '').lower().replace('ё', 'е') for stem in stems)]
    from catalog_comparison import records
    from supplier_characteristics import primary_match
    request_records = records(source.get('requested_name'))
    views = [v for v in views if primary_match(request_records, v['features'], requested_kind)]
    decisions = {**settings(item).get('features', {}), **settings(item).get('values', {})}
    # Features of every offer indexed by field once: the loops below ask "which features of this
    # offer belong to this field" for every field and every offer, which was the slow part.
    by_field = []
    for view in views:
        indexed = {}
        for feature in view['features']:
            indexed.setdefault(feature['field'], []).append(feature)
        by_field.append(indexed)
    groups = {}
    seen_sources = {}
    for view in views:
        for feature in view['features']:
            group = groups.setdefault(feature['field'], {})
            entry = group.setdefault(feature['id'], {**feature, 'sources': [], 'offer_label': (view['supplier'] + (' · '+view['sku'] if view['sku'] else '')) if feature.get('offer_specific') else '', 'state': decisions.get(feature['id'],
                'matched' if not feature['differs'] else 'pending')})
            evidence = dict(key=view['key'], supplier=view['supplier'], sku=view['sku'], name=view['name'],
                            url=view.get('url'), price=view.get('price'), origin=feature.get('origin'),
                            evidence=feature.get('evidence'), date=view.get('source_date'))
            # A set of keys per value instead of scanning the whole list: with thousands of offers the scan was quadratic.
            seen = seen_sources.setdefault((feature['field'], feature['id']), set())
            if evidence['key'] not in seen:
                seen.add(evidence['key'])
                entry['sources'].append(evidence)
    # Absence is a value too. Extra, unspecified parameters do not impose a new
    # requirement on products which do not declare them.
    for field, values in groups.items():
        missing = [v for v, indexed in zip(views, by_field) if field not in indexed]
        if missing:
            identifier = feature_id(field, 'Не указано')
            if identifier not in values:
                required = field not in {'Слово в наименовании', 'Нет слова из заявки', 'Проверка прайса', 'Проверка категории', 'Проверка соответствия'} and any(f['required'] for f in values.values())
                values[identifier] = dict(id=identifier, field=field, value='Не указано',
                    required=[], state=decisions.get(identifier, 'pending' if required else 'matched'),
                    ambiguous=True, differs=required, sources=[dict(key=v['key'],supplier=v['supplier'],sku=v['sku'],
                        name=v['name'],url=v.get('url'),price=v.get('price'),origin='missing',evidence='Не указано в предложении')
                        for v in missing])
    # Evaluate requested parameters in their request order, narrowing the same
    # candidate set at each step. Optional fields follow; their defaults pass.
    from catalog_comparison import records
    request_records = records(source.get('requested_name'))
    order = list(dict.fromkeys(r['field'] for r in request_records))
    groups = {field: groups[field] for field in [*order, *sorted(set(groups) - set(order))] if field in groups}
    remaining = list(range(len(views)))
    stages = [dict(field='Тип товара', remaining=len(remaining))]
    missing_ids = {field: feature_id(field, 'Не указано') for field in groups}
    for field, values in groups.items():
        accepted = {i for i, entry in values.items() if entry['state'] in {'matched', 'accepted'}}
        def passes(position):
            ids = [f['id'] for f in by_field[position].get(field, ())] or [missing_ids[field]]
            return any(i in accepted for i in ids)
        remaining = [position for position in remaining if passes(position)]
        stages.append(dict(field=field, remaining=len(remaining)))
    output = []
    for position, view in enumerate(views):
        reasons = []
        for field, values in groups.items():
            found = by_field[position].get(field, ())
            ids = [f['id'] for f in found] or [missing_ids[field]]
            allowed = lambda identifier: values[identifier]['state'] in {'matched', 'accepted'}
            # Unknown-word checks are separate constraints, never alternatives
            # within a single catch-all word parameter.
            passed = all(allowed(i) for i in ids) if field in {'Слово в наименовании', 'Нет слова из заявки', 'Проверка прайса'} else any(allowed(i) for i in ids)
            if not passed:
                reasons.append('Не разрешено: ' + field)
        if not view['enabled'] or not enabled(view['supplier']) or (respect_row_exclusion and item.get('decision') == 'excluded' and not settings(item).get('value_workflow')):
            reasons.append('Предложение, поставщик или позиция исключены')
        if not view['factor']:
            reasons.append('Нет коэффициента пересчёта единиц')
        if not view['pricing_ready'] or view['currency'] != 'RUB':
            reasons.append('Нет сопоставимой цены в рублях с известными условиями НДС')
        if view['price'] is None or view['price'] <= 0:
            reasons.append('Нет положительной цены')
        elif view['unit_price'] is None:
            reasons.append('Цена есть, но не пересчитана: единица поставщика ' + str(view['unit']))
        try:
            age = (today - date.fromisoformat(str(view['source_date'])[:10])).days
            if not 0 <= age <= 30:
                reasons.append('Цена старше 30 дней или дата в будущем')
        except (ValueError, TypeError):
            reasons.append('Дата цены не указана')
        if 'Предложение отсутствует в актуальном подборе прайсов' in view['reasons']:
            reasons.append('Предложение отсутствует в актуальном подборе')
        output.append({**view, 'comparable': not reasons, 'comparison_reasons': reasons})
    suitable = [v for v in output if v['comparable']]
    best = min(suitable, key=lambda v: (v['unit_price'], v['key']), default=None)
    from catalog_comparison import records
    request_records = records(source.get('requested_name'))
    return dict(groups=[dict(field=field, values=list(values.values()), request_spans=[dict(text=r['evidence'],value=r['value']) for r in request_records if r['field']==field and r['evidence']]) for field, values in sorted(groups.items())],
                filter_stages=stages,
                unrelated_count=len(all_views)-len(views),
                offers=output, minimum=best, suitable_count=len(suitable))


def request_stems(requested):
    """Stems of the leading one or two Cyrillic words of a request («Компенсатор сильфонный» ->
    компенсат, сильфон); endings are dropped so that singular/plural and cases still match."""
    import re
    words = []
    for token in str(requested or '').split():
        word = token.strip('.,;:()"«»').lower().replace('ё', 'е')
        if re.fullmatch(r'[а-я-]{3,}', word) and word not in {'для', 'под', 'без', 'или', 'требуется', 'товар', 'изделие', 'комплект', 'набор'}:
            words.append(word)
            if len(words) == 2:
                break
        elif words or re.search(r'\d', word):
            break
    return [w[:-2] if len(w) >= 7 else w[:-1] if len(w) >= 5 else w for w in words]
