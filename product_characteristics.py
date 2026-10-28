"""Conservative, evidence-backed extraction. Unknown is never a match.

Category rules are deliberately finite. Unsupported descriptions stay reviewable.
No attributes are derived from worksheet formatting or neighbouring products.
"""
import re

VERSION = "2026-10-22.4"
N = r"\d+(?:[.,]\d+)?"
CATEGORIES = [
    (r"\bогнетушител\w*", "Огнетушители"),
    (r"\bкабел\w*", "Кабели"),
    (r"фанер", "Фанера"), (r"зенков", "Зенковки"),
    (r"труба\s+профильн", "Профильные трубы"),
    (r"\bтруб[аы]\b", "Трубы"), (r"\bсверл", "Сверла"),
    (r"\bотвод\w*", "Отводы"), (r"\bпереход\w*", "Переходы"), (r"\bтройник\w*", "Тройники"),
    (r"\bдвер[ьи]\w*", "Двери"), (r"\bпанел[ьи]\w*", "Панели"),
    (r"\bдозатор\w*", "Дозаторы"), (r"\bинсталляци\w*", "Инсталляции"),
    (r"\bписсуар\w*", "Писсуары"), (r"\bумывальник\w*|\bраковин\w*", "Раковины"),
    (r"\bунитаз\w*", "Унитазы"), (r"\bсмесител\w*", "Смесители"),
    (r"\bпрофил[ьи]\w*", "Профили"), (r"\bподвес\w*", "Подвесы"),
    (r"\bметчик", "Метчики"), (r"\bфрез", "Фрезы"),
    (r"подшипник", "Подшипники"), (r"штанген", "Измерительный инструмент"),
    (r"\bкруг\b", "Абразивные круги"),
    (r"креплени|опора|скоба|струбцин|траверс|хомут|шина монтаж", "Крепёж"),
    (r"пл[её]нк", "Плёнки"), (r"краска|эмаль", "Краски и эмали"),
    (r"шпа[тк]лев", "Шпаклёвки"), (r"штукатур", "Штукатурки"),
    (r"\bклей\b", "Клеи"), (r"цемент|алебастр|\bгипс\b", "Вяжущие"),
]

# These categories currently have dedicated dimensional/identity extraction.
# Recognising a noun or colour alone in other categories is not confirmation.
MATCH_CATEGORIES = {"Огнетушители", "Кабели", "Фанера", "Зенковки", "Трубы", "Профильные трубы", "Крепёж", "Сверла", "Отводы", "Переходы", "Тройники"}


