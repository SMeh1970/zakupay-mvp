import html
import os
import time
from urllib.parse import urlencode

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from mcp_server import install_mcp
from security import install_security

app = FastAPI(
    title="Zakupay MVP",
    description="Рабочая панель поставщика для анализа заявок Закупай",
    version="0.6.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

ZAKUPAY_API_KEY = os.getenv("ZAKUPAY_API_KEY")
ZAKUPAY_BASE_URL = os.getenv("ZAKUPAY_BASE_URL", "https://prodavay.sel-be.ru")
CACHE_TTL_SECONDS = 60
_orders_cache = {"ts": 0.0, "key": "", "orders": []}


def esc(value):
    return "" if value is None else html.escape(str(value))


def zakupay_headers():
    if not ZAKUPAY_API_KEY:
        raise HTTPException(status_code=500, detail="Не настроена переменная ZAKUPAY_API_KEY")
    return {"ZakupayToken": ZAKUPAY_API_KEY, "Accept": "application/json"}


def clean_params(params):
    result = {}
    for key, value in params.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            result[key] = str(value).lower()
        else:
            result[key] = value
    return result


def request_orders_page(page=1, page_size=100, api_filters=None):
    url = f"{ZAKUPAY_BASE_URL}/api/v1/orders"
    params = {"status": "actual", "isoDate": "true", "page": page, "pageSize": page_size}
    if api_filters:
        params.update(clean_params(api_filters))
    try:
        response = requests.get(url, headers=zakupay_headers(), params=params, timeout=30)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Ошибка соединения с Закупай: {exc}")
    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="Закупай отклонил токен")
    if response.status_code == 403:
        raise HTTPException(status_code=403, detail="Недостаточно прав для получения заявок")
    try:
        data = response.json()
    except ValueError:
        raise HTTPException(status_code=response.status_code, detail=response.text[:3000])
    if not response.ok:
        raise HTTPException(status_code=response.status_code, detail=data)
    return data


def fetch_all_orders(force=False, api_filters=None):
    api_filters = clean_params(api_filters or {})
    cache_key = urlencode(sorted(api_filters.items()))
    now = time.time()
    if (
        not force
        and _orders_cache["orders"]
        and _orders_cache["key"] == cache_key
        and now - _orders_cache["ts"] < CACHE_TTL_SECONDS
    ):
        return _orders_cache["orders"]

    all_orders, seen_ids = [], set()
    for page in range(1, 101):
        data = request_orders_page(page=page, page_size=100, api_filters=api_filters)
        batch = data.get("orders") or []
        if not batch:
            break
        new_count = 0
        for order in batch:
            oid = order.get("id")
            if oid in seen_ids:
                continue
            seen_ids.add(oid)
            all_orders.append(order)
            new_count += 1
        if new_count == 0:
            break
        returned_page_size = data.get("pageSize")
        if returned_page_size and len(batch) < returned_page_size:
            break
        if len(batch) < 10:
            break

    _orders_cache.update({"ts": now, "key": cache_key, "orders": all_orders})
    return all_orders


def has_my_offer(order):
    if order.get("offers"):
        return True
    return any(item.get("offerIds") for item in order.get("orderItems") or [])


def max_competitors(order):
    vals = [
        item.get("companiesWithOffersCount")
        for item in order.get("orderItems") or []
        if isinstance(item.get("companiesWithOffersCount"), (int, float))
    ]
    return max(vals) if vals else 0


def order_categories(order):
    names = []
    for item in order.get("orderItems") or []:
        name = ((item.get("category") or {}).get("name"))
        if name and name not in names:
            names.append(name)
    return names


def format_delay(delay):
    if delay is None:
        return "—"
    return "Предоплата / без отсрочки" if delay == 0 else f"{delay} дней"


def filter_orders(orders, payment="all", region="", category="", min_positions=0, max_competitors_value=None, only_without_my_offer=False):
    result = []
    rq, cq = region.strip().lower(), category.strip().lower()
    for order in orders:
        delay = order.get("delay")
        items = order.get("orderItems") or []
        region_name = ((order.get("region") or {}).get("name") or "").lower()
        categories = " | ".join(order_categories(order)).lower()
        if payment == "prepayment" and delay != 0:
            continue
        if payment == "delay" and (delay is None or delay == 0):
            continue
        if rq and rq not in region_name:
            continue
        if cq and cq not in categories:
            continue
        if len(items) < min_positions:
            continue
        if max_competitors_value is not None and max_competitors(order) > max_competitors_value:
            continue
        if only_without_my_offer and has_my_offer(order):
            continue
        result.append(order)
    return result


