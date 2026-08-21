from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
import os
import random
import requests
import html

app = FastAPI(
    title="Zakupay MVP",
    description="MVP автоматизации работы поставщика с Закупай",
    version="0.3"
)

# =========================================================
# НАСТРОЙКИ
# =========================================================

ZAKUPAY_API_KEY = os.getenv("ZAKUPAY_API_KEY")
ZAKUPAY_BASE_URL = os.getenv(
    "ZAKUPAY_BASE_URL",
    "https://prodavay.sel-be.ru"
)

START_MARGIN_PERCENT = float(os.getenv("START_MARGIN", "15"))

# =========================================================
# СТАРЫЙ ТЕСТОВЫЙ MVP
# =========================================================

class Order(BaseModel):
    client_name: str
    product: str
    quantity: int
    price_per_unit: float


orders_db: List[dict] = []


@app.get("/")
def root():
    return {
        "message": "Zakupay MVP запущен",
        "zakupay_token_configured": bool(ZAKUPAY_API_KEY),
        "dashboard": "/dashboard"
    }


@app.post("/order")
def create_order(order: Order):
    start_margin_percent = START_MARGIN_PERCENT
    max_discount = 5

    base_total = order.quantity * order.price_per_unit
    total_with_margin = round(
        base_total * (1 + start_margin_percent / 100),
        2
    )

    zakupay_delta = random.uniform(0, 10)

    final_total = round(
        total_with_margin *
        (1 - min(zakupay_delta, max_discount) / 100),
        2
    )

    order_data = {
        "client_name": order.client_name,
        "product": order.product,
        "quantity": order.quantity,
        "price_per_unit": order.price_per_unit,
        "base_total": base_total,
        "total_with_margin": total_with_margin,
        "zakupay_response_delta_percent": round(zakupay_delta, 2),
        "final_total": final_total,
        "created_at": datetime.utcnow().isoformat(),
        "mode": "SIMULATION"
    }

    orders_db.append(order_data)

    return {
        "status": "success",
        "order": order_data
    }


@app.get("/orders")
def get_orders():
    return {"orders": orders_db}


# =========================================================
# ЗАКУПАЙ API
# =========================================================

def zakupay_headers():
    if not ZAKUPAY_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="В Render не настроена переменная ZAKUPAY_API_KEY"
        )

    return {
        "ZakupayToken": ZAKUPAY_API_KEY,
        "Accept": "application/json"
    }


def fetch_zakupay_orders(
    status="actual",
    only_not_enough=None,
    only_with_my_offers=None
):
    url = f"{ZAKUPAY_BASE_URL}/api/v1/orders"

    params = {
        "status": status,
        "isoDate": "true"
    }

    if only_not_enough is not None:
        params["onlyNotEnough"] = str(only_not_enough).lower()

    if only_with_my_offers is not None:
        params["onlyWithMyOffers"] = str(only_with_my_offers).lower()

    try:
        response = requests.get(
            url,
            headers=zakupay_headers(),
            params=params,
            timeout=30
        )
    except requests.RequestException as e:
        raise HTTPException(
            status_code=502,
            detail=f"Ошибка соединения с Закупай: {str(e)}"
        )

    if response.status_code == 401:
        raise HTTPException(
            status_code=401,
            detail="Закупай отклонил токен."
        )

    if response.status_code == 403:
        raise HTTPException(
            status_code=403,
            detail="Недостаточно прав для получения заявок."
        )

    try:
        data = response.json()
    except ValueError:
        raise HTTPException(
            status_code=response.status_code,
            detail=response.text[:5000]
        )

    if not response.ok:
        raise HTTPException(
            status_code=response.status_code,
            detail=data
        )

    return data


@app.get("/zakupay/test")
def test_zakupay_connection():
    data = fetch_zakupay_orders()

    return {
        "connected_to": f"{ZAKUPAY_BASE_URL}/api/v1/orders",
        "success": True,
        "zakupay_response": data
    }


@app.get("/zakupay/orders")
def get_real_zakupay_orders(
    status: str = "actual",
    onlyNotEnough: Optional[bool] = None,
    onlyWithMyOffers: Optional[bool] = None
):
    data = fetch_zakupay_orders(
        status=status,
        only_not_enough=onlyNotEnough,
        only_with_my_offers=onlyWithMyOffers
    )

    return {
        "source": "REAL_ZAKUPAY",
        "data": data
    }


# =========================================================
# DASHBOARD
# =========================================================

def esc(value):
    if value is None:
        return ""
    return html.escape(str(value))


def get_competitor_count(order):
    counts = []

    for item in order.get("orderItems", []):
        count = item.get("companiesWithOffersCount")
        if count is not None:
            counts.append(count)

    if not counts:
        return 0

    return max(counts)


