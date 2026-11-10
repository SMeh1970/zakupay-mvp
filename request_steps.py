"""One request, several stages: Заявка → Мониторинг → Счёт → Оплата → Отгрузка.

The bar of stages is shown on top of every page of a request; each stage is a tab with its state.
«Оплата» and «Отгрузка» are not kept in the app yet — they are shown as «позже».
"""
import html
import json

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

STEPS_CSS = (
    ".steps{display:flex;flex-wrap:wrap;gap:0;margin:0 0 14px;font:14px/1.35 'IBM Plex Sans',system-ui,Arial,sans-serif}"
    ".steps a,.steps span.step{display:flex;flex-direction:column;gap:1px;padding:8px 18px 8px 22px;text-decoration:none;"
    "color:#1d221f;background:#fff;border:1px solid #d7dcd6;margin-right:-1px;position:relative}"
    ".steps>:first-child{border-radius:8px 0 0 8px;padding-left:14px}.steps>:last-child{border-radius:0 8px 8px 0}"
    ".steps b{font-weight:600}.steps small{color:#5f6a63;font-size:12px}"
    ".steps .on{background:#e4ebf4;border-color:#3d5a80;z-index:1}.steps .on b{color:#3d5a80}"
    ".steps .done small{color:#2f6b3f}.steps span.step{color:#9aa59d;cursor:default}"
    ".steps a:hover{background:#f3f4f1}.steps-back{font:14px 'IBM Plex Sans',system-ui,Arial,sans-serif;margin:0 0 8px}"
    ".steps-back a{color:#3d5a80}"
)


def _stage_states(row, result):
    items = (result or {}).get("items") or []
    chosen = sum(1 for item in items if item.get("selected") and item.get("decision") == "approved")
    sent = bool((result or {}).get("live_offer_created") or row["status"] == "offer_created")
    invoice = row["invoice_number"]
    return {
        "request": (f"{len(items)} поз.", bool(items)),
        "podbor": (f"выбрано {chosen} из {len(items)}" if items else "поиск не запускался", bool(items) and chosen == len(items)),
        "invoice": ("выставлен в Закупай" if sent else f"№ {invoice}" if invoice else "не сформирован", sent),
    }


def steps_html(job_id, active, row, result):
    """The bar of stages; `active` is one of request / podbor / invoice."""
    states = _stage_states(row, result)
    base = f"/dashboard/automation/jobs/{int(job_id)}"
    tabs = [("request", "Заявка", base + "/request"), ("podbor", "Мониторинг", base + "/podbor"),
            ("invoice", "Счёт", base + "/review")]
    parts = []
    for key, title, href in tabs:
        note, done = states[key]
        cls = " ".join(c for c in ("on" if key == active else "", "done" if done else "") if c)
        parts.append(f"<a class='{cls}' href='{href}'{' aria-current=page' if key == active else ''}>"
                     f"<b>{title}</b><small>{html.escape(note)}</small></a>")
    for title in ("Оплата", "Отгрузка"):
        parts.append(f"<span class='step' title='Будет позже'><b>{title}</b><small>позже</small></span>")
    return (f"<style>{STEPS_CSS}</style><p class='steps-back'><a href='/dashboard/automation'>← Все заявки</a></p>"
            f"<nav class='steps' aria-label='Этапы заявки'>{''.join(parts)}</nav>")


def install(app):
    from automation_pipeline import _connect, _execute
    from invoice_generator import normalize_customer
    from supplier_comparison import _session

    @app.get("/dashboard/automation/jobs/{job_id}/request", response_class=HTMLResponse)
    def request_page(job_id: int, request: Request):
        _session(request)
        with _connect() as conn:
            row = _execute(conn, "SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
            cached = None
            if row and row["event_type"] != "manual_order":
                cached = _execute(conn, "SELECT state,checked_at FROM order_actuality WHERE order_id=?", (row["order_id"],)).fetchone()
        if not row:
            raise HTTPException(404, "Заявка не найдена")
        result = json.loads(row["result_json"]) if row["result_json"] else {}
        order = json.loads(row["order_json"]) if row["order_json"] else {}
        is_manual = row["event_type"] == "manual_order"
        heading = (f"Ручная заявка {html.escape(str(result.get('manual_reference') or 'без номера'))}"
                   if is_manual else f"Заявка Закупай № {html.escape(str(row['order_id']))}")
        customer = normalize_customer(result.get("customer") or order.get("customer"))
        import secrets
        nonce = secrets.token_urlsafe(18)
        banner = ""
        if not is_manual:
            from order_actuality import banner as actuality_banner
            banner = actuality_banner(row["order_id"], job_id, nonce, cached)
        items = result.get("items") or []
        if not items:
            items = [{"position": i + 1, "requested_name": p.get("name") or p.get("title") or "",
                      "quantity": p.get("count") or p.get("quantity"), "unit": p.get("unit") or p.get("measure") or ""}
                     for i, p in enumerate(order.get("items") or order.get("orderItems") or []) if isinstance(p, dict)]
        rows = "".join(
            f"<tr><td class='num'>{html.escape(str(item.get('position') or ''))}</td>"
            f"<td><a href='/dashboard/automation/jobs/{job_id}/podbor#p{html.escape(str(item.get('position')), quote=True)}'>"
            f"{html.escape(str((item.get('comparison_source') or item).get('requested_name') or ''))}</a></td>"
            f"<td class='num'>{html.escape(str((item.get('comparison_source') or item).get('quantity') or ''))}</td>"
            f"<td>{html.escape(str((item.get('comparison_source') or item).get('unit') or ''))}</td></tr>"
            for item in items)
        customer_line = (f"{html.escape(customer['name'])}" + (f" · ИНН {html.escape(customer['inn'])}" if customer.get("inn") else "")
                         if customer.get("name") else "не указан")
        body = (
            "<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Заявка · {heading}</title><style>"
            "body{margin:0;background:#f3f4f1;color:#1d221f;font:15px/1.5 'IBM Plex Sans',system-ui,Arial,sans-serif}"
            "main{max-width:1280px;margin:0 auto;padding:16px}section{background:#fff;border:1px solid #d7dcd6;border-radius:8px;padding:14px 16px;margin:0 0 14px}"
            "h1{font-size:20px;font-weight:600;margin:0 0 6px}table{border-collapse:collapse;width:100%;font-size:14px}"
            "th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #d7dcd6;vertical-align:top}th{color:#5f6a63;font-size:12px}"
            ".num{text-align:right;white-space:nowrap}a{color:#2b5fb4}.meta{color:#5f6a63;font-size:14px;margin:2px 0}"
            "</style></head><body><main>"
            + steps_html(job_id, "request", row, result)
            + f"<section><h1>{heading}</h1>"
            f"<p class='meta'>Заказчик: {customer_line}</p>"
            f"<p class='meta'>Позиций: {len(items)}</p>{banner}</section>"
            "<section><table><thead><tr><th class='num'>№</th><th>Наименование</th><th class='num'>Кол-во</th><th>Ед.</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></section></main></body></html>"
        )
        return HTMLResponse(body, headers={"Cache-Control": "no-store", "Content-Security-Policy": (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; connect-src 'self'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")})
