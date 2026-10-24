"""Filters and pagination for persisted requests, without supplier/API lookups."""

import html
import json
import math
import re
import unicodedata
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from invoice_generator import normalize_customer
from zakupay_categories import category_id, category_choices, category_descendants


URL = "/dashboard/automation"
STATUSES = {
    "pending_search": "Поиск не запускался",
    "processing": "Идёт поиск цен",
    "ready_for_review": "Готова к проверке",
    "needs_review": "Требуется проверка",
    "offer_created": "Счёт отправлен",
    "failed": "Ошибка обработки",
}
DEFAULTS = {
    "keyword": "", "title": "", "order_id": "", "customer": "", "inn": "",
    "source": "all", "status": "all", "viewed": "all", "offer": "all",
    "region": "", "category": "", "category_id": (), "payment": "all",
    "creationDateFrom": "", "creationDateTo": "",
    "finishDateFrom": "", "finishDateTo": "",
    "min_positions": "", "max_positions": "", "max_competitors": "",
    "quantity_min": "", "quantity_max": "", "quantity_unit": "",
    "delayFrom": "", "delayTo": "", "page_size": "25",
}
DATE_FIELDS = {"creationDateFrom", "creationDateTo", "finishDateFrom", "finishDateTo"}
NUMBER_FIELDS = {"min_positions", "max_positions", "max_competitors", "delayFrom", "delayTo", "quantity_min", "quantity_max"}

UNIT_ALIASES = {
    "шт": "шт", "штука": "шт", "штуки": "шт", "штук": "шт", "pcs": "шт",
    "м2": "м²", "m2": "м²", "квм": "м²", "мкв": "м²", "квметр": "м²", "квметров": "м²", "квадратныйметр": "м²",
    "м3": "м³", "m3": "м³", "кубм": "м³", "мкуб": "м³", "кубметр": "м³", "кубметров": "м³", "кубическийметр": "м³",
    "м": "м", "метр": "м", "метров": "м", "погм": "м", "мп": "м",
    "кг": "кг", "килограмм": "кг", "килограммов": "кг",
    "т": "т", "тонна": "т", "тонн": "т", "л": "л", "литр": "л", "литров": "л",
    "уп": "упак", "упак": "упак", "упаковка": "упак", "упаковок": "упак",
    "компл": "компл", "комплект": "компл", "комплектов": "компл",
}
COMMON_UNITS = ("шт", "м", "м²", "м³", "кг", "т", "л", "упак", "компл")


def normalize_unit(value):
    if isinstance(value, dict):
        value = value.get("name") or value.get("shortName") or ""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    key = "".join(text.replace(".", "").split())
    return UNIT_ALIASES.get(key, text)


def order_datetime(raw):
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=ZoneInfo("Europe/Moscow")) if parsed.tzinfo is None else parsed.astimezone(ZoneInfo("Europe/Moscow"))
    except (TypeError, ValueError):
        return None


def _dict(raw):
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _name(value):
    return str((value.get("name") or "") if isinstance(value, dict) else value or "")


def _norm(value):
    return str(value or "").casefold().replace("ё", "е").strip()


def short_company_name(value):
    name = " ".join(str(value or "").split())
    prefix = re.compile(r'^(?:общество\s+с\s+ограниченной\s+ответственностью|'
        r'публичное\s+акционерное\s+общество|непубличное\s+акционерное\s+общество|'
        r'акционерное\s+общество|закрытое\s+акционерное\s+общество|открытое\s+акционерное\s+общество|'
        r'индивидуальный\s+предприниматель|специализированный\s+застройщик|ООО|ПАО|НАО|ЗАО|ОАО|АО|ИП|СЗ)\b[\s«»"“”]*', re.I)
    original = name
    name = name.strip(' «»"“”')
    while prefix.match(name):
        name = prefix.sub('', name, count=1).strip(' «»"“”')
    return name or original


