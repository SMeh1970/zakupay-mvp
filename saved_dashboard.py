"""Filters and pagination for persisted requests, without supplier/API lookups."""

import html
import json
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from invoice_generator import normalize_customer


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
    "region": "", "category": "", "payment": "all",
    "creationDateFrom": "", "creationDateTo": "",
    "finishDateFrom": "", "finishDateTo": "",
    "min_positions": "", "max_positions": "", "max_competitors": "",
    "delayFrom": "", "delayTo": "", "page_size": "25",
}
DATE_FIELDS = {"creationDateFrom", "creationDateTo", "finishDateFrom", "finishDateTo"}
NUMBER_FIELDS = {"min_positions", "max_positions", "max_competitors", "delayFrom", "delayTo"}


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


def _number(value):
    try:
        return float(value) if value is not None and str(value).strip() else None
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
    category = " | ".join(_name(item.get("category")) for item in items)
    title = str(order.get("name") or result.get("order_name") or row["subject"] or "")
    search_text = " ".join([title, customer["name"], str(row["order_id"]), str(result.get("manual_reference") or "")] +
                           [str(item.get("goodName") or item.get("name") or "") + " " + str(item.get("comment") or "") for item in items] +
                           [str(item.get("requested_name") or "") for item in legacy_items])
    competitors = [_number(item.get("companiesWithOffersCount")) for item in items]
    known_competitors = [number for number in competitors if number is not None]
    return {
        "id": int(row["id"]), "order_id": str(row["order_id"]),
        "reference": str(result.get("manual_reference") or ""), "title": title,
        "customer": customer["name"], "inn": customer["inn"],
        "source": "manual" if row["event_type"] == "manual_order" else "zakupay",
        "status": "offer_created" if result.get("live_offer_created") else row["status"],
        "viewed": bool(row["viewed_at"]), "sent": sent,
        "date": order_datetime(order.get("creationDate") or order.get("publicDate") or result.get("order_creation_date")),
        "finish": order_datetime(order.get("finishDate")),
        "positions": len(items) if items else summary.get("positions") or len(legacy_items),
        "region": _name(order.get("region")), "category": category,
        "delay": _number(order.get("delay")), "terms": str(order.get("paymentTerms") or ""),
        "competitors": max(known_competitors) if known_competitors else None,
        "search": search_text,
    }


def filters_from_query(query):
    filters = {key: str(query.get(key, default)).strip() for key, default in DEFAULTS.items()}
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
    for lower, upper in (("min_positions", "max_positions"), ("delayFrom", "delayTo")):
        if _number(filters[lower]) is not None and _number(filters[upper]) is not None and _number(filters[lower]) > _number(filters[upper]):
            errors.append("Минимальное значение не может превышать максимальное.")
    return filters, list(dict.fromkeys(errors))


def matches(item, filters):
    for key, field in (("keyword", "search"), ("title", "title"), ("customer", "customer"), ("inn", "inn"), ("region", "region"), ("category", "category")):
        if filters[key] and _norm(filters[key]) not in _norm(item[field]):
            return False
    if filters["order_id"] and filters["order_id"] not in {item["order_id"], item["reference"]}:
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


def select_page(rows, query):
    filters, errors = filters_from_query(query)
    saved = [describe(row) for row in rows]
    filtered = [] if errors else [item for item in saved if matches(item, filters)]
    filtered.sort(key=lambda item: (item["date"].timestamp() if item["date"] else float("-inf"), item["id"]), reverse=True)
    size = int(filters["page_size"])
    pages = max(1, (len(filtered) + size - 1) // size)
    try:
        page = min(pages, max(1, int(query.get("page", "1"))))
    except (ValueError, TypeError):
        page = 1
    return {
        "filters": filters, "errors": errors, "total": len(saved), "matched": len(filtered),
        "page": page, "pages": pages, "size": size,
        "items": filtered[(page - 1) * size:page * size],
    }


def filter_form(state):
    filters = state["filters"]
    def field(key, label, kind="text"):
        minimum = " min='0'" if kind == "number" else ""
        return f"<label>{label}<input type='{kind}' name='{key}' value='{html.escape(filters[key], quote=True)}'{minimum}></label>"
    def select(key, label, options):
        values = "".join(f"<option value='{value}' {'selected' if filters[key] == value else ''}>{text}</option>" for value, text in options.items())
        return f"<label>{label}<select name='{key}'>{values}</select></label>"
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
    advanced_keys = {"region", "category", "payment", "finishDateFrom", "finishDateTo", *NUMBER_FIELDS}
    expanded = any(filters[key] != DEFAULTS[key] for key in advanced_keys)
    advanced = (
        field("region", "Регион") + field("category", "Категория товаров") +
        select("payment", "Условия оплаты", {"all": "Все", "prepayment": "Предоплата / без отсрочки", "delay": "Отсрочка"}) +
        field("min_positions", "Минимум позиций", "number") + field("max_positions", "Максимум позиций", "number") +
        field("max_competitors", "Не более конкурентов", "number") +
        field("finishDateFrom", "Поставка от", "date") + field("finishDateTo", "Поставка до", "date") +
        field("delayFrom", "Отсрочка от, дней", "number") + field("delayTo", "Отсрочка до, дней", "number")
    )
    errors = "".join(f"<p class='filter-error' role='alert'>{html.escape(error)}</p>" for error in state["errors"])
    return (
        f"<section class='filter-panel'><form method='get' action='{URL}'><div class='filters'>{primary}</div>"
        f"<details {'open' if expanded else ''}><summary>Дополнительные фильтры</summary><div class='filters'>{advanced}</div></details>"
        f"{errors}<div class='actions'><button class='button' type='submit'>Применить фильтры</button>"
        f"<a class='button secondary' href='{URL}'>Сбросить фильтры</a></div></form></section>"
        f"<p class='list-count' role='status'>Сохранено: <b>{state['total']}</b> · По фильтрам: <b>{state['matched']}</b> · Свежие даты сверху</p>"
    )


def pagination(state):
    params = {key: value for key, value in state["filters"].items() if value != DEFAULTS[key]}
    def link(page, text):
        target = URL + "?" + urlencode({**params, "page": page})
        return f"<a class='button secondary' href='{html.escape(target, quote=True)}'>{text}</a>"
    previous = link(state["page"] - 1, "← Предыдущая") if state["page"] > 1 else ""
    following = link(state["page"] + 1, "Следующая →") if state["page"] < state["pages"] else ""
    return f"<nav class='pager' aria-label='Страницы заявок'>{previous}<span>Страница {state['page']} из {state['pages']}</span>{following}</nav>"
