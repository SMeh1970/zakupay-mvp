"""Inbound email -> Zakupay order -> VI matching automation pipeline.

The module deliberately stops at a reviewable offer draft.  Uploading an invoice
and creating a live offer require explicit commercial configuration and are kept
behind the existing confirmed offer form.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser

from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse

from supplier_adapters import VseinstrumentiAdapter
from vi_order_match import _label, _score_details
from zakupay_email import parse_zakupay_email


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


def build_vi_draft(order: dict, invoice_number: int | None = None) -> dict:
    """Build a conservative, reviewable offer draft from VI search results."""
    vi = VseinstrumentiAdapter()
    items = list(order.get("orderItems") or [])
    rows_by_position = {}

    def match_item(position, item):
        requested = str(item.get("goodName") or "").strip()
        quotes = vi.search(requested, limit=VI_CANDIDATE_LIMIT)
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
        enough_stock = stock is not None and stock >= requested_qty
        if score >= AUTO_MATCH_THRESHOLD and enough_stock and best.get("price") is not None:
            decision = "auto_ready"
        elif score >= REVIEW_MATCH_THRESHOLD:
            decision = "review"
        else:
            decision = "manual"
        purchase_price = (best or {}).get("price")
        offer_price = round(purchase_price * (1 + DEFAULT_MARKUP), 2) if purchase_price is not None else None
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
    status = "ready_for_review" if len(ready) == len(rows) and rows else "needs_review"
    return {
        "order_id": order.get("id"),
        "order_name": order.get("name"),
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
        },
        "items": rows,
    }


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
            return {
                "duplicate": True,
                "job_id": existing["id"],
                "status": existing["status"],
                "error": existing["error"],
                "result": result,
            }
        if existing:
            job_id = existing["id"]
            invoice_number = existing["invoice_number"]
            conn.execute(
                "UPDATE automation_jobs SET status='processing', error=NULL, updated_at=? WHERE id=?",
                (now, job_id),
            )
        else:
            last_number = conn.execute(
                "SELECT MAX(invoice_number) FROM automation_jobs"
            ).fetchone()[0]
            invoice_number = max(INVOICE_NUMBER_START, (last_number or INVOICE_NUMBER_START - 1) + 1)
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
    return {"duplicate": False, "job_id": job_id, "status": status, "result": result}


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