def short_category_name(value):
    name = str(value or '').strip()
    # Drop parent sections only where the source explicitly supplies a path.
    leaf = re.split(r'\s*(?:→|>| / | \\ )\s*', name)[-1].strip()
    return leaf if len(leaf) <= 70 else leaf[:67].rstrip() + '…'


def order_city(order):
    for key in ('city', 'deliveryCity', 'locality', 'settlement'):
        value = _name(order.get(key)).strip()
        if value:
            return value
    address = order.get('deliveryAddress') or order.get('address') or ''
    if isinstance(address, dict):
        value = order_city(address)
        if value:
            return value
        address = address.get('fullAddress') or address.get('address') or ''
    match = re.search(r'(?:^|[,;]\s*|\s)(?:город\s+|г\.\s*)([^,;\n]+)', str(address), re.I)
    return match[1].strip() if match else ''


def _number(value):
    try:
        number = float(str(value).strip().replace(",", ".")) if value is not None and str(value).strip() else None
        return number if number is not None and math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def listing_query(postgres=False):
    """Keep large supplier candidates out of the all-request filtering read.

    Full results are loaded only for the current page. Older jobs without an
    order snapshot retain their result lines as a compatibility fallback.
    """
    keys = ("order_name", "order_creation_date", "customer", "manual_reference", "summary", "live_offer_created")
    if postgres:
        doc = "CAST(COALESCE(NULLIF(result_json,''),'{}') AS jsonb)"
        fields = ",".join(f"'{key}',{doc}->'{key}'" for key in keys)
        fields += f",'items',CASE WHEN order_json IS NULL OR order_json='' THEN {doc}->'items' ELSE '[]'::jsonb END"
        result = f"CAST(jsonb_build_object({fields}) AS text)"
    else:
        doc = "COALESCE(NULLIF(result_json,''),'{}')"
        fields = ",".join(f"'{key}',json_extract({doc},'$.{key}')" for key in keys)
        fields += f",'items',json(CASE WHEN order_json IS NULL OR order_json='' THEN COALESCE(json_extract({doc},'$.items'),'[]') ELSE '[]' END)"
        result = f"json_object({fields})"
    return (
        "SELECT id,order_id,event_type,subject,status,viewed_at,order_json,"
        f"{result} AS result_json FROM automation_jobs WHERE status != 'skipped_not_prepayment'"
    )


def describe(row):
    result = _dict(row["result_json"])
    order = _dict(row["order_json"])
    customer = normalize_customer(order.get("customer"))
    if not customer["name"]:
        customer = normalize_customer(result.get("customer"))
    items = order.get("orderItems") or []
    legacy_items = result.get("items") or []
    summary = result.get("summary") or {}
    sent = bool(result.get("live_offer_created") or row["status"] == "offer_created" or order.get("offers") or any(item.get("offerIds") for item in items))
    categories = {}
    for item in items:
        value = item.get("category")
        name = _name(value).strip()
        key = category_id(value.get("id")) if isinstance(value, dict) else ""
        if name:
            categories[key or "name:" + _norm(name)] = name
    category = " | ".join(categories.values())
    title = str(order.get("name") or result.get("order_name") or row["subject"] or "")
    search_text = " ".join([title, customer["name"], str(row["order_id"]), str(result.get("manual_reference") or "")] +
                           [str(item.get("goodName") or item.get("name") or "") + " " + str(item.get("comment") or "") for item in items] +
                           [str(item.get("requested_name") or "") for item in legacy_items])
    competitors = [_number(item.get("companiesWithOffersCount")) for item in items]
    known_competitors = [number for number in competitors if number is not None]
    quantity_rows = []
    for position, item in enumerate(items or legacy_items, 1):
        category_value = item.get("category")
        category_key = category_id(category_value.get("id")) if isinstance(category_value, dict) else ""
        category_name = _name(category_value).strip()
        quantity_rows.append({
            "position": position,
            "name": str(item.get("goodName") or item.get("name") or item.get("requested_name") or ""),
            "search": _norm(str(item.get("goodName") or item.get("name") or item.get("requested_name") or "") + " " + str(item.get("comment") or "")),
            "quantity": _number(item.get("count") if item.get("count") is not None else item.get("quantity")),
            "unit": normalize_unit(item.get("unit") or item.get("unitName")),
            "category": category_key or ("name:" + _norm(category_name) if category_name else ""),
        })
    return {
        "id": int(row["id"]), "order_id": str(row["order_id"]),
        "reference": str(result.get("manual_reference") or ""), "title": title,
        "customer": customer["name"], "inn": customer["inn"],
        "customer_short": short_company_name(customer["name"]), "city": order_city(order),
        "category_short": " · ".join(dict.fromkeys(short_category_name(v) for v in categories.values())),
        "source": "manual" if row["event_type"] == "manual_order" else "zakupay",
        "status": "offer_created" if result.get("live_offer_created") else row["status"],
        "viewed": bool(row["viewed_at"]), "sent": sent,
        "date": order_datetime(order.get("creationDate") or order.get("publicDate") or result.get("order_creation_date")),
        "finish": order_datetime(order.get("finishDate")),
        "positions": len(items) if items else summary.get("positions") or len(legacy_items),
        "region": _name(order.get("region")), "category": category, "categories": categories,
        "delay": _number(order.get("delay")), "terms": str(order.get("paymentTerms") or ""),
        "competitors": max(known_competitors) if known_competitors else None,
        "search": search_text,
        "quantity_rows": quantity_rows,
    }


