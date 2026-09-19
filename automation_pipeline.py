"""Inbound email -> Zakupay order -> VI matching automation pipeline.

The module deliberately stops at a reviewable offer draft.  Uploading an invoice
and creating a live offer require explicit commercial configuration and are kept
behind the existing confirmed offer form.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
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
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
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
GITHUB_OIDC_AUDIENCE = "zakupay-mvp"
GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_REPOSITORY = os.getenv("GITHUB_AUTOMATION_REPOSITORY", "SMeh1970/zakupay-mvp")

_lock = threading.Lock()
logger = logging.getLogger("zakupay.automation")


def _prepayment_confirmed(order: dict) -> bool:
    """Return True only when API or email explicitly confirms no payment delay."""
    delay = order.get("delay")
    if delay is not None and str(delay).strip() != "":
        try:
            return float(delay) == 0
        except (TypeError, ValueError):
            return False

    terms = str(order.get("paymentTerms") or "").lower().replace("ё", "е")
    terms = " ".join(terms.split())
    if not terms:
        # Zakupay omits the entire delay row in autorequest emails when delay is zero.
        return order.get("source") == "email_fallback"
    if "предоплат" in terms or "без отсроч" in terms or "отсрочка не требуется" in terms:
        return True
    if terms in {"нет", "не требуется", "0", "0 дней", "0 день", "0 дн."}:
        return True
    return False


def _authorized_automation_call(webhook_secret: str | None, authorization: str | None) -> bool:
    if WEBHOOK_SECRET and webhook_secret and hmac.compare_digest(webhook_secret, WEBHOOK_SECRET):
        return True
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False
    try:
        import jwt

        signing_key = jwt.PyJWKClient(f"{GITHUB_OIDC_ISSUER}/.well-known/jwks").get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=GITHUB_OIDC_AUDIENCE,
            issuer=GITHUB_OIDC_ISSUER,
        )
    except Exception:
        return False
    return (
        claims.get("repository") == GITHUB_REPOSITORY
        and claims.get("ref") in {"refs/heads/main", "refs/heads/ai-analysis-v1"}
        and claims.get("event_name") in {"schedule", "workflow_dispatch", "push"}
    )


def _connect():
    if DATABASE_URL:
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS automation_jobs (
                id BIGSERIAL PRIMARY KEY,
                dedupe_key TEXT NOT NULL UNIQUE,
                message_id TEXT,
                order_id BIGINT NOT NULL,
                event_type TEXT NOT NULL,
                subject TEXT,
                sender TEXT,
                status TEXT NOT NULL,
                error TEXT,
                order_json TEXT,
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                invoice_number BIGINT
            )"""
        )
        conn.execute("ALTER TABLE automation_jobs ADD COLUMN IF NOT EXISTS order_json TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_automation_jobs_order ON automation_jobs(order_id, created_at)")
        conn.commit()
        return conn
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
            order_json TEXT,
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
    if "order_json" not in columns:
        conn.execute("ALTER TABLE automation_jobs ADD COLUMN order_json TEXT")
    return conn


def _execute(conn, sql: str, params=()):
    if DATABASE_URL:
        sql = sql.replace("?", "%s")
    return conn.execute(sql, params)


def _message_id(raw_email: bytes) -> str:
    message = BytesParser(policy=policy.default).parsebytes(raw_email)
    return str(message.get("Message-ID") or "").strip()


def _dedupe_key(raw_email: bytes, event) -> str:
    message_id = _message_id(raw_email)
    stable = message_id or f"{event.event_type}:{event.order_id}:{event.subject}"
    return hashlib.sha256(stable.encode("utf-8", errors="replace")).hexdigest()


