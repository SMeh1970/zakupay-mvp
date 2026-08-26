"""Experimental entrypoint for AI-assisted Zakupay analysis.

Safe wrapper around the current production app. The AI test panel deliberately
uses one upstream Zakupay request per refresh so login/redirect does not sit
waiting while the experimental paginator scans many pages.
"""

from main import (
    app,
    compact_order,
    esc,
    has_my_offer,
    max_competitors,
    request_orders_page,
)
from ai_panel import install_ai_panel


def fast_fetch_orders(force: bool = False):
    """Fetch one current Zakupay batch for the interactive AI test panel.

    This is intentionally bounded for responsiveness. The production/full
    collector remains unchanged in main.py while pagination is validated.
    """
    data = request_orders_page(page=1, page_size=100, api_filters=None)
    return data.get("orders") or []


install_ai_panel(
    app,
    fetch_all_orders=fast_fetch_orders,
    compact_order=compact_order,
    has_my_offer=has_my_offer,
    max_competitors=max_competitors,
    esc=esc,
)

__all__ = ["app"]