def filters_from_query(query):
    filters = {key: str(query.get(key, default)).strip() for key, default in DEFAULTS.items() if key != "category_id"}
    categories = query.getlist("category_id") if hasattr(query, "getlist") else query.get("category_id", ())
    if isinstance(categories, str):
        categories = [categories]
    filters["category_id"] = tuple(dict.fromkeys(str(value).strip() for value in categories if str(value).strip()))
    filters["quantity_unit"] = normalize_unit(filters["quantity_unit"])
    enums = {"source": {"all", "manual", "zakupay"}, "status": {"all", *STATUSES},
             "viewed": {"all", "yes", "no"}, "offer": {"all", "sent", "unsent"},
             "payment": {"all", "prepayment", "delay"}}
    for key, allowed in enums.items():
        if filters[key] not in allowed:
            filters[key] = DEFAULTS[key]
    if _norm(query.get("only_without_my_offer")) in {"true", "1", "on"}:
        filters["offer"] = "unsent"
    elif any(_norm(query.get(key)) in {"true", "1", "on"} for key in ("onlyWithMyOffers", "withOffers")):
        filters["offer"] = "sent"
    filters["page_size"] = filters["page_size"] if filters["page_size"] in {"25", "50", "100"} else "25"
    errors = []
    for key in DATE_FIELDS:
        if filters[key] and (order_datetime(filters[key]) is None or len(filters[key]) != 10):
            errors.append("Введите даты в формате ГГГГ-ММ-ДД.")
    for key in NUMBER_FIELDS:
        if filters[key] and (_number(filters[key]) is None or _number(filters[key]) < 0):
            errors.append("Количество, число конкурентов и отсрочка должны быть неотрицательными числами.")
    for lower, upper in (("creationDateFrom", "creationDateTo"), ("finishDateFrom", "finishDateTo")):
        if filters[lower] and filters[upper] and filters[lower] > filters[upper]:
            errors.append("Начало периода не может быть позже его окончания.")
    for lower, upper in (("min_positions", "max_positions"), ("delayFrom", "delayTo"), ("quantity_min", "quantity_max")):
        if _number(filters[lower]) is not None and _number(filters[upper]) is not None and _number(filters[lower]) > _number(filters[upper]):
            errors.append("Минимальное значение не может превышать максимальное.")
    if (filters["quantity_min"] or filters["quantity_max"]) and not filters["quantity_unit"]:
        errors.append("Для фильтра количества выберите единицу измерения.")
    return filters, list(dict.fromkeys(errors))


