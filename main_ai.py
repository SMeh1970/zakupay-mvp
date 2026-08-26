"""Experimental entrypoint for AI-assisted Zakupay analysis."""

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
