"""Durable product identities only. Prices and stock are always fetched live."""
from contextlib import closing
import base64
import json
import zlib

# Never persist (or keep in process memory) credentials, tokens, prices or stock.
IDENTITY_FIELDS = frozenset({'Код', 'Наименование', 'ПолноеНаименование', 'Артикул', 'Категория', 'МаркаНаименование', 'ЕдиницаИзмерения'})


def compact(items):
    """Identity fields only. Used right after download so memory holds the same shape as the stored cache."""
    return [{k: v for k, v in item.items() if k in IDENTITY_FIELDS} for item in items]


def encode(items, batch=2000):
    """base64(zlib(JSON array)) built in batches.

    The format is unchanged, so rows written by earlier versions still load. Unlike a single
    json.dumps, the full JSON text, its UTF-8 bytes and a compacted copy of the catalogue never
    exist at the same time.
    """
    packer = zlib.compressobj()
    parts = [packer.compress(b'[')]
    for start in range(0, len(items), batch):
        chunk = compact(items[start:start + batch])
        text = json.dumps(chunk, ensure_ascii=False)[1:-1]
        if start:
            parts.append(packer.compress(b','))
        parts.append(packer.compress(text.encode('utf-8')))
    parts.append(packer.compress(b']'))
    parts.append(packer.flush())
    return base64.b64encode(b''.join(parts)).decode('ascii')


def load(key, now, allow_expired=False):
    from automation_pipeline import _connect, _execute
    with closing(_connect()) as conn, conn:
        conn.execute('CREATE TABLE IF NOT EXISTS supplier_catalog_cache (cache_key TEXT PRIMARY KEY, expires_at DOUBLE PRECISION NOT NULL, payload TEXT NOT NULL)')
        row = _execute(conn, 'SELECT expires_at, payload FROM supplier_catalog_cache WHERE cache_key=?', (key,)).fetchone()
    if not row or (row['expires_at'] <= now and not allow_expired):
        return None
    expires_at = float(row['expires_at'])
    items = json.loads(zlib.decompress(base64.b64decode(row['payload'])))
    del row
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError('Invalid supplier catalogue cache')
    return items, expires_at


def save(key, items, expires_at):
    from automation_pipeline import _connect, _execute
    payload = encode(items)
    with closing(_connect()) as conn, conn:
        conn.execute('CREATE TABLE IF NOT EXISTS supplier_catalog_cache (cache_key TEXT PRIMARY KEY, expires_at DOUBLE PRECISION NOT NULL, payload TEXT NOT NULL)')
        _execute(conn, 'INSERT INTO supplier_catalog_cache(cache_key, expires_at, payload) VALUES(?,?,?) ON CONFLICT(cache_key) DO UPDATE SET expires_at=excluded.expires_at, payload=excluded.payload', (key, expires_at, payload))
