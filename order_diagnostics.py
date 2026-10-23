"""Opt-in, expiring, read-only diagnostics for one exact Zakupay order."""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

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
    paths = schema.get("paths") or {}
    # Cynteka labels buyer/supplier variants directly in its Swagger path keys.
    path = paths.get("/api/v1/orders (поставщик)") or paths.get("/api/v1/orders") or paths.get("/orders") or {}
    path = _resolved(path, schema)
    operation = _resolved(path.get("get") or {}, schema)
    parameters = []
    for value in list(path.get("parameters") or []) + list(operation.get("parameters") or []):
        value = _resolved(value, schema)
        if isinstance(value, dict) and value.get("in") == "query":
            parameters.append(value)
    return parameters


def diagnose_order_lookup(order_id, base_url, headers, *, get=None, cookie_factory=None):
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
            # Parse bytes: Requests can guess Latin-1 for UTF-8 YAML without a
            # charset, producing C1 control characters in Russian descriptions.
            schema = yaml.safe_load(response.content)
            metadata = {"type": type(schema).__name__}
            if isinstance(schema, dict):
                metadata["keys"] = sorted(str(key) for key in schema)
                metadata["order_paths"] = [str(key) for key in (schema.get("paths") or {}) if "order" in str(key).lower()][:20]
            logger.warning("order diagnostic schema_metadata=%s", json.dumps(metadata, ensure_ascii=False))
            parameters = _order_parameters(schema)
            documented = {p.get("name") for p in parameters if isinstance(p.get("name"), str)}
            logger.warning("order diagnostic parameter_names=%s", json.dumps(sorted(documented), ensure_ascii=False))
            # The descriptions are from a public specification, not API data.
            relevant = [
                {"name": p["name"], "schema": p.get("schema") or {"type": p.get("type")},
                 "description": str(p.get("description") or "")[:400]}
                for p in parameters
            ]
            logger.warning("order diagnostic schema=%s", json.dumps(relevant, ensure_ascii=False))
    except Exception as exc:
        logger.warning("order diagnostic schema_error=%s", type(exc).__name__)

    common = {"ids": order_id, "format": "json", "isoDate": "true", "ignoreKeywordFilter": "true"}
    cases = [("current", common, headers, None),
             ("visible_feed_control", {"format": "json", "isoDate": "true", "pageSize": 1, "page": 1}, headers, None)]
    # Reuse only the already configured cabinet session, on its exact origin.
    # Redirects are disabled below, so the cookie cannot leave that origin.
    parsed = urlparse(base_url)
    if cookie_factory and parsed.scheme == "https" and parsed.netloc == "prodavay.sel-be.ru":
        cookie = cookie_factory()
        if cookie:
            cookies = {"PLAY_SESSION": cookie}
            cases.extend([
                ("cabinet_session_only", common, {"Accept": "application/json"}, cookies),
                ("token_and_cabinet_session", common, headers, cookies),
            ])
    for label, params, request_headers, cookies in cases:
        if time.monotonic() >= deadline:
            logger.warning("order diagnostic order=%s stopped=deadline", order_id)
            break
        started = time.monotonic()
        try:
            kwargs = dict(headers=request_headers, params=params, timeout=(3, 8), allow_redirects=False)
            if cookies:
                kwargs["cookies"] = cookies
            response = get(base_url.rstrip("/") + "/api/v1/orders", **kwargs)
            data = response.json()
            from main import _orders_from_payload
            orders = _orders_from_payload(data)
            lookup_id = params.get("ids", order_id)
            matches = [row for row in orders if str(row.get("id")) == str(lookup_id)]
            result = {
                "order_id": order_id, "queried_order_id": lookup_id, "probe": label, "params": params,
                "status": response.status_code, "seconds": round(time.monotonic() - started, 2),
                "api_result": data.get("result") if isinstance(data, dict) and isinstance(data.get("result"), bool) else None,
                "returned": len(orders), "found": bool(matches),
                "items": len(matches[0].get("orderItems") or []) if matches else 0,
            }
            if matches:
                from main import has_my_offer
                result["has_my_offer"] = has_my_offer(matches[0])
                result["actuality"] = matches[0].get("actualityStatus") if matches[0].get("actualityStatus") in {"ACTUAL", "NOT_ACTUAL", "CLOSED"} else None
            if isinstance(data, dict):
                result["pagination"] = {key: data[key] for key in ("count", "page", "pageSize") if isinstance(data.get(key), (int, float))}
            logger.warning("order diagnostic result=%s", json.dumps(result, ensure_ascii=False))
            if label == "visible_feed_control" and orders:
                # The control order is already visible to this same token.
                # Verify the filter independently of the inaccessible target.
                control_id = int(orders[0].get("id") or 0)
                if control_id > 0:
                    cases.extend([
                        ("visible_id_control", {**common, "ids": control_id}, headers, None),
                        ("visible_id_control_without_keyword_flag", {"ids": control_id, "format": "json", "isoDate": "true"}, headers, None),
                    ])
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
            from browser_prices import _session_cookie
            diagnose_order_lookup(order_id, base_url, headers_factory(), cookie_factory=_session_cookie)
        except Exception as exc:
            logger.warning("order diagnostic failed=%s", type(exc).__name__)

    threading.Thread(target=run, name="zakupay-order-diagnostic", daemon=True).start()