def compact_order(order):
    customer, region = order.get("customer") or {}, order.get("region") or {}
    items = []
    for item in order.get("orderItems") or []:
        items.append({
            "id": item.get("id"),
            "name": item.get("goodName"),
            "quantity": item.get("count"),
            "unit": (item.get("unit") or {}).get("name"),
            "category": (item.get("category") or {}).get("name"),
            "competitors": item.get("companiesWithOffersCount"),
            "best_offer_item": item.get("bestOfferItem"),
            "comment": item.get("comment"),
            "status": item.get("actualityStatusDesc"),
        })
    return {
        "id": order.get("id"), "name": order.get("name"),
        "customer": customer.get("shortName") or customer.get("name"),
        "region": region.get("name"), "delay_days": order.get("delay"),
        "payment_type": "prepayment_or_no_delay" if order.get("delay") == 0 else "delay",
        "finish_date": order.get("finishDate"), "delivery_address": order.get("deliveryAddress"),
        "positions_count": len(items), "max_competitors": max_competitors(order),
        "has_my_offer": has_my_offer(order), "categories": order_categories(order), "items": items,
    }


def api_filter_dict(**kwargs):
    allowed = {
        "company", "category_id", "creationDateFrom", "creationDateTo", "finishDateFrom", "finishDateTo",
        "publicDateFrom", "publicDateTo", "showNotInteresting", "onlyNotInteresting", "showAllCompanies",
        "fromAllCompanies", "showAllRegions", "allRegions", "showAllCategories", "allCategories",
        "offersState", "onlyWithMyOffers", "showWithMyOffers", "withOffers", "tookInWork", "region_id",
        "ignoreKeywordFilter", "delayFrom", "delayTo", "inn", "onlyNotEnough", "updateTimeFrom",
        "updateTimeTo", "senderId"
    }
    mapping = {"category_id": "category", "region_id": "region"}
    result = {}
    for key, value in kwargs.items():
        if key in allowed and value not in (None, ""):
            result[mapping.get(key, key)] = value
    return result


@app.get("/")
def root():
    return {"message": "Закрытая панель «Закупай» работает", "version": "0.6.0", "login": "/login", "mcp": "/mcp", "access": "private"}


@app.get("/health")
def health():
    return {"ok": True, "version": "0.6.0"}


