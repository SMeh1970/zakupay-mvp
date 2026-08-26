import os
from urllib.parse import parse_qsl, urlencode

from main import app, api_filter_dict, compact_order, esc, fetch_all_orders, filter_orders, has_my_offer, max_competitors
import ai_panel
from ai_panel import install_ai_panel
from price_estimator import analyze_order_v2
from supplier_panel import install_supplier_panel

# Replace the first MVP estimator with the v2 estimator.
ai_panel.analyze_order = analyze_order_v2

_OPTIONAL_NUMERIC_QUERY_FIELDS = {
    "max_competitors", "min_positions", "min_score", "min_estimated_total",
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


def _looks_like_non_purchase_request(order):
    title = str(order.get("name") or "").lower().replace("ё", "е")
    return any(keyword.lower().replace("ё", "е") in title for keyword in _non_purchase_keywords())


# The checkbox value is passed only for the current request, avoiding global state.
_filter_context = {"exclude_non_purchase": True}


def filter_orders_ai(orders, payment="all", region="", category="", min_positions=0,
                     max_competitors_value=None, only_without_my_offer=False):
    normalized_region = (region or "").strip()
    if normalized_region.lower() in {"россия", "рф", "russia", "russian federation"}:
        normalized_region = ""
    filtered = filter_orders(
        orders, payment, normalized_region, category, min_positions,
        max_competitors_value, only_without_my_offer,
    )
    if _filter_context.get("exclude_non_purchase", True):
        filtered = [o for o in filtered if not _looks_like_non_purchase_request(o)]
    return filtered


# Wrap the AI panel's request flow so a normal query checkbox controls exclusion.
_original_install = install_ai_panel


def install_ai_panel_with_non_purchase_checkbox(app, **kwargs):
    # ai_panel builds the HTML internally. We pass a filter function that reads
    # a request-scoped query value populated by middleware below. Middleware also
    # injects a checked checkbox into the generated analysis form.
    return _original_install(app, **kwargs)


@app.middleware("http")
async def analysis_filter_middleware(request, call_next):
    raw_query = request.scope.get("query_string", b"").decode("utf-8", errors="ignore")
    pairs = parse_qsl(raw_query, keep_blank_values=True) if raw_query else []
    query = dict(pairs)

    # Default is ON. Browser sends exclude_non_purchase=0 via hidden input and
    # exclude_non_purchase=1 when the checkbox is checked.
    if request.url.path in {"/dashboard/analysis", "/analysis/ranked"}:
        values = [v for k, v in pairs if k == "exclude_non_purchase"]
        if values:
            _filter_context["exclude_non_purchase"] = values[-1].lower() not in {"0", "false", "no", "off", ""}
        else:
            _filter_context["exclude_non_purchase"] = True

    cleaned = [
        (key, value) for key, value in pairs
        if not (key in _OPTIONAL_NUMERIC_QUERY_FIELDS and value.strip() == "")
        and key != "exclude_non_purchase"
    ]
    if cleaned != pairs:
        request.scope["query_string"] = urlencode(cleaned, doseq=True).encode("utf-8")

    response = await call_next(request)

    # Add the checkbox to the existing HTML without rewriting the whole panel.
    if request.url.path == "/dashboard/analysis" and response.headers.get("content-type", "").startswith("text/html"):
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        text = body.decode("utf-8")
        checked = "checked" if _filter_context.get("exclude_non_purchase", True) else ""
        control = (
            "<div class='check'>"
            "<input type='hidden' name='exclude_non_purchase' value='0'>"
            f"<input type='checkbox' id='exclude_non_purchase' name='exclude_non_purchase' value='1' {checked}>"
            "<label for='exclude_non_purchase' style='margin:0'>Исключать тендеры, расчёты и сбор оценочных предложений</label>"
            "</div>"
        )
        marker = "<div class='filters'>"
        text = text.replace(marker, marker + control, 1)
        from starlette.responses import Response
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(content=text, status_code=response.status_code, headers=headers, media_type="text/html")
    return response


install_ai_panel_with_non_purchase_checkbox(
    app,
    fetch_all_orders=fetch_all_orders,
    compact_order=compact_order,
    filter_orders=filter_orders_ai,
    api_filter_dict=api_filter_dict,
    has_my_offer=has_my_offer,
    max_competitors=max_competitors,
    esc=esc,
)
install_supplier_panel(app, esc=esc)

__all__ = ["app"]
