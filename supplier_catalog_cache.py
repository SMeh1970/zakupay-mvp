"""Durable product identities only. Prices and stock are always fetched live."""
from contextlib import closing
import base64
import json
import zlib


def load(key, now):
    from automation_pipeline import _connect, _execute
    with closing(_connect()) as conn, conn:
        conn.execute('CREATE TABLE IF NOT EXISTS supplier_catalog_cache (cache_key TEXT PRIMARY KEY, expires_at DOUBLE PRECISION NOT NULL, payload TEXT NOT NULL)')
        row = _execute(conn, 'SELECT expires_at, payload FROM supplier_catalog_cache WHERE cache_key=?', (key,)).fetchone()
    if not row or row['expires_at'] <= now:
        return None
    items = json.loads(zlib.decompress(base64.b64decode(row['payload'])))
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError('Invalid supplier catalogue cache')
    return items, float(row['expires_at'])


def save(key, items, expires_at):
    from automation_pipeline import _connect, _execute
    # Never persist credentials, tokens, prices or stock in the identity cache.
    fields = {'Код', 'Наименование', 'ПолноеНаименование', 'Артикул', 'Категория', 'МаркаНаименование', 'ЕдиницаИзмерения'}
    compact = [{k: v for k, v in item.items() if k in fields} for item in items]
    payload = base64.b64encode(zlib.compress(json.dumps(compact, ensure_ascii=False).encode('utf-8'))).decode('ascii')
    with closing(_connect()) as conn, conn:
        conn.execute('CREATE TABLE IF NOT EXISTS supplier_catalog_cache (cache_key TEXT PRIMARY KEY, expires_at DOUBLE PRECISION NOT NULL, payload TEXT NOT NULL)')
        _execute(conn, 'INSERT INTO supplier_catalog_cache(cache_key, expires_at, payload) VALUES(?,?,?) ON CONFLICT(cache_key) DO UPDATE SET expires_at=excluded.expires_at, payload=excluded.payload', (key, expires_at, payload))