def format_delay(delay):
    if delay is None:
        return "—"

    if delay == 0:
        return "Предоплата / без отсрочки"

    return f"{delay} дней"


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    data = fetch_zakupay_orders(status="actual")
    orders = data.get("orders", [])

    rows = ""

    for order in orders:
        order_id = order.get("id")
        customer = order.get("customer") or {}
        region = order.get("region") or {}
        items = order.get("orderItems") or []

        rows += f"""
        <tr>
            <td>
                <a href="/dashboard/order/{order_id}">
                    {order_id}
                </a>
            </td>
            <td>{esc(order.get("name"))}</td>
            <td>{esc(customer.get("shortName") or customer.get("name"))}</td>
            <td>{esc(region.get("name"))}</td>
            <td>{format_delay(order.get("delay"))}</td>
            <td>{esc(order.get("finishDate"))}</td>
            <td>{len(items)}</td>
            <td>{get_competitor_count(order)}</td>
            <td>{esc(order.get("actualityStatusDesc"))}</td>
        </tr>
        """

    page = f"""
    <!doctype html>
    <html lang="ru">
    <head>
        <meta charset="utf-8">
        <title>Zakupay MVP</title>

        <style>
            body {{
                font-family: Arial, sans-serif;
                margin: 30px;
                background: #f5f5f5;
                color: #222;
            }}

            h1 {{
                margin-bottom: 5px;
            }}

            .subtitle {{
                color: #666;
                margin-bottom: 25px;
            }}

            .card {{
                background: white;
                border-radius: 10px;
                padding: 20px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.08);
            }}

            table {{
                width: 100%;
                border-collapse: collapse;
                font-size: 14px;
            }}

            th {{
                background: #eeeeee;
                text-align: left;
                padding: 10px;
                border-bottom: 1px solid #ccc;
            }}

            td {{
                padding: 10px;
                border-bottom: 1px solid #eee;
                vertical-align: top;
            }}

            tr:hover {{
                background: #fafafa;
            }}

            a {{
                color: #4c39d4;
                text-decoration: none;
                font-weight: bold;
            }}

            .counter {{
                display: inline-block;
                background: #222;
                color: white;
                border-radius: 20px;
                padding: 5px 12px;
                margin-bottom: 15px;
            }}
        </style>
    </head>

    <body>

        <h1>Закупай — входящие заявки</h1>

        <div class="subtitle">
            Реальные актуальные заявки из API Синтеки
        </div>

        <div class="counter">
            Получено заявок: {len(orders)}
        </div>

        <div class="card">
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Заявка</th>
                        <th>Заказчик</th>
                        <th>Регион</th>
                        <th>Оплата</th>
                        <th>Поставка</th>
                        <th>Позиций</th>
                        <th>Конкурентов</th>
                        <th>Статус</th>
                    </tr>
                </thead>

                <tbody>
                    {rows}
                </tbody>
            </table>
        </div>

    </body>
    </html>
    """

    return HTMLResponse(content=page)


@app.get("/dashboard/order/{order_id}", response_class=HTMLResponse)
def dashboard_order(order_id: int):
    data = fetch_zakupay_orders(status="actual")
    orders = data.get("orders", [])

    order = None

    for current_order in orders:
        if current_order.get("id") == order_id:
            order = current_order
            break

    if not order:
        raise HTTPException(
            status_code=404,
            detail="Заявка не найдена"
        )

    customer = order.get("customer") or {}
    region = order.get("region") or {}
    items = order.get("orderItems") or []

    item_rows = ""

    for number, item in enumerate(items, start=1):

        unit = item.get("unit") or {}
        category = item.get("category") or {}

        item_rows += f"""
        <tr>
            <td>{number}</td>
            <td>{esc(item.get("goodName"))}</td>
            <td>{esc(category.get("name"))}</td>
            <td>{esc(item.get("count"))}</td>
            <td>{esc(unit.get("name"))}</td>
            <td>{esc(item.get("companiesWithOffersCount"))}</td>
            <td>{esc(item.get("bestOfferItem") or "—")}</td>
            <td>{esc(item.get("comment") or "")}</td>
        </tr>
        """

    page = f"""
    <!doctype html>
    <html lang="ru">

    <head>
        <meta charset="utf-8">
        <title>Заявка {order_id}</title>

        <style>
            body {{
                font-family: Arial, sans-serif;
                margin: 30px;
                background: #f5f5f5;
                color: #222;
            }}

            .back {{
                margin-bottom: 20px;
            }}

            a {{
                color: #4c39d4;
                text-decoration: none;
                font-weight: bold;
            }}

            .card {{
                background: white;
                border-radius: 10px;
                padding: 20px;
                margin-bottom: 20px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.08);
            }}

            .info-grid {{
                display: grid;
                grid-template-columns: 200px 1fr;
                gap: 8px;
            }}

            .label {{
                font-weight: bold;
            }}

            table {{
                width: 100%;
                border-collapse: collapse;
                font-size: 14px;
            }}

            th {{
                background: #eeeeee;
                text-align: left;
                padding: 10px;
                border-bottom: 1px solid #ccc;
            }}

            td {{
                padding: 10px;
                border-bottom: 1px solid #eee;
                vertical-align: top;
            }}
        </style>
    </head>

    <body>

        <div class="back">
            <a href="/dashboard">← Назад к заявкам</a>
        </div>

        <div class="card">

            <h1>{esc(order.get("name"))}</h1>

            <div class="info-grid">

                <div class="label">ID заявки:</div>
                <div>{order_id}</div>

                <div class="label">Заказчик:</div>
                <div>{esc(customer.get("shortName") or customer.get("name"))}</div>

                <div class="label">Регион:</div>
                <div>{esc(region.get("name"))}</div>

                <div class="label">Адрес:</div>
                <div>{esc(order.get("deliveryAddress"))}</div>

                <div class="label">Условия оплаты:</div>
                <div>{format_delay(order.get("delay"))}</div>

                <div class="label">Срок поставки:</div>
                <div>{esc(order.get("finishDate"))}</div>

                <div class="label">Создана:</div>
                <div>{esc(order.get("creationDate"))}</div>

            </div>

        </div>

        <div class="card">

            <h2>Позиции</h2>

            <table>

                <thead>
                    <tr>
                        <th>№</th>
                        <th>Наименование</th>
                        <th>Категория</th>
                        <th>Количество</th>
                        <th>Ед.</th>
                        <th>Конкурентов</th>
                        <th>Лучшее предложение</th>
                        <th>Комментарий</th>
                    </tr>
                </thead>

                <tbody>
                    {item_rows}
                </tbody>

            </table>

        </div>

    </body>

    </html>
    """

    return HTMLResponse(content=page)
