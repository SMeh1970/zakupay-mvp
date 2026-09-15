"""Inbound email -> Zakupay order -> VI matching automation pipeline.

The module deliberately stops at a reviewable offer draft.  Uploading an invoice
and creating a live offer require explicit commercial configuration and are kept
behind the existing confirmed offer form.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import math
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser

from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from supplier_adapters import VseinstrumentiAdapter
from vi_order_match import _identifiers, _label, _measurements, _norm, _score_details
from zakupay_email import parse_zakupay_email
from invoice_generator import build_invoice_xlsx


DB_PATH = os.getenv("AUTOMATION_DB_PATH", "automation.db")
WEBHOOK_SECRET = os.getenv("ZAKUPAY_EMAIL_WEBHOOK_SECRET", "").strip()
AUTO_MATCH_THRESHOLD = float(os.getenv("AUTO_MATCH_THRESHOLD", "0.88"))
REVIEW_MATCH_THRESHOLD = float(os.getenv("REVIEW_MATCH_THRESHOLD", "0.72"))
DEFAULT_MARKUP = float(os.getenv("AUTO_OFFER_MARKUP", "0.05"))
DEFAULT_VAT_RATE = float(os.getenv("AUTO_OFFER_VAT_RATE", "0.22"))
DEFAULT_PREPAYMENT_PERCENT = float(os.getenv("AUTO_OFFER_PREPAYMENT_PERCENT", "100"))
DEFAULT_DELIVERY_INCLUDED = os.getenv("AUTO_OFFER_DELIVERY_INCLUDED", "true").lower() in {
    "1", "true", "yes", "on",
}
INVOICE_NUMBER_START = int(os.getenv("AUTO_INVOICE_NUMBER_START", "240"))
VI_CANDIDATE_LIMIT = int(os.getenv("AUTO_VI_CANDIDATE_LIMIT", "8"))
APP_PUBLIC_URL = os.getenv("APP_PUBLIC_URL", "https://zakupay-mvp.onrender.com").rstrip("/")

_lock = threading.Lock()


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS automation_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedupe_key TEXT NOT NULL UNIQUE,
            message_id TEXT,
            order_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            subject TEXT,
            sender TEXT,
            status TEXT NOT NULL,
            error TEXT,
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_automation_jobs_order
            ON automation_jobs(order_id, created_at);
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(automation_jobs)")}
    if "invoice_number" not in columns:
        conn.execute("ALTER TABLE automation_jobs ADD COLUMN invoice_number INTEGER")
    return conn


def _message_id(raw_email: bytes) -> str:
    message = BytesParser(policy=policy.default).parsebytes(raw_email)
    return str(message.get("Message-ID") or "").strip()


def _dedupe_key(raw_email: bytes, event) -> str:
    message_id = _message_id(raw_email)
    stable = message_id or f"{event.event_type}:{event.order_id}:{event.subject}"
    return hashlib.sha256(stable.encode("utf-8", errors="replace")).hexdigest()


def _unit_name(item):
    unit = item.get("unit") or item.get("unitName") or ""
    if isinstance(unit, dict):
        return unit.get("name") or unit.get("shortName") or str(unit.get("id") or "")
    return str(unit)


def _search_variants(requested: str) -> list[str]:
    """Search exact identifiers first, then progressively broader text variants."""
    variants = []
    for identifier in sorted(_identifiers(requested), key=len, reverse=True):
        variants.append(identifier)
    cleaned = re.sub(r"https?://\S+", " ", requested, flags=re.I)
    cleaned = re.sub(r"\b(?:комментарий|код товара)\s*:\s*", " ", cleaned, flags=re.I)
    cleaned = " ".join(cleaned.split())
    key_tokens = [
        token for token in _norm(cleaned).split()
        if len(token) >= 2 and token not in {
            "для", "шт", "штук", "упаковка", "комплект", "набор", "требуется",
            "эквивалент", "аналог", "цвет", "материал", "товар", "изделие",
        }
    ]
    if key_tokens:
        variants.append(" ".join(key_tokens[:10]))
    # Common catalogue names differ between the request and VI. These variants
    # widen retrieval only; hard dimensions/models are still checked below.
    synonym_groups = (
        ("коронка", "пила кольцевая"),
        ("щетка чашка", "корщетка чашечная"),
        ("саморез", "винт самонарезающий"),
        ("стекло защитное", "светофильтр"),
        ("держатель", "адаптер"),
    )
    normalized = _norm(cleaned)
    for left, right in synonym_groups:
        if left in normalized:
            variants.append(normalized.replace(left, right))
        if right in normalized:
            variants.append(normalized.replace(right, left))
    variants.extend([cleaned, requested])
    return list(dict.fromkeys(value for value in variants if value.strip()))


def _search_candidates(vi, requested: str) -> list:
    quotes = []
    seen = set()
    for query in _search_variants(requested):
        for quote in vi.search(query, limit=VI_CANDIDATE_LIMIT):
            key = quote.sku or (quote.article, quote.name)
            if key in seen:
                continue
            seen.add(key)
            quotes.append(quote)
    return quotes


def _pack_size(name: str, supplier_unit: str | None, requested_unit: str) -> int:
    """Return pieces in one supplier sales unit when that can be read safely."""
    if "упак" in _norm(requested_unit) or "комплект" in _norm(requested_unit):
        return 1
    text = f"{name or ''} {supplier_unit or ''}".lower().replace("штук", "шт")
    matches = re.findall(r"(?<![xх×*])\b(\d{1,6})\s*шт\.?\b", text)
    values = [int(value) for value in matches if int(value) > 1]
    return max(values) if values else 1


def _hard_conflicts(requested: str, selected: dict) -> list[str]:
    conflicts = []
    candidate_text = " ".join(str(selected.get(key) or "") for key in ("name", "article", "sku"))
    requested_ids = _identifiers(requested)
    candidate_ids = _identifiers(candidate_text)
    if requested_ids and candidate_ids and not (requested_ids & candidate_ids):
        conflicts.append("не совпадает модель/артикул")
    requested_measures = _measurements(requested)
    candidate_measures = _measurements(selected.get("name") or "")
    if requested_measures and candidate_measures and not requested_measures.issubset(candidate_measures):
        missing = ", ".join(sorted(requested_measures - candidate_measures))
        conflicts.append(f"не совпадают размеры: {missing}")
    return conflicts


def build_vi_draft(order: dict, invoice_number: int | None = None) -> dict:
    """Build a conservative, reviewable offer draft from VI search results."""
    vi = VseinstrumentiAdapter()
    items = list(order.get("orderItems") or [])
    rows_by_position = {}

    def match_item(position, item):
        requested = str(item.get("goodName") or "").strip()
        quotes = _search_candidates(vi, requested)
        candidates = []
        for quote in quotes:
            if quote.error:
                candidates.append({"error": quote.error})
                continue
            score, reasons = _score_details(requested, quote)
            candidate = quote.to_dict()
            candidate.update({
                "match_score": score,
                "match_level": _label(score),
                "match_reasons": reasons,
            })
            candidates.append(candidate)
        candidates.sort(key=lambda x: (
            x.get("error") is not None,
            -(x.get("match_score") or 0),
            x.get("price") if x.get("price") is not None else float("inf"),
        ))
        best = next((x for x in candidates if not x.get("error")), None)
        score = (best or {}).get("match_score") or 0
        requested_qty = float(item.get("count") or 0)
        stock = (best or {}).get("stock")
        requested_unit = _unit_name(item)
        pack_size = _pack_size((best or {}).get("name") or "", (best or {}).get("unit"), requested_unit)
        purchase_units = math.ceil(requested_qty / pack_size) if requested_qty else 0
        enough_stock = stock is not None and stock >= purchase_units
        courier_date = (best or {}).get("courier_date")
        pickup_date = (best or {}).get("pickup_date")
        dated_availability = stock is None and bool(courier_date or pickup_date)
        conflicts = _hard_conflicts(requested, best or {}) if best else []
        exact_identifier = bool(
            _identifiers(requested)
            & _identifiers(" ".join(str((best or {}).get(k) or "") for k in ("name", "article", "sku")))
        )
        match_status = "точное соответствие" if not conflicts and (exact_identifier or score >= AUTO_MATCH_THRESHOLD) else "замена"
        if conflicts:
            match_status = "сомнительное соответствие"
        availability_status = (
            "количество подтверждено" if enough_stock else
            "доступно к заказу, количество не подтверждено" if courier_date else
            "доступно к самовывозу, количество не подтверждено" if pickup_date else
            "подтвержденного количества недостаточно" if stock is not None else
            "наличие не подтверждено"
        )
        can_auto = not conflicts and (exact_identifier or score >= AUTO_MATCH_THRESHOLD)
        if can_auto and (enough_stock or dated_availability) and best.get("price") is not None:
            decision = "auto_ready"
        elif score >= REVIEW_MATCH_THRESHOLD:
            decision = "review"
        else:
            decision = "manual"
        purchase_price = (best or {}).get("price")
        unit_purchase_price = purchase_price / pack_size if purchase_price is not None else None
        offer_price = round(unit_purchase_price * (1 + DEFAULT_MARKUP), 2) if unit_purchase_price is not None else None
        return {
            "position": position,
            "order_item_id": item.get("id"),
            "requested_name": requested,
            "quantity": item.get("count"),
            "unit": _unit_name(item),
            "decision": decision,
            "selected": best,
            "purchase_price": purchase_price,
            "proposed_unit_price": offer_price,
            "stock_confirmed": enough_stock,
            "availability_status": availability_status,
            "courier_date": courier_date,
            "pickup_date": pickup_date,
            "pack_size": pack_size,
            "purchase_units": purchase_units,
            "match_status": match_status,
            "replacement_details": conflicts,
            "candidates": candidates[:3],
        }

    workers = max(1, min(int(os.getenv("AUTO_VI_WORKERS", "8")), 12, len(items) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(match_item, position, item): position
            for position, item in enumerate(items, 1)
        }
        for future in as_completed(futures):
            position = futures[future]
            try:
                rows_by_position[position] = future.result()
            except Exception as exc:
                item = items[position - 1]
                rows_by_position[position] = {
                    "position": position,
                    "order_item_id": item.get("id"),
                    "requested_name": item.get("goodName") or "",
                    "quantity": item.get("count"),
                    "unit": _unit_name(item),
                    "decision": "manual",
                    "selected": None,
                    "purchase_price": None,
                    "proposed_unit_price": None,
                    "stock_confirmed": False,
                    "candidates": [{"error": f"{type(exc).__name__}: {exc}"}],
                }
    rows = [rows_by_position[position] for position in sorted(rows_by_position)]
    ready = [row for row in rows if row["decision"] == "auto_ready"]
    status = "ready_for_review" if ready else "needs_review"
    return {
        "order_id": order.get("id"),
        "order_name": order.get("name"),
        "customer": order.get("customer") or {},
        "status": status,
        "live_offer_created": False,
        "invoice_number": invoice_number,
        "markup": DEFAULT_MARKUP,
        "vat_rate": DEFAULT_VAT_RATE,
        "vat_included": True,
        "prepayment_percent": DEFAULT_PREPAYMENT_PERCENT,
        "delivery_included": DEFAULT_DELIVERY_INCLUDED,
        "summary": {
            "positions": len(rows),
            "auto_ready": len(ready),
            "review": sum(row["decision"] == "review" for row in rows),
            "manual": sum(row["decision"] == "manual" for row in rows),
            "included_in_invoice": len(ready),
            "excluded_from_invoice": len(rows) - len(ready),
        },
        "items": rows,
    }



def _gmail_draft_payload(result: dict, job_id: int) -> dict:
    summary = result.get("summary") or {}
    lines = [
        f"Заявка Закупай № {result.get('order_id')}",
        f"Счёт ООО «АВИОР» № {result.get('invoice_number')}",
        "",
        f"Позиций: {summary.get('positions', 0)}",
        f"Готово автоматически: {summary.get('auto_ready', 0)}",
        f"Требует проверки: {summary.get('review', 0)}",
        f"Ручной подбор: {summary.get('manual', 0)}",
        "",
    ]
    for row in result.get("items") or []:
        selected = row.get("selected") or {}
        dates = []
        if row.get("courier_date"):
            dates.append(f"доставка: {row.get('courier_date')}")
        if row.get("pickup_date"):
            dates.append(f"самовывоз: {row.get('pickup_date')}")
        lines.append(
            f"{row.get('position')}. {row.get('requested_name')} — "
            f"{row.get('quantity')} {row.get('unit')}; "
            f"подбор: {selected.get('name') or 'не найден'}; "
            f"статус подбора: {row.get('match_status') or 'не определён'}; "
            f"замена: {', '.join(row.get('replacement_details') or []) or 'нет'}; "
            f"наличие: {row.get('availability_status') or 'не подтверждено'}; "
            f"к покупке: {row.get('purchase_units') or '—'} ед. ВИ по {row.get('pack_size') or 1} шт.; "
            f"{'; '.join(dates) or 'дата доставки не передана'}; "
            f"цена продажи за единицу заявки: {row.get('proposed_unit_price') or '—'} руб.; "
            f"в счёт: {'да' if row.get('decision') == 'auto_ready' else 'нет'}"
        )
    review_url = f"{APP_PUBLIC_URL}/dashboard/automation/jobs/{job_id}/review"
    lines.extend(["", "Проверить заявку и продолжить:", review_url])
    payload = {
        "to": os.getenv("GMAIL_REVIEW_RECIPIENT", "1043324@gmail.com"),
        "subject": (
            f"Проверка заявки Закупай № {result.get('order_id')} / счёт № {result.get('invoice_number')}"
            if result.get("invoice_number") is not None else
            f"Проверка заявки Закупай № {result.get('order_id')} / счёт не сформирован"
        ),
        "body": "\n".join(lines),
        "review_url": review_url,
    }
    if (result.get("summary") or {}).get("auto_ready", 0) > 0:
        invoice = build_invoice_xlsx(result)
        payload["attachment_name"] = (
            f"AVIOR_invoice_{result.get('invoice_number')}_order_{result.get('order_id')}.xlsx"
        )
        payload["attachment_base64"] = base64.b64encode(invoice).decode("ascii")
    return payload

def process_email(raw_email: bytes, fetch_order_by_id) -> dict:
    event = parse_zakupay_email(raw_email)
    if event.event_type != "new_order":
        raise ValueError(f"Email event is not a new order: {event.event_type}")
    key = _dedupe_key(raw_email, event)
    now = datetime.now(timezone.utc).isoformat()
    message_id = _message_id(raw_email)
    with _lock, _connect() as conn:
        existing = conn.execute(
            "SELECT * FROM automation_jobs WHERE dedupe_key = ?", (key,)
        ).fetchone()
        if existing and existing["status"] != "failed":
            result = json.loads(existing["result_json"]) if existing["result_json"] else None
            response = {
                "duplicate": True,
                "job_id": existing["id"],
                "status": existing["status"],
                "error": existing["error"],
                "result": result,
            }
            if result:
                response["gmail_draft"] = _gmail_draft_payload(result, existing["id"])
            return response
        if existing:
            job_id = existing["id"]
            invoice_number = existing["invoice_number"]
            conn.execute(
                "UPDATE automation_jobs SET status='processing', error=NULL, updated_at=? WHERE id=?",
                (now, job_id),
            )
        else:
            invoice_number = None
            cursor = conn.execute(
                """INSERT INTO automation_jobs
                   (dedupe_key,message_id,order_id,event_type,subject,sender,status,created_at,updated_at,invoice_number)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (key, message_id, event.order_id, event.event_type, event.subject, event.sender,
                 "processing", now, now, invoice_number),
            )
            job_id = cursor.lastrowid

    try:
        # The email is a notification. Zakupay remains the primary source of truth.
        order = fetch_order_by_id(event.order_id, force=True)
        if not order and event.order_items:
            # Safe fallback for orders temporarily omitted by the Zakupay API.
            order = {
                "id": event.order_id,
                "name": event.subject,
                "orderItems": list(event.order_items),
                "deliveryDate": event.delivery_date,
                "deliveryAddress": event.delivery_address,
                "paymentTerms": event.payment_terms,
                "source": "email_fallback",
            }
        if not order:
            raise LookupError("Заявка не получена из API Закупай, состав отсутствует в письме")
        result = build_vi_draft(order, invoice_number=invoice_number)
        if result["summary"]["auto_ready"] > 0 and invoice_number is None:
            with _lock, _connect() as conn:
                last_number = conn.execute("SELECT MAX(invoice_number) FROM automation_jobs").fetchone()[0]
                invoice_number = max(INVOICE_NUMBER_START, (last_number or INVOICE_NUMBER_START - 1) + 1)
                conn.execute("UPDATE automation_jobs SET invoice_number=? WHERE id=?", (invoice_number, job_id))
            result["invoice_number"] = invoice_number
        status = result["status"]
        error = None
    except Exception as exc:
        result = None
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"

    updated = datetime.now(timezone.utc).isoformat()
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE automation_jobs SET status=?, error=?, result_json=?, updated_at=? WHERE id=?",
            (status, error, json.dumps(result, ensure_ascii=False) if result else None, updated, job_id),
        )
    if error:
        raise RuntimeError(error)
    return {
        "duplicate": False,
        "job_id": job_id,
        "status": status,
        "result": result,
        "gmail_draft": _gmail_draft_payload(result, job_id),
    }


