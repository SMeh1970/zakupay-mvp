from fastapi import FastAPI
from pydantic import BaseModel
from typing import List
from datetime import datetime
import random

app = FastAPI()

# ----- Модель заявки -----
class Order(BaseModel):
    client_name: str
    product: str
    quantity: int
    price_per_unit: float

# ----- Временное хранилище (MVP) -----
orders_db: List[dict] = []

# ----- Корневой маршрут -----
@app.get("/")
def root():
    return {"message": "Zakupay MVP запущен"}

# ----- Создание заявки с маржей 15% -----
@app.post("/order")
def create_order(order: Order):
    # Начальная маржа
    start_margin_percent = 15

    base_total = order.quantity * order.price_per_unit
    total_with_margin = round(base_total * (1 + start_margin_percent / 100), 2)

    order_data = {
        "client_name": order.client_name,
        "product": order.product,
        "quantity": order.quantity,
        "price_per_unit": order.price_per_unit,
        "base_total": base_total,
        "total_with_margin": total_with_margin,
        "created_at": datetime.utcnow(),
        "zakupay_response_delta_percent": None,
        "final_total": None
    }

    orders_db.append(order_data)

    return {
        "status": "success",
        "order": order_data
    }

# ----- Имитация ответа Zakupay и пересчёт -----
@app.post("/order/finalize/{order_index}")
def finalize_order(order_index: int):
    """
    order_index — это порядковый номер заявки в списке orders_db (начинается с 0)
    """
    if order_index < 0 or order_index >= len(orders_db):
        return {"status": "error", "message": "Order index out of range"}

    order = orders_db[order_index]

    # Имитация ответа Zakupay — насколько наша цена дороже конкурента
    # Например, Zakupay говорит, что наша цена +5% дороже минимальной
    zakupay_delta = random.uniform(0, 10)  # случайно 0–10%

    # Максимально допустимая корректировка (например, можем снижать до -5%)
    max_discount = 5  # в %

    # Итоговая цена с учётом реакции Zakupay
    final_total = round(order["total_with_margin"] * (1 - min(zakupay_delta, max_discount) / 100), 2)

    # Сохраняем в заказ
    order["zakupay_response_delta_percent"] = round(zakupay_delta, 2)
    order["final_total"] = final_total

    return {
        "status": "success",
        "order": order
    }

# ----- Просмотр всех заявок -----
@app.get("/orders")
def get_orders():
    return {"orders": orders_db}
