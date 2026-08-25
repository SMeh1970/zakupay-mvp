"""Render entrypoint.

The production service historically starts uvicorn app:app. Keep that
contract while the implementation lives in main.py.
"""

from main import app

__all__ = ["app"]