@app.get("/analysis/orders")
def analysis_orders(
    payment: str = Query("all", pattern="^(all|prepayment|delay)$"), region: str = "", category: str = "",
    min_positions: int = 0, max_competitors_value: int | None = Query(None, alias="max_competitors"),
    only_without_my_offer: bool = False, refresh: bool = False,
    creationDateFrom: str = "", creationDateTo: str = "", finishDateFrom: str = "", finishDateTo: str = "",
    publicDateFrom: str = "", publicDateTo: str = "", delayFrom: int | None = None, delayTo: int | None = None,
    offersState: str = "", onlyWithMyOffers: bool | None = None, onlyNotEnough: bool | None = None,
    tookInWork: bool | None = None, inn: str = "", senderId: int | None = None,
):
    af = api_filter_dict(**locals())
    orders = fetch_all_orders(force=refresh, api_filters=af)
    filtered = filter_orders(orders, payment, region, category, min_positions, max_competitors_value, only_without_my_offer)
    return {"source": "REAL_ZAKUPAY", "api_filters": af, "total_actual": len(orders), "filtered_count": len(filtered), "orders": [compact_order(x) for x in filtered]}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    payment: str = "all", region: str = "", category: str = "", min_positions: int = 0,
    max_competitors_value: int | None = Query(None, alias="max_competitors"), only_without_my_offer: bool = False,
    page: int = 1, page_size: int = 50, refresh: bool = False,
    creationDateFrom: str = "", creationDateTo: str = "", finishDateFrom: str = "", finishDateTo: str = "",
    publicDateFrom: str = "", publicDateTo: str = "", updateTimeFrom: str = "", updateTimeTo: str = "",
    delayFrom: int | None = None, delayTo: int | None = None, offersState: str = "",
    onlyWithMyOffers: bool | None = None, showWithMyOffers: bool | None = None, withOffers: bool | None = None,
    onlyNotEnough: bool | None = None, tookInWork: bool | None = None, inn: str = "", senderId: int | None = None,
    company: int | None = None, category_id: int | None = None, region_id: int | None = None,
    showNotInteresting: bool | None = None, onlyNotInteresting: bool | None = None,
    ignoreKeywordFilter: bool | None = None, showAllCompanies: bool | None = None, fromAllCompanies: bool | None = None,
    showAllRegions: bool | None = None, allRegions: bool | None = None, showAllCategories: bool | None = None,
    allCategories: bool | None = None,
):
    page, page_size = max(1, page), min(max(page_size, 10), 100)
    local_vars = locals().copy()
    af = api_filter_dict(**local_vars)
    all_orders = fetch_all_orders(force=refresh, api_filters=af)
    filtered = filter_orders(all_orders, payment, region, category, min_positions, max_competitors_value, only_without_my_offer)
    total_pages = max(1, (len(filtered) + page_size - 1) // page_size)
    page = min(page, total_pages)
    visible = filtered[(page - 1) * page_size: page * page_size]

    rows = ""
    for order in visible:
        oid = order.get("id")
        customer, robj = order.get("customer") or {}, order.get("region") or {}
        items = order.get("orderItems") or []
        rows += f"<tr><td><a href='/dashboard/order/{oid}'>{oid}</a></td><td>{esc(order.get('name'))}</td><td>{esc(customer.get('shortName') or customer.get('name'))}</td><td>{esc(robj.get('name'))}</td><td>{esc(format_delay(order.get('delay')))}</td><td>{esc(order.get('finishDate'))}</td><td>{len(items)}</td><td>{max_competitors(order)}</td><td>{'Да' if has_my_offer(order) else 'Нет'}</td></tr>"

    qbase = {k: v for k, v in local_vars.items() if k not in {"page", "refresh", "local_vars", "af", "all_orders", "filtered", "visible", "rows", "total_pages", "qbase"} and v not in (None, "", False)}
    qbase["page_size"] = page_size
    prev_link = f"<a class='btn' href='/dashboard?{urlencode(dict(qbase, page=page-1))}'>← Предыдущая</a>" if page > 1 else ""
    next_link = f"<a class='btn' href='/dashboard?{urlencode(dict(qbase, page=page+1))}'>Следующая →</a>" if page < total_pages else ""

    def ck(v): return "checked" if v else ""
    def sv(v): return "" if v is None else esc(v)
    payment_options = "".join(f"<option value='{k}' {'selected' if payment == k else ''}>{v}</option>" for k, v in {"all":"Все условия","prepayment":"Только предоплата / без отсрочки","delay":"Только отсрочка"}.items())
    state_options = "".join(f"<option value='{k}' {'selected' if offersState == k else ''}>{v}</option>" for k,v in {"":"Любой","ACCEPTED":"ACCEPTED","DECLINED":"DECLINED","WAITING":"WAITING"}.items())

    page_html = f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Zakupay MVP</title>
<style>body{{font-family:Arial,sans-serif;margin:24px;background:#f5f5f5;color:#222}}h1{{margin-bottom:4px}}.sub{{color:#666;margin-bottom:18px}}.card{{background:#fff;border-radius:12px;padding:18px;margin-bottom:18px;box-shadow:0 2px 8px rgba(0,0,0,.08)}}.filters{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;align-items:end}}label{{font-size:12px;color:#666;display:block;margin-bottom:5px}}input,select{{width:100%;box-sizing:border-box;padding:9px;border:1px solid #ccc;border-radius:7px}}.check{{display:flex;gap:8px;align-items:center;padding:9px 0}}.check input{{width:auto}}button,.btn{{background:#222;color:#fff;border:0;padding:10px 14px;border-radius:8px;text-decoration:none;cursor:pointer;display:inline-block}}.btn.secondary{{background:#666}}details{{margin-top:14px;border-top:1px solid #eee;padding-top:12px}}summary{{cursor:pointer;font-weight:bold}}.note{{font-size:12px;color:#8a5b00;background:#fff7df;padding:8px;border-radius:7px;margin:10px 0}}.stats{{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}}.pill{{background:#222;color:#fff;border-radius:18px;padding:6px 12px}}table{{width:100%;border-collapse:collapse;font-size:13px}}th{{background:#eee;text-align:left;padding:9px;position:sticky;top:0}}td{{padding:9px;border-bottom:1px solid #eee;vertical-align:top}}a{{color:#4c39d4;font-weight:bold;text-decoration:none}}.pager{{display:flex;justify-content:space-between;margin-top:14px}}.tablewrap{{overflow:auto;max-height:70vh}}</style></head><body>
<div style='float:right'><a href='/logout'>Выйти</a></div><h1>Закупай — входящие заявки</h1><div class='sub'>Фильтры API Синтеки + локальные фильтры панели.</div>
<div class='card'><form method='get' action='/dashboard'>
<div class='filters'><div><label>Оплата</label><select name='payment'>{payment_options}</select></div><div><label>Регион содержит</label><input name='region' value='{esc(region)}'></div><div><label>Категория содержит</label><input name='category' value='{esc(category)}'></div><div><label>Минимум позиций</label><input type='number' min='0' name='min_positions' value='{min_positions}'></div><div><label>Не более конкурентов</label><input type='number' min='0' name='max_competitors' value='{sv(max_competitors_value)}'></div><div class='check'><input type='checkbox' name='only_without_my_offer' value='true' {ck(only_without_my_offer)}><span>Только без моего предложения</span></div></div>
<details><summary>Расширенные фильтры Закупай</summary><div class='note'>Поля с пометкой «платная лицензия» могут игнорироваться Закупай на бесплатном тарифе.</div><div class='filters'>
<div><label>Создана от</label><input name='creationDateFrom' value='{esc(creationDateFrom)}' placeholder='2026-08-01'></div><div><label>Создана до</label><input name='creationDateTo' value='{esc(creationDateTo)}' placeholder='2026-08-31'></div><div><label>Поставка от</label><input name='finishDateFrom' value='{esc(finishDateFrom)}'></div><div><label>Поставка до</label><input name='finishDateTo' value='{esc(finishDateTo)}'></div><div><label>Размещена от</label><input name='publicDateFrom' value='{esc(publicDateFrom)}'></div><div><label>Размещена до</label><input name='publicDateTo' value='{esc(publicDateTo)}'></div><div><label>Обновлена от</label><input name='updateTimeFrom' value='{esc(updateTimeFrom)}'></div><div><label>Обновлена до</label><input name='updateTimeTo' value='{esc(updateTimeTo)}'></div>
<div><label>Отсрочка от, дней</label><input type='number' name='delayFrom' value='{sv(delayFrom)}'></div><div><label>Отсрочка до, дней</label><input type='number' name='delayTo' value='{sv(delayTo)}'></div><div><label>Статус моего счета</label><select name='offersState'>{state_options}</select></div><div><label>ИНН заказчика — платная лицензия</label><input name='inn' value='{esc(inn)}'></div><div><label>ID заявки покупателя</label><input type='number' name='senderId' value='{sv(senderId)}'></div><div><label>ID компании — платная лицензия</label><input type='number' name='company' value='{sv(company)}'></div><div><label>ID категории</label><input type='number' name='category_id' value='{sv(category_id)}'></div><div><label>ID региона</label><input type='number' name='region_id' value='{sv(region_id)}'></div>
<div class='check'><input type='checkbox' name='onlyWithMyOffers' value='true' {ck(onlyWithMyOffers)}><span>Только с моими счетами</span></div><div class='check'><input type='checkbox' name='showWithMyOffers' value='true' {ck(showWithMyOffers)}><span>Показывать с моими счетами</span></div><div class='check'><input type='checkbox' name='withOffers' value='true' {ck(withOffers)}><span>С моими счетами</span></div><div class='check'><input type='checkbox' name='onlyNotEnough' value='true' {ck(onlyNotEnough)}><span>Мало счетов</span></div><div class='check'><input type='checkbox' name='tookInWork' value='true' {ck(tookInWork)}><span>Взяты в работу</span></div><div class='check'><input type='checkbox' name='showNotInteresting' value='true' {ck(showNotInteresting)}><span>Показывать неинтересные</span></div><div class='check'><input type='checkbox' name='onlyNotInteresting' value='true' {ck(onlyNotInteresting)}><span>Только неинтересные</span></div><div class='check'><input type='checkbox' name='ignoreKeywordFilter' value='true' {ck(ignoreKeywordFilter)}><span>Игнорировать ключевые слова</span></div>
<div class='check'><input type='checkbox' name='showAllCompanies' value='true' {ck(showAllCompanies)}><span>Все компании — платная лицензия</span></div><div class='check'><input type='checkbox' name='fromAllCompanies' value='true' {ck(fromAllCompanies)}><span>Из всех компаний</span></div><div class='check'><input type='checkbox' name='showAllRegions' value='true' {ck(showAllRegions)}><span>Все регионы</span></div><div class='check'><input type='checkbox' name='allRegions' value='true' {ck(allRegions)}><span>Все регионы (alt)</span></div><div class='check'><input type='checkbox' name='showAllCategories' value='true' {ck(showAllCategories)}><span>Все категории</span></div><div class='check'><input type='checkbox' name='allCategories' value='true' {ck(allCategories)}><span>Все категории (alt)</span></div>
</div></details><div style='margin-top:14px;display:flex;gap:10px'><button type='submit'>Применить фильтры</button><a class='btn secondary' href='/dashboard?refresh=true'>Сбросить и обновить</a></div></form></div>
<div class='stats'><div class='pill'>Всего после API-фильтра: {len(all_orders)}</div><div class='pill'>После локального фильтра: {len(filtered)}</div><div class='pill'>Страница: {page}/{total_pages}</div></div>
<div class='card tablewrap'><table><thead><tr><th>ID</th><th>Заявка</th><th>Заказчик</th><th>Регион</th><th>Оплата</th><th>Поставка</th><th>Позиций</th><th>Конкурентов</th><th>Моё предложение</th></tr></thead><tbody>{rows}</tbody></table></div><div class='pager'><div>{prev_link}</div><div>{next_link}</div></div></body></html>"""
    return HTMLResponse(content=page_html)


@app.get("/dashboard/order/{order_id}", response_class=HTMLResponse)
def dashboard_order(order_id: int):
    orders = fetch_all_orders()
    order = next((x for x in orders if x.get("id") == order_id), None)
    if not order:
        orders = fetch_all_orders(force=True)
        order = next((x for x in orders if x.get("id") == order_id), None)
    if not order:
        raise HTTPException(status_code=404, detail="Заявка не найдена среди актуальных")
    customer, region = order.get("customer") or {}, order.get("region") or {}
    item_rows = ""
    for n, item in enumerate(order.get("orderItems") or [], 1):
        item_rows += f"<tr><td>{n}</td><td>{esc(item.get('goodName'))}</td><td>{esc((item.get('category') or {}).get('name'))}</td><td>{esc(item.get('count'))}</td><td>{esc((item.get('unit') or {}).get('name'))}</td><td>{esc(item.get('companiesWithOffersCount'))}</td><td>{esc(item.get('bestOfferItem') or '—')}</td><td>{esc(item.get('comment') or '')}</td></tr>"
    page_html = f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><title>Заявка {order_id}</title><style>body{{font-family:Arial;margin:24px;background:#f5f5f5}}.card{{background:#fff;border-radius:12px;padding:18px;margin-bottom:18px}}a{{color:#4c39d4;font-weight:bold;text-decoration:none}}.grid{{display:grid;grid-template-columns:190px 1fr;gap:8px}}table{{width:100%;border-collapse:collapse;font-size:13px}}th{{background:#eee;text-align:left;padding:9px}}td{{padding:9px;border-bottom:1px solid #eee}}</style></head><body><p><a href='/dashboard'>← Назад к заявкам</a> · <a href='/logout'>Выйти</a></p><div class='card'><h1>{esc(order.get('name'))}</h1><div class='grid'><b>ID:</b><div>{order_id}</div><b>Заказчик:</b><div>{esc(customer.get('shortName') or customer.get('name'))}</div><b>Регион:</b><div>{esc(region.get('name'))}</div><b>Адрес:</b><div>{esc(order.get('deliveryAddress'))}</div><b>Оплата:</b><div>{esc(format_delay(order.get('delay')))}</div><b>Срок поставки:</b><div>{esc(order.get('finishDate'))}</div><b>Создана:</b><div>{esc(order.get('creationDate'))}</div><b>Моё предложение:</b><div>{'Есть' if has_my_offer(order) else 'Нет'}</div></div></div><div class='card'><h2>Позиции</h2><table><thead><tr><th>№</th><th>Наименование</th><th>Категория</th><th>Количество</th><th>Ед.</th><th>Конкурентов</th><th>Лучшее предложение</th><th>Комментарий</th></tr></thead><tbody>{item_rows}</tbody></table></div></body></html>"""
    return HTMLResponse(content=page_html)


install_security(app)
install_mcp(app, fetch_all_orders, filter_orders, compact_order)
