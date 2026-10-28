"""Account-scoped filter preferences persisted alongside saved requests."""
import hmac
import html
import json
import secrets
import re
from contextlib import closing

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from saved_dashboard import DEFAULTS, URL, filters_from_query

SCHEMA = "CREATE TABLE IF NOT EXISTS panel_filter_preferences (username TEXT PRIMARY KEY, filters_json TEXT NOT NULL, enabled INTEGER NOT NULL)"


def load_preferences(conn, execute, username):
    row = execute(conn, "SELECT filters_json, enabled FROM panel_filter_preferences WHERE username=?", (username,)).fetchone()
    if not row:
        return None
    filters, _ = filters_from_query(json.loads(row["filters_json"]))
    return {"filters": filters, "enabled": bool(row["enabled"])}


def install_preferences(app, connect, execute, username):
    @app.get(URL + "/filters")
    async def filters_page():
        # Browser navigation (including returning from login) must not save settings.
        return RedirectResponse(URL, status_code=303)

    @app.post(URL + "/filters")
    async def update_filters(request: Request):
        form = await request.form()
        token = request.cookies.get("panel_filter_csrf", "")
        if not token or not hmac.compare_digest(token, str(form.get("csrf", ""))):
            raise HTTPException(403, "Обновите страницу и повторите действие.")
        with closing(connect()) as conn, conn:
            existing = load_preferences(conn, execute, username)
            enabled = form.get("enabled") == "on"
            if form.get("action") == "toggle":
                filters = existing["filters"] if existing else DEFAULTS.copy()
            else:
                filters, errors = filters_from_query(form)
                if errors:
                    return HTMLResponse("<html lang='ru'><meta charset='utf-8'><h1>Проверьте фильтры</h1><p>"
                                        + html.escape(" ".join(errors))
                                        + f"</p><p>Предыдущие настройки сохранены.</p><a href='{URL}'>Вернуться к фильтрам</a></html>", status_code=422)
            execute(conn, "INSERT INTO panel_filter_preferences (username, filters_json, enabled) VALUES (?, ?, ?) ON CONFLICT(username) DO UPDATE SET filters_json=excluded.filters_json, enabled=excluded.enabled",
                    (username, json.dumps(filters, ensure_ascii=False), int(enabled)))
            conn.commit()
        return RedirectResponse(URL, status_code=303)


def csrf_token(request):
    token = request.cookies.get("panel_filter_csrf", "")
    return token if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) else secrets.token_urlsafe(32)
