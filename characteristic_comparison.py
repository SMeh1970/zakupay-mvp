"""Position-level value filters and comparable prices, independent of invoice approval."""
from datetime import date, datetime, timezone


def build(item, views, enabled=lambda supplier: True, today=None):
    from supplier_comparison import feature_id, settings, lexical_tokens, norm
    today = today or datetime.now(timezone.utc).date()
    from product_characteristics import extract
    source = item.get('comparison_source') or item
    category = extract(source.get('requested_name') or '')['category']
    all_views = views
    if category != 'Не определена':
        views = [v for v in views if extract(v['name'])['category'] == category]
    covered = {norm(value) for view in views for feature in view['features'] for value in feature['required']}
    words = sorted({w for w in lexical_tokens(source.get('requested_name')) if len(w)>1 and w not in covered})
    enriched = []
    for view in views:
        features = list(view['features'])
        offered = set(lexical_tokens(view['name']))
        for word in words:
            field = 'Наименование: '+word
            present = word in offered
            value = word if present else 'Не указано'
            identifier = feature_id(field, value) + ('' if present else ':'+feature_id('Предложение', view['key']))
            features.append(dict(id=identifier,field=field,value=value,required=[word],
                differs=not present,ambiguous=not present,offer_specific=not present))
        enriched.append({**view,'features':features})
    views = enriched
    decisions = {**settings(item).get('features', {}), **settings(item).get('values', {})}
    groups = {}
    for view in views:
        for feature in view['features']:
            group = groups.setdefault(feature['field'], {})
            group.setdefault(feature['id'], {**feature, 'offer_label': (view['supplier'] + (' · '+view['sku'] if view['sku'] else '')) if feature.get('offer_specific') else '', 'state': decisions.get(feature['id'],
                'matched' if not feature['differs'] else 'pending')})
    # Absence is a value too. Extra, unspecified parameters do not impose a new
    # requirement on products which do not declare them.
    for field, values in groups.items():
        if any(not any(f['field'] == field for f in v['features']) for v in views):
            identifier = feature_id(field, 'Не указано')
            required = field not in {'Слово в наименовании', 'Нет слова из заявки', 'Проверка прайса', 'Проверка категории', 'Проверка соответствия'} and any(f['required'] for f in values.values())
            values.setdefault(identifier, dict(id=identifier, field=field, value='Не указано',
                required=[], state=decisions.get(identifier, 'pending' if required else 'matched'),
                ambiguous=True, differs=required))
    output = []
    for view in views:
        reasons = []
        for field, values in groups.items():
            found = [f for f in view['features'] if f['field'] == field]
            ids = [f['id'] for f in found] or [feature_id(field, 'Не указано')]
            allowed = lambda identifier: values[identifier]['state'] in {'matched', 'accepted'}
            # Unknown-word checks are separate constraints, never alternatives
            # within a single catch-all word parameter.
            passed = all(allowed(i) for i in ids) if field in {'Слово в наименовании', 'Нет слова из заявки', 'Проверка прайса'} else any(allowed(i) for i in ids)
            if not passed:
                reasons.append('Не разрешено: ' + field)
        if not view['enabled'] or not enabled(view['supplier']) or item.get('decision') == 'excluded':
            reasons.append('Предложение, поставщик или позиция исключены')
        if not view['factor']:
            reasons.append('Нет коэффициента пересчёта единиц')
        if not view['pricing_ready'] or view['currency'] != 'RUB':
            reasons.append('Нет сопоставимой цены в рублях с известными условиями НДС')
        if view['unit_price'] is None or view['unit_price'] <= 0:
            reasons.append('Нет положительной цены')
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
    return dict(groups=[dict(field=field, values=list(values.values()), request_evidence=[r['evidence'] for r in request_records if r['field']==field and r['evidence']]) for field, values in sorted(groups.items())],
                unrelated_count=len(all_views)-len(views),
                offers=output, minimum=best, suitable_count=len(suitable))
