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

# ----- Создание заявки с маржей 15% и автоматическим финальным пересчётом -----
@app.post("/order")
def create_order(order: Order):
    start_margin_percent = 15
    max_discount = 5  # максимально допустимая корректировка после Zakupay

    base_total = order.quantity * order.price_per_unit
    total_with_margin = round(base_total * (1 + start_margin_percent / 100), 2)

    # Имитация ответа Zakupay — насколько дороже наша цена
    zakupay_delta = random.uniform(0, 10)  # например, 0–10%

    # Пересчёт финальной цены с учётом реакции Zakupay
    final_total = round(total_with_margin * (1 - min(zakupay_delta, max_discount) / 100), 2)

    order_data = {
        "client_name": order.client_name,
        "product": order.product,
        "quantity": order.quantity,
        "price_per_unit": order.price_per_unit,
        "base_total": base_total,
        "total_with_margin": total_with_margin,
        "zakupay_response_delta_percent": round(zakupay_delta, 2),
        "final_total": final_total,
        "created_at": datetime.utcnow()
    }

    orders_db.append(order_data)

    return {
        "status": "success",
        "order": order_data
    }

# ----- Просмотр всех заявок -----
@app.get("/orders")
def get_orders():
    return {"orders": orders_db}