def extract(name):
    text = str(name or "")
    categories = [(m.start(), n, c) for n, (p, c) in enumerate(CATEGORIES) if (m := re.search(p, text, re.I))]
    # The earliest product noun wins: "дверь ... ПВХ плёнка" is a door,
    # "писсуар ... с креплением" is a urinal, not a fastener.
    category = min(categories)[2] if categories else "Не определена"
    attributes = []

    def add(key, value, unit, match, ambiguous=False):
        evidence = match if isinstance(match, str) else match[0]
        entry = dict(parameter=key, value=value, unit=unit, evidence=evidence,
                     origin="name", ambiguous=ambiguous)
        if entry not in attributes:
            attributes.append(entry)

    def numeric(value):
        return float(value.replace(",", "."))

    def size(key, match, group=1, unit_group=2):
        unit = match[unit_group] if unit_group else None
        value = numeric(match[group])
        add(key, value * ({"см": 10, "м": 1000}.get(unit.lower(), 1) if unit else 1),
            "мм" if unit else None, match, not bool(unit))

    for m in re.finditer(r"(?<!\w)(ГОСТ(?:\s+Р)?|ТУ)\s*([\d][\dА-Яа-яA-Za-z./–-]*)", text, re.I):
        add("Стандарт " + m[1].upper().replace("  ", " "), m[2].upper().replace("–", "-"), None, m)
    for m in re.finditer(r"\b(Р6М5|Р18|HSS(?:-Co)?|ВК8)\b", text, re.I):
        add("Материал инструмента", m[1].upper(), None, m)
    m = re.search(r"\b(?:сталь\b|ст\.)\s*([0-9А-Яа-яA-Za-z]+)", text, re.I)
    if m:
        add("Марка стали", m[1].upper(), None, m)
    if category == "Огнетушители":
        from extinguisher_identity import identity
        item = identity(text)
        for key, label in [("model", "Модель огнетушителя"), ("charging", "Тип исполнения"), ("classes", "Классы пожара")]:
            if item and item[key]:
                pattern = {"model": r"ОП\s*[-–]?\s*\d+", "charging": r"\(\s*з\s*\)|/\s*з\b|\bзакачн\w*|(?<= )з\b", "classes": r"[АA][ВB][СC][ЕE]"}[key]
                evidence = re.search(pattern, text, re.I)
                add(label, item[key], None, evidence)
    if category == "Трубы":
        m = re.search(r"(" + N + r")\s*[хx×*]\s*(" + N + r")\s*(мм|см)?", text, re.I)
        if m:
            size("Наружный диаметр", m, 1, 3)
            size("Толщина стенки", m, 2, 3)
    elif category == "Кабели":
        from cable_identity import identity
        cable = identity(text)
        if cable['mark']:
            add('Марка кабеля', cable['mark'], None, cable['mark_evidence'])
        if cable['cores'] is not None:
            add('Число жил', cable['cores'], None, cable['size_evidence'])
            add('Сечение жилы', cable['section'], 'мм²', cable['size_evidence'])
    elif category == "Профильные трубы":
        m = re.search(r"(" + N + r")\s*[хx×*]\s*(" + N + r")\s*[хx×*]\s*(" + N + r")\s*(мм|см)?", text, re.I)
        if m:
            for k, i in [("Высота сечения", 1), ("Ширина сечения", 2), ("Толщина стенки", 3)]:
                size(k, m, i, 4)
    elif category == "Фанера":
        m = re.search(r"(" + N + r")\s*[хx×*]\s*(" + N + r")\s*[хx×*]\s*(" + N + r")\s*(мм|см)?", text, re.I)
        if m:
            for k, i in [("Длина", 1), ("Ширина", 2), ("Толщина", 3)]:
                size(k, m, i, 4)
    for key, pattern in [
        ("Толщина стенки" if category == "Трубы" else "Толщина", r"(?:толщин[аойе]*|\bs\s*=|стенка)\s*(" + N + r")\s*(мм|см)?"),
        ("Наружный диаметр" if category == "Трубы" else "Диаметр", r"(?:диаметр(?:ом)?|[фФØ⌀]|\b[dD]\s*=?)\s*(" + N + r")\s*(мм|см)?"),
        ("Длина", r"(?:длина|\bL\s*=)\s*(" + N + r")\s*(мм|см|м\b)?"),
        ("Ширина", r"ширина\s*(" + N + r")\s*(мм|см)?"),
    ]:
        for m in re.finditer(pattern, text, re.I):
            size(key, m)
    if category in {"Зенковки", "Сверла"} and not any(a["parameter"] == "Диаметр" for a in attributes):
        m = re.search(r"(?:зенковка|сверло)\s+(" + N + r")\s*(мм)?", text, re.I)
        if m:
            size("Диаметр", m)
    for m in re.finditer(r"(" + N + r")\s*(?:°|градус\w*|гр\b)", text, re.I):
        add("Угол", numeric(m[1]), "°", m)
    for m in re.finditer(r"\b(?:Ду|DN)\s*(" + N + r")", text, re.I):
        add("Номинальный диаметр", numeric(m[1]), "DN", m)
    for m in re.finditer(r"\bRAL\s*(\d{4})\b", text, re.I):
        add("Цвет RAL", m[1], None, m)
    for m in re.finditer(r"\bNCS\s*(?:S\s*)?(\d{4})\s*-\s*([A-Z]\d{0,2}[A-Z]?)\b", text, re.I):
        add("Цвет NCS", m[1] + "-" + m[2].upper(), None, m)
    if category == "Крепёж":
        for pattern, value in [(r"креплени\w*", "Крепление"), (r"хомут\w*", "Хомут"),
            (r"скоба\w*", "Скоба"), (r"опора\w*", "Опора"), (r"струбцин\w*", "Струбцина")]:
            m = re.search(pattern, text, re.I)
            if m:
                add("Тип крепежа", value, None, m)
        m = re.search(r"\b([LVZ])-образн\w*", text, re.I)
        if m:
            add("Форма крепления", m[1].upper(), None, m)
    combined_thread = bool(re.search(r"[МM]\s*\d+\s*/\s*(?:[МM]\s*)?\d+", text))
    for m in re.finditer(r"(?<!\w)[МM]\s*(" + N + r")", text):
        add("Метрическая резьба", "М" + str(numeric(m[1])).removesuffix(".0"), None, m,
            combined_thread or bool(re.match(r"\s*\*", text[m.end():])))
    if category == "Фанера":
        m = re.search(r"(?<!\w)(ФСФ|ФК|ФОФ)(?!\w)", text, re.I)
        if m:
            add("Марка фанеры", m[1].upper(), None, m)
        m = re.search(r"сорт\s*(\d\s*/\s*\d|\d)", text, re.I)
        if m:
            add("Сорт", m[1].replace(" ", ""), None, m)
    for pattern, key, value in [
        (r"ламиниров\w*", "Поверхность", "Ламинированная"),
        (r"нешлиф\w*", "Поверхность", "Нешлифованная"),
        (r"(?<!не)шлифован\w*", "Поверхность", "Шлифованная"),
        (r"оцинкован\w*|\bОц\b", "Покрытие", "Оцинкованное"),
        (r"бер[её]з\w*", "Материал", "Берёза"),
        (r"полипропилен\w*|\bПП\b", "Материал", "Полипропилен"),
        (r"полиэтилен\w*|\bПНД\b", "Материал", "Полиэтилен"),
        (r"\bПВХ\b", "Материал", "ПВХ"),
        (r"алюмин\w*|\bалюм\.", "Материал", "Алюминий"),
        (r"нержаве\w*", "Материал", "Нержавеющая сталь"),
        (r"\bбел(?:ый|ая|ое|ые)\b", "Цвет", "Белый"),
        (r"\bч[её]рн(?:ый|ая|ое|ые)\b", "Цвет", "Чёрный"),
        (r"\bсер(?:ый|ая|ое|ые)\b", "Цвет", "Серый"),
        (r"\bзел[её]н(?:ый|ая|ое|ые)\b", "Цвет", "Зелёный"),
        (r"\bсин(?:ий|яя|ее|ие)\b", "Цвет", "Синий"),
        (r"под мрамор", "Рисунок", "Мрамор"),
        (r"под дерево", "Рисунок", "Дерево"),
        (r"с виброгасителем", "Виброгаситель", True),
        (r"электросварн\w*|\bсварн\w*|\bэ/с\b", "Тип трубы", "Сварная"),
        (r"бесшовн\w*|\bб/ш\b", "Тип трубы", "Бесшовная"),
        (r"грувлок\w*", "Тип соединения", "Грувлок"),
        (r"под приварку", "Тип соединения", "Сварное"),
        (r"резьбов\w*", "Тип соединения", "Резьбовое"),
        (r"\bНГ\b", "Горючесть", "НГ"),
    ]:
        m = re.search(pattern, text, re.I)
        if m:
            add(key, value, None, m)
    for m in re.finditer(r"(" + N + r")\s*(кг|л|г)(?![\w/])", text, re.I):
        add("Фасовка", numeric(m[1]), m[2].lower(), m)
    for m in re.finditer(r"(\d+)\s*шт\.?\s*(?:/\s*уп|в упаковке|в упак)", text, re.I):
        add("Штук в упаковке", int(m[1]), "шт", m)
    return dict(category=category, attributes=attributes, parser_version=VERSION)


