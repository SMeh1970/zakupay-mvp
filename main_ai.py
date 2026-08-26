from urllib.parse import parse_qsl, urlencode

from main import app, api_filter_dict, compact_order, esc, fetch_all_orders, filter_orders, has_my_offer, max_competitors
from ai_panel import install_ai_panel
from supplier_panel import install_supplier_panel

_OPTIONAL_NUMERIC_QUERY_FIELDS = {
    "max_competitors",
    "min_positions",
    "min_score",
    "min_estimated_total",
    "delayFrom",
    "delayTo",
    "senderId",
    "company",
    "category_id",
    "region_id",
}


def filter_orders_ai(orders, payment="all", region="", category="", min_positions=0, max_competitors_value=None, only_without_my_offer=False):
    # Закупай возвращает в поле region конкретный город/область, а не страну.
    # Поэтому ввод "Россия" должен означать "все регионы России", а не искать
    # буквальное слово "Россия" внутри названия региона.
    normalized_region = (region or "").strip()
    if normalized_region.lower() in {"россия", "рф", "russia", "russian federation"}:
        normalized_region = ""
    return filter_orders(
        orders,
        payment,
        normalized_region,
        category,
        min_positions,
        max_competitors_value,
        only_without_my_offer,
    )


@app.middleware("http")
async def strip_empty_optional_numeric_filters(request, call_next):
    raw_query = request.scope.get("query_string", b"").decode("utf-8", errors="ignore")
    if raw_query:
        pairs = parse_qsl(raw_query, keep_blank_values=True)
        cleaned = [
            (key, value)
            for key, value in pairs
            if not (key in _OPTIONAL_NUMERIC_QUERY_FIELDS and value.strip() == "")
        ]
        if cleaned != pairs:
            request.scope["query_string"] = urlencode(cleaned, doseq=True).encode("utf-8")
    return await call_next(request)


install_ai_panel(
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
