from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from typing import Optional
import os
import time
import html
import requests
from urllib.parse import urlencode

app = FastAPI(
    title="Zakupay MVP",
    description="Рабочая панель поставщика для анализа заявок Закупай",
    version="0.4"
)

ZAKUPAY_API_KEY = os.getenv("ZAKUPAY_API_KEY")
ZAKUPAY_BASE_URL = os.getenv("ZAKUPAY_BASE_URL", "https://prodavay.sel-be.ru")

CACHE_TTL_SECONDS = 60
_orders_cache = {"ts": 0.0, "orders": []}


def esc(value):
    if value is None:
        return ""
    return html.escape(str(value))


def zakupay_headers():
    if not ZAKUPAY_API_KEY:
        raise HTTPException(status_code=500, detail="Не настроена переменная ZAKUPAY_API_KEY")
    return {
        "ZakupayToken": ZAKUPAY_API_KEY,
        "Accept": "application/json"
    }


def request_orders_page(page: int = 1, page_size: int = 100, status: str = "actual"):
    url = f"{ZAKUPAY_BASE_URL}/api/v1/orders"
    params = {
        "status": status,
        "isoDate": "true",
        "page": page,
        "pageSize": page_size
    }

    try:
        response = requests.get(
            url,
            headers=zakupay_headers(),
            params=params,
            timeout=30
        )
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


def fetch_all_orders(force: bool = False):
    now = time.time()
    if not force and _orders_cache["orders"] and now - _orders_cache["ts"] < CACHE_TTL_SECONDS:
        return _orders_cache["orders"]

    all_orders = []
    seen_ids = set()
    max_pages = 100

    for page in range(1, max_pages + 1):
        data = request_orders_page(page=page, page_size=100, status="actual")
        batch = data.get("orders") or []
        if not batch:
            break

        new_count = 0
        for order in batch:
            order_id = order.get("id")
            if order_id in seen_ids:
                continue
            seen_ids.add(order_id)
            all_orders.append(order)
            new_count += 1

        if new_count == 0:
            break

        returned_page_size = data.get("pageSize")
        if returned_page_size and len(batch) < returned_page_size:
            break
        if len(batch) < 10:
            break

    _orders_cache["ts"] = now
    _orders_cache["orders"] = all_orders
    return all_orders


def has_my_offer(order):
    offers = order.get("offers") or []
    if offers:
        return True
    for item in order.get("orderItems") or []:
        if item.get("offerIds"):
            return True
    return False


def max_competitors(order):
    values = []
    for item in order.get("orderItems") or []:
        value = item.get("companiesWithOffersCount")
        if isinstance(value, (int, float)):
            values.append(value)
    return max(values) if values else 0


def order_categories(order):
    names = []
    for item in order.get("orderItems") or []:
        category = item.get("category") or {}
        name = category.get("name")
        if name and name not in names:
            names.append(name)
    return names


def format_delay(delay):
    if delay is None:
        return "—"
    if delay == 0:
        return "Предоплата / без отсрочки"
    return f"{delay} дней"