def compare(request, product):
    conflicts, missing = [], []
    if request["category"] not in MATCH_CATEGORIES:
        missing.append("Категория требует ручной проверки")
    if not request["attributes"]:
        missing.append("Обязательные характеристики не выделены")
    if request["category"] != "Не определена":
        if product["category"] == "Не определена":
            missing.append("Категория")
        elif request["category"] != product["category"]:
            conflicts.append("Категория")
    else:
        missing.append("Категория заявки не определена")
    for required in request["attributes"]:
        key = required["parameter"]
        values = [a for a in product["attributes"] if a["parameter"] == key]
        unambiguous = [a for a in values if not a.get("ambiguous")]
        # Missing units prevent confirmation, but a different magnitude must
        # not be offered as the requested diameter/thickness either.
        if values and not any(a["value"] == required["value"] for a in values):
            conflicts.append(key)
            continue
        if required.get("ambiguous") or not unambiguous:
            missing.append(key)
        elif not any(a["value"] == required["value"] and a.get("unit") == required.get("unit") for a in unambiguous):
            if all(a.get("unit") != required.get("unit") for a in unambiguous):
                missing.append(key + ": единица")
            else:
                conflicts.append(key)
        elif any(a["value"] != required["value"] for a in unambiguous):
            missing.append(key + ": несколько значений")
    return dict(status="conflict" if conflicts else "review" if missing else "matched",
                conflicts=list(dict.fromkeys(conflicts)), missing=list(dict.fromkeys(missing)))
