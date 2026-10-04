import os
import hmac
import secrets
import time
import re
from urllib.parse import parse_qsl, urlencode

from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from main import (
    app, api_filter_dict, compact_order, esc, fetch_all_orders, fetch_order_by_id, filter_orders,
    has_my_offer, max_competitors, zakupay_headers, ZAKUPAY_BASE_URL,
)
from automation_pipeline import (
    begin_offer_submission,
    commercial_order_changes,
    commercial_order_hash,
    enrich_automation_offer_context,
    finish_offer_submission,
    install_automation_pipeline,
    latest_offer_submission,
    load_automation_offer_context,
    mark_automation_offer_created,
    saved_job_id,
)
import ai_panel
from ai_panel_v2 import install_ai_panel_v2
from analysis_detail import install_analysis_detail
from api_discovery import install_api_discovery
from offer_panel_safe import install_offer_panel
from invoice_generator import build_invoice_xlsx
from price_estimator import analyze_order_v2
from supplier_panel import install_supplier_panel
from procurement import install_procurement
from security import OAUTH_SCOPE, PANEL_USERNAME, _origin, _sign_payload, _valid_session, current_mcp_resource
from abacus_mcp import install_abacus_mcp

ai_panel.analyze_order = analyze_order_v2

DEPLOY_MARKER = "unified-saved-dashboard-2026-10-04"


@app.get("/version")
def version_marker():
    return {"deploy": DEPLOY_MARKER, "offer_panel": True, "abacus_mcp": True}


_OPTIONAL_NUMERIC_QUERY_FIELDS = {
    "max_competitors", "min_positions", "min_score", "min_estimated_total", "order_id",
    "delayFrom", "delayTo", "senderId", "company", "category_id", "region_id",
}

_DEFAULT_NON_PURCHASE_TITLE_KEYWORDS = [
    "тендер", "расчет", "расчёт", "для расчета", "для расчёта",
    "предварительный расчет", "предварительный расчёт", "оценка стоимости",
    "оценочная стоимость", "сбор предложений", "сбор коммерческих предложений",
    "запрос коммерческого предложения", "запрос кп", "мониторинг цен",
    "анализ цен", "исследование рынка", "бюджетирование", "для бюджета",
]


def _non_purchase_keywords():
    raw = os.getenv("NON_PURCHASE_TITLE_KEYWORDS", "").strip()
    if not raw:
        return _DEFAULT_NON_PURCHASE_TITLE_KEYWORDS
    return [x.strip().lower().replace("ё", "е") for x in raw.split(",") if x.strip()]


def looks_like_non_purchase_request(order):
    title = str(order.get("name") or "").lower().replace("ё", "е")
    return any(keyword.lower().replace("ё", "е") in title for keyword in _non_purchase_keywords())


def filter_orders_ai(orders, payment="all", region="", category="", min_positions=0,
                     max_competitors_value=None, only_without_my_offer=False):
    normalized_region = (region or "").strip()
    if normalized_region.lower() in {"россия", "рф", "russia", "russian federation"}:
        normalized_region = ""
    return filter_orders(
        orders, payment, normalized_region, category, min_positions,
        max_competitors_value, only_without_my_offer,
    )


def _replace_authorization_header(request, value: str) -> None:
    headers = []
    replaced = False
    for key, existing in request.scope.get("headers", []):
        if key.lower() == b"authorization":
            headers.append((key, value.encode("latin-1")))
            replaced = True
        else:
            headers.append((key, existing))
    if not replaced:
        headers.append((b"authorization", value.encode("latin-1")))
    request.scope["headers"] = headers


def _mint_abacus_access_token(request) -> str:
    now = int(time.time())
    resource = current_mcp_resource(request)
    payload = {
        "sub": PANEL_USERNAME,
        "client_id": "abacus-static",
        "aud": resource,
        "iss": _origin(request),
        "scope": OAUTH_SCOPE,
        "iat": now,
        "nbf": now - 5,
        "exp": now + 300,
        "jti": secrets.token_urlsafe(12),
    }
    return _sign_payload(payload, "oauth-access", "za_at")