def filter_orders(
    orders,
    payment: str = "all",
    region: str = "",
    category: str = "",
    min_positions: int = 0,
    max_competitors_value: Optional[int] = None,
    only_without_my_offer: bool = False,
):
    result = []
    region_q = region.strip().lower()
    category_q = category.strip().lower()

    for order in orders:
        delay = order.get("delay")
        items = order.get("orderItems") or []
        region_name = ((order.get("region") or {}).get("name") or "").lower()
        categories = " | ".join(order_categories(order)).lower()

        if payment == "prepayment" and delay != 0:
            continue
        if payment == "delay" and (delay is None or delay == 0):
            continue
        if region_q and region_q not in region_name:
            continue
        if category_q and category_q not in categories:
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
    customer = order.get("customer") or {}
    region = order.get("region") or {}
    items = []
    for item in order.get("orderItems") or []:
        unit = item.get("unit") or {}
        category = item.get("category") or {}
        items.append({
            "id": item.get("id"),
            "name": item.get("goodName"),
            "quantity": item.get("count"),
            "unit": unit.get("name"),
            "category": category.get("name"),
            "competitors": item.get("companiesWithOffersCount"),
            "best_offer_item": item.get("bestOfferItem"),
            "comment": item.get("comment"),
            "status": item.get("actualityStatusDesc")
        })

    return {
        "id": order.get("id"),
        "name": order.get("name"),
        "customer": customer.get("shortName") or customer.get("name"),
        "region": region.get("name"),
        "delay_days": order.get("delay"),
        "payment_type": "prepayment_or_no_delay" if order.get("delay") == 0 else "delay",
        "finish_date": order.get("finishDate"),
        "delivery_address": order.get("deliveryAddress"),
        "positions_count": len(items),
        "max_competitors": max_competitors(order),
        "has_my_offer": has_my_offer(order),
        "categories": order_categories(order),
        "items": items
    }


@app.get("/")
def root():
    return {
        "message": "Zakupay MVP запущен",
        "version": "0.4",
        "dashboard": "/dashboard",
        "analysis_api": "/analysis/orders"
    }


