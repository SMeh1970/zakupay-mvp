"""Product values from supplier evidence, distinct from admission decisions."""
import re


def canonical(field, value, unit=None):
    field = str(field).strip()
    value = str(value).strip()
    aliases = {'Диаметр резьбы': 'Метрическая резьба', 'DIN': 'DIN',
               'Класс прочности': 'Класс прочности', 'Тип покрытия': 'Покрытие',
               'Номинальный диаметр': 'Условный проход'}
    field = aliases.get(field, field)
    if field == 'Метрическая резьба':
        value = re.sub(r'^[mм]\s*', 'М', value, flags=re.I)
    if field == 'Класс прочности':
        value = value.replace(',', '.')
    if field == 'Покрытие':
        if value.lower() == 'нет' or re.search(r'неоцинк|без\s+покрытия', value, re.I):
            value = 'Без покрытия'
        elif re.search(r'гор\.?\s*цинк|горяч\w*\s*цинк|\btZn\b', value, re.I):
            value = 'Горячее цинкование'
        elif re.search(r'гал[ьъ]ван|электролит', value, re.I):
            value = 'Гальваническое цинкование'
        elif re.search(r'цинк|\bZn\b|оцинк', value, re.I):
            value = 'Цинк (способ не указан)'
    if field == 'Условный проход':
        value = re.sub(r'^(?:DN|Ду)\s*', '', value, flags=re.I)
        unit = None
    return field, value, unit


def name_records(name, product):
    if product not in {'гайка', 'шайба', 'шпилька', 'гвозди'}:
        return []
    text = str(name or '')
    out = []
    def add(field, value, match, unit=None):
        field, value, unit = canonical(field, value, unit)
        out.append(dict(field=field, value=value, raw_value=value, unit=unit,
                        evidence=match[0], origin='name', ambiguous=False))
    if product == 'гвозди':
        dims = re.search(r'(?<![\d.,])(\d+(?:[.,]\d+)?)\s*[xх×*]\s*(\d+(?:[.,]\d+)?)(?:\s*(мм|см))?', text, re.I)
        if dims:
            factor = 10 if (dims[3] or '').lower() == 'см' else 1
            add('Диаметр гвоздя', f'{float(dims[1].replace(",", "."))*factor:g} мм', dims)
            add('Длина гвоздя', f'{float(dims[2].replace(",", "."))*factor:g} мм', dims)
        else:
            length = re.search(r'(?:длина\s*|\bL\s*=\s*|\bгвозди\s+)(\d+(?:[.,]\d+)?)\s*(мм|см)\b', text, re.I)
            if length:
                factor = 10 if length[2].lower() == 'см' else 1
                add('Длина гвоздя', f'{float(length[1].replace(",", "."))*factor:g} мм', length)
    for m in re.finditer(r'(?<!\w)[МM]\s*(\d+(?:[.,]\d+)?)(?!\d)', text, re.I):
        add('Метрическая резьба', 'М' + m[1].replace(',', '.'), m)
    for m in re.finditer(r'\bDIN\s*(\d+[A-Z]?)\b', text, re.I):
        add('DIN', m[1].upper(), m)
    for m in re.finditer(r'(?:кл\.?\s*пр\.?|класс\s+прочности)\s*(\d+(?:[.,]\d+)?)(?!\d)', text, re.I):
        add('Класс прочности', m[1], m)
    coating = re.search(r'неоцинк\w*|без\s+покрытия|гор\.?\s*цинк\w*|горяч\w*\s+цинк\w*|\btZn\b|гальван\w*(?:\s+цинк\w*)?|электролит\w*(?:\s+цинк\w*)?|оцинк\w*|\bцинк\w*|\bZn\b', text, re.I)
    # The specific process in a specification wins over its generic Zn column.
    specific = re.search(r'гор\.?\s*цинк\w*|горяч\w*\s+цинк\w*|\btZn\b|гальван\w*(?:\s+цинк\w*)?|электролит\w*(?:\s+цинк\w*)?', text, re.I)
    if specific or coating:
        m = specific or coating
        add('Покрытие', m[0], m)
    return out


def api_records(candidate):
    specs = candidate.get('technical_specifications')
    if not isinstance(specs, list):
        return []
    out = []
    from product_identity import kind
    product = kind(candidate.get('name'))
    for spec in specs:
        if not isinstance(spec, dict) or not spec.get('name') or spec.get('value') is None:
            continue
        field = {'Длина': 'Длина гвоздя', 'Диаметр': 'Диаметр гвоздя'}.get(spec['name'], spec['name']) if product == 'гвозди' else spec['name']
        field, value, unit = canonical(field, spec['value'], spec.get('unit'))
        if not value:
            continue
        if unit and field not in {'Метрическая резьба', 'DIN', 'Класс прочности'}:
            value += ' ' + str(unit)
        out.append(dict(field=field, value=value, raw_value=spec['value'], unit=unit,
                        evidence=str(spec['name']) + ': ' + str(spec['value']), origin='supplier_api', ambiguous=False))
    return out


def primary_match(required_records, found_records, product):
    """Remaining values are choices; primary mismatches never enter that pool."""
    if product not in {'гайка', 'шайба', 'шпилька', 'гвозди'}:
        return True
    fields = ['Тип товара', 'Длина гвоздя'] if product == 'гвозди' else ['Тип товара', 'Метрическая резьба', 'DIN']
    for field in fields:
        required = {r['value'] for r in required_records if r['field'] == field}
        if not required:
            continue
        found = {r['value'] for r in found_records if r['field'] == field and not r.get('ambiguous')}
        if found != required:
            return False
    return True


def nail_checks(requested, candidate):
    from product_identity import kind
    if kind(requested) != 'гвозди':
        return [], []
    from catalog_comparison import records
    required = records(requested)
    found = records(candidate.get('name'), candidate)
    conflicts, missing = [], []
    for field in ['Длина гвоздя', 'Диаметр гвоздя']:
        needed = {r['value'] for r in required if r['field'] == field}
        actual = {r['value'] for r in found if r['field'] == field and not r['ambiguous']}
        if needed and not actual:
            missing.append('не подтверждено: ' + field)
        elif needed and needed != actual:
            conflicts.append('не совпадает ' + field + ': требуется ' + '/'.join(sorted(needed)) + ', найдено ' + '/'.join(sorted(actual)))
    return conflicts, missing


def nail_pack_mass(candidate, supplied, requested):
    """Only explicit product packing, never shipping weight or package sizes."""
    from product_identity import kind
    if kind(candidate.get('name')) != 'гвозди' or requested not in {'кг', 'г', 'т'}:
        return None
    if supplied not in {'шт', 'упак'} and not re.fullmatch(r'упаковка\s*1\s*шт\.?', str(candidate.get('unit') or ''), re.I):
        return None
    from catalog_comparison import records
    masses = set()
    for record in records(candidate.get('name'), candidate):
        if record['field'] != 'Фасовка':
            continue
        match = re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*(кг|г)', record['value'], re.I)
        if match:
            if record['ambiguous']:
                return None
            masses.add(float(match[1].replace(',', '.')) * (1 if match[2].lower() == 'кг' else .001))
    if len(masses) != 1 or next(iter(masses)) <= 0:
        return None
    return next(iter(masses)) * {'кг': 1, 'г': 1000, 'т': .001}[requested]
