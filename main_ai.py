"""Experimental entrypoint for AI-assisted Zakupay analysis."""

from urllib.parse import parse_qsl, urlencode

from main import (
    app,
    api_filter_dict,
    compact_order,
    esc,
    fetch_all_orders,
    filter_orders,
    has_my_offer,
    max_competitors,
)
from ai_panel import install_ai_panel


# HTML forms submit empty numeric fields as ?field=.
# FastAPI rejects those before the route handler can run because an empty string
# cannot be parsed as int. Strip only empty values for optional numeric filters;
# filled values are preserved unchanged.
_OPTIONAL_NUMERIC_QUERY_FIELDS = {
    "max_competitors",
    "delayFrom",
    "delayTo",
    "senderId",
    "company",
    "category_id",
    "region_id",
}


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
    filter_orders=filter_orders,
    api_filter_dict=api_filter_dict,
    has_my_offer=has_my_offer,
    max_competitors=max_competitors,
    esc=esc,
)

__all__ = ["app"]
