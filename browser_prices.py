import os
import hashlib
import time
import math
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
    return os.getenv("ZAKUPAY_BROWSER_PRICES_ENABLED", "").lower() == "true" and bool(_session_cookie())


def session_revision():
    """Opaque cache key: a session replacement must invalidate old diagnostics."""
    return hashlib.sha256(_session_cookie().encode('utf-8')).hexdigest()


def _item_id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def fetch_best_prices(item_ids, force=False):
    """Experimental server-side adapter. Never use integration tokens here."""
    ids = list(dict.fromkeys(i for v in item_ids or [] if (i := _item_id(v)) is not None))
    cookie = _session_cookie()
    enabled = browser_price_source_enabled()
    def result(prices, errors):
        return {"ok": not errors, "enabled": enabled, "prices": prices, "errors": errors}
    if not enabled:
        return result({}, ["not_configured"])
    if BASE_URL != "https://prodavay.sel-be.ru":
        return result({}, ["unsupported_origin"])
    revision = session_revision()
    prices = {}
    with _cache_lock:
        # Session removal/replacement must never expose another account's cache.
        for key in list(_cache):
            if key[0] != revision or time.time() - _cache[key]["ts"] >= CACHE_TTL:
                del _cache[key]
        if not force:
            prices = {i: _cache[(revision, i)]["data"] for i in ids if (revision, i) in _cache}
    missing = [i for i in ids if i not in prices]
    errors = []
    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "X-Requested-With": "XMLHttpRequest", "Origin": BASE_URL,
               "Referer": BASE_URL + "/core/supplier/registry"}
    with requests.Session() as session:
        session.cookies.set("PLAY_SESSION", cookie, domain="prodavay.sel-be.ru", path="/")
        for start in range(0, len(missing), max(1, BATCH_SIZE)):
            batch = missing[start:start + max(1, BATCH_SIZE)]
            try:
                response = session.post(PRICE_ENDPOINT, headers=headers, json=batch,
                                        timeout=30, allow_redirects=False)
                if response.status_code in (401, 403) or 300 <= response.status_code < 400:
                    with _cache_lock:
                        _cache.clear()
                    return result({}, ["forbidden" if response.status_code == 403 else "unauthorized"])
                if response.status_code != 200:
                    errors.append("upstream_error")
                    continue
                data = response.json()
                if not isinstance(data, list):
                    errors.append("invalid_contract")
                    continue
                found = {}
                invalid = set()
                for row in data:
                    item_id = _item_id(row.get("orderItemId")) if isinstance(row, dict) else None
                    if item_id not in batch:
                        errors.append("invalid_contract")
                        continue
                    if item_id in found:
                        invalid.add(item_id)
                        errors.append("invalid_contract")
                    found[item_id] = row
                for item_id in invalid:
                    del found[item_id]
                with _cache_lock:
                    for item_id, row in found.items():
                        _cache[(revision, item_id)] = {"ts": time.time(), "data": row}
                prices.update(found)
            except requests.RequestException:
                errors.append("connection_error")
            except ValueError:
                errors.append("invalid_contract")
    return result(prices, list(dict.fromkeys(errors)))


def price_state(result, row=None):
    errors = result.get("errors") or []
    labels = {"not_configured": "Источник лучшей цены не настроен",
              "unauthorized": "Требуется авторизация в Закупай",
              "forbidden": "Нет прав на просмотр лучшей цены",
              "invalid_contract": "Формат ответа Закупай изменился",
              "connection_error": "Источник цен временно недоступен",
              "upstream_error": "Источник цен временно недоступен",
              "unsupported_origin": "Адрес источника цен не поддерживается"}
    if best_price_value(row) is not None:
        return "Закупай · bestPrice (экспериментальный источник)"
    if errors:
        return labels.get(errors[0], "Лучшая цена недоступна")
    if isinstance(row, dict) and row.get("offerDeviation") is not None:
        return "Доступен только коридор цен"
    return "Лучшая цена не предоставлена"


def prefetch_best_prices(orders, force=False):
    orders = list(orders or [])
    # The analysis dashboard may contain hundreds/thousands of active orders.
    # Bulk-prefetching every item's browser-session price before rendering makes
    # the whole page wait on many external HTTP calls. The dashboard currently
    # does not use these prefetched prices for its heuristic score, so skip this
    # expensive warm-up for large result sets. Detailed order pages still pass a
    # single order and therefore keep exact best-price loading unchanged.
    max_prefetch_orders = int(os.getenv("ZAKUPAY_BROWSER_PRICE_PREFETCH_MAX_ORDERS", "20"))
    if len(orders) > max_prefetch_orders:
        return {
            "ok": True,
            "enabled": browser_price_source_enabled(),
            "prices": {},
            "errors": [],
            "skipped": True,
            "reason": f"too_many_orders:{len(orders)}",
        }

    item_ids = []
    for order in orders:
        for item in order.get("orderItems") or []:
            if item.get("id") is not None:
                item_ids.append(item.get("id"))
    return fetch_best_prices(item_ids, force=force)


def best_price_rows_for_order(order, force=False):
    item_ids = [item.get("id") for item in (order.get("orderItems") or []) if item.get("id") is not None]
    return fetch_best_prices(item_ids, force=force)


def best_price_value(row):
    if not isinstance(row, dict) or not isinstance(row.get("bestPrice"), dict):
        return None
    best = row["bestPrice"]
    value = best.get("priceInDefaultCurrency")
    if value is None:
        # The UI displays rubles; do not silently label foreign currency as RUB.
        if best.get("currencyCode") not in (None, "RUB", "RUR"):
            return None
        value = best.get("price")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None
