"""Opt-in, expiring, read-only diagnostics for one exact Zakupay order."""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import requests
import yaml


logger = logging.getLogger(__name__)
SCHEMA_URL = "https://swagger.cynteka.ru/specs/swagger-core.yaml"
PROFILE_FLAGS = {
    "showAllCompanies", "fromAllCompanies", "showAllRegions", "allRegions",
    "showAllCategories", "allCategories", "showNotInteresting",
}


def _resolved(value, schema):
    if isinstance(value, dict) and str(value.get("$ref", "")).startswith("#/"):
        current = schema
        for key in value["$ref"][2:].split("/"):
            current = current.get(key.replace("~1", "/").replace("~0", "~"), {})
        return current
    return value


def _order_parameters(schema):
    if not isinstance(schema, dict):
        return []
    path = (schema.get("paths") or {}).get("/api/v1/orders") or (schema.get("paths") or {}).get("/orders") or {}
    path = _resolved(path, schema)
    operation = path.get("get") or {}
    parameters = []
    for value in list(path.get("parameters") or []) + list(operation.get("parameters") or []):
        value = _resolved(value, schema)
        if isinstance(value, dict) and value.get("in") == "query":
            parameters.append(value)
    return parameters


def diagnose_order_lookup(order_id, base_url, headers, *, get=None):
    """GET only. Never log credentials, raw payloads, customer data or prices."""
    get = get or requests.get
    order_id = int(order_id)
    if order_id < 1:
        raise ValueError("Invalid order id")
    deadline = time.monotonic() + 45
    documented = set()
    try:
        response = get(SCHEMA_URL, timeout=(3, 8), allow_redirects=False)
        if response.status_code == 200:
            parameters = _order_parameters(yaml.safe_load(response.text))
            documented = {p.get("name") for p in parameters if isinstance(p.get("name"), str)}
            # The descriptions are from a public specification, not API data.
            relevant = [
                {"name": p["name"], "schema": p.get("schema") or {"type": p.get("type")},
                 "description": str(p.get("description") or "")[:400]}
                for p in parameters if p.get("name") in PROFILE_FLAGS | {"ids", "ignoreKeywordFilter", "actualityStatus", "status"}
            ]
            logger.warning("order diagnostic schema=%s", json.dumps(relevant, ensure_ascii=False))
    except Exception as exc:
        logger.warning("order diagnostic schema_error=%s", type(exc).__name__)

    common = {"ids": order_id, "format": "json", "isoDate": "true", "ignoreKeywordFilter": "true"}
    cases = [("current", common)]
    scope = {key: "true" for key in sorted(PROFILE_FLAGS & documented)}
    if scope:
        cases.append(("documented_profile_scope", {**common, **scope}))
    cases.append(("support_exact", {"ids": order_id}))
    for label, params in cases:
        if time.monotonic() >= deadline:
            logger.warning("order diagnostic order=%s stopped=deadline", order_id)
            break
        started = time.monotonic()
        try:
            response = get(base_url.rstrip("/") + "/api/v1/orders", headers=headers,
                           params=params, timeout=(3, 8), allow_redirects=False)
            data = response.json()
            from main import _orders_from_payload
            orders = _orders_from_payload(data)
            matches = [row for row in orders if str(row.get("id")) == str(order_id)]
            result = {
                "order_id": order_id, "probe": label, "params": params,
                "status": response.status_code, "seconds": round(time.monotonic() - started, 2),
                "api_result": data.get("result") if isinstance(data, dict) and isinstance(data.get("result"), bool) else None,
                "returned": len(orders), "found": bool(matches),
                "items": len(matches[0].get("orderItems") or []) if matches else 0,
            }
            logger.warning("order diagnostic result=%s", json.dumps(result, ensure_ascii=False))
        except Exception as exc:
            logger.warning("order diagnostic order=%s probe=%s error=%s", order_id, label, type(exc).__name__)


def start_configured_diagnostic(base_url, headers_factory):
    """An admin flag expires in at most 30 minutes; no polling or scheduler."""
    raw = os.getenv("ZAKUPAY_ORDER_DIAGNOSTIC", "").strip()
    if not raw:
        return
    try:
        config = json.loads(raw)
        order_id = int(config["order_id"])
        remaining = datetime.fromisoformat(config["until"]).timestamp() - datetime.now(timezone.utc).timestamp()
        if order_id < 1 or not 0 < remaining <= 1800:
            return
    except (KeyError, ValueError, TypeError):
        logger.warning("order diagnostic ignored=invalid_configuration")
        return

    def run():
        try:
            diagnose_order_lookup(order_id, base_url, headers_factory())
        except Exception as exc:
            logger.warning("order diagnostic failed=%s", type(exc).__name__)

    threading.Thread(target=run, name="zakupay-order-diagnostic", daemon=True).start()
