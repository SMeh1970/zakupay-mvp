"""Experimental entrypoint for AI-assisted Zakupay analysis.

Safe wrapper around the current production app. It imports the existing
application and installs read-only analysis routes without changing the
working main.py implementation.
"""

from main import app, compact_order, esc, fetch_all_orders, has_my_offer, max_competitors
from ai_panel import install_ai_panel

install_ai_panel(
    app,
    fetch_all_orders=fetch_all_orders,
    compact_order=compact_order,
    has_my_offer=has_my_offer,
    max_competitors=max_competitors,
    esc=esc,
)

__all__ = ["app"]
