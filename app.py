from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
import random
import os
import requests

app = FastAPI(
    title="Zakupay MVP",
    description="MVP автоматизации работы поставщика с Закупай",
    version="0.2"
)

# =========================================================
# НАСТРОЙКИ
# =========================================================

ZAKUPAY_API_KEY = os.getenv("ZAKUPAY_API_KEY")

# Для поставщика согласно документации Синтеки
ZAKUPAY_BASE_URL = os.getenv(
    "ZAKUPAY_BASE_URL",
    "https://prodavay.sel-be.ru"
)

START_MARGIN_PERCENT = float(os.getenv("START_MARGIN", "15"))

# =========================================================
# СТАРЫЙ ТЕСТОВЫЙ MVP
# Пока оставляем, чтобы ничего из уже работающего не потерять.
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
        "zakupay_token_configured": bool(ZAKUPAY_API_KEY)
    }


@app.post("/order")
def create_order(order: Order):
    """
    Старый тестовый метод.
    НЕ отправляет ничего в Закупай.
    """

    start_margin_percent = START_MARGIN_PERCENT
    max_discount = 5

    base_total = order.quantity * order.price_per_unit
    total_with_margin = round(
        base_total * (1 + start_margin_percent / 100),
        2
    )

    # Пока это только старая симуляция.
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
    """
    Старые локальные тестовые заявки.
    """
    return {"orders": orders_db}


# =========================================================
# РЕАЛЬНОЕ ПОДКЛЮЧЕНИЕ К ЗАКУПАЙ
# Только чтение. Ничего не изменяет и не отправляет.
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


@app.get("/zakupay/test")
def test_zakupay_connection():
    """
    Безопасная проверка подключения к API Закупай.

    Выполняется GET запрос.
    Никаких данных в Закупай не отправляет.
    """

    url = f"{ZAKUPAY_BASE_URL}/api/v1/orders"

    params = {
        "status": "actual",
        "isoDate": "true"
    }

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
            detail=f"Не удалось подключиться к Закупай: {str(e)}"
        )

    # Токен в ответ никогда не выводим.
    result = {
        "connected_to": url,
        "http_status": response.status_code,
        "success": response.ok
    }

    try:
        result["zakupay_response"] = response.json()
    except ValueError:
        result["zakupay_response"] = response.text[:5000]

    return result


@app.get("/zakupay/orders")
def get_real_zakupay_orders(
    status: str = "actual",
    creationDateFrom: Optional[str] = None,
    creationDateTo: Optional[str] = None,
    publicDateFrom: Optional[str] = None,
    publicDateTo: Optional[str] = None,
    delayFrom: Optional[int] = None,
    delayTo: Optional[int] = None,
    onlyNotEnough: Optional[bool] = None,
    onlyWithMyOffers: Optional[bool] = None
):
    """
    Получение РЕАЛЬНЫХ заявок поставщика из Закупай.

    Метод работает только на чтение.
    """

    url = f"{ZAKUPAY_BASE_URL}/api/v1/orders"

    params = {
        "status": status,
        "isoDate": "true"
    }

    if creationDateFrom:
        params["creationDateFrom"] = creationDateFrom

    if creationDateTo:
        params["creationDateTo"] = creationDateTo

    if publicDateFrom:
        params["publicDateFrom"] = publicDateFrom

    if publicDateTo:
        params["publicDateTo"] = publicDateTo

    if delayFrom is not None:
        params["delayFrom"] = delayFrom

    if delayTo is not None:
        params["delayTo"] = delayTo

    if onlyNotEnough is not None:
        params["onlyNotEnough"] = str(onlyNotEnough).lower()

    if onlyWithMyOffers is not None:
        params["onlyWithMyOffers"] = str(onlyWithMyOffers).lower()

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
            detail="Закупай отклонил токен. Проверь ZAKUPAY_API_KEY."
        )

    if response.status_code == 403:
        raise HTTPException(
            status_code=403,
            detail=(
                "Токен принят, но у пользователя недостаточно прав "
                "для получения заявок."
            )
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

    return {
        "source": "REAL_ZAKUPAY",
        "query": params,
        "data": data
    }
