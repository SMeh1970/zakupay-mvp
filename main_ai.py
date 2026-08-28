import os
from urllib.parse import parse_qsl, urlencode

from main import (
    app, api_filter_dict, compact_order, esc, fetch_all_orders, filter_orders,
    has_my_offer, max_competitors, zakupay_headers, ZAKUPAY_BASE_URL,
)
import ai_panel
from ai_panel_v2 import install_ai_panel_v2
from offer_panel import install_offer_panel
from price_estimator import analyze_order_v2
from supplier_panel import install_supplier_panel

# Use v2 price estimator: competitor price when nomenclature matches, then supplier/catalog prices,
# then cautious fallback estimation for amount filtering.
ai_panel.analyze_order = analyze_order_v2

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


@app.middleware("http")
async def strip_empty_optional_numeric_filters(request, call_next):
    raw_query = request.scope.get("query_string", b"").decode("utf-8", errors="ignore")
    if raw_query:
        pairs = parse_qsl(raw_query, keep_blank_values=True)
        cleaned = [
            (key, value) for key, value in pairs
            if not (key in _OPTIONAL_NUMERIC_QUERY_FIELDS and value.strip() == "")
        ]
        if cleaned != pairs:
            request.scope["query_string"] = urlencode(cleaned, doseq=True).encode("utf-8")
    return await call_next(request)


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
install_supplier_panel(app, esc=esc)
install_offer_panel(
    app,
    fetch_all_orders=fetch_all_orders,
    zakupay_headers=zakupay_headers,
    zakupay_base_url=ZAKUPAY_BASE_URL,
    esc=esc,
)

__all__ = ["app"]
