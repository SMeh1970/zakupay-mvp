from fastapi import FastAPI
from pydantic import BaseModel
from typing import List
from datetime import datetime

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

# ----- Создание заявки -----
@app.post("/order")
def create_order(order: Order):
    total_price = order.quantity * order.price_per_unit

    order_data = {
        "client_name": order.client_name,
        "product": order.product,
        "quantity": order.quantity,
        "price_per_unit": order.price_per_unit,
        "total_price": total_price,
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
