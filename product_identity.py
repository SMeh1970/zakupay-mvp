"""Ordered product identity, independent of catalogue wording and word order."""
from functools import lru_cache as _lru_cache

import re
from functools import lru_cache

KINDS = [
 ('цепь', r'\bцеп[ьи]\b'),
 ('пульт', r'\bпульт\w*'),
 ('таль', r'\bтал(?:ь|и|ей|ью|ям|ями|ях)\b'),
 ('монтажный клей', r'\bжидк\w*\s+гвозд\w*'),
 ('дюбель', r'\bдюбел\w*'),
 ('кляймер', r'\bкляймер\w*'),
 ('скоба', r'\bскоб\w*'),
 ('гвозди', r'\bгвозд(?:ь|и|ей|ями|ям|ях|ём|ем)\b'),
 ('гайка', r'\bгайк\w*'),
 ('шайба', r'\bшайб\w*'),
 ('шуруп-шпилька', r'\bшуруп[-\s]*шпильк\w*'),
 ('шпилька', r'\bшпильк\w*|\bштанг\w*(?=.*(?:DIN\s*97[56]|резьб))'),
 ('респиратор', r'\bреспиратор\w*'),
 ('клапан', r'\bклапан\w*'),
 ('бур', r'\b(?:бур(?:а|ы|ов|у|ом)?|квадробур\w*|бур-сверло)\b|\bсверл\w*.*?\bSDS[ -]?(?:plus|max|\+)'),
 ('маркер', r'\bмаркер\w*'),
 ('ковёр', r'\bков[её]р\w*'),
 ('хомут', r'\bхомут\w*'),
 ('болт', r'\bболт\w*'),
 ('держатель', r'\bдержател\w*'),
 ('набор', r'\b(?:набор\w*|комплект\w*)'),
 ('запчасть', r'\b(?:якорь|статор|ротор|запчаст\w*)'),
 ('скотч', r'\b(?:скотч|лент\w*\s+малярн\w*)'),
 ('мастика', r'\bмастик\w*'),
 ('праймер', r'\bпраймер\w*'),
 ('утеплитель', r'\b(?:утеплител\w*|плит\w*\s+теплоизоляц\w*)'),
 ('наконечник', r'\bнаконечник\w*'),
 ('кабель', r'\bкабел\w*'),
 ('гофрированная труба', r'\bгофр(?!о?карт)\w*'),
 ('огнетушитель', r'\bогнетушител\w*'),
 ('щит пожарный', r'\bщит\w*\s+пожар\w*'),
 ('ящик для песка', r'\bящик\w*.*?песк\w*'),
 ('угловая шлифмашина', r'\b(?:шлифмашин\w*|шлифовальн\w*\s+машин\w*|болгарк\w*|ушм)\b'),
 ('пила', r'\bпил[аы]\b'),
 ('диск', r'\b(?:диск(?:а|и|ов|у|ом)?|круг(?:а|и|ов|у|ом)?)\b'),
 ('щётка', r'\b(?:щ[её]тк\w*|корщ[её]тк\w*)'),
 ('бита', r'\bбит[аы]\b'),
 ('саморез', r'\b(?:саморез\w*|винт\w*\s+самонарезающ\w*)'),
 ('очиститель пены', r'\bочистител\w*.*?пен\w*'),
 ('пистолет для пены', r'\bпистолет\w*.*?пен\w*'),
 ('монтажная пена', r'\bпен[аыу]\s+монтаж\w*'),
 ('плёнка', r'\bпл[её]нк\w*'),
 ('плоскогубцы', r'\b(?:плоскогубц\w*|пассатиж\w*)'),
 ('рулетка', r'\bрулетк\w*'),
 ('сетка', r'\bсетк\w*'),
 ('черенок', r'\bчерен(?:ок|к\w*)'),
 ('конус Абрамса', r'\bконус\w*.*?(?:абрамс|\bка\b)'),
 ('гидроизоляция рулонная', r'\b(?:гидроизол\w*|бикрост\w*|унифлекс\w*|линокром\w*|биполь\w*)'),
 ('профлист', r'\b(?:профлист\w*|профнастил\w*|лист\s+профилирован\w*)'),
 ('лист', r'\bлист\w*'),
]