def matching_quantity_rows(item, filters, selected_categories=None):
    """Every product constraint must match the same source line."""
    lower, upper = _number(filters["quantity_min"]), _number(filters["quantity_max"])
    categories = selected_categories or set(filters["category_id"])
    keyword = _norm(filters["keyword"])
    return [row for row in item["quantity_rows"] if
            row["unit"] == filters["quantity_unit"]
            and (not keyword or keyword in row["search"])
            and (not categories or row["category"] in categories)
            and (lower is None or (row["quantity"] is not None and row["quantity"] >= lower))
            and (upper is None or (row["quantity"] is not None and row["quantity"] <= upper))]


def matches(item, filters, selected_categories=None):
    for key, field in (("keyword", "search"), ("title", "title"), ("customer", "customer"), ("inn", "inn"), ("region", "region")):
        if filters[key] and _norm(filters[key]) not in _norm(item[field]):
            return False
    if filters["category_id"] and not set(item["categories"]).intersection(selected_categories or set(filters["category_id"])):
        return False
    if filters["order_id"] and filters["order_id"] not in {item["order_id"], item["reference"]}:
        return False
    if filters["quantity_unit"]:
        if not matching_quantity_rows(item, filters, selected_categories):
            return False
    for key in ("source", "status"):
        if filters[key] != "all" and filters[key] != item[key]:
            return False
    if filters["viewed"] != "all" and item["viewed"] != (filters["viewed"] == "yes"):
        return False
    if filters["offer"] != "all" and item["sent"] != (filters["offer"] == "sent"):
        return False
    if filters["payment"] == "prepayment" and not (item["delay"] == 0 or (item["delay"] is None and ("предоплат" in _norm(item["terms"]) or "без отсроч" in _norm(item["terms"])))):
        return False
    if filters["payment"] == "delay" and not (item["delay"] is not None and item["delay"] > 0):
        return False
    for key, field, minimum in (("min_positions", "positions", True), ("max_positions", "positions", False),
                                ("max_competitors", "competitors", False), ("delayFrom", "delay", True), ("delayTo", "delay", False)):
        limit = _number(filters[key])
        if limit is not None and (item[field] is None or (item[field] < limit if minimum else item[field] > limit)):
            return False
    for field, lower, upper in (("date", "creationDateFrom", "creationDateTo"), ("finish", "finishDateFrom", "finishDateTo")):
        value = item[field].date().isoformat() if item[field] else ""
        if (filters[lower] or filters[upper]) and (not value or (filters[lower] and value < filters[lower]) or (filters[upper] and value > filters[upper])):
            return False
    return True


