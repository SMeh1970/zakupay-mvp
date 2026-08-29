import os
import time
from threading import Lock

import requests

BASE_URL = os.getenv("ZAKUPAY_BASE_URL", "https://prodavay.sel-be.ru").rstrip("/")
PRICE_ENDPOINT = BASE_URL + "/core/supplier/getoffersdeviationpercent"
CACHE_TTL = int(os.getenv("ZAKUPAY_BROWSER_PRICE_CACHE_TTL", "300"))
BATCH_SIZE = int(os.getenv("ZAKUPAY_BROWSER_PRICE_BATCH_SIZE", "200"))

_cache = {}
_cache_lock = Lock()


def _session_cookie():
    value = (os.getenv("ZAKUPAY_PLAY_SESSION") or "").strip()
    if value.startswith("PLAY_SESSION="):
        value = value.split("=", 1)[1].strip()
    return value


def browser_price_source_enabled():
    return bool(_session_cookie())


def _cached(item_id):
    now = time.time()
    with _cache_lock:
        row = _cache.get(int(item_id))
        if not row:
            return None
        if now - row["ts"] > CACHE_TTL:
            _cache.pop(int(item_id), None)
            return None
        return row["data"]


def _store(rows):
    now = time.time()
    with _cache_lock:
        for row in rows:
            item_id = row.get("orderItemId") if isinstance(row, dict) else None
            if item_id is not None:
                _cache[int(item_id)] = {"ts": now, "data": row}


def fetch_best_prices(item_ids, force=False):
    ids = []
    for value in item_ids or []:
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value not in ids:
            ids.append(value)
    if not ids:
        return {"ok": True, "enabled": browser_price_source_enabled(), "prices": {}, "errors": []}

    prices = {}
    missing = []
    if not force:
        for item_id in ids:
            row = _cached(item_id)
            if row is None:
                missing.append(item_id)
            else:
                prices[item_id] = row
    else:
        missing = ids[:]

    cookie = _session_cookie()
    if not missing:
        return {"ok": True, "enabled": bool(cookie), "prices": prices, "errors": []}
    if not cookie:
        return {"ok": False, "enabled": False, "prices": prices, "errors": ["ZAKUPAY_PLAY_SESSION is not configured"]}

    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": BASE_URL,
        "Referer": BASE_URL + "/core/supplier/registry",
        "User-Agent": "Mozilla/5.0",
    }
    errors = []
    session = requests.Session()
    session.cookies.set("PLAY_SESSION", cookie, domain="prodavay.sel-be.ru", path="/")

    for start in range(0, len(missing), max(1, BATCH_SIZE)):
        batch = missing[start:start + max(1, BATCH_SIZE)]
        try:
            r = session.post(PRICE_ENDPOINT, headers=headers, json=batch, timeout=30)
            if not r.ok:
                try:
                    body = r.json()
                except ValueError:
                    body = r.text[:1000]
                errors.append({"status": r.status_code, "body": body})
                continue
            data = r.json()
            if not isinstance(data, list):
                errors.append({"status": r.status_code, "body": data})
                continue
            _store(data)
            for row in data:
                if isinstance(row, dict) and row.get("orderItemId") is not None:
                    prices[int(row["orderItemId"])] = row
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    return {"ok": not errors, "enabled": True, "prices": prices, "errors": errors}


def prefetch_best_prices(orders, force=False):
    item_ids = []
    for order in orders or []:
        for item in order.get("orderItems") or []:
            if item.get("id") is not None:
                item_ids.append(item.get("id"))
    return fetch_best_prices(item_ids, force=force)


def best_price_rows_for_order(order, force=False):
    item_ids = [item.get("id") for item in (order.get("orderItems") or []) if item.get("id") is not None]
    return fetch_best_prices(item_ids, force=force)


def best_price_value(row):
    if not isinstance(row, dict):
        return None
    best = row.get("bestPrice")
    if isinstance(best, dict):
        value = best.get("priceInDefaultCurrency")
        if value is None:
            value = best.get("price")
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None