@lru_cache(maxsize=8192)
def _kind_uncached(text):
    text = str(text or '')
    # A noun after «с/со/для/под/без» names a part or a purpose, not the product itself:
    # «Держатель … со шпилькой М8» is a holder, «Гайка для шпильки» is a nut.
    component = re.compile(r'(?:^|[\s(,])(?:с|со|для|под|без)\s+(?:[а-яё-]+\s+){0,2}$', re.I)
    matches, parts = [], []
    for i, (name, pattern) in enumerate(KINDS):
        for m in re.finditer(pattern, text, re.I):
            (parts if component.search(text[:m.start()]) else matches).append((m.start(), i, name))
    # Only when nothing else names the product does the part decide («Втулочный анкер с болтом»
    # is still a bolt for «Анкерный болт»); «Держатель … со шпилькой» is a holder.
    matches = matches or parts
    if matches:
        return min(matches)[2]
    from product_characteristics import extract
    category = extract(text)['category']
    return category if category != 'Не определена' else None


@lru_cache(maxsize=8192)
def models(text):
    # Letters and digits belong to one model even with spaces: HS 7601 / HS7601.
    # Preserve suffixes: HS7601K is a different model, not an exact HS7601.
    text = re.sub(r'\bMOS\b', ' ', str(text or ''), flags=re.I)
    text = text.translate(str.maketrans('АВЕКМНОРСТХУавекмнорстху', 'ABEKMHOPCTXYabekmhopctxy'))
    return {re.sub(r'[^A-Z0-9]', '', m[0].upper())
            for m in re.finditer(r'(?<!\w)(?:[A-Za-z]{1,5}\s*[-]?\s*\d{3,7}(?:\s*[A-Za-z]{1,3})?|\d{4,8}[A-Za-z]{1,3})(?!\w)',str(text or ''))}


