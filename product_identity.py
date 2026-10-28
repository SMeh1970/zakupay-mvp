"""Ordered product identity, independent of catalogue wording and word order."""
import re
from functools import lru_cache

KINDS = [
 ('маркер', r'\bмаркер\w*'),
 ('ковёр', r'\bков[её]р\w*'),
 ('хомут', r'\bхомут\w*'),
 ('болт', r'\bболт\w*'),
 ('набор', r'\b(?:набор\w*|комплект\w*)'),
 ('запчасть', r'\b(?:якорь|статор|ротор|запчаст\w*)'),
 ('скотч', r'\b(?:скотч|лент\w*\s+малярн\w*)'),
 ('мастика', r'\bмастик\w*'),
 ('праймер', r'\bпраймер\w*'),
 ('утеплитель', r'\b(?:утеплител\w*|плит\w*\s+теплоизоляц\w*)'),
 ('наконечник', r'\bнаконечник\w*'),
 ('кабель', r'\bкабел\w*'),
 ('гофрированная труба', r'\bгофр\w*'),
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
def kind(text):
    text = str(text or '')
    matches = [(m.start(), i, name) for i,(name,pattern) in enumerate(KINDS)
               if (m := re.search(pattern,text,re.I))]
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


def records(text):
    text = str(text or '')
    product = kind(text)
    result = []
    def add(field, value, evidence):
        result.append(dict(field=field,value=str(value),raw_value=value,unit=None,evidence=evidence,origin='name',ambiguous=False))
    if product:
        add('Тип товара',product,'')
    from vi_order_match import _requested_brands
    for brand in sorted(_requested_brands(text)):
        add('Бренд',brand,'')
    if product in {'пила','угловая шлифмашина','пистолет для пены'}:
        for model in sorted(models(text)):
            letters={'M':'[MМ]','C':'[CС]','A':'[AА]','B':'[BВ]','E':'[EЕ]','K':'[KК]','H':'[HН]','O':'[OО]','P':'[PР]','T':'[TТ]','X':'[XХ]'}
            evidence=re.search(r'\s*[-]?\s*'.join(letters.get(c,re.escape(c)) for c in model),text,re.I)
            add('Модель',model,evidence[0] if evidence else '')
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