@app.middleware("http")
async def panel_request_cleanup(request, call_next):
    path = request.url.path

    # Legacy compatibility for clients using the OAuth-protected /mcp endpoint.
    if path == "/mcp":
        configured = os.getenv("ABACUS_MCP_TOKEN", "").strip()
        authorization = request.headers.get("authorization", "")
        scheme, _, supplied = authorization.partition(" ")
        if (
            configured
            and scheme.lower() == "bearer"
            and supplied
            and hmac.compare_digest(supplied.strip(), configured)
        ):
            internal_token = _mint_abacus_access_token(request)
            _replace_authorization_header(request, f"Bearer {internal_token}")

    legacy_detail = re.fullmatch(r"/dashboard/(?:analysis/)?order/(\d+)/?", path)
    if request.method in {"GET", "HEAD"} and legacy_detail and _valid_session(request):
        order_id = int(legacy_detail.group(1))
        job_id = await run_in_threadpool(saved_job_id, order_id)
        target = f"/dashboard/automation/jobs/{job_id}/review" if job_id else "/dashboard/automation?" + urlencode({"order_id": order_id})
        return RedirectResponse(url=target, status_code=303, headers={"Cache-Control": "no-store"})

    raw_query = request.scope.get("query_string", b"").decode("utf-8", errors="ignore")
    if raw_query:
        pairs = parse_qsl(raw_query, keep_blank_values=True)
        cleaned = [
            (key, value) for key, value in pairs
            if not (key in _OPTIONAL_NUMERIC_QUERY_FIELDS and value.strip() == "")
        ]
        if cleaned != pairs:
            request.scope["query_string"] = urlencode(cleaned, doseq=True).encode("utf-8")

    response = await call_next(request)

    return response


install_ai_panel_v2(
    app,
    fetch_all_orders=fetch_all_orders,
    compact_order=compact_order,
    filter_orders=filter_orders_ai,
    api_filter_dict=api_filter_dict,
    has_my_offer=has_my_offer,
    max_competitors=max_competitors,
    esc=esc,
    non_purchase_predicate=looks_like_non_purchase_request,
)
install_analysis_detail(
    app,
    fetch_all_orders=fetch_all_orders,
    zakupay_headers=zakupay_headers,
    zakupay_base_url=ZAKUPAY_BASE_URL,
    esc=esc,
    fetch_order_by_id=fetch_order_by_id,
)
install_api_discovery(
    app,
    fetch_all_orders=fetch_all_orders,
    zakupay_headers=zakupay_headers,
    zakupay_base_url=ZAKUPAY_BASE_URL,
)
install_supplier_panel(app, esc=esc)
install_offer_panel(
    app,
    fetch_all_orders=fetch_all_orders,
    zakupay_headers=zakupay_headers,
    zakupay_base_url=ZAKUPAY_BASE_URL,
    esc=esc,
    fetch_order_by_id=fetch_order_by_id,
    load_offer_context=load_automation_offer_context,
    enrich_offer_context=enrich_automation_offer_context,
    mark_offer_created=mark_automation_offer_created,
    begin_submission=begin_offer_submission,
    finish_submission=finish_offer_submission,
    latest_submission=latest_offer_submission,
    order_hash=commercial_order_hash,
    order_changes=commercial_order_changes,
    build_invoice=build_invoice_xlsx,
)
install_automation_pipeline(
    app,
    fetch_order_by_id=fetch_order_by_id,
    fetch_all_orders=fetch_all_orders,
    has_my_offer=has_my_offer,
)
install_procurement(app)
install_abacus_mcp(
    app,
    fetch_all_orders=fetch_all_orders,
    filter_orders=filter_orders_ai,
    compact_order=compact_order,
)

__all__ = ["app"]