def install_automation_pipeline(app, fetch_order_by_id):
    @app.post("/automation/email/ingest")
    async def ingest_zakupay_email(
        request: Request,
        x_webhook_secret: str | None = Header(default=None),
    ):
        if not WEBHOOK_SECRET:
            raise HTTPException(status_code=503, detail="ZAKUPAY_EMAIL_WEBHOOK_SECRET не настроен")
        if x_webhook_secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Неверный секрет webhook")
        raw = await request.body()
        if not raw:
            raise HTTPException(status_code=400, detail="Пустое письмо")
        try:
            return JSONResponse(process_email(raw, fetch_order_by_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.get("/automation/jobs")
    def automation_jobs(limit: int = 100):
        limit = max(1, min(limit, 500))
        with _connect() as conn:
            rows = conn.execute(
                """SELECT id,invoice_number,order_id,event_type,subject,sender,status,error,created_at,updated_at
                   FROM automation_jobs ORDER BY id DESC LIMIT ?""", (limit,)
            ).fetchall()
        return {"count": len(rows), "jobs": [dict(row) for row in rows]}

    @app.get("/dashboard/automation/jobs/{job_id}/review")
    def automation_review(job_id: int):
        with _connect() as conn:
            row = conn.execute("SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Задание не найдено")
        result = json.loads(row["result_json"]) if row["result_json"] else None
        if not result:
            raise HTTPException(status_code=409, detail=row["error"] or "Расчёт ещё не готов")
        table_rows = []
        for item in result.get("items") or []:
            selected = item.get("selected") or {}
        table_rows.append(
                "<tr>"
                f"<td>{item.get('position')}</td>"
                f"<td>{html.escape(str(item.get('requested_name') or ''))}</td>"
                f"<td>{html.escape(str(selected.get('name') or 'Не найден'))}</td>"
                f"<td>{html.escape(str(item.get('quantity') or ''))} {html.escape(str(item.get('unit') or ''))}</td>"
                f"<td>{html.escape(str(item.get('proposed_unit_price') or '—'))}</td>"
                f"<td>{html.escape(str(item.get('match_status') or '—'))}</td>"
                f"<td>{html.escape(', '.join(item.get('replacement_details') or []) or 'нет')}</td>"
                f"<td>{html.escape(str(item.get('availability_status') or '—'))}</td>"
                f"<td>{html.escape(str(item.get('courier_date') or item.get('pickup_date') or '—'))}</td>"
                f"<td>{html.escape(str(item.get('decision') or ''))}</td>"
                "</tr>"
            )
        invoice_link = (
            f"<p><a href='/dashboard/automation/jobs/{job_id}/invoice.xlsx'>Скачать сформированный счёт</a></p>"
            if result.get("status") == "ready_for_review" else
            "<p><b>Счёт пока не сформирован: имеются позиции для проверки.</b></p>"
        )
        offer_link = (
            f"<p><a href='/dashboard/order/{row['order_id']}/offer'>Перейти к подтверждению предложения в Закупай</a></p>"
        )
        return Response(
            content=(
                "<!doctype html><html lang='ru'><meta charset='utf-8'>"
                "<title>Проверка заявки</title><style>body{font-family:Arial;margin:24px}"
                "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:8px}"
                "th{background:#eee}</style><body>"
                f"<h1>Заявка Закупай № {row['order_id']}</h1>"
                f"<p>Счёт № {row['invoice_number']} · статус: {html.escape(str(row['status']))}</p>"
                "<table><tr><th>№</th><th>Заявка</th><th>Подбор ВИ</th><th>Количество</th><th>Цена</th>"
                "<th>Статус подбора</th><th>Замена</th><th>Наличие</th><th>Срок</th><th>Решение</th></tr>"
                + "".join(table_rows) + "</table>" + invoice_link + offer_link + "</body></html>"
            ),
            media_type="text/html",
        )

    @app.get("/dashboard/automation/jobs/{job_id}/invoice.xlsx")
    def dashboard_automation_invoice(job_id: int):
        with _connect() as conn:
            row = conn.execute("SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
        if not row or not row["result_json"]:
            raise HTTPException(status_code=404, detail="Готовый счёт не найден")
        try:
            content = build_invoice_xlsx(json.loads(row["result_json"]))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        filename = f"AVIOR_invoice_{row['invoice_number']}_order_{row['order_id']}.xlsx"
        return Response(content=content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/automation/jobs/{job_id}/invoice.xlsx")
    def automation_invoice(job_id: int):
        with _connect() as conn:
            row = conn.execute("SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Задание не найдено")
        if not row["result_json"]:
            raise HTTPException(status_code=409, detail=row["error"] or "Расчёт ещё не готов")
        result = json.loads(row["result_json"])
        try:
            content = build_invoice_xlsx(result)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        filename = f"AVIOR_invoice_{row['invoice_number']}_order_{row['order_id']}.xlsx"
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/automation/jobs/{job_id}")
    def automation_job(job_id: int):
        with _connect() as conn:
            row = conn.execute("SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Задание не найдено")
        data = dict(row)
        data["result"] = json.loads(data.pop("result_json")) if data.get("result_json") else None
        data.pop("dedupe_key", None)
        return data
