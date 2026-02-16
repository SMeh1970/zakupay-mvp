import sqlite3

conn = sqlite3.connect('zakupay.db', check_same_thread=False)
cursor = conn.cursor()

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