def select_page(rows, query, reference=None, reference_updated=""):
    filters, errors = filters_from_query(query)
    saved = [describe(row) for row in rows]
    reference = reference or []
    choices = category_choices(saved, reference)
    unit_choices = {unit: unit for unit in COMMON_UNITS}
    for item in saved:
        for row in item["quantity_rows"]:
            if row["unit"]:
                unit_choices.setdefault(row["unit"], row["unit"])
    if filters["quantity_unit"] and filters["quantity_unit"] not in unit_choices:
        errors.append("Выберите единицу измерения из списка.")
    # Old links may contain a category name. Accept only one exact known name,
    # never the previous arbitrary substring search.
    if filters["category"] and not filters["category_id"]:
        names = {row["id"]: row["name"] for row in reference}
        for item in saved:
            for key, name in item["categories"].items():
                names.setdefault(key, name)
        keys = [key for key, name in names.items() if _norm(name) == _norm(filters["category"])]
        if len(keys) == 1:
            filters["category_id"], filters["category"] = (keys[0],), ""
        else:
            errors.append("Выберите категорию Закупай из списка: свободный ввод не поддерживается.")
    if any(key not in choices for key in filters["category_id"]):
        errors.append("Одна из выбранных рубрик недоступна. Обновите категории или снимите её отметку.")
    if filters["category_id"]:
        filters["category"] = ""
    selected = set()
    for key in filters["category_id"]:
        selected.update(category_descendants(key, reference))
    filtered = [] if errors else [item for item in saved if matches(item, filters, selected)]
    filtered.sort(key=lambda item: (item["date"].timestamp() if item["date"] else float("-inf"), item["id"]), reverse=True)
    size = int(filters["page_size"])
    pages = max(1, (len(filtered) + size - 1) // size)
    try:
        page = min(pages, max(1, int(query.get("page", "1"))))
    except (ValueError, TypeError):
        page = 1
    visible = filtered[(page - 1) * size:page * size]
    for item in visible:
        item["matching_quantity_rows"] = matching_quantity_rows(item, filters, selected) if filters["quantity_unit"] else []
    return {
        "filters": filters, "errors": errors, "total": len(saved), "matched": len(filtered),
        "page": page, "pages": pages, "size": size,
        "items": visible,
        "category_choices": choices, "category_reference_updated": reference_updated,
        "unit_choices": unit_choices,
    }


CATEGORY_PICKER_CSS = """
.category-picker{border:1px solid #c9cdd3;border-radius:9px;padding:14px;margin:0 0 18px;background:#fafbfc}
.category-picker summary{color:#202124}.category-help{color:#5f6368;font-size:14px;margin:8px 0 12px}
.category-tools{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin-bottom:12px}
.category-tools label{flex:1;min-width:180px;font-size:14px;color:#5f6368}
.category-tools input{display:block;width:100%;box-sizing:border-box;padding:9px;margin-top:6px;border:1px solid #c9cdd3;border-radius:7px}
.category-clear{border:1px solid #c9cdd3;background:#fff;color:#174ea6;padding:9px 12px;border-radius:7px;cursor:pointer}
.category-options{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,290px),1fr));max-height:280px;overflow:auto;gap:4px;padding:4px;background:#fff;border:1px solid #e1e5eb;border-radius:7px}
.category-option{display:flex;gap:9px;align-items:flex-start;padding:10px;cursor:pointer;font-size:14px;overflow-wrap:anywhere}
.category-option:has(input:checked){background:#fff3e0;border-radius:5px;color:#70430b}
.category-option input{flex:0 0 auto;width:18px;height:18px;margin:0;accent-color:#1a73e8}
.category-picker [hidden]{display:none!important}
.filter-cell{padding:9px;border:1px solid transparent;border-radius:8px}
.filters .filter-cell.active-filter,.category-picker.active-filter{background:#fff3e0;border-color:#efc18d}
.filters .active-filter input,.filters .active-filter select{background:#fff8ef;border-color:#dfb274}
.quantity-help{color:#5f6368;font-size:14px;margin:0 0 18px}
"""


def category_picker_script(nonce):
    # Only UI filtering happens here. The checkbox form also works without JS.
    return f"<script nonce='{html.escape(nonce, quote=True)}'>" + """
(() => {
  const picker = document.getElementById('category-picker');
  if (!picker) return;
  const rows = Array.from(picker.querySelectorAll('.category-option'));
  const boxes = rows.map(row => row.querySelector('input'));
  const search = document.getElementById('category-search');
  const count = document.getElementById('category-count');
  const clear = document.getElementById('category-clear');
  const norm = text => text.toLocaleLowerCase('ru').replaceAll('ё', 'е').trim();
  const update = () => {
    const selected = boxes.filter(box => box.checked).length;
    count.textContent = selected ? 'Выбрано: ' + selected : 'Все рубрики';
    clear.disabled = selected === 0;
    picker.classList.toggle('active-filter', selected > 0);
  };
  picker.querySelector('.category-tools').hidden = false;
  boxes.forEach(box => box.addEventListener('change', update));
  search.addEventListener('input', () => {
    const words = norm(search.value).split(/\\s+/).filter(Boolean);
    rows.forEach(row => row.hidden = !words.every(word => norm(row.textContent).includes(word)));
    document.getElementById('category-no-matches').hidden = rows.some(row => !row.hidden);
  });
  clear.addEventListener('click', () => {
    boxes.forEach(box => box.checked = false);
    update();
  });
  update();
})();
</script>"""


def category_picker(state):
    selected = set(state["filters"]["category_id"])
    choices = dict(state["category_choices"])
    for key in selected:
        choices.setdefault(key, "Недоступная рубрика — снимите отметку")
    options = "".join(
        f"<label class='category-option'><input type='checkbox' name='category_id' value='{html.escape(key, quote=True)}'"
        f"{' checked' if key in selected else ''}><span>{html.escape(label)}</span></label>"
        for key, label in sorted(choices.items(), key=lambda pair: (pair[0] not in selected, pair[1].casefold(), pair[0]))
    )
    count = f"Выбрано: {len(selected)}" if selected else "Все рубрики"
    return (
        f"<details class='category-picker{' active-filter' if selected else ''}' id='category-picker' open>"
        f"<summary>Рубрики Закупай · <span id='category-count'>{count}</span></summary>"
        "<p class='category-help'>Отметьте одну или несколько рубрик. Заявка попадёт в список, если содержит товар хотя бы из одной выбранной рубрики или её подкатегорий. Без отметок — все рубрики.</p>"
        "<div class='category-tools' hidden><label>Найти рубрику<input type='search' id='category-search' placeholder='Название рубрики' autocomplete='off'></label>"
        "<button class='category-clear' id='category-clear' type='button'>Снять выбор рубрик</button></div>"
        f"<div class='category-options' role='group' aria-label='Рубрики Закупай'>{options}</div>"
        + ("" if options else "<p>Рубрики пока не загружены. Нажмите «Обновить категории».</p>")
        + "<p id='category-no-matches' hidden role='status'>Рубрики не найдены. Измените текст поиска.</p></details>"
    )


def filter_form(state):
    filters = state["filters"]
    def label_start(key):
        active = " active-filter" if filters[key] != DEFAULTS[key] else ""
        return f"<label class='filter-cell{active}' data-filter-default='{html.escape(str(DEFAULTS[key]), quote=True)}'>"
    def field(key, label, kind="text"):
        minimum = " min='0'" if kind == "number" else ""
        if key in {"quantity_min", "quantity_max"}:
            minimum += " step='any' inputmode='decimal'"
        return f"{label_start(key)}{label}<input type='{kind}' name='{key}' value='{html.escape(filters[key], quote=True)}'{minimum}></label>"
    def select(key, label, options):
        values = "".join(f"<option value='{html.escape(value, quote=True)}' {'selected' if filters[key] == value else ''}>{html.escape(text)}</option>" for value, text in options.items())
        return f"{label_start(key)}{label}<select name='{key}'>{values}</select></label>"
    primary = (
        field("keyword", "Поиск по заявке и товарам") + field("title", "Название заявки") + field("order_id", "Номер заявки") +
        field("customer", "Заказчик") + field("inn", "ИНН заказчика") +
        select("source", "Источник", {"all": "Все", "zakupay": "Закупай", "manual": "Ручной ввод"}) +
        select("status", "Статус обработки", {"all": "Все", **STATUSES}) +
        select("viewed", "Просмотр", {"all": "Все", "no": "Не просмотрены", "yes": "Просмотрены"}) +
        select("offer", "Моё предложение", {"all": "Все", "unsent": "Не отправлено", "sent": "Отправлено"}) +
        field("creationDateFrom", "Дата заявки от", "date") + field("creationDateTo", "Дата заявки до", "date") +
        select("page_size", "На странице", {"25": "25", "50": "50", "100": "100"})
    )
    advanced = (
        field("region", "Регион") +
        select("payment", "Условия оплаты", {"all": "Все", "prepayment": "Предоплата / без отсрочки", "delay": "Отсрочка"}) +
        field("min_positions", "Минимум позиций", "number") + field("max_positions", "Максимум позиций", "number") +
        field("max_competitors", "Не более конкурентов", "number") +
        field("finishDateFrom", "Поставка от", "date") + field("finishDateTo", "Поставка до", "date") +
        field("delayFrom", "Отсрочка от, дней", "number") + field("delayTo", "Отсрочка до, дней", "number")
    )
    quantity = (
        field("quantity_min", "Количество от", "number") + field("quantity_max", "Количество до", "number") +
        select("quantity_unit", "Единица измерения", {"": "Без ограничения", **state["unit_choices"],
               **({filters["quantity_unit"]: "Недоступная единица — измените выбор"}
                  if filters["quantity_unit"] and filters["quantity_unit"] not in state["unit_choices"] else {})})
    )
    errors = "".join(f"<p class='filter-error' role='alert'>{html.escape(error)}</p>" for error in state["errors"])
    category_note = (
        "Категории загружены из справочника Закупай. Выбор раздела включает его подкатегории."
        if state["category_reference_updated"] else
        "В списке категорий пока только категории сохранённых заявок. Нажмите «Обновить категории», чтобы загрузить справочник Закупай."
    )
    return (
        f"<section class='filter-panel' id='request-filters'><h2>Фильтры заявок</h2>"
        "<p>Чтобы показать только заявки Закупай, выберите источник «Закупай». Фильтры применяются к сохранённым заявкам.</p>"
        f"<p>{category_note}</p>"
        f"<form method='get' action='{URL}'><div class='filters'>{primary}</div>"
        + category_picker(state)
        + f"<h3>Количество товара</h3><div class='filters'>{quantity}</div>"
        + "<p class='quantity-help'>При выборе количества текст из поля «Поиск по заявке и товарам», рубрика, единица и диапазон проверяются в одной товарной строке. Например: «керамогранит», от 100 м² — количество других товаров не учитывается. Границы включены; строки не суммируются, единицы не пересчитываются.</p>"
        + f"<details open><summary>Регион, оплата и другие параметры</summary><div class='filters'>{advanced}</div></details>"
        f"{errors}<div class='actions'><button class='button' type='submit'>Применить фильтры</button>"
        f"<a class='button secondary' href='{URL}'>Сбросить фильтры</a></div></form></section>"
        f"<p class='list-count' role='status'>Сохранено: <b>{state['total']}</b> · По фильтрам: <b>{state['matched']}</b> · Свежие даты сверху</p>"
    )


def filter_highlight_script(nonce):
    return f"<script nonce='{html.escape(nonce, quote=True)}'>" + """
(() => {
  const panel = document.getElementById('request-filters');
  if (!panel) return;
  panel.querySelectorAll('.filter-cell').forEach(cell => {
    const control = cell.querySelector('input, select');
    const update = () => cell.classList.toggle('active-filter', control.value.trim() !== cell.dataset.filterDefault);
    control.addEventListener('input', update);
    control.addEventListener('change', update);
    update();
  });
})();
</script>"""


def pagination(state):
    params = {key: value for key, value in state["filters"].items() if value != DEFAULTS[key]}
    def link(page, text):
        target = URL + "?" + urlencode({**params, "page": page}, doseq=True)
        return f"<a class='button secondary' href='{html.escape(target, quote=True)}'>{text}</a>"
    previous = link(state["page"] - 1, "← Предыдущая") if state["page"] > 1 else ""
    following = link(state["page"] + 1, "Следующая →") if state["page"] < state["pages"] else ""
    return f"<nav class='pager' aria-label='Страницы заявок'>{previous}<span>Страница {state['page']} из {state['pages']}</span>{following}</nav>"