@app.get("/analysis/orders")
def analysis_orders(
    payment: str = Query("all", pattern="^(all|prepayment|delay)$"),
    region: str = "",
    category: str = "",
    min_positions: int = 0,
    max_competitors_value: Optional[int] = Query(None, alias="max_competitors"),
    only_without_my_offer: bool = False,
    refresh: bool = False,
):
    orders = fetch_all_orders(force=refresh)
    filtered = filter_orders(
        orders,
        payment=payment,
        region=region,
        category=category,
        min_positions=min_positions,
        max_competitors_value=max_competitors_value,
        only_without_my_offer=only_without_my_offer,
    )
    return {
        "source": "REAL_ZAKUPAY",
        "total_actual": len(orders),
        "filtered_count": len(filtered),
        "orders": [compact_order(order) for order in filtered]
    }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    payment: str = "all",
    region: str = "",
    category: str = "",
    min_positions: int = 0,
    max_competitors_value: Optional[int] = Query(None, alias="max_competitors"),
    only_without_my_offer: bool = False,
    page: int = 1,
    page_size: int = 50,
    refresh: bool = False,
):
    page = max(1, page)
    page_size = min(max(page_size, 10), 100)

    all_orders = fetch_all_orders(force=refresh)
    filtered = filter_orders(
        all_orders,
        payment=payment,
        region=region,
        category=category,
        min_positions=min_positions,
        max_competitors_value=max_competitors_value,
        only_without_my_offer=only_without_my_offer,
    )

    total_pages = max(1, (len(filtered) + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    visible = filtered[start:start + page_size]

    rows = ""
    for order in visible:
        order_id = order.get("id")
        customer = order.get("customer") or {}
        region_obj = order.get("region") or {}
        items = order.get("orderItems") or []
        rows += f"""
        <tr>
            <td><a href="/dashboard/order/{order_id}">{order_id}</a></td>
            <td>{esc(order.get('name'))}</td>
            <td>{esc(customer.get('shortName') or customer.get('name'))}</td>
            <td>{esc(region_obj.get('name'))}</td>
            <td>{esc(format_delay(order.get('delay')))}</td>
            <td>{esc(order.get('finishDate'))}</td>
            <td>{len(items)}</td>
            <td>{max_competitors(order)}</td>
            <td>{'Да' if has_my_offer(order) else 'Нет'}</td>
        </tr>
        """

    base_params = {
        "payment": payment,
        "region": region,
        "category": category,
        "min_positions": min_positions,
        "page_size": page_size,
    }
    if max_competitors_value is not None:
        base_params["max_competitors"] = max_competitors_value
    if only_without_my_offer:
        base_params["only_without_my_offer"] = "true"

    prev_link = ""
    next_link = ""
    if page > 1:
        q = dict(base_params, page=page - 1)
        prev_link = f'<a class="btn" href="/dashboard?{urlencode(q)}">← Предыдущая</a>'
    if page < total_pages:
        q = dict(base_params, page=page + 1)
        next_link = f'<a class="btn" href="/dashboard?{urlencode(q)}">Следующая →</a>'

    checked = "checked" if only_without_my_offer else ""
    payment_options = {
        "all": "Все условия",
        "prepayment": "Только предоплата / без отсрочки",
        "delay": "Только отсрочка",
    }
    options_html = "".join(
        f'<option value="{k}" {"selected" if payment == k else ""}>{v}</option>'
        for k, v in payment_options.items()
    )

    max_comp_value = "" if max_competitors_value is None else str(max_competitors_value)

    page_html = f"""
    <!doctype html>
    <html lang="ru">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Zakupay MVP</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 24px; background: #f5f5f5; color:#222; }}
            h1 {{ margin-bottom: 4px; }}
            .sub {{ color:#666; margin-bottom:18px; }}
            .card {{ background:white; border-radius:12px; padding:18px; margin-bottom:18px; box-shadow:0 2px 8px rgba(0,0,0,.08); }}
            .filters {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; align-items:end; }}
            label {{ font-size:12px; color:#666; display:block; margin-bottom:5px; }}
            input, select {{ width:100%; box-sizing:border-box; padding:9px; border:1px solid #ccc; border-radius:7px; }}
            .check {{ display:flex; gap:8px; align-items:center; padding:9px 0; }}
            .check input {{ width:auto; }}
            button, .btn {{ background:#222; color:white; border:0; padding:10px 14px; border-radius:8px; text-decoration:none; cursor:pointer; display:inline-block; }}
            .btn.secondary {{ background:#666; }}
            .stats {{ display:flex; gap:10px; flex-wrap:wrap; margin:14px 0; }}
            .pill {{ background:#222; color:white; border-radius:18px; padding:6px 12px; }}
            table {{ width:100%; border-collapse:collapse; font-size:13px; }}
            th {{ background:#eee; text-align:left; padding:9px; position:sticky; top:0; }}
            td {{ padding:9px; border-bottom:1px solid #eee; vertical-align:top; }}
            tr:hover {{ background:#fafafa; }}
            a {{ color:#4c39d4; font-weight:bold; text-decoration:none; }}
            .pager {{ display:flex; justify-content:space-between; align-items:center; margin-top:14px; }}
            .tablewrap {{ overflow:auto; max-height:70vh; }}
        </style>
    </head>
    <body>
        <h1>Закупай — входящие заявки</h1>
        <div class="sub">Все доступные актуальные заявки из API Синтеки. Данные обновляются не чаще одного раза в минуту.</div>

        <div class="card">
            <form method="get" action="/dashboard" class="filters">
                <div><label>Оплата</label><select name="payment">{options_html}</select></div>
                <div><label>Регион содержит</label><input name="region" value="{esc(region)}" placeholder="Москва"></div>
                <div><label>Категория содержит</label><input name="category" value="{esc(category)}" placeholder="Метизы"></div>
                <div><label>Минимум позиций</label><input type="number" min="0" name="min_positions" value="{min_positions}"></div>
                <div><label>Не более конкурентов</label><input type="number" min="0" name="max_competitors" value="{max_comp_value}" placeholder="например 2"></div>
                <div class="check"><input type="checkbox" name="only_without_my_offer" value="true" {checked}><span>Только без моего предложения</span></div>
                <div><button type="submit">Применить фильтры</button></div>
                <div><a class="btn secondary" href="/dashboard?refresh=true">Обновить из Закупай</a></div>
            </form>
        </div>

        <div class="stats">
            <div class="pill">Всего актуальных: {len(all_orders)}</div>
            <div class="pill">После фильтра: {len(filtered)}</div>
            <div class="pill">Страница: {page} / {total_pages}</div>
        </div>

        <div class="card tablewrap">
            <table>
                <thead><tr>
                    <th>ID</th><th>Заявка</th><th>Заказчик</th><th>Регион</th><th>Оплата</th><th>Поставка</th><th>Позиций</th><th>Конкурентов</th><th>Моё предложение</th>
                </tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </div>

        <div class="pager"><div>{prev_link}</div><div>{next_link}</div></div>
    </body>
    </html>
    """
    return HTMLResponse(content=page_html)


@app.get("/dashboard/order/{order_id}", response_class=HTMLResponse)
def dashboard_order(order_id: int):
    orders = fetch_all_orders()
    order = next((item for item in orders if item.get("id") == order_id), None)
    if not order:
        orders = fetch_all_orders(force=True)
        order = next((item for item in orders if item.get("id") == order_id), None)
    if not order:
        raise HTTPException(status_code=404, detail="Заявка не найдена среди актуальных")

    customer = order.get("customer") or {}
    region = order.get("region") or {}
    item_rows = ""
    for number, item in enumerate(order.get("orderItems") or [], 1):
        unit = item.get("unit") or {}
        category = item.get("category") or {}
        item_rows += f"""
        <tr>
            <td>{number}</td>
            <td>{esc(item.get('goodName'))}</td>
            <td>{esc(category.get('name'))}</td>
            <td>{esc(item.get('count'))}</td>
            <td>{esc(unit.get('name'))}</td>
            <td>{esc(item.get('companiesWithOffersCount'))}</td>
            <td>{esc(item.get('bestOfferItem') or '—')}</td>
            <td>{esc(item.get('comment') or '')}</td>
        </tr>
        """

    page_html = f"""
    <!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Заявка {order_id}</title>
    <style>
        body {{ font-family:Arial,sans-serif; margin:24px; background:#f5f5f5; color:#222; }}
        .card {{ background:white; border-radius:12px; padding:18px; margin-bottom:18px; box-shadow:0 2px 8px rgba(0,0,0,.08); }}
        a {{ color:#4c39d4; font-weight:bold; text-decoration:none; }}
        .grid {{ display:grid; grid-template-columns:190px 1fr; gap:8px; }}
        .label {{ font-weight:bold; }}
        table {{ width:100%; border-collapse:collapse; font-size:13px; }}
        th {{ background:#eee; text-align:left; padding:9px; }} td {{ padding:9px; border-bottom:1px solid #eee; vertical-align:top; }}
        .tablewrap {{ overflow:auto; }}
    </style></head><body>
    <p><a href="/dashboard">← Назад к заявкам</a></p>
    <div class="card"><h1>{esc(order.get('name'))}</h1>
    <div class="grid">
        <div class="label">ID заявки:</div><div>{order_id}</div>
        <div class="label">Заказчик:</div><div>{esc(customer.get('shortName') or customer.get('name'))}</div>
        <div class="label">Регион:</div><div>{esc(region.get('name'))}</div>
        <div class="label">Адрес:</div><div>{esc(order.get('deliveryAddress'))}</div>
        <div class="label">Оплата:</div><div>{esc(format_delay(order.get('delay')))}</div>
        <div class="label">Срок поставки:</div><div>{esc(order.get('finishDate'))}</div>
        <div class="label">Создана:</div><div>{esc(order.get('creationDate'))}</div>
        <div class="label">Моё предложение:</div><div>{'Есть' if has_my_offer(order) else 'Нет'}</div>
    </div></div>
    <div class="card tablewrap"><h2>Позиции</h2><table><thead><tr>
        <th>№</th><th>Наименование</th><th>Категория</th><th>Количество</th><th>Ед.</th><th>Конкурентов</th><th>Лучшее предложение</th><th>Комментарий</th>
    </tr></thead><tbody>{item_rows}</tbody></table></div>
    </body></html>
    """
    return HTMLResponse(content=page_html)