def _records_uncached(text):
    text = str(text or '')
    product = kind(text)
    result = []
    def add(field, value, evidence):
        result.append(dict(field=field,value=str(value),raw_value=value,unit=None,evidence=evidence,origin='name',ambiguous=False))
    if product:
        add('Тип товара',product,'')
    if product == 'таль':
        from hoist_identity import records as hoist_records
        result.extend(hoist_records(text))
    from supplier_characteristics import name_records
    result.extend(name_records(text, product))
    if product == 'клапан':
        for field, patterns in {
            'Назначение клапана': [('Балансировочный', r'балансировочн\w*|балансир\.')],
            'Управление клапаном': [('Ручное', r'ручн\w*'), ('Автоматическое', r'автоматическ\w*')],
            'Присоединение клапана': [('Фланцевое', r'фланц\w*'), ('Резьбовое', r'резьб\w*|муфтов\w*')],
            'Материал корпуса клапана': [('Чугун', r'\bчугун\w*\b'), ('Латунь', r'\bлатун\w*\b(?!\s+(?:сердечник|шток|ниппел))')],
            'Измерительные ниппели': [('Нет', r'без\s+(?:измерит\w*\.?\s*)?ниппел\w*'),
                                     ('Есть', r'с\s+измерит\w*\.?\s*ниппел\w*')],
        }.items():
            for value, pattern in patterns:
                match = re.search(pattern, text, re.I)
                if match:
                    add(field, value, match[0])
        match = re.search(r'\b(?:Ду|DN)\s*(\d+)\b', text, re.I)
        if match:
            add('Условный проход', str(int(match[1])), match[0])
    from vi_order_match import _requested_brands
    for brand in sorted(_requested_brands(text)):
        add('Бренд',brand,'')
    if product in {'пила','угловая шлифмашина','пистолет для пены'}:
        for model in sorted(models(text)):
            letters={'M':'[MМ]','C':'[CС]','A':'[AА]','B':'[BВ]','E':'[EЕ]','K':'[KК]','H':'[HН]','O':'[OО]','P':'[PР]','T':'[TТ]','X':'[XХ]'}
            evidence=re.search(r'\s*[-]?\s*'.join(letters.get(c,re.escape(c)) for c in model),text,re.I)
            add('Модель',model,evidence[0] if evidence else '')
    if product == 'бур':
        # Catalogue notation: diameter x working/overall length or diameter x
        # overall x working length. Never use package dimensions as drill size.
        m = re.search(r'(?<![\d.,])(\d+(?:[.,]\d+)?)\s*[xх×*]\s*(\d+(?:[.,]\d+)?)(?:\s*[/xх×*]\s*(\d+(?:[.,]\d+)?))?', text, re.I)
        if m:
            add('Диаметр бура', f'{float(m[1].replace(",", ".")):g} мм', m[1])
            length = max((v for v in (m[2], m[3]) if v), key=lambda v: float(v.replace(',', '.')))
            add('Общая длина бура', f'{float(length.replace(",", ".")):g} мм', length)
        tail = re.search(r'\bSDS\s*[- ]?\s*(plus|max|\+)', text, re.I)
        if tail:
            add('Хвостовик', 'SDS-' + ('plus' if tail[1].lower() in {'plus', '+'} else 'max'), tail[0])
    if product == 'бита':
        m=re.search(r'(?<![A-Z])(?:PH|PZ|SL|TX|T)\s*-?\s*\d+(?!\d)',text,re.I)
        if m:add('Профиль биты',re.sub(r'[\s-]','',m[0].upper()),m[0])
    if product in {'рулетка','плоскогубцы','бита','черенок','щётка','гофрированная труба','очиститель пены','монтажная пена'}:
        if product == 'рулетка':pattern=r'(\d+(?:[.,]\d+)?)\s*(м|m)(?![а-яa-z])'
        elif product in {'очиститель пены','монтажная пена'}:pattern=r'(\d+(?:[.,]\d+)?)\s*(мл|ml|л|l)(?![а-яa-z])'
        else:pattern=r'(\d+(?:[.,]\d+)?)\s*(мм|mm)(?![а-яa-z])'
        # PH2x50 has one length, not a fictitious 2 mm dimension.
        m=re.search(pattern,text,re.I)
        if product=='гофрированная труба':
            m=re.search(r'(?<!\w)[dDдДØ⌀]\s*[-=]?\s*(\d+(?:[.,]\d+)?)(?:\s*(мм))?',text) or m
        if m:
            value=float(m[1].replace(',','.'))
            unit=(m[2] or 'мм').lower()
            if product in {'очиститель пены','монтажная пена'}:
                value*=1000 if unit in {'л','l'} else 1
                add('Объём',f'{value:g} мл',m[0])
            else:
                field='Диаметр' if product in {'черенок','гофрированная труба'} else 'Длина изделия'
                add(field,f'{value:g} '+('м' if product=='рулетка' else 'мм'),m[0])
    if product == 'шпилька':
        # Rod length: «L=1м», «(1 m)», «M8x1 м», «М8х1000», «1000 мм». Grams such as «(230 гр.)» are not a size.
        length = None
        for pattern in (r'\bL\s*=?\s*(\d+(?:[.,]\d+)?)\s*(мм|mm|м|m)(?![а-яa-z])',
                        r'\(\s*(\d+(?:[.,]\d+)?)\s*(м|m)\s*\)',
                        r'[MМ]\s*\d+(?:[.,]\d+)?\s*[xх×*]\s*(\d+(?:[.,]\d+)?)\s*(мм|mm|м|m)?(?![а-яa-z\d])',
                        r'(\d+(?:[.,]\d+)?)\s*(мм|mm)(?![а-яa-z])',
                        r'(?<![\w.,])\d{1,2}\s*[xх×]\s*(\d{2,4})()(?!\d)(?![.,]\d)'):
            if (m := re.search(pattern, text, re.I)):
                value = float(m[1].replace(',', '.'))
                unit = (m[2] or ('м' if value < 10 else 'мм')).lower()
                length = (value * 1000 if unit in {'м', 'm'} else value, m[0])
                break
        if length:
            add('Длина изделия', f'{length[0]:g} мм', length[1])
        # «шпилька 8x200»: the first number is the thread.
        bare = re.search(r'(?<![\w.,])(\d{1,2})\s*[xх×]\s*\d{2,4}(?!\d)(?![.,]\d)', text)
        if bare and not re.search(r'(?<!\w)[MМ]\s*\d', text):
            add('Метрическая резьба', 'М' + bare[1], bare[0])
    for field,patterns in {
        'Назначение диска':[(r'отрезн\w*','Отрезной'),(r'шлифов\w*','Шлифовальный'),(r'пильн\w*','Пильный')],
        'Форма щётки':[(r'чаш\w*','Чашечная'),(r'дисков\w*|плоск\w*','Дисковая')],
    }.items():
        if (field=='Назначение диска' and product!='диск') or (field=='Форма щётки' and product!='щётка'):continue
        for pattern,value in patterns:
            if (m:=re.search(pattern,text,re.I)):add(field,value,m[0])
    if product == 'рулетка':
        m=re.search(r'(\d+(?:[.,]\d+)?)\s*(?:мм|mm)(?!\w)',text,re.I)
        if m:add('Ширина ленты',f'{float(m[1].replace(",",".")):g} мм',m[0])
    if product == 'бита' and re.search(r'торцев\w*|головк\w*',text,re.I):
        m=re.search(r'(\d+(?:[.,]\d+)?)\s*[xх×]\s*\d+',text,re.I)
        if m:add('Размер головки',f'{float(m[1].replace(",",".")):g} мм',m[0])
    if product == 'скотч' and re.search(r'малярн\w*',text,re.I):
        add('Тип ленты','Малярная','малярный')
    if product == 'диск':
        for pattern,value in [(r'по\s+металл\w*','Металл'),(r'по\s+дерев\w*','Дерево')]:
            if (m:=re.search(pattern,text,re.I)):add('Материал обработки',value,m[0])
        for pattern,value in [(r'алмазн\w*','Алмазный'),(r'сегментн\w*','Сегментный')]:
            if (m:=re.search(pattern,text,re.I)):add('Исполнение диска',value,m[0])
        m=re.search(r'(\d+)\s*(?:зуб\w*|[TТ](?!\w))',text,re.I)
        if m:add('Число зубьев',int(m[1]),m[0])
    # Parameters that were never extracted from a request impose no requirement,
    # so any candidate passed. Make these ones mandatory (order 37210407 control set).
    if product == 'респиратор':
        m=re.search(r'\bFFP\s*([123])\b',text,re.I)
        if m:add('Класс защиты','FFP'+m[1],m[0])
    if product == 'диск':
        # Grit P40 / Р40 (Latin or Cyrillic capital P). "зерно 40" is deliberately not
        # treated as equal: it stays an operator decision.
        m=re.search(r'(?<![A-Za-zА-Яа-я0-9])[PР]\s*-?\s*(\d{2,4})(?!\d)',text)
        if m:add('Зернистость','P'+m[1],m[0])
    if product == 'Подшипники':
        # Designation right after the product word, plus an equivalent in brackets.
        for pattern in (r'подшипник\w*(?:\s+[А-Яа-яЁё]+){0,2}?\s*(?:№\s*)?((?:[A-Z]{1,3}-?)?\d{3,7})(?![\w])',
                        r'\(\s*((?:[A-Z]{1,3}-?)?\d{3,7})(?![\w])'):
            m=re.search(pattern,text,re.I)
            if m:add('Обозначение подшипника',re.sub(r'[-\s]','',m[1]).upper(),m[0])
    if product == 'ящик для песка':
        m=re.search(r'(\d+(?:[.,]\d+)?)\s*м[³3]',text,re.I)
        if m:add('Объём',f'{float(m[1].replace(",",".")):g} м³',m[0])
    if product == 'щётка':
        m=re.search(r'деревян\w*',text,re.I)
        if m:add('Материал корпуса','Дерево',m[0])
        if re.search(r'без\s+черенк\w*',text,re.I):add('Комплектация черенком','Без черенка','без черенка')
        elif re.search(r'с\s+черенк\w*',text,re.I):add('Комплектация черенком','С черенком','с черенком')
    if product == 'рулетка' and re.search(r'геодезическ\w*',text,re.I):
        add('Исполнение рулетки','Геодезическая','геодезическая')
    if product == 'плоскогубцы' and re.search(r'комбинирован\w*',text,re.I):
        add('Исполнение плоскогубцев','Комбинированные','комбинированные')
    return result