def _api_dedupe_key(order_id: int) -> str:
    return hashlib.sha256(f"zakupay-api:{int(order_id)}".encode("utf-8")).hexdigest()


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

    # VI's public site expands Russian word forms automatically, while the
    # OpenAPI product search is less forgiving.  A catalogue request such as
    # "нарукавники брезентовые" can therefore return no products even though
    # the site has a full category.  Add conservative word-order and inflection
    # variants before falling back to the original phrase.
    words = normalized.split()
    if 2 <= len(words) <= 4:
        variants.append(" ".join(reversed(words)))
    russian_catalog_forms = {
        "нарукавники": "нарукавник",
        "брезентовые": "брезентовый",
    }
    inflected = [russian_catalog_forms.get(word, word) for word in words]
    if inflected != words:
        variants.append(" ".join(inflected))
        if 2 <= len(inflected) <= 4:
            variants.append(" ".join(reversed(inflected)))

    # For a short generic catalogue name, one precise noun is a useful final
    # retrieval query.  Matching and hard-conflict checks still decide whether
    # any returned product may be used in an invoice.
    if len(words) <= 4:
        variants.extend(word for word in words if len(word) >= 5)

    # Verified VI catalogue SKUs provide a deterministic OpenAPI fallback for
    # a category that the public site finds but the API text search may omit.
    if "нарукавник" in normalized and "брезент" in normalized:
        variants.extend(["36641496", "28210004", "30698780", "38046468"])
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


def _purchase_label(candidate: dict, requested_unit: str) -> str:
    """Human-readable VI purchase price with its sales-unit context."""
    name = str(candidate.get("name") or "Товар ВИ")
    price = candidate.get("price")
    if price is None:
        return f"{name} — закупочная цена ВИ не передана"
    numeric_price = float(price)
    price_text = (
        str(int(numeric_price))
        if numeric_price.is_integer()
        else f"{numeric_price:.2f}".rstrip("0").rstrip(".")
    )
    pack_size = _pack_size(name, candidate.get("unit"), requested_unit)
    if pack_size > 1:
        price_basis = f"за упаковку {pack_size} шт."
    else:
        supplier_unit = str(candidate.get("unit") or requested_unit or "ед.").strip()
        price_basis = f"за 1 {supplier_unit}"
    return f"{name} — закупка ВИ: {price_text} ₽ {price_basis}"


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


def _included(row: dict) -> bool:
    return row.get("decision") in {"auto_ready", "approved"}


def _refresh_summary(result: dict) -> None:
    rows = result.get("items") or []
    included = sum(_included(row) for row in rows)
    result["status"] = "ready_for_review" if included else "needs_review"
    result["summary"] = {
        "positions": len(rows),
        "auto_ready": sum(row.get("decision") == "auto_ready" for row in rows),
        "approved": sum(row.get("decision") == "approved" for row in rows),
        "review": sum(row.get("decision") == "review" for row in rows),
        "manual": sum(row.get("decision") == "manual" for row in rows),
        "excluded": sum(row.get("decision") == "excluded" for row in rows),
        "included_in_invoice": included,
        "excluded_from_invoice": len(rows) - included,
    }


def _order_from_saved_result(row, result: dict) -> dict | None:
    """Rebuild the immutable request lines when Zakupay no longer lists it."""
    saved_items = result.get("items") or []
    if not saved_items:
        return None
    return {
        "id": int(row["order_id"]),
        "name": result.get("order_name") or row["subject"],
        "customer": result.get("customer") or {},
        "source": "saved_automation_job",
        "orderItems": [
            {
                "id": item.get("order_item_id"),
                "goodName": item.get("requested_name") or "",
                "count": item.get("quantity"),
                "unit": {"name": item.get("unit") or ""},
            }
            for item in saved_items
        ],
    }


def _saved_order_snapshot(row, result: dict) -> dict | None:
    """Load the original stored order; support older rows via result recovery."""
    keys = row.keys() if hasattr(row, "keys") else row
    raw = row["order_json"] if "order_json" in keys else None
    if raw:
        try:
            order = json.loads(raw)
            if isinstance(order, dict) and order.get("orderItems"):
                order["source"] = "saved_order_snapshot"
                return order
        except (TypeError, ValueError):
            logger.warning("invalid saved order snapshot job_id=%s", row.get("id") if hasattr(row, "get") else "unknown")
    return _order_from_saved_result(row, result)


