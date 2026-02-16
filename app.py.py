from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
import sqlite3
import os

app = FastAPI()

conn = sqlite3.connect('zakupay.db', check_same_thread=False)
cursor = conn.cursor()

START_MARGIN = float(os.getenv("START_MARGIN", 0.15))
MIN_MARGIN = float(os.getenv("MIN_MARGIN", 0.03))
DELTA_COMPETITOR = float(os.getenv("DELTA_COMPETITOR", 0.005))
REPRICE_DELAY_MIN = int(os.getenv("REPRICE_DELAY_MIN", 60))

cursor.execute("""
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zakupay_order_id TEXT UNIQUE,
    sku TEXT,
    base_price REAL,
    first_price REAL,
    final_price REAL,
    competitor_price REAL,
    status TEXT,
    profit REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
conn.commit()

def get_new_orders():
    return [{"zakupay_order_id": "123", "sku": "SKU1", "quantity": 10}]

def get_price_from_1c(sku):
    return 100.0

def get_price_from_xmlriver(sku):
    return 95.0

def send_invoice(order_id, price):
    print(f"Отправлен счет {order_id}: {price}")

def get_competitor_price(order_id):
    return 105.0

def log_profit(order_id, final_price, base_price):
    profit = final_price - base_price
    cursor.execute("""
        UPDATE orders SET final_price=?, profit=?, status='won', updated_at=CURRENT_TIMESTAMP
        WHERE zakupay_order_id=?
    """, (final_price, profit, order_id))
    conn.commit()

def process_orders():
    orders = get_new_orders()
    for o in orders:
        base_price = get_price_from_1c(o['sku']) or get_price_from_xmlriver(o['sku'])
        first_price = base_price * (1 + START_MARGIN)
        cursor.execute("""
            INSERT OR IGNORE INTO orders (zakupay_order_id, sku, base_price, first_price, status)
            VALUES (?, ?, ?, ?, 'new')
        """, (o['zakupay_order_id'], o['sku'], base_price, first_price))
        conn.commit()
        send_invoice(o['zakupay_order_id'], first_price)

def reprice_orders():
    cursor.execute("SELECT * FROM orders WHERE status='new'")
    rows = cursor.fetchall()
    for row in rows:
        order_id, sku, base_price = row[1], row[2], row[3]
        competitor_price = get_competitor_price(order_id)
        target_price = max(base_price*(1+MIN_MARGIN), competitor_price*(1-DELTA_COMPETITOR))
        send_invoice(order_id, target_price)
        log_profit(order_id, target_price, base_price)

scheduler = BackgroundScheduler()
scheduler.add_job(process_orders, 'interval', minutes=2)
scheduler.add_job(reprice_orders, 'interval', minutes=REPRICE_DELAY_MIN)
scheduler.start()

@app.get("/")
def root():
    return {"message": "Zakupay MVP запущен"}
