"""Separate hoist identity/capacity from operator-selectable remaining values."""
import re


def records(text):
    text = str(text or '')
    out = []
    def add(field, value, evidence):
        out.append(dict(field=field,value=value,raw_value=value,unit=None,evidence=evidence,
                        origin='name',ambiguous=False))
    for field, patterns in {
        'Тип тали': [('Цепная',r'цепн\w*'),('Канатная',r'канатн\w*|тросов\w*')],
        'Привод тали': [('Электрический',r'электрическ\w*'),('Ручной',r'ручн\w*')],
        'Исполнение тали': [('Передвижная',r'передвижн\w*|с\s+(?:тележк|каретк)\w*'),('Стационарная',r'стационарн\w*|\bСТАЦ\b')],
        'Число скоростей': [('1',r'\b1\s*скорост\w*|односкоростн\w*'),('2',r'\b2\s*скорост\w*|двухскоростн\w*')],
        'Число ветвей цепи': [('2',r'\b2(?:ух|х)?\s*цепн\w*|двухцепн\w*')],
    }.items():
        for value, pattern in patterns:
            m = re.search(pattern,text,re.I)
            if m: add(field,value,m[0])
    capacity = re.search(r'(?<![\w.,])(\d+(?:[.,]\d+)?)\s*(тонн\w*|т)(?!\w)',text,re.I)
    if capacity:
        add('Грузоподъемность',f'{float(capacity[1].replace(",","."))*1000:g} кг',capacity[0])
    voltage = re.search(r'(?<!\w)(\d+)\s*[ВV]\b',text,re.I)
    if voltage: add('Напряжение',voltage[1]+' В',voltage[0])
    height = re.search(r'(?<![\w.,/])(\d+(?:[.,]\d+)?)\s*м\b(?!\s*/)',text,re.I)
    if height: add('Высота подъема',f'{float(height[1].replace(",",".")):g} м',height[0])
    model = re.search(r'\b(?:HHBD|DHS|DHP|HHB)[A-Z0-9]*(?:-[A-Z0-9]+)*\b|\b\d{2}-\d{2}S\b',text,re.I)
    if model: add('Модель тали',model[0].upper(),model[0])
    return out


def api_field(field, value, unit):
    """Never substitute through-block capacity for the nominal capacity."""
    field = field.strip()
    value = str(value).strip()
    if field == 'Тип':
        field = 'Тип тали'
        value = {'цепной':'Цепная','канатный':'Канатная','тросовый':'Канатная'}.get(value.lower(),value)
    if field in {'Грузоподъемность','Грузоподъемность через блок'}:
        try:
            amount = float(value.replace(',','.')) * (1000 if unit in {'т','тонна','тонн'} else 1)
            value, unit = f'{amount:g}', 'кг'
        except ValueError:
            pass
    if field == 'Напряжение':
        value, unit = re.sub(r'\s*[ВV]$', '',value,flags=re.I), 'В'
    if field == 'Наличие каретки' and value.lower() in {'да','есть','нет'}:
        field, value, unit = 'Исполнение тали', 'Передвижная' if value.lower() in {'да','есть'} else 'Стационарная', None
    if field == 'Высота подъема':
        unit = unit or 'м'
    return field,value,unit


def checks(requested, candidate):
    from product_identity import kind
    if kind(requested) != 'таль':
        return [], []
    from catalog_comparison import records as product_records
    required = product_records(requested)
    found = product_records(candidate.get('name'),candidate)
    primary = {'Тип товара','Тип тали','Привод тали','Грузоподъемность'}
    fields = primary | {'Модель тали','Высота подъема','Напряжение','Исполнение тали','Число скоростей','Число ветвей цепи'}
    conflicts, clarification = [], []
    for field in sorted(fields):
        need = {r['value'] for r in required if r['field']==field}
        actual = {r['value'] for r in found if r['field']==field and not r['ambiguous']}
        if not need or need == actual:
            continue
        reason = ('не совпадает ' + field + ': требуется ' + '/'.join(sorted(need)) + ', найдено ' + '/'.join(sorted(actual))) if actual else 'не подтверждено: ' + field
        (conflicts if field in primary else clarification).append(reason)
    return conflicts, clarification