def load_automation_offer_context(order_id: int) -> dict | None:
    """Return the latest locally persisted, reviewable application snapshot."""
    with _connect() as conn:
        row = _execute(
            conn,
            """SELECT * FROM automation_jobs
               WHERE order_id=? AND result_json IS NOT NULL
                 AND status != 'skipped_not_prepayment'
               ORDER BY id DESC LIMIT 1""",
            (int(order_id),),
        ).fetchone()
    if not row:
        return None
    result = json.loads(row["result_json"])
    order = _saved_order_snapshot(row, result)
    if not order:
        return None
    return {
        "job_id": int(row["id"]),
        "invoice_number": row["invoice_number"],
        "order": order,
        "result": result,
    }


def mark_automation_offer_created(job_id: int, offer_id=None, file_id=None, response=None) -> None:
    """Persist a successful live submission to prevent accidental duplicates."""
    with _lock, _connect() as conn:
        row = _execute(conn, "SELECT result_json FROM automation_jobs WHERE id=?", (int(job_id),)).fetchone()
        if not row or not row["result_json"]:
            raise LookupError("Сохранённая обработка заявки не найдена")
        result = json.loads(row["result_json"])
        result["live_offer_created"] = True
        result["live_offer_created_at"] = datetime.now(timezone.utc).isoformat()
        result["live_offer_id"] = offer_id
        result["live_offer_file_id"] = file_id
        result["live_offer_response"] = response
        _execute(
            conn,
            "UPDATE automation_jobs SET status='offer_created', result_json=?, updated_at=? WHERE id=?",
            (json.dumps(result, ensure_ascii=False), result["live_offer_created_at"], int(job_id)),
        )