def valve_checks(requested, offered):
    """Separate missing evidence from known contradictions for auto matching."""
    if kind(requested) != 'клапан':
        return [], []
    required = {r['field']: r['value'] for r in records(requested)}
    found = {}
    for record in records(offered):
        found.setdefault(record['field'], set()).add(record['value'])
    conflicts, missing = [], []
    for field, value in required.items():
        if field not in found:
            missing.append('не подтверждено: ' + field)
        elif found[field] != {value}:
            conflicts.append('не совпадает ' + field + ': требуется ' + value + ', найдено ' + '/'.join(sorted(found[field])))
    return conflicts, missing


# Pure functions of the text, called for every offer on every review page load.
@_lru_cache(maxsize=20000)
def _kind_cached(text):
    return _kind_uncached(text)


def kind(text):
    return _kind_cached(str(text or ''))


@_lru_cache(maxsize=20000)
def _records_cached(text):
    return _records_uncached(text)


def records(text):
    return _clone(_records_cached(str(text or '')))


def _clone(value):
    """Copy of JSON-like data (dicts, lists, tuples of plain values); faster than deepcopy."""
    if isinstance(value, dict):
        return {key: _clone(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clone(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_clone(item) for item in value)
    if isinstance(value, set):
        return {_clone(item) for item in value}
    return value