def process_email(raw_email: bytes, fetch_order_by_id) -> dict:
    event = parse_zakupay_email(raw_email)
    if event.event_type != "new_order":
        raise ValueError(f"Email event is not a new order: {event.event_type}")
    key = _dedupe_key(raw_email, event)
    now = datetime.now(timezone.utc).isoformat()
    message_id = _message_id(raw_email)
    with _lock, _connect() as conn:
        existing = _execute(conn,
            "SELECT * FROM automation_jobs WHERE dedupe_key = ?", (key,)
        ).fetchone()
        if existing and existing["status"] not in {"failed", "skipped_not_prepayment"}:
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
            _execute(conn,
                "UPDATE automation_jobs SET status='processing', error=NULL, updated_at=? WHERE id=?",
                (now, job_id),
            )
        else:
            invoice_number = None
            insert_sql = """INSERT INTO automation_jobs
                   (dedupe_key,message_id,order_id,event_type,subject,sender,status,created_at,updated_at,invoice_number)
                   VALUES (?,?,?,?,?,?,?,?,?,?)"""
            if DATABASE_URL:
                insert_sql += " RETURNING id"
            cursor = _execute(conn,
                insert_sql,
                (key, message_id, event.order_id, event.event_type, event.subject, event.sender,
                 "processing", now, now, invoice_number),
            )
            job_id = cursor.fetchone()["id"] if DATABASE_URL else cursor.lastrowid

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
        with _lock, _connect() as conn:
            _execute(
                conn,
                "UPDATE automation_jobs SET order_json=?, updated_at=? WHERE id=?",
                (json.dumps(order, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), job_id),
            )
        prepayment_confirmed = _prepayment_confirmed(order)
        logger.warning(
            "email order=%s source=%s positions=%s prepayment=%s payment_terms=%r",
            event.order_id,
            order.get("source", "zakupay_api"),
            len(order.get("orderItems") or []),
            prepayment_confirmed,
            str(order.get("paymentTerms") or "")[:120],
        )
        if not prepayment_confirmed:
            result = {
                "order_id": order.get("id"),
                "order_name": order.get("name"),
                "status": "skipped_not_prepayment",
                "reason": "Условие предоплаты не подтверждено ни API Закупай, ни письмом",
                "invoice_number": None,
                "summary": {"positions": len(order.get("orderItems") or []), "auto_ready": 0},
                "items": [],
            }
        else:
            result = build_vi_draft(order, invoice_number=invoice_number)
        if result["summary"]["auto_ready"] > 0 and invoice_number is None:
            with _lock, _connect() as conn:
                last_row = _execute(conn, "SELECT MAX(invoice_number) AS max_invoice FROM automation_jobs").fetchone()
                last_number = last_row["max_invoice"] if DATABASE_URL else last_row[0]
                invoice_number = max(INVOICE_NUMBER_START, (last_number or INVOICE_NUMBER_START - 1) + 1)
                _execute(conn, "UPDATE automation_jobs SET invoice_number=? WHERE id=?", (invoice_number, job_id))
            result["invoice_number"] = invoice_number
        status = result["status"]
        error = None
    except Exception as exc:
        result = None
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"

    updated = datetime.now(timezone.utc).isoformat()
    with _lock, _connect() as conn:
        _execute(conn,
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


def process_api_order(order: dict) -> dict:
    """Create one persistent review job from a Zakupay API order."""
    order_id = int(order.get("id") or 0)
    if not order_id:
        raise ValueError("У заявки отсутствует ID")
    if not order.get("orderItems"):
        raise ValueError(f"Заявка {order_id} не содержит позиций")

    key = _api_dedupe_key(order_id)
    now = datetime.now(timezone.utc).isoformat()
    with _lock, _connect() as conn:
        existing = _execute(
            conn, "SELECT * FROM automation_jobs WHERE dedupe_key = ?", (key,)
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
            _execute(
                conn,
                "UPDATE automation_jobs SET status='processing', error=NULL, updated_at=? WHERE id=?",
                (now, job_id),
            )
        else:
            invoice_number = None
            insert_sql = """INSERT INTO automation_jobs
                (dedupe_key,message_id,order_id,event_type,subject,sender,status,created_at,updated_at,invoice_number)
                VALUES (?,?,?,?,?,?,?,?,?,?)"""
            if DATABASE_URL:
                insert_sql += " RETURNING id"
            cursor = _execute(
                conn,
                insert_sql,
                (
                    key, None, order_id, "api_order", order.get("name") or f"Заявка {order_id}",
                    "Zakupay API", "processing", now, now, invoice_number,
                ),
            )
            job_id = cursor.fetchone()["id"] if DATABASE_URL else cursor.lastrowid

    try:
        with _lock, _connect() as conn:
            _execute(
                conn,
                "UPDATE automation_jobs SET order_json=?, updated_at=? WHERE id=?",
                (json.dumps(order, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), job_id),
            )
        result = build_vi_draft(order, invoice_number=invoice_number)
        if result["summary"]["auto_ready"] > 0 and invoice_number is None:
            with _lock, _connect() as conn:
                last_row = _execute(
                    conn, "SELECT MAX(invoice_number) AS max_invoice FROM automation_jobs"
                ).fetchone()
                last_number = last_row["max_invoice"] if DATABASE_URL else last_row[0]
                invoice_number = max(
                    INVOICE_NUMBER_START, (last_number or INVOICE_NUMBER_START - 1) + 1
                )
                _execute(
                    conn,
                    "UPDATE automation_jobs SET invoice_number=? WHERE id=?",
                    (invoice_number, job_id),
                )
            result["invoice_number"] = invoice_number
        status = result["status"]
        error = None
    except Exception as exc:
        result = None
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"

    updated = datetime.now(timezone.utc).isoformat()
    with _lock, _connect() as conn:
        _execute(
            conn,
            "UPDATE automation_jobs SET status=?, error=?, result_json=?, updated_at=? WHERE id=?",
            (
                status,
                error,
                json.dumps(result, ensure_ascii=False) if result else None,
                updated,
                job_id,
            ),
        )
    if error:
        raise RuntimeError(error)
    return {
        "duplicate": False,
        "job_id": job_id,
        "status": status,
        "result": result,
    }


def install_automation_pipeline(app, fetch_order_by_id, fetch_all_orders=None, has_my_offer=None):
    @app.post("/automation/email/ingest")
    async def ingest_zakupay_email(
        request: Request,
        x_webhook_secret: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        if not _authorized_automation_call(x_webhook_secret, authorization):
            raise HTTPException(status_code=401, detail="Неверная авторизация автоматизации")
        raw = await request.body()
        if not raw:
            raise HTTPException(status_code=400, detail="Пустое письмо")
        try:
            return JSONResponse(process_email(raw, fetch_order_by_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.post("/automation/api/poll")
    def poll_zakupay_api(
        x_webhook_secret: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        if not _authorized_automation_call(x_webhook_secret, authorization):
            raise HTTPException(status_code=401, detail="Неверная авторизация автоматизации")
        if fetch_all_orders is None:
            raise HTTPException(status_code=503, detail="Получение списка заявок не подключено")

        orders = fetch_all_orders(force=True)
        prepayment = []
        skipped_payment = skipped_offer = skipped_empty = 0
        for order in orders:
            try:
                delay = float(order.get("delay"))
            except (TypeError, ValueError):
                skipped_payment += 1
                continue
            if delay != 0:
                skipped_payment += 1
                continue
            if has_my_offer and has_my_offer(order):
                skipped_offer += 1
                continue
            if not order.get("orderItems"):
                skipped_empty += 1
                continue
            prepayment.append(order)

        # Keep an hourly HTTP invocation bounded. Persistent deduplication makes
        # the next invocation continue with the remaining new orders.
        max_orders = max(1, min(int(os.getenv("AUTO_API_MAX_ORDERS_PER_RUN", "5")), 25))
        processed = []
        duplicates = 0
        attempted = 0
        failures = []
        for order in prepayment:
            if attempted >= max_orders:
                break
            try:
                outcome = process_api_order(order)
                if outcome.get("duplicate"):
                    duplicates += 1
                    continue
                attempted += 1
                processed.append({
                    "order_id": order.get("id"),
                    "job_id": outcome.get("job_id"),
                    "status": outcome.get("status"),
                    "invoice_number": (outcome.get("result") or {}).get("invoice_number"),
                })
            except Exception as exc:
                attempted += 1
                failures.append({"order_id": order.get("id"), "error": f"{type(exc).__name__}: {exc}"})

        response = {
            "source": "Zakupay API",
            "total_actual": len(orders),
            "prepayment_candidates": len(prepayment),
            "attempted": attempted,
            "processed_new": len(processed),
            "already_processed": duplicates,
            "processed": processed,
            "failed": failures,
            "skipped": {
                "not_confirmed_prepayment": skipped_payment,
                "already_has_our_offer": skipped_offer,
                "without_items": skipped_empty,
            },
            "batch_limit": max_orders,
        }
        logger.warning(
            "api poll total=%s prepayment=%s attempted=%s new=%s duplicates=%s failed=%s",
            response["total_actual"], response["prepayment_candidates"], response["attempted"],
            response["processed_new"], response["already_processed"], len(response["failed"]),
        )
        return response

    @app.get("/automation/jobs")
    def automation_jobs(limit: int = 100):
        limit = max(1, min(limit, 500))
        with _connect() as conn:
            rows = _execute(conn,
                """SELECT id,invoice_number,order_id,event_type,subject,sender,status,error,created_at,updated_at
                   FROM automation_jobs ORDER BY id DESC LIMIT ?""", (limit,)
            ).fetchall()
        return {"count": len(rows), "jobs": [dict(row) for row in rows]}

    @app.get("/dashboard/automation")
    def automation_dashboard():
        with _connect() as conn:
            rows = _execute(
                conn,
                "SELECT * FROM automation_jobs WHERE status != 'skipped_not_prepayment' ORDER BY id DESC LIMIT 300",
            ).fetchall()
        cards = []
        for row in rows:
            result = json.loads(row["result_json"]) if row["result_json"] else {}
            summary = result.get("summary") or {}
            total = summary.get("positions", 0)
            exact = summary.get("auto_ready", 0)
            approved = summary.get("approved", 0)
            review = summary.get("review", 0)
            manual = summary.get("manual", 0)
            excluded = summary.get("excluded", 0)
            ready = summary.get("included_in_invoice", exact + approved)
            parts = [f"{total} позиций", f"{exact} точных"]
            if approved:
                parts.append(f"{approved} подтверждено вручную")
            if review:
                parts.append(f"{review} замен/проверок")
            if manual:
                parts.append(f"{manual} не найдено")
            if excluded:
                parts.append(f"{excluded} исключено")
            cls = "ok" if total and ready == total else "warn" if ready else "bad"
            invoice = (
                f"<a class='button secondary' href='/dashboard/automation/jobs/{row['id']}/invoice.xlsx'>Скачать счёт</a>"
                if ready else ""
            )
            send = (
                f"<a class='button send' href='/dashboard/order/{row['order_id']}/offer'>Отправить {ready} поз.</a>"
                if ready else "<span class='muted'>Нет позиций для отправки</span>"
            )
            order_label = f" / {html.escape(str(result.get('order_name')))}" if result.get("order_name") else ""
            cards.append(
                f"<section class='card {cls}'><div><a class='title' href='/dashboard/automation/jobs/{row['id']}/review'>"
                f"Заявка №{row['order_id']}{order_label}</a><div class='meta'>{html.escape(' · '.join(parts))}</div>"
                f"<div class='meta'>Статус: {html.escape(str(row['status']))} · счёт: {row['invoice_number'] or '—'}</div></div>"
                f"<div class='actions'><a class='button' href='/dashboard/automation/jobs/{row['id']}/review'>Открыть</a>{invoice}{send}</div></section>"
            )
        return Response(content=(
            "<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Обработка заявок</title><style>body{font-family:Arial;margin:0;background:#f4f6f8;color:#202124}main{max-width:1200px;margin:auto;padding:28px}"
            ".card{display:flex;justify-content:space-between;gap:20px;background:#fff;border-left:7px solid #9aa0a6;border-radius:12px;padding:18px;margin:12px 0;box-shadow:0 2px 8px #0001}.card.ok{border-color:#188038}.card.warn{border-color:#f9ab00}.card.bad{border-color:#d93025}"
            ".title{font-size:20px;font-weight:700;color:#174ea6;text-decoration:none}.meta{margin-top:8px;color:#5f6368}.actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.button{background:#1a73e8;color:#fff;padding:10px 13px;border-radius:7px;text-decoration:none;font-weight:700}.secondary{background:#5f6368}.send{background:#188038}.muted{color:#777}@media(max-width:760px){.card{display:block}.actions{margin-top:14px}}</style>"
            "<main><h1>Заявки Закупай</h1><p>Подбор ВИ, частичные счета и контроль перед отправкой.</p>" + "".join(cards) + "</main></html>"
        ), media_type="text/html")

    @app.get("/dashboard/automation/jobs/{job_id}/review")
    def automation_review(job_id: int):
        with _connect() as conn:
            row = _execute(conn, "SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Задание не найдено")
        result = json.loads(row["result_json"]) if row["result_json"] else None
        if not result:
            raise HTTPException(status_code=409, detail=row["error"] or "Расчёт ещё не готов")
        table_rows = []
        for item in result.get("items") or []:
            selected = item.get("selected") or {}
            candidates = item.get("candidates") or []
            candidate_options = []
            selected_sku = str(selected.get("sku") or selected.get("article") or selected.get("name") or "")
            usable_candidates = [candidate for candidate in candidates if not candidate.get("error")]
            for idx, candidate in enumerate(usable_candidates):
                key = str(candidate.get("sku") or candidate.get("article") or candidate.get("name") or "")
                label = _purchase_label(candidate, str(item.get("unit") or ""))
                candidate_options.append(f"<option value='{idx}' {'selected' if key == selected_sku else ''}>{html.escape(label)}</option>")
            checked = "checked" if _included(item) else ""
            table_rows.append(
                "<tr>"
                f"<td><input type='checkbox' name='include_{item.get('position')}' value='1' {checked}></td>"
                f"<td>{item.get('position')}</td>"
                f"<td>{html.escape(str(item.get('requested_name') or ''))}</td>"
                f"<td><select name='candidate_{item.get('position')}'>{''.join(candidate_options) or '<option>Не найден</option>'}</select></td>"
                f"<td><input class='qty' name='quantity_{item.get('position')}' type='number' step='0.001' value='{html.escape(str(item.get('quantity') or ''))}'> {html.escape(str(item.get('unit') or ''))}</td>"
                f"<td><input class='price' name='price_{item.get('position')}' type='number' step='0.01' value='{html.escape(str(item.get('proposed_unit_price') or ''))}'></td>"
                f"<td>{html.escape(str(item.get('match_status') or '—'))}</td>"
                f"<td>{html.escape(', '.join(item.get('replacement_details') or []) or 'нет')}</td>"
                f"<td>{html.escape(str(item.get('availability_status') or '—'))}</td>"
                f"<td>{html.escape(str(item.get('courier_date') or item.get('pickup_date') or '—'))}</td>"
                f"<td>{'В счёте' if _included(item) else 'Исключено'}</td>"
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
        search_report = ""
        if result.get("last_search_at"):
            search_report = (
                "<p style='padding:10px;background:#e6f4ea;border-radius:7px'>"
                f"Последний повторный поиск: {html.escape(str(result.get('last_search_at')))} · "
                f"найдены кандидаты для {int(result.get('last_search_found_positions') or 0)} "
                f"из {len(result.get('items') or [])} позиций."
                "</p>"
            )
        return Response(
            content=(
                "<!doctype html><html lang='ru'><meta charset='utf-8'>"
                "<title>Проверка заявки</title><style>body{font-family:Arial;margin:24px;background:#f4f6f8}main{background:#fff;padding:20px;border-radius:12px;overflow:auto}"
                "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:8px}"
                "th{background:#eee}select{min-width:280px}.qty{width:90px}.price{width:100px}button,.button{display:inline-block;padding:11px 16px;background:#1a73e8;color:white;border:0;border-radius:7px;text-decoration:none;font-weight:bold}.send{background:#188038}</style><body><main>"
                "<p><a href='/dashboard/automation'>← Все заявки</a></p>"
                f"<h1>Заявка Закупай № {row['order_id']}</h1>"
                f"<p>Счёт № {row['invoice_number']} · статус: {html.escape(str(row['status']))}</p>"
                f"<form method='post' action='/dashboard/automation/jobs/{job_id}/refresh'><p><button class='button' type='submit'>Повторить поиск в ВИ</button></p></form>{search_report}"
                f"<form method='post' action='/dashboard/automation/jobs/{job_id}/review'><table><tr><th>Включить</th><th>№</th><th>Заявка</th><th>Подбор ВИ<br><small>(закупочная цена)</small></th><th>Количество</th><th>Наша цена<br><small>за единицу заявки (+5%)</small></th>"
                "<th>Статус подбора</th><th>Замена</th><th>Наличие</th><th>Срок</th><th>Решение</th></tr>"
                + "".join(table_rows) + "</table><p><button type='submit'>Сохранить и пересчитать счёт</button></p></form>" + invoice_link + offer_link + "</main></body></html>"
            ),
            media_type="text/html",
        )

    @app.post("/dashboard/automation/jobs/{job_id}/refresh")
    def automation_review_refresh(job_id: int):
        with _connect() as conn:
            row = _execute(conn, "SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Задание не найдено")
        old_result = json.loads(row["result_json"]) if row["result_json"] else {}
        if old_result.get("live_offer_created"):
            raise HTTPException(status_code=409, detail="Предложение уже отправлено; повторный подбор заблокирован")

        order = _saved_order_snapshot(row, old_result)
        if not order:
            raise HTTPException(status_code=409, detail="Состав заявки не был сохранён")
        result = build_vi_draft(order, invoice_number=row["invoice_number"])
        result["last_search_at"] = datetime.now(timezone.utc).isoformat()
        result["last_search_source"] = order.get("source") or "zakupay_api"
        result["last_search_found_positions"] = sum(
            bool(item.get("selected")) for item in result.get("items") or []
        )
        invoice_number = row["invoice_number"]
        if result["summary"]["auto_ready"] > 0 and invoice_number is None:
            with _lock, _connect() as conn:
                last_row = _execute(conn, "SELECT MAX(invoice_number) AS max_invoice FROM automation_jobs").fetchone()
                last_number = last_row["max_invoice"] if DATABASE_URL else last_row[0]
                invoice_number = max(INVOICE_NUMBER_START, (last_number or INVOICE_NUMBER_START - 1) + 1)
                _execute(conn, "UPDATE automation_jobs SET invoice_number=? WHERE id=?", (invoice_number, job_id))
            result["invoice_number"] = invoice_number

        updated = datetime.now(timezone.utc).isoformat()
        with _lock, _connect() as conn:
            _execute(
                conn,
                "UPDATE automation_jobs SET status=?, error=NULL, result_json=?, updated_at=? WHERE id=?",
                (result["status"], json.dumps(result, ensure_ascii=False), updated, job_id),
            )
        return Response(status_code=303, headers={"Location": f"/dashboard/automation/jobs/{job_id}/review"})

    @app.post("/dashboard/automation/jobs/{job_id}/review")
    async def automation_review_save(job_id: int, request: Request):
        form = await request.form()
        with _lock, _connect() as conn:
            row = _execute(conn, "SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
            if not row or not row["result_json"]:
                raise HTTPException(status_code=404, detail="Заявка не найдена")
            result = json.loads(row["result_json"])
            for item in result.get("items") or []:
                pos = item.get("position")
                raw_idx = str(form.get(f"candidate_{pos}") or "")
                candidates = [x for x in (item.get("candidates") or []) if not x.get("error")]
                if raw_idx.isdigit() and int(raw_idx) < len(candidates):
                    item["selected"] = candidates[int(raw_idx)]
                try:
                    item["quantity"] = float(form.get(f"quantity_{pos}") or item.get("quantity") or 0)
                    item["proposed_unit_price"] = float(form.get(f"price_{pos}") or 0)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"Некорректное количество или цена в позиции {pos}")
                include = form.get(f"include_{pos}") == "1"
                item["decision"] = "approved" if include and item.get("selected") and item.get("proposed_unit_price") is not None else "excluded"
                item["operator_included"] = include
            _refresh_summary(result)
            _execute(conn,
                "UPDATE automation_jobs SET status=?, result_json=?, updated_at=? WHERE id=?",
                (result["status"], json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), job_id),
            )
        return Response(status_code=303, headers={"Location": f"/dashboard/automation/jobs/{job_id}/review"})

    @app.get("/dashboard/automation/jobs/{job_id}/invoice.xlsx")
    def dashboard_automation_invoice(job_id: int):
        with _connect() as conn:
            row = _execute(conn, "SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
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
            row = _execute(conn, "SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
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
            row = _execute(conn, "SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Задание не найдено")
        data = dict(row)
        data["result"] = json.loads(data.pop("result_json")) if data.get("result_json") else None
        data["order_snapshot_saved"] = bool(data.pop("order_json", None))
        data.pop("dedupe_key", None)
        return data
