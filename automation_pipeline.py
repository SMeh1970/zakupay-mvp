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
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser

from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from supplier_adapters import KrepKompAdapter, VseinstrumentiAdapter
from vi_order_match import _identifiers, _label, _measurements, _norm, _score_details
from zakupay_email import parse_zakupay_email
from invoice_generator import build_invoice_xlsx, customer_validation_error, normalize_customer


DB_PATH = os.getenv("AUTOMATION_DB_PATH", "automation.db")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
WEBHOOK_SECRET = os.getenv("ZAKUPAY_EMAIL_WEBHOOK_SECRET", "").strip()
EMAIL_INGEST_ENABLED = os.getenv("ENABLE_ZAKUPAY_EMAIL_INGEST", "false").lower() in {
    "1", "true", "yes", "on",
}
API_POLL_ENABLED = os.getenv("ENABLE_ZAKUPAY_API_POLL", "false").lower() in {
    "1", "true", "yes", "on",
}
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
_schema_lock = threading.Lock()
_initialized_database_key = None
_poll_lock = threading.Lock()
_last_api_poll_started = 0.0
logger = logging.getLogger("zakupay.automation")


class AutomationDatabaseUnavailable(RuntimeError):
    """Raised when the persistent automation database cannot be reached."""


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


def _database_key():
    return ("postgres", DATABASE_URL) if DATABASE_URL else ("sqlite", os.path.abspath(DB_PATH))


def _initialize_schema(conn) -> None:
    """Run idempotent migrations once per process/database, never per request."""
    global _initialized_database_key
    key = _database_key()
    if _initialized_database_key == key:
        return
    with _schema_lock:
        if _initialized_database_key == key:
            return
        if DATABASE_URL:
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
                viewed_at TEXT,
                invoice_number BIGINT
            )"""
            )
            conn.execute("ALTER TABLE automation_jobs ADD COLUMN IF NOT EXISTS order_json TEXT")
            conn.execute("ALTER TABLE automation_jobs ADD COLUMN IF NOT EXISTS viewed_at TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_automation_jobs_order ON automation_jobs(order_id, created_at)")
            conn.execute(
            """CREATE TABLE IF NOT EXISTS match_feedback (
                id BIGSERIAL PRIMARY KEY,
                request_key TEXT NOT NULL,
                candidate_key TEXT NOT NULL,
                action TEXT NOT NULL,
                requested_name TEXT NOT NULL,
                candidate_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(request_key, candidate_key)
            )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_match_feedback_request ON match_feedback(request_key, action)")
            conn.execute(
            """CREATE TABLE IF NOT EXISTS offer_submissions (
                id BIGSERIAL PRIMARY KEY,
                job_id BIGINT NOT NULL,
                order_id BIGINT NOT NULL,
                attempt_key TEXT NOT NULL UNIQUE,
                external_guid TEXT NOT NULL,
                snapshot_hash TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                invoice_number TEXT NOT NULL,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                file_id TEXT,
                offer_id TEXT,
                payload_json TEXT,
                response_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_offer_submissions_job ON offer_submissions(job_id, created_at)")
            conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_offer_submissions_active_job
               ON offer_submissions(job_id)
               WHERE status IN ('sending', 'unknown')"""
            )
        else:
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
        CREATE TABLE IF NOT EXISTS match_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_key TEXT NOT NULL,
            candidate_key TEXT NOT NULL,
            action TEXT NOT NULL,
            requested_name TEXT NOT NULL,
            candidate_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(request_key, candidate_key)
        );
        CREATE TABLE IF NOT EXISTS offer_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            order_id INTEGER NOT NULL,
            attempt_key TEXT NOT NULL UNIQUE,
            external_guid TEXT NOT NULL,
            snapshot_hash TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            invoice_number TEXT NOT NULL,
            status TEXT NOT NULL,
            stage TEXT NOT NULL,
            file_id TEXT,
            offer_id TEXT,
            payload_json TEXT,
            response_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_offer_submissions_job
            ON offer_submissions(job_id, created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_offer_submissions_active_job
            ON offer_submissions(job_id)
            WHERE status IN ('sending', 'unknown');
        CREATE INDEX IF NOT EXISTS idx_match_feedback_request
            ON match_feedback(request_key, action);
        """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(automation_jobs)")}
            if "invoice_number" not in columns:
                conn.execute("ALTER TABLE automation_jobs ADD COLUMN invoice_number INTEGER")
            if "order_json" not in columns:
                conn.execute("ALTER TABLE automation_jobs ADD COLUMN order_json TEXT")
            if "viewed_at" not in columns:
                conn.execute("ALTER TABLE automation_jobs ADD COLUMN viewed_at TEXT")
        conn.commit()
        _initialized_database_key = key


def _connect():
    if DATABASE_URL:
        import psycopg
        from psycopg.rows import dict_row
        try:
            conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=8)
        except psycopg.Error as exc:
            logger.exception("automation database connection failed")
            raise AutomationDatabaseUnavailable(
                "База заявок временно недоступна. Проверьте лимит проекта Neon."
            ) from exc
    else:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
    try:
        _initialize_schema(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _execute(conn, sql: str, params=()):
    if DATABASE_URL:
        sql = sql.replace("?", "%s")
    return conn.execute(sql, params)


def _commercial_order_data(order: dict | None) -> dict:
    """Return only fields whose change can make a prepared offer obsolete."""
    order = order or {}

    def number(value):
        if value in (None, ""):
            return ""
        try:
            numeric = float(str(value).replace(" ", "").replace(",", "."))
            return format(numeric, ".12g")
        except (TypeError, ValueError):
            return str(value).strip()

    items = []
    for item in order.get("orderItems") or []:
        items.append(
            {
                "id": str(_order_item_id(item) or ""),
                "name": _norm(item.get("goodName") or item.get("name") or ""),
                "count": number(item.get("count") if item.get("count") is not None else item.get("quantity")),
                "unit": _norm(_unit_name(item)),
            }
        )
    terms = {}
    for key in (
        "delay", "paymentTerms", "prepaidPercent", "deliveryAddress", "deliveryDate",
        "deliveryDeadline", "deadline", "finishDate", "address", "city", "region",
    ):
        if order.get(key) not in (None, ""):
            terms[key] = order.get(key)
    return {"id": str(order.get("id") or ""), "items": items, "terms": terms}


def commercial_order_hash(order: dict | None) -> str:
    stable = json.dumps(_commercial_order_data(order), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def commercial_order_changes(saved: dict | None, fresh: dict | None) -> list[str]:
    before = _commercial_order_data(saved)
    after = _commercial_order_data(fresh)
    changes = []
    if before["items"] != after["items"]:
        changes.append("изменились позиции, количества или единицы измерения")
    if before["terms"] != after["terms"]:
        changes.append("изменились коммерческие условия, адрес или срок")
    if before["id"] != after["id"]:
        changes.append("изменился идентификатор заявки")
    return changes


def _save_match_feedback(requested_name: str, candidate: dict, action: str) -> None:
    if action not in {"approved", "excluded"} or not candidate:
        return
    request_key = _norm(requested_name)
    candidate_key = _candidate_key(candidate)
    if not request_key or not candidate_key:
        return
    now = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(candidate, ensure_ascii=False)
    with _lock, _connect() as conn:
        if DATABASE_URL:
            _execute(
                conn,
                """INSERT INTO match_feedback
                   (request_key,candidate_key,action,requested_name,candidate_json,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT (request_key,candidate_key) DO UPDATE SET
                   action=EXCLUDED.action, candidate_json=EXCLUDED.candidate_json, updated_at=EXCLUDED.updated_at""",
                (request_key, candidate_key, action, requested_name, payload, now, now),
            )
        else:
            _execute(
                conn,
                """INSERT INTO match_feedback
                   (request_key,candidate_key,action,requested_name,candidate_json,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(request_key,candidate_key) DO UPDATE SET
                   action=excluded.action, candidate_json=excluded.candidate_json, updated_at=excluded.updated_at""",
                (request_key, candidate_key, action, requested_name, payload, now, now),
            )


def _excluded_feedback_keys(requested_name: str) -> set[str]:
    with _connect() as conn:
        rows = _execute(
            conn,
            "SELECT candidate_key FROM match_feedback WHERE request_key=? AND action='excluded'",
            (_norm(requested_name),),
        ).fetchall()
    return {str(row["candidate_key"]) for row in rows}


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


def _order_item_id(item: dict | None):
    """Return a Zakupay line ID across payload variants used by its APIs."""
    item = item or {}
    for key in ("id", "orderItemId", "order_item_id", "itemId"):
        value = item.get(key)
        if value not in (None, ""):
            return value
    nested = item.get("orderItem")
    if isinstance(nested, dict) and nested.get("id") not in (None, ""):
        return nested.get("id")
    return None


def _order_with_selected_positions(order: dict, selected_positions: set[int]) -> dict:
    """Return an order snapshot containing only operator-selected 1-based rows."""
    items = list((order or {}).get("orderItems") or [])
    chosen = [item for index, item in enumerate(items, 1) if index in selected_positions]
    selected_order = dict(order or {})
    selected_order["orderItems"] = chosen
    return selected_order


def _merge_line_ids(order: dict, detailed: dict | None) -> dict:
    """Merge IDs from one exact lookup without replacing the saved request."""
    if not detailed:
        return order
    source_items = list(detailed.get("orderItems") or [])
    if not source_items:
        return order
    merged = dict(order)
    target_items = [dict(item) for item in (order.get("orderItems") or [])]
    unused = set(range(len(source_items)))
    for position, item in enumerate(target_items):
        if _order_item_id(item) is not None:
            continue
        requested = _norm(item.get("goodName") or "")
        match_index = next((
            idx for idx in unused
            if requested and _norm(source_items[idx].get("goodName") or "") == requested
        ), None)
        if match_index is None and position < len(source_items):
            candidate = _norm(source_items[position].get("goodName") or "")
            if requested and candidate and (requested in candidate or candidate in requested):
                match_index = position
        if match_index is None:
            continue
        line_id = _order_item_id(source_items[match_index])
        if line_id is not None:
            item["id"] = line_id
            unused.discard(match_index)
    merged["orderItems"] = target_items
    return merged


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


def _search_candidates(suppliers, requested: str, time_budget_seconds: float | None = None) -> tuple[list, list[dict], bool]:
    if not isinstance(suppliers, (list, tuple)):
        suppliers = [suppliers]
    quotes = []
    seen = set()
    max_variants = max(1, int(os.getenv("AUTO_SEARCH_VARIANT_LIMIT", "8")))
    enough_candidates = max(VI_CANDIDATE_LIMIT, int(os.getenv("AUTO_SEARCH_ENOUGH_CANDIDATES", "12")))
    deadline = time.monotonic() + time_budget_seconds if time_budget_seconds else None
    diagnostics = []
    timed_out = False
    for supplier in suppliers:
        supplier_started = time.monotonic()
        supplier_queries = 0
        supplier_errors = []
        # KREP-KOMP ranks a cached full catalogue locally. Repeating VI-style
        # text variants would only duplicate price/stock API calls and consume
        # the supplier's 1000-request daily allowance.
        queries = [requested] if getattr(supplier, "code", "") == "krep_komp" else _search_variants(requested)[:max_variants]
        supplier_found = 0
        for query in queries:
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                break
            supplier_queries += 1
            supplier_quotes = supplier.search(query, limit=VI_CANDIDATE_LIMIT)
            for quote in supplier_quotes:
                if quote.error:
                    supplier_errors.append(quote.error)
                key = (quote.supplier, quote.sku or quote.article or quote.name)
                if key in seen:
                    continue
                seen.add(key)
                quotes.append(quote)
                if not quote.error:
                    supplier_found += 1
            # A relevant API page already provides enough alternatives for
            # scoring. Avoid issuing every synonym/inflection query needlessly.
            if supplier_found >= enough_candidates:
                break
        elapsed = round(time.monotonic() - supplier_started, 3)
        diagnostics.append({
            "supplier": getattr(supplier, "name", getattr(supplier, "code", "поставщик")),
            "queries": supplier_queries,
            "candidates": supplier_found,
            "seconds": elapsed,
            "errors": list(dict.fromkeys(supplier_errors))[:3],
            "timed_out": timed_out,
        })
        if timed_out:
            break
    return quotes, diagnostics, timed_out


def _pack_size(name: str, supplier_unit: str | None, requested_unit: str) -> int:
    """Return pieces in one supplier sales unit when that can be read safely."""
    if "упак" in _norm(requested_unit) or "комплект" in _norm(requested_unit):
        return 1
    text = f"{name or ''} {supplier_unit or ''}".lower().replace("штук", "шт")
    matches = re.findall(r"(?<![xх×*])\b(\d{1,6})\s*шт\.?\b", text)
    values = [int(value) for value in matches if int(value) > 1]
    return max(values) if values else 1


def _purchase_label(candidate: dict, requested_unit: str) -> str:
    """Human-readable supplier purchase price with its sales-unit context."""
    # Historical saved drafts did not persist a supplier field and were VI-only.
    supplier = str(candidate.get("supplier") or "ВсеИнструменты.ру")
    name = str(candidate.get("name") or "Товар")
    price = candidate.get("price")
    if price is None:
        return f"{name} — закупочная цена {supplier} не передана"
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
    supplier_label = "ВИ" if supplier == "ВсеИнструменты.ру" else supplier
    return f"{name} — закупка {supplier_label}: {price_text} ₽ {price_basis}"


_PRODUCT_TYPE_MARKERS = {
    "мешок": ("мешок", "мешки", "пакет для мусора"),
    "компрессор": ("компрессор",),
    "анкер": ("анкер",),
    "болт": ("болт",),
    "гайка": ("гайка",),
    "шайба": ("шайба",),
    "саморез": ("саморез", "винт самонарезающий"),
    "дюбель": ("дюбель",),
    "диск": ("диск отрезной", "диск алмазный", "круг отрезной"),
    "бур": ("бур ", "бур sds"),
    "сверло": ("сверло",),
    "коронка": ("коронка", "пила кольцевая"),
    "валик": ("валик", "мини валик"),
    "кисть": ("кисть",),
    "перчатки": ("перчат",),
    "нарукавники": ("нарукавник",),
    "очки": ("очки защит",),
    "стекло": ("стекло защит", "светофильтр"),
    "маркер": ("маркер",),
    "отвертка": ("отвертк",),
    "ключ": ("ключ рожков", "ключ трубн", "ключ гаечн"),
    "молоток": ("молоток",),
    "ножницы": ("ножницы",),
    "резак": ("резак",),
    "адаптер": ("адаптер", "держатель для корон"),
    "пленка": ("пленк",),
    "скотч": ("скотч", "лента клейк"),
    "фартук": ("фартук",),
    "зарядное устройство": ("зарядное устройство", "зарядник"),
}


def _product_types(text: str) -> set[str]:
    """Return conservative product classes; unknown is safer than a false class."""
    normalized = f" {_norm(text)} "
    found = set()
    for product_type, markers in _PRODUCT_TYPE_MARKERS.items():
        if any(_norm(marker) in normalized for marker in markers):
            found.add(product_type)
    return found


def _candidate_key(candidate: dict) -> str:
    supplier = _norm(str(candidate.get("supplier") or ""))
    identity = str(candidate.get("sku") or candidate.get("article") or candidate.get("name") or "")
    return f"{supplier}:{_norm(identity)}"


def _hard_conflicts(requested: str, selected: dict) -> list[str]:
    conflicts = []
    candidate_text = " ".join(str(selected.get(key) or "") for key in (
        "name", "article", "sku", "technical_specifications", "breadcrumbs"
    ))
    requested_types = _product_types(requested)
    candidate_types = _product_types(candidate_text)
    if requested_types and candidate_types and requested_types.isdisjoint(candidate_types):
        conflicts.append(
            "не совпадает тип товара: требуется "
            + "/".join(sorted(requested_types))
            + ", найдено "
            + "/".join(sorted(candidate_types))
        )
    requested_ids = _identifiers(requested)
    candidate_ids = _identifiers(candidate_text)
    if requested_ids and not candidate_ids:
        conflicts.append("в найденном товаре отсутствует обязательная модель/артикул")
    elif requested_ids and not (requested_ids & candidate_ids):
        conflicts.append("не совпадает модель/артикул")
    requested_measures = _measurements(requested)
    candidate_measures = _measurements(candidate_text)
    if requested_measures and not candidate_measures:
        conflicts.append("в найденном товаре отсутствуют обязательные размеры/характеристики")
    elif requested_measures and not requested_measures.issubset(candidate_measures):
        missing = ", ".join(sorted(requested_measures - candidate_measures))
        conflicts.append(f"не совпадают размеры: {missing}")
    return conflicts


def _match_status(requested: str, candidate: dict | None, score: float, conflicts: list[str]) -> str:
    if not candidate:
        return "не соответствует"
    if conflicts:
        return "не соответствует"
    candidate_text = " ".join(str(candidate.get(key) or "") for key in ("name", "article", "sku"))
    exact_identifier = bool(_identifiers(requested) & _identifiers(candidate_text))
    if exact_identifier or score >= AUTO_MATCH_THRESHOLD:
        return "точное"
    if score >= REVIEW_MATCH_THRESHOLD:
        return "аналог"
    if score >= 0.48:
        return "сомнительное"
    return "не соответствует"


def _supplier_balanced_candidates(candidates: list[dict], per_supplier: int = 3, limit: int = 8) -> list[dict]:
    """Keep good alternatives from every supplier instead of only the global top 3."""
    selected = []
    counts = {}
    for candidate in candidates:
        supplier = str(candidate.get("supplier") or "не указан")
        if counts.get(supplier, 0) >= per_supplier:
            continue
        selected.append(candidate)
        counts[supplier] = counts.get(supplier, 0) + 1
        if len(selected) >= limit:
            break
    return selected


def _build_match_row(position: int, item: dict, suppliers, excluded_keys: set[str] | None = None) -> dict:
    requested = str(item.get("goodName") or "").strip()
    excluded_keys = set(excluded_keys or ())
    position_started = time.monotonic()
    position_budget = max(6.0, float(os.getenv("AUTO_POSITION_SEARCH_TIMEOUT", "45")))
    quotes, search_diagnostics, search_timed_out = _search_candidates(
        suppliers, requested, time_budget_seconds=position_budget,
    )
    candidates = []
    rejected = []
    for quote in quotes:
        if quote.error:
            rejected.append({"error": quote.error})
            continue
        score, reasons = _score_details(requested, quote)
        candidate = quote.to_dict()
        candidate.update({
            "match_score": score,
            "match_level": _label(score),
            "match_reasons": reasons,
        })
        key = _candidate_key(candidate)
        conflicts = _hard_conflicts(requested, candidate)
        candidate["candidate_key"] = key
        candidate["hard_conflicts"] = conflicts
        if key in excluded_keys:
            candidate["rejection_reason"] = "уже показывался в этой позиции"
            rejected.append(candidate)
        elif conflicts:
            candidate["rejection_reason"] = "; ".join(conflicts)
            rejected.append(candidate)
        else:
            candidates.append(candidate)
    candidates.sort(key=lambda x: (
        -(x.get("match_score") or 0),
        x.get("price") if x.get("price") is not None else float("inf"),
    ))
    rejected.sort(key=lambda x: (x.get("error") is not None, -(x.get("match_score") or 0)))
    # Candidates below the minimum semantic threshold remain visible only in
    # diagnostics; they must never become the selected invoice line.
    acceptable = [x for x in candidates if (x.get("match_score") or 0) >= 0.48]
    rejected.extend(x for x in candidates if (x.get("match_score") or 0) < 0.48)
    candidates = acceptable
    best = candidates[0] if candidates else None
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
    match_status = _match_status(requested, best, score, conflicts)
    availability_status = (
        "количество подтверждено" if enough_stock else
        "доступно к заказу, количество не подтверждено" if courier_date else
        "доступно к самовывозу, количество не подтверждено" if pickup_date else
        "подтвержденного количества недостаточно" if stock is not None else
        "наличие не подтверждено"
    )
    can_auto = match_status == "точное"
    if can_auto and (enough_stock or dated_availability) and best.get("price") is not None:
        decision = "auto_ready"
    elif match_status in {"аналог", "сомнительное"}:
        decision = "review"
    else:
        decision = "manual"
    purchase_price = (best or {}).get("price")
    unit_purchase_price = purchase_price / pack_size if purchase_price is not None else None
    offer_price = round(unit_purchase_price * (1 + DEFAULT_MARKUP), 2) if unit_purchase_price is not None else None
    return {
        "position": position,
        "order_item_id": _order_item_id(item),
        "requested_name": requested,
        "quantity": item.get("count"),
        "unit": requested_unit,
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
        "candidates": _supplier_balanced_candidates(candidates),
        "rejected_candidates": rejected[:20],
        "excluded_candidate_keys": sorted(excluded_keys),
        "search_elapsed_seconds": round(time.monotonic() - position_started, 3),
        "search_timed_out": search_timed_out,
        "search_diagnostics": search_diagnostics,
    }


def build_vi_draft(order: dict, invoice_number: int | None = None, progress_callback=None) -> dict:
    """Build a conservative, reviewable draft from all configured suppliers."""
    suppliers = [VseinstrumentiAdapter()]
    krep_komp = KrepKompAdapter()
    deferred_suppliers = []
    if krep_komp.enabled:
        if krep_komp.catalog_ready():
            suppliers.append(krep_komp)
        else:
            krep_komp.warm_catalog_async()
            deferred_suppliers.append("КРЕП-КОМП: каталог загружается в фоне")
    items = list(order.get("orderItems") or [])
    rows_by_position = {}
    feedback_exclusions = {
        position: _excluded_feedback_keys(str(item.get("goodName") or ""))
        for position, item in enumerate(items, 1)
    }

    def match_item(position, item):
        return _build_match_row(position, item, suppliers, feedback_exclusions.get(position))

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
                row = rows_by_position[position]
                logger.warning(
                    "supplier position completed order=%s position=%s seconds=%s timeout=%s suppliers=%s",
                    order.get("id"), position, row.get("search_elapsed_seconds"), row.get("search_timed_out"),
                    json.dumps(row.get("search_diagnostics") or [], ensure_ascii=False),
                )
            except Exception as exc:
                item = items[position - 1]
                rows_by_position[position] = {
                    "position": position,
                    "order_item_id": _order_item_id(item),
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
            if progress_callback:
                progress_callback(
                    len(rows_by_position),
                    len(items),
                    rows_by_position[position],
                )
    rows = [rows_by_position[position] for position in sorted(rows_by_position)]
    ready = [row for row in rows if row["decision"] == "auto_ready"]
    status = "ready_for_review" if ready else "needs_review"
    return {
        "order_id": order.get("id"),
        "order_name": order.get("name"),
        "manual_reference": order.get("manualReference") or "",
        "source_type": "manual" if order.get("source") == "manual_entry" else "zakupay",
        "source_label": "Ручной ввод" if order.get("source") == "manual_entry" else "Закупай",
        "customer": order.get("customer") or {},
        "zakupay_line_ids_complete": bool(items) and all(_order_item_id(item) is not None for item in items),
        "order_source": order.get("source") or "zakupay_api",
        "processed_snapshot_hash": commercial_order_hash(order),
        "status": status,
        "live_offer_created": False,
        "invoice_number": invoice_number,
        "markup": DEFAULT_MARKUP,
        "vat_rate": DEFAULT_VAT_RATE,
        "vat_included": True,
        "prepayment_percent": DEFAULT_PREPAYMENT_PERCENT,
        "delivery_included": DEFAULT_DELIVERY_INCLUDED,
        "deferred_suppliers": deferred_suppliers,
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
        try:
            invoice = build_invoice_xlsx(result)
        except ValueError as exc:
            payload["subject"] = f"Проверка заявки Закупай № {result.get('order_id')} / счёт не сформирован"
            payload["body"] += "\n\n" + str(exc)
            return payload
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
                order.setdefault("source", "saved_order_snapshot")
                order["snapshotStorage"] = "local_database"
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


def _preserve_invoice_customer(result: dict, previous: dict) -> None:
    """Keep operator-confirmed payer details when rebuilding supplier matches."""
    if previous.get("invoice_customer_confirmed_at"):
        for key in ("customer", "invoice_customer_confirmed_at", "invoice_customer_history"):
            if key in previous:
                result[key] = previous[key]


def save_invoice_customer(job_id: int, customer: dict) -> None:
    customer = normalize_customer(customer)
    error = customer_validation_error(customer)
    if error:
        raise ValueError(error)
    with _lock, _connect() as conn:
        row = _execute(conn, "SELECT * FROM automation_jobs WHERE id=?", (int(job_id),)).fetchone()
        if not row or not row["result_json"]:
            raise LookupError("Обработка заявки не найдена")
        if row["status"] == "processing":
            raise ValueError("Дождитесь завершения подбора, затем сохраните реквизиты")
        submission = _execute(
            conn, "SELECT status FROM offer_submissions WHERE job_id=? ORDER BY id DESC LIMIT 1", (int(job_id),),
        ).fetchone()
        if submission and submission["status"] in {"sending", "unknown"}:
            raise ValueError("Отправка выполняется или её результат не подтверждён; изменение реквизитов заблокировано")
        result = json.loads(row["result_json"])
        now = datetime.now(timezone.utc).isoformat()
        history = result.setdefault("invoice_customer_history", [])
        history.append({"previous_customer": result.get("customer") or {}, "customer": customer, "confirmed_at": now})
        result["customer"] = customer
        result["invoice_customer_confirmed_at"] = now
        _execute(
            conn, "UPDATE automation_jobs SET result_json=?, updated_at=? WHERE id=?",
            (json.dumps(result, ensure_ascii=False), now, int(job_id)),
        )


def enrich_automation_offer_context(order_id: int, fresh_order: dict | None) -> dict | None:
    """Persist one deliberate API enrichment of missing Zakupay line IDs."""
    context = load_automation_offer_context(order_id)
    if not context:
        return None
    result = context["result"]
    saved_order = context["order"]
    result["order_id_enrichment_attempted_at"] = datetime.now(timezone.utc).isoformat()
    result["order_id_enrichment_found"] = False

    fresh_items = list((fresh_order or {}).get("orderItems") or [])
    result_items = list(result.get("items") or [])
    if fresh_items:
        unused = set(range(len(fresh_items)))
        for position, row in enumerate(result_items):
            requested = _norm(row.get("requested_name") or "")
            match_index = next(
                (
                    idx for idx in unused
                    if requested and _norm(fresh_items[idx].get("goodName") or "") == requested
                ),
                None,
            )
            if match_index is None and position < len(fresh_items):
                candidate_name = _norm(fresh_items[position].get("goodName") or "")
                if requested and (requested in candidate_name or candidate_name in requested):
                    match_index = position
            if match_index is None:
                continue
            item_id = _order_item_id(fresh_items[match_index])
            if item_id is None:
                continue
            row["order_item_id"] = item_id
            unused.discard(match_index)

        if all(row.get("order_item_id") is not None for row in result_items):
            result["order_id_enrichment_found"] = True
        saved_order = _merge_line_ids(saved_order, fresh_order)
        saved_order["source"] = "zakupay_api_enrichment"

    updated = datetime.now(timezone.utc).isoformat()
    with _lock, _connect() as conn:
        _execute(
            conn,
            "UPDATE automation_jobs SET order_json=?, result_json=?, updated_at=? WHERE id=?",
            (
                json.dumps(saved_order, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                updated,
                context["job_id"],
            ),
        )
    return load_automation_offer_context(order_id)


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


def begin_offer_submission(
    job_id: int,
    order_id: int,
    attempt_key: str,
    external_guid: str,
    snapshot_hash: str,
    payload_hash: str,
    invoice_number: str,
    file_id,
    payload: dict,
) -> dict:
    """Atomically claim one live POST. A concurrent click receives the existing claim."""
    now = datetime.now(timezone.utc).isoformat()
    with _lock, _connect() as conn:
        cursor = _execute(
            conn,
            """INSERT INTO offer_submissions
               (job_id,order_id,attempt_key,external_guid,snapshot_hash,payload_hash,
                invoice_number,status,stage,file_id,payload_json,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,'sending','offer_post',?,?,?,?)
               ON CONFLICT DO NOTHING
               RETURNING id""",
            (
                int(job_id), int(order_id), attempt_key, external_guid, snapshot_hash,
                payload_hash, str(invoice_number), str(file_id or ""),
                json.dumps(payload, ensure_ascii=False), now, now,
            ),
        )
        inserted = cursor.fetchone()
        if inserted:
            submission_id = inserted["id"] if hasattr(inserted, "keys") else inserted[0]
            return {"claimed": True, "id": int(submission_id), "status": "sending"}
        row = _execute(
            conn,
            """SELECT * FROM offer_submissions
               WHERE attempt_key=? OR (job_id=? AND status IN ('sending','unknown'))
               ORDER BY id DESC LIMIT 1""",
            (attempt_key, int(job_id)),
        ).fetchone()
        if not row:
            raise RuntimeError("Не удалось зафиксировать попытку отправки")
        return {
            "claimed": False,
            "id": int(row["id"]),
            "status": row["status"],
            "offer_id": row["offer_id"],
            "error": row["error"],
        }


def finish_offer_submission(
    submission_id: int,
    status: str,
    *,
    offer_id=None,
    response=None,
    error=None,
) -> None:
    """Persist the known outcome; unknown deliberately remains retry-blocking."""
    if status not in {"confirmed", "unknown", "rejected"}:
        raise ValueError("Недопустимый статус отправки")
    now = datetime.now(timezone.utc).isoformat()
    with _lock, _connect() as conn:
        row = _execute(conn, "SELECT * FROM offer_submissions WHERE id=?", (int(submission_id),)).fetchone()
        if not row:
            raise LookupError("Попытка отправки не найдена")
        _execute(
            conn,
            """UPDATE offer_submissions
               SET status=?, stage='completed', offer_id=?, response_json=?, error=?, updated_at=?
               WHERE id=? AND status='sending'""",
            (
                status, str(offer_id or ""),
                json.dumps(response, ensure_ascii=False) if response is not None else None,
                str(error or ""), now, int(submission_id),
            ),
        )
        if status == "confirmed":
            job = _execute(conn, "SELECT result_json FROM automation_jobs WHERE id=?", (int(row["job_id"]),)).fetchone()
            if not job or not job["result_json"]:
                raise LookupError("Сохранённая обработка заявки не найдена")
            result = json.loads(job["result_json"])
            result.update(
                {
                    "live_offer_created": True,
                    "live_offer_created_at": now,
                    "live_offer_id": offer_id,
                    "live_offer_file_id": row["file_id"],
                    "live_offer_response": response,
                    "offer_submission_id": int(submission_id),
                }
            )
            _execute(
                conn,
                "UPDATE automation_jobs SET status='offer_created', result_json=?, updated_at=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False), now, int(row["job_id"])),
            )


def latest_offer_submission(job_id: int) -> dict | None:
    with _connect() as conn:
        row = _execute(
            conn,
            "SELECT * FROM offer_submissions WHERE job_id=? ORDER BY id DESC LIMIT 1",
            (int(job_id),),
        ).fetchone()
    return dict(row) if row else None

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


def save_api_order_for_manual_start(order: dict) -> dict:
    """Persist a Zakupay application without starting supplier searches."""
    order_id = int(order.get("id") or 0)
    items = list(order.get("orderItems") or [])
    if not order_id or not items:
        raise ValueError("Заявка не содержит ID или позиций")
    key = _api_dedupe_key(order_id)
    now = datetime.now(timezone.utc).isoformat()
    pending_result = {
        "order_id": order_id,
        "order_name": order.get("name"),
        "customer": order.get("customer") or {},
        "status": "pending_search",
        "live_offer_created": False,
        "invoice_number": None,
        "summary": {
            "positions": len(items), "auto_ready": 0, "review": 0, "manual": 0,
            "included_in_invoice": 0, "excluded_from_invoice": len(items),
        },
        "items": [],
    }
    with _lock, _connect() as conn:
        existing = _execute(conn, "SELECT * FROM automation_jobs WHERE dedupe_key=?", (key,)).fetchone()
        if existing:
            return {"duplicate": True, "job_id": existing["id"], "status": existing["status"]}
        insert_sql = """INSERT INTO automation_jobs
            (dedupe_key,message_id,order_id,event_type,subject,sender,status,error,order_json,result_json,created_at,updated_at,invoice_number)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        if DATABASE_URL:
            insert_sql += " RETURNING id"
        cursor = _execute(conn, insert_sql, (
            key, None, order_id, "api_manual", order.get("name") or f"Заявка {order_id}",
            "Zakupay API", "pending_search", None, json.dumps(order, ensure_ascii=False),
            json.dumps(pending_result, ensure_ascii=False), now, now, None,
        ))
        job_id = cursor.fetchone()["id"] if DATABASE_URL else cursor.lastrowid
    return {"duplicate": False, "job_id": job_id, "status": "pending_search"}


def save_manual_order(
    title: str,
    items: list[dict],
    *,
    reference: str = "",
    customer_name: str = "",
    customer_inn: str = "",
    delivery_address: str = "",
) -> dict:
    """Persist an operator-entered request without any Zakupay dependency."""
    clean_items = []
    for item in items:
        name = str(item.get("goodName") or item.get("name") or "").strip()
        unit = str(item.get("unit") or "шт").strip() or "шт"
        try:
            count = float(str(item.get("count") or "").replace(" ", "").replace(",", "."))
        except (TypeError, ValueError):
            raise ValueError(f"Некорректное количество для позиции «{name or 'без названия'}»")
        if not name or count <= 0:
            raise ValueError("У каждой позиции должны быть наименование и количество больше нуля")
        clean_items.append({"goodName": name, "count": count, "unit": {"name": unit}})
    if not clean_items:
        raise ValueError("Добавьте хотя бы одну позицию")

    manual_uuid = uuid.uuid4()
    order_id = -int(manual_uuid.int % 8_000_000_000_000 + 1_000_000_000_000)
    now = datetime.now(timezone.utc).isoformat()
    title = str(title or "").strip() or "Ручная заявка"
    reference = str(reference or "").strip()
    order = {
        "id": order_id,
        "name": title,
        "source": "manual_entry",
        "manualReference": reference,
        "creationDate": now,
        "deliveryAddress": str(delivery_address or "").strip(),
        "customer": {
            "name": str(customer_name or "").strip(),
            "inn": str(customer_inn or "").strip(),
        },
        "orderItems": clean_items,
    }
    pending_result = {
        "order_id": order_id,
        "order_name": title,
        "manual_reference": reference,
        "source_type": "manual",
        "source_label": "Ручной ввод",
        "customer": order["customer"],
        "status": "pending_search",
        "live_offer_created": False,
        "invoice_number": None,
        "summary": {
            "positions": len(clean_items), "auto_ready": 0, "review": 0, "manual": 0,
            "included_in_invoice": 0, "excluded_from_invoice": len(clean_items),
        },
        "items": [],
    }
    with _lock, _connect() as conn:
        insert_sql = """INSERT INTO automation_jobs
            (dedupe_key,message_id,order_id,event_type,subject,sender,status,error,order_json,result_json,created_at,updated_at,invoice_number)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        if DATABASE_URL:
            insert_sql += " RETURNING id"
        cursor = _execute(
            conn,
            insert_sql,
            (
                f"manual:{manual_uuid}", None, order_id, "manual_order", title,
                "Ручной ввод", "pending_search", None, json.dumps(order, ensure_ascii=False),
                json.dumps(pending_result, ensure_ascii=False), now, now, None,
            ),
        )
        job_id = cursor.fetchone()["id"] if DATABASE_URL else cursor.lastrowid
    return {"job_id": int(job_id), "order_id": order_id, "status": "pending_search"}


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
    @app.exception_handler(AutomationDatabaseUnavailable)
    async def automation_database_unavailable(request: Request, exc: AutomationDatabaseUnavailable):
        message = html.escape(str(exc))
        if request.url.path.startswith("/dashboard"):
            return Response(
                content=(
                    "<!doctype html><html lang='ru'><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                    "<title>База заявок недоступна</title>"
                    "<style>body{font-family:Arial;background:#f4f6f8;color:#202124;margin:0}"
                    "main{max-width:760px;margin:60px auto;padding:28px;background:#fff;"
                    "border-radius:12px;box-shadow:0 2px 10px #0002}h1{color:#b3261e}"
                    "a{color:#174ea6}</style><main><h1>База заявок временно недоступна</h1>"
                    f"<p>{message}</p>"
                    "<p>Данные не удалены. После восстановления лимита Neon страница снова "
                    "покажет сохранённые заявки.</p>"
                    "<p><a href='/dashboard/automation'>Повторить проверку</a></p></main></html>"
                ),
                status_code=503,
                media_type="text/html",
            )
        return JSONResponse({"detail": str(exc)}, status_code=503)

    def order_with_line_ids(order: dict) -> dict:
        """Perform one exact lookup at intake when the collection omits line IDs."""
        items = list(order.get("orderItems") or [])
        if not items or all(_order_item_id(item) is not None for item in items):
            return order
        try:
            detailed = fetch_order_by_id(int(order.get("id")), force=True)
        except Exception as exc:
            logger.warning("line id lookup deferred order=%s error=%s", order.get("id"), type(exc).__name__)
            return order
        return _merge_line_ids(order, detailed)

    @app.post("/automation/email/ingest")
    async def ingest_zakupay_email(
        request: Request,
        x_webhook_secret: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        if not _authorized_automation_call(x_webhook_secret, authorization):
            raise HTTPException(status_code=401, detail="Неверная авторизация автоматизации")
        if not EMAIL_INGEST_ENABLED:
            return JSONResponse({
                "accepted": False,
                "status": "email_ingest_disabled",
                "message": "Приём заявок из Gmail отключён; используется API Закупай",
            })
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
        global _last_api_poll_started
        if not _authorized_automation_call(x_webhook_secret, authorization):
            raise HTTPException(status_code=401, detail="Неверная авторизация автоматизации")
        if not API_POLL_ENABLED:
            return JSONResponse({
                "accepted": False,
                "status": "zakupay_api_poll_disabled",
                "message": "Автоматические запросы в API Закупай отключены",
            })
        if fetch_all_orders is None:
            raise HTTPException(status_code=503, detail="Получение списка заявок не подключено")

        # The scheduler normally runs hourly.  Ignore accidental duplicate
        # invocations so they do not wake Neon and Zakupay repeatedly.
        min_interval = max(
            300,
            int(os.getenv("AUTO_API_POLL_MIN_INTERVAL_SECONDS", "3300")),
        )
        now = time.monotonic()
        with _poll_lock:
            elapsed = now - _last_api_poll_started
            if _last_api_poll_started and elapsed < min_interval:
                return JSONResponse({
                    "accepted": False,
                    "status": "rate_limited",
                    "retry_after_seconds": int(min_interval - elapsed) + 1,
                    "message": "Повторный фоновый опрос пропущен",
                })
            _last_api_poll_started = now

        try:
            orders = fetch_all_orders(force=True)
        except HTTPException as exc:
            if exc.status_code not in {502, 503, 504}:
                raise
            logger.warning("api poll deferred because Zakupay is unavailable: %s", exc.detail)
            # A temporary upstream timeout is not a failed scheduler run.  No
            # application is marked as processed; the next hourly run retries.
            return JSONResponse({
                "source": "Zakupay API",
                "status": "temporarily_unavailable",
                "processed_new": 0,
                "retry": "next_hourly_run",
                "detail": str(exc.detail),
            })
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
                order = order_with_line_ids(order)
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

    @app.post("/dashboard/automation/sync")
    def automation_dashboard_sync():
        """Operator-triggered import with a graceful fallback to saved data."""
        if fetch_all_orders is None:
            return Response(status_code=303, headers={"Location": "/dashboard/automation?sync_error=not_configured"})
        try:
            orders = fetch_all_orders(force=True)
        except HTTPException as exc:
            logger.warning("manual Zakupay sync unavailable: %s", exc.detail)
            return Response(status_code=303, headers={"Location": "/dashboard/automation?sync_error=api_unavailable"})
        except Exception as exc:
            logger.exception("manual Zakupay sync failed: %s", exc)
            return Response(status_code=303, headers={"Location": "/dashboard/automation?sync_error=api_unavailable"})
        added = duplicates = skipped = 0
        for order in orders[:300]:
            if not _prepayment_confirmed(order):
                skipped += 1
                continue
            if has_my_offer and has_my_offer(order):
                skipped += 1
                continue
            if not order.get("orderItems"):
                skipped += 1
                continue
            # Intake must stay fast.  Some collection responses omit line IDs;
            # they are not needed to save and review an order.  Enrich only the
            # selected order later, immediately before an offer is submitted.
            outcome = save_api_order_for_manual_start(order)
            if outcome.get("duplicate"):
                duplicates += 1
            else:
                added += 1
        return Response(
            status_code=303,
            headers={"Location": f"/dashboard/automation?added={added}&duplicates={duplicates}&skipped={skipped}"},
        )

    @app.post("/dashboard/automation/jobs/{job_id}/start")
    async def automation_dashboard_start(job_id: int, request: Request):
        with _connect() as conn:
            row = _execute(conn, "SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Заявка не найдена")
        old_result = json.loads(row["result_json"]) if row["result_json"] else {}
        if old_result.get("live_offer_created"):
            raise HTTPException(status_code=409, detail="Предложение уже отправлено")
        if row["status"] == "processing":
            return Response(status_code=303, headers={"Location": f"/dashboard/automation/jobs/{job_id}/review"})
        order = _saved_order_snapshot(row, old_result)
        if not order:
            raise HTTPException(status_code=409, detail="Состав заявки не сохранён")
        full_order_snapshot_hash = commercial_order_hash(order)
        form = await request.form()
        selected_positions = {
            int(value) for value in form.getlist("selected_position")
            if str(value).isdigit() and int(value) > 0
        }
        order = _order_with_selected_positions(order, selected_positions)
        if not order.get("orderItems"):
            raise HTTPException(status_code=400, detail="Отметьте хотя бы одну позицию для поиска цен")
        progress_result = dict(old_result)
        progress_result["processing_progress"] = {
            "completed": 0,
            "total": len(order.get("orderItems") or []),
            "last_position": None,
        }
        with _lock, _connect() as conn:
            _execute(
                conn,
                "UPDATE automation_jobs SET status='processing', error=NULL, result_json=?, updated_at=? WHERE id=?",
                (json.dumps(progress_result, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), job_id),
            )

        def run_search():
            started_at = time.monotonic()
            last_progress_saved_at = 0.0
            logger.warning(
                "supplier search started job=%s order=%s positions=%s",
                job_id, order.get("id"), len(order.get("orderItems") or []),
            )
            try:
                def save_progress(completed, total, completed_row):
                    nonlocal last_progress_saved_at
                    progress_result["processing_progress"] = {
                        "completed": completed,
                        "total": total,
                        "last_position": completed_row.get("position"),
                        "last_name": completed_row.get("requested_name"),
                    }
                    now = time.monotonic()
                    if completed < total and now - last_progress_saved_at < 15:
                        return
                    last_progress_saved_at = now
                    with _lock, _connect() as conn:
                        _execute(
                            conn,
                            "UPDATE automation_jobs SET result_json=?, updated_at=? WHERE id=? AND status='processing'",
                            (
                                json.dumps(progress_result, ensure_ascii=False),
                                datetime.now(timezone.utc).isoformat(),
                                job_id,
                            ),
                        )

                result = build_vi_draft(
                    order,
                    invoice_number=row["invoice_number"],
                    progress_callback=save_progress,
                )
                _preserve_invoice_customer(result, old_result)
                result["processed_snapshot_hash"] = full_order_snapshot_hash
                invoice_number = row["invoice_number"]
                if result["summary"]["auto_ready"] > 0 and invoice_number is None:
                    with _lock, _connect() as conn:
                        last_row = _execute(conn, "SELECT MAX(invoice_number) AS max_invoice FROM automation_jobs").fetchone()
                        last_number = last_row["max_invoice"] if DATABASE_URL else last_row[0]
                        invoice_number = max(INVOICE_NUMBER_START, (last_number or INVOICE_NUMBER_START - 1) + 1)
                        _execute(conn, "UPDATE automation_jobs SET invoice_number=? WHERE id=?", (invoice_number, job_id))
                    result["invoice_number"] = invoice_number
                with _lock, _connect() as conn:
                    _execute(
                        conn,
                        "UPDATE automation_jobs SET status=?, error=NULL, result_json=?, updated_at=? WHERE id=?",
                        (result["status"], json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), job_id),
                    )
                logger.warning(
                    "supplier search completed job=%s order=%s seconds=%.1f",
                    job_id, order.get("id"), time.monotonic() - started_at,
                )
            except Exception as exc:
                logger.exception("background supplier search failed job=%s", job_id)
                with _lock, _connect() as conn:
                    _execute(
                        conn,
                        "UPDATE automation_jobs SET status='failed', error=?, updated_at=? WHERE id=?",
                        (f"{type(exc).__name__}: {exc}", datetime.now(timezone.utc).isoformat(), job_id),
                    )

        threading.Thread(target=run_search, name=f"supplier-search-{job_id}", daemon=True).start()
        return Response(status_code=303, headers={"Location": f"/dashboard/automation/jobs/{job_id}/review"})

    @app.get("/dashboard/automation/manual")
    def automation_manual_form():
        initial_rows = "".join(
            "<tr><td class='row-number'></td>"
            "<td><input name='item_name' required placeholder='Полное наименование товара'></td>"
            "<td><input name='item_quantity' required type='number' min='0.001' step='0.001'></td>"
            "<td><input name='item_unit' value='шт'></td>"
            "<td><button class='remove' type='button' onclick='this.closest(\"tr\").remove();renumber()'>Удалить</button></td></tr>"
            for _ in range(3)
        )
        return Response(
            content=(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Добавить ручную заявку</title><style>body{font-family:Arial;margin:0;background:#f4f6f8;color:#202124}main{max-width:1100px;margin:auto;padding:28px}"
                ".card{background:#fff;padding:22px;border-radius:12px}label{display:block;font-weight:700;margin:12px 0 5px}input{box-sizing:border-box;width:100%;padding:9px}"
                ".grid{display:grid;grid-template-columns:2fr 1fr;gap:14px}table{width:100%;border-collapse:collapse;margin:20px 0}th,td{border:1px solid #ddd;padding:8px}th{background:#eee}"
                "td:nth-child(1){width:45px}td:nth-child(3),td:nth-child(4){width:130px}button,.button{border:0;border-radius:7px;padding:11px 15px;background:#1a73e8;color:#fff;font-weight:700;cursor:pointer;text-decoration:none}.remove{background:#b3261e;padding:8px}.secondary{background:#5f6368}</style>"
                "<body><main><p><a href='/dashboard/automation'>← Все заявки</a></p><div class='card'><h1>Добавить заявку вручную</h1>"
                "<p>Эта заявка сохраняется в нашей базе и не запрашивается у Закупай.</p>"
                "<form method='post' action='/dashboard/automation/manual'><div class='grid'><div><label>Название заявки</label><input name='title' required placeholder='Например: Заявка клиента № 154'></div>"
                "<div><label>Внутренний номер / ссылка</label><input name='reference' placeholder='Необязательно'></div><div><label>Заказчик</label><input name='customer_name'></div>"
                "<div><label>ИНН заказчика</label><input name='customer_inn'></div></div><label>Адрес доставки</label><input name='delivery_address'>"
                "<table><thead><tr><th>№</th><th>Наименование</th><th>Количество</th><th>Ед.</th><th></th></tr></thead><tbody id='items'>"
                f"{initial_rows}</tbody></table><p><button class='secondary' type='button' onclick='addRow()'>+ Добавить позицию</button></p>"
                "<button type='submit'>Сохранить заявку</button></form></div></main>"
                "<script>function renumber(){document.querySelectorAll('#items .row-number').forEach((x,i)=>x.textContent=i+1)}"
                "function addRow(){const tr=document.createElement('tr');tr.innerHTML=`<td class=\"row-number\"></td><td><input name=\"item_name\" required placeholder=\"Полное наименование товара\"></td><td><input name=\"item_quantity\" required type=\"number\" min=\"0.001\" step=\"0.001\"></td><td><input name=\"item_unit\" value=\"шт\"></td><td><button class=\"remove\" type=\"button\" onclick=\"this.closest('tr').remove();renumber()\">Удалить</button></td>`;document.getElementById('items').appendChild(tr);renumber()}renumber()</script>"
                "</body></html>"
            ),
            media_type="text/html",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.post("/dashboard/automation/manual")
    async def automation_manual_save(request: Request):
        form = await request.form()
        names = list(form.getlist("item_name"))
        quantities = list(form.getlist("item_quantity"))
        units = list(form.getlist("item_unit"))
        items = [
            {
                "goodName": name,
                "count": quantities[index] if index < len(quantities) else "",
                "unit": units[index] if index < len(units) else "шт",
            }
            for index, name in enumerate(names)
            if str(name or "").strip()
        ]
        try:
            saved = save_manual_order(
                str(form.get("title") or ""),
                items,
                reference=str(form.get("reference") or ""),
                customer_name=str(form.get("customer_name") or ""),
                customer_inn=str(form.get("customer_inn") or ""),
                delivery_address=str(form.get("delivery_address") or ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return Response(
            status_code=303,
            headers={"Location": f"/dashboard/automation/jobs/{saved['job_id']}/review"},
        )

    @app.get("/dashboard/automation")
    def automation_dashboard(
        added: int = 0,
        duplicates: int = 0,
        skipped: int = 0,
        sync_error: str = "",
        source: str = "all",
    ):
        source = source if source in {"all", "zakupay", "manual"} else "all"
        with _connect() as conn:
            if source == "manual":
                rows = _execute(conn, "SELECT * FROM automation_jobs WHERE status != 'skipped_not_prepayment' AND event_type='manual_order' ORDER BY id DESC LIMIT 300").fetchall()
            elif source == "zakupay":
                rows = _execute(conn, "SELECT * FROM automation_jobs WHERE status != 'skipped_not_prepayment' AND event_type!='manual_order' ORDER BY id DESC LIMIT 300").fetchall()
            else:
                rows = _execute(conn, "SELECT * FROM automation_jobs WHERE status != 'skipped_not_prepayment' ORDER BY id DESC LIMIT 300").fetchall()
        cards = []
        has_processing = any(row["status"] == "processing" for row in rows)
        for row in rows:
            result = json.loads(row["result_json"]) if row["result_json"] else {}
            is_manual = row["event_type"] == "manual_order"
            viewed = bool(row["viewed_at"])
            offer_created = bool(result.get("live_offer_created") or row["status"] == "offer_created")
            summary = result.get("summary") or {}
            total = summary.get("positions", 0)
            exact = summary.get("auto_ready", 0)
            approved = summary.get("approved", 0)
            review = summary.get("review", 0)
            manual = summary.get("manual", 0)
            excluded = summary.get("excluded", 0)
            ready = summary.get("included_in_invoice", exact + approved)
            progress = result.get("processing_progress") or {}
            is_processing = row["status"] == "processing"
            if is_processing:
                completed = int(progress.get("completed") or 0)
                progress_total = int(progress.get("total") or total or 0)
                parts = [f"идёт подбор: {completed} из {progress_total} позиций"]
            else:
                parts = [f"{total} позиций", f"{exact} точных"]
            if approved:
                parts.append(f"{approved} подтверждено вручную")
            if review:
                parts.append(f"{review} замен/проверок")
            if manual:
                parts.append(f"{manual} не найдено")
            if excluded:
                parts.append(f"{excluded} исключено")
            cls = "processing" if is_processing else "ok" if offer_created or (total and ready == total) else "warn" if ready else "bad"
            pending = row["status"] == "pending_search"
            customer_error = customer_validation_error(result.get("customer"))
            invoice = (
                f"<a class='button secondary' href='/dashboard/automation/jobs/{row['id']}/invoice.xlsx'>Скачать счёт</a>"
                if ready and not customer_error else ""
            )
            if offer_created:
                offer_id = html.escape(str(result.get("live_offer_id") or "—"))
                send = f"<span class='sent'>Счёт выставлен · предложение ID {offer_id}</span>"
            else:
                send = (
                    f"<a class='button send' href='/dashboard/order/{row['order_id']}/offer'>Отправить {ready} поз.</a>"
                    if ready and not is_manual and not customer_error else
                    "<span class='muted'>Счёт заблокирован: заполните реквизиты плательщика в проверке заявки</span>" if ready and customer_error else
                    "<span class='muted'>Ручная заявка: отправка в Закупай недоступна</span>" if ready and is_manual else
                    "<span class='muted'>Нет позиций для отправки</span>"
                )
            order_label = f" / {html.escape(str(result.get('order_name')))}" if result.get("order_name") else ""
            supplier_names = []
            for item in result.get("items") or []:
                options = [item.get("selected")] + list(item.get("candidates") or [])
                for option in options:
                    supplier = str((option or {}).get("supplier") or "").strip()
                    if supplier and supplier not in supplier_names:
                        supplier_names.append(supplier)
            suppliers_label = ", ".join(supplier_names) if supplier_names else "подбор ещё не выполнен"
            progress_bar = ""
            if is_processing:
                completed = int(progress.get("completed") or 0)
                progress_total = max(1, int(progress.get("total") or 1))
                progress_percent = min(100, round(completed * 100 / progress_total))
                progress_bar = f"<div class='progress'><span style='width:{progress_percent}%'></span></div>"
            offer_badge = "<span class='offer-badge'>СЧЁТ ВЫСТАВЛЕН</span>" if offer_created else ""
            source_badge = "<span class='source-badge manual'>РУЧНОЙ ВВОД</span>" if is_manual else "<span class='source-badge'>ЗАКУПАЙ</span>"
            display_number = html.escape(str(result.get("manual_reference") or "без номера")) if is_manual else str(row["order_id"])
            primary_action = (
                f"<a class='button' target='_blank' rel='noopener' href='/dashboard/automation/jobs/{row['id']}/review'>Выбрать позиции</a>"
                if pending else
                f"<a class='button' target='_blank' rel='noopener' href='/dashboard/automation/jobs/{row['id']}/review'>Открыть</a>"
            )
            cards.append(
                f"<section class='card {cls} {'offer-created' if offer_created else ''} {'read' if viewed else 'unread'}'><div><a class='title' target='_blank' rel='noopener' href='/dashboard/automation/jobs/{row['id']}/review'>"
                f"Заявка №{display_number}{order_label}</a>{source_badge}{offer_badge}<div class='meta'>{html.escape(' · '.join(parts))}</div>"
                f"{progress_bar}"
                f"<div class='meta'><b>Поставщики:</b> {html.escape(suppliers_label)}</div>"
                f"<div class='meta'>Статус: {'счёт выставлен в Закупай' if offer_created else html.escape(str(row['status']))} · счёт: {row['invoice_number'] or '—'}</div></div>"
                f"<div class='actions'>{primary_action}{invoice}{send}</div></section>"
            )
        sync_report = ""
        if added or duplicates or skipped:
            sync_report = (
                "<p style='padding:12px;background:#e6f4ea;border-radius:8px'>"
                f"Обновление завершено: новых {added}, уже сохранённых {duplicates}, пропущено {skipped}."
                "</p>"
            )
        if sync_error:
            sync_report += (
                "<p style='padding:12px;background:#fff0e8;border-radius:8px'>"
                "<b>Закупай временно не отдал список заявок.</b> Уже сохранённые заявки доступны ниже; "
                "ничего не удалено. Повторите синхронизацию позже."
                "</p>"
            )
        return Response(content=(
            "<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Обработка заявок</title><style>body{font-family:Arial;margin:0;background:#f4f6f8;color:#202124}main{max-width:1200px;margin:auto;padding:28px}"
            ".card{display:flex;justify-content:space-between;gap:20px;background:#fff;border-left:7px solid #9aa0a6;border-radius:12px;padding:18px;margin:12px 0;box-shadow:0 2px 8px #0001}.card.read{background:#e9eef3}.card.unread{background:#fff}.card.ok{border-color:#188038}.card.warn{border-color:#f9ab00}.card.bad{border-color:#d93025}.card.processing{border-color:#1a73e8;background:#e8f0fe}.card.offer-created{background:#e6f4ea;border-color:#188038}.offer-badge,.source-badge{display:inline-block;margin-left:12px;padding:5px 9px;border-radius:12px;background:#188038;color:#fff;font-size:12px;font-weight:700;vertical-align:middle}.source-badge{background:#174ea6}.source-badge.manual{background:#7b1fa2}.progress{height:8px;max-width:460px;background:#c7d5ec;border-radius:5px;margin-top:9px;overflow:hidden}.progress span{display:block;height:100%;background:#1a73e8}"
            ".title{font-size:20px;font-weight:700;color:#174ea6;text-decoration:none}.meta{margin-top:8px;color:#5f6368}.actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.button{background:#1a73e8;color:#fff;padding:10px 13px;border-radius:7px;text-decoration:none;font-weight:700}.secondary{background:#5f6368}.send{background:#188038}.sent{display:inline-block;padding:10px 13px;background:#e6f4ea;color:#137333;border-radius:7px;font-weight:700}.muted{color:#777}@media(max-width:760px){.card{display:block}.actions{margin-top:14px}}</style>"
            "<main><h1>Заявки</h1><p>Подбор у поставщиков, частичные счета и контроль перед отправкой.</p>"
            "<div class='actions'><form method='post' action='/dashboard/automation/sync'><button class='button' type='submit'>Получить из Закупай</button></form>"
            "<a class='button secondary' href='/dashboard/automation/manual'>+ Добавить вручную</a></div>"
            "<form method='get' action='/dashboard/automation' style='margin-top:18px'><label><b>Источник:</b> <select name='source' onchange='this.form.submit()'>"
            f"<option value='all' {'selected' if source == 'all' else ''}>Все</option><option value='zakupay' {'selected' if source == 'zakupay' else ''}>Закупай</option><option value='manual' {'selected' if source == 'manual' else ''}>Ручной ввод</option></select></label></form>"
            + ("<p class='muted'>Есть заявки в обработке. Страница больше не обновляется каждые 5 секунд — нажмите <a href='/dashboard/automation'>обновить список</a>, когда потребуется.</p>" if has_processing else "")
            + "<p class='muted'>Ручная синхронизация запускается по нажатию; автоматический опрос выполняется каждый час.</p>"
            + sync_report + "".join(cards) + "</main></html>"
        ), media_type="text/html", headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"})

    @app.get("/dashboard/automation/jobs/{job_id}/review")
    def automation_review(job_id: int):
        with _connect() as conn:
            row = _execute(conn, "SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
            if row and not row["viewed_at"]:
                _execute(
                    conn,
                    "UPDATE automation_jobs SET viewed_at=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(), job_id),
                )
        if not row:
            raise HTTPException(status_code=404, detail="Задание не найдено")
        result = json.loads(row["result_json"]) if row["result_json"] else None
        if not result:
            raise HTTPException(status_code=409, detail=row["error"] or "Расчёт ещё не готов")
        is_manual = row["event_type"] == "manual_order"
        request_heading = (
            f"Ручная заявка {html.escape(str(result.get('manual_reference') or 'без номера'))}"
            if is_manual else f"Заявка Закупай № {row['order_id']}"
        )
        offer_created = bool(result.get("live_offer_created") or row["status"] == "offer_created")
        if row["status"] == "processing":
            progress = (result or {}).get("processing_progress") or {}
            completed = int(progress.get("completed") or 0)
            total = int(progress.get("total") or 0)
            last_name = str(progress.get("last_name") or "").strip()
            progress_text = f"Обработано позиций: <b>{completed} из {total}</b>." if total else "Подготовка поиска."
            if last_name:
                progress_text += f"<br>Последняя обработанная позиция: {html.escape(last_name)}."
            try:
                updated_at = datetime.fromisoformat(str(row["updated_at"]).replace("Z", "+00:00"))
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                processing_age = (datetime.now(timezone.utc) - updated_at).total_seconds()
            except (TypeError, ValueError):
                processing_age = 0
            stale_after = max(60, int(os.getenv("AUTO_SEARCH_STALE_SECONDS", "180")))
            if processing_age > stale_after:
                with _lock, _connect() as conn:
                    _execute(
                        conn,
                        "UPDATE automation_jobs SET status='pending_search', error=?, updated_at=? WHERE id=? AND status='processing'",
                        (
                            "Предыдущий поиск был прерван перезапуском сервиса. Выберите позиции и запустите его повторно.",
                            datetime.now(timezone.utc).isoformat(),
                            job_id,
                        ),
                    )
                return Response(
                    status_code=303,
                    headers={"Location": f"/dashboard/automation/jobs/{job_id}/review"},
                )
            return Response(
                content=(
                    "<!doctype html><html lang='ru'><meta charset='utf-8'><meta http-equiv='refresh' content='30'>"
                    "<title>Идёт подбор</title><style>body{font-family:Arial;margin:24px;background:#f4f6f8}main{max-width:760px;background:#fff;padding:24px;border-radius:12px}"
                    ".status{padding:16px;background:#e8f0fe;border-radius:8px}</style><body><main>"
                    "<p><a href='/dashboard/automation'>← Все заявки</a></p>"
                    f"<h1>{request_heading}</h1>"
                    "<div class='status'><b>Идёт подбор товаров и цен у поставщиков.</b><br>"
                    f"{progress_text}<br>"
                    "Можно закрыть эту страницу. Она обновляется автоматически раз в 30 секунд, а подбор продолжится в фоне.</div>"
                    "</main></body></html>"
                ),
                media_type="text/html",
            )
        if row["status"] == "pending_search" or not (result.get("items") or []):
            order = _saved_order_snapshot(row, result) or {}
            source_items = list(order.get("orderItems") or [])
            pending_rows = "".join(
                "<tr>"
                f"<td><input class='position-checkbox' type='checkbox' name='selected_position' value='{index}' checked></td>"
                f"<td>{index}</td>"
                f"<td>{html.escape(str(item.get('goodName') or item.get('name') or ''))}</td>"
                f"<td>{html.escape(str(item.get('count') or item.get('quantity') or ''))}</td>"
                f"<td>{html.escape(str((item.get('unit') or {}).get('name') if isinstance(item.get('unit'), dict) else item.get('unit') or ''))}</td>"
                "</tr>"
                for index, item in enumerate(source_items, 1)
            )
            return Response(
                content=(
                    "<!doctype html><html lang='ru'><meta charset='utf-8'>"
                    "<title>Начать подбор</title><style>body{font-family:Arial;margin:24px;background:#f4f6f8}main{background:#fff;padding:20px;border-radius:12px}"
                    "table{width:100%;border-collapse:collapse;margin:18px 0}td,th{border:1px solid #ccc;padding:8px}th{background:#eee}"
                    "button{padding:12px 18px;background:#1a73e8;color:#fff;border:0;border-radius:7px;font-weight:bold;cursor:pointer}input[type=checkbox]{width:20px;height:20px}</style><body><main>"
                    "<p><a href='/dashboard/automation'>← Все заявки</a></p>"
                    f"<h1>{request_heading}</h1>"
                    f"<p>Получено и сохранено позиций: <b>{len(source_items)}</b>. Отметьте строки, для которых нужно найти цены и подготовить предложение.</p>"
                    f"<form method='post' action='/dashboard/automation/jobs/{job_id}/start'>"
                    "<p><label><input id='select-all' type='checkbox' checked onchange=\"document.querySelectorAll('.position-checkbox').forEach(x=>x.checked=this.checked)\"> Выбрать все позиции</label></p>"
                    "<table><tr><th>Выбрать</th><th>№</th><th>Позиция заявки</th><th>Количество</th><th>Ед.</th></tr>"
                    f"{pending_rows}</table>"
                    "<button type='submit'>Найти цены по выбранным позициям</button></form>"
                    "</main></body></html>"
                ),
                media_type="text/html",
            )
        table_rows = []
        row_search_forms = []
        for item in result.get("items") or []:
            selected = item.get("selected") or {}
            candidates = item.get("candidates") or []
            candidate_options = []
            selected_sku = str(selected.get("sku") or selected.get("article") or selected.get("name") or "")
            usable_candidates = [candidate for candidate in candidates if not candidate.get("error")]
            for idx, candidate in enumerate(usable_candidates):
                key = str(candidate.get("sku") or candidate.get("article") or candidate.get("name") or "")
                label = _purchase_label(candidate, str(item.get("unit") or ""))
                pack_size = _pack_size(candidate.get("name") or "", candidate.get("unit"), str(item.get("unit") or ""))
                purchase_price = candidate.get("price")
                offer_price = round(float(purchase_price) / pack_size * (1 + DEFAULT_MARKUP), 2) if purchase_price is not None else ""
                candidate_options.append(
                    f"<option value='{idx}' data-price='{offer_price}' {'selected' if key == selected_sku else ''}>"
                    f"{html.escape(label)}</option>"
                )
            checked = "checked" if _included(item) else ""
            disabled = "disabled" if offer_created else ""
            pos = item.get("position")
            search_form_id = f"search-position-{pos}"
            row_search_forms.append(
                f"<form id='{search_form_id}' method='post' action='/dashboard/automation/jobs/{job_id}/items/{pos}/refresh'></form>"
            )
            rejected_count = len(item.get("rejected_candidates") or [])
            table_rows.append(
                "<tr>"
                f"<td><input form='review-form' type='checkbox' name='include_{pos}' value='1' {checked} {disabled}></td>"
                f"<td>{pos}</td>"
                f"<td>{html.escape(str(item.get('requested_name') or ''))}</td>"
                f"<td><select form='review-form' name='candidate_{pos}' {disabled} onchange=\"document.getElementById('price-{pos}').value=this.options[this.selectedIndex].dataset.price||''\">{''.join(candidate_options) or '<option>Не найден</option>'}</select>"
                f"<div>{'' if offer_created else f'''<button class='row-search' form='{search_form_id}' type='submit'>Искать другие варианты</button>'''}"
                f"<small> ранее отклонено: {rejected_count}</small></div></td>"
                f"<td><input form='review-form' class='qty' name='quantity_{pos}' type='number' step='0.001' value='{html.escape(str(item.get('quantity') or ''))}' {disabled}> {html.escape(str(item.get('unit') or ''))}</td>"
                f"<td><input form='review-form' id='price-{pos}' class='price' name='price_{pos}' type='number' step='0.01' value='{html.escape(str(item.get('proposed_unit_price') or ''))}' {disabled}></td>"
                f"<td>{html.escape(str(item.get('match_status') or '—'))}<br><small>поиск: {html.escape(str(item.get('search_elapsed_seconds') or '—'))} сек."
                f"{' · лимит времени' if item.get('search_timed_out') else ''}</small></td>"
                f"<td>{html.escape(', '.join(item.get('replacement_details') or []) or 'нет')}</td>"
                f"<td>{html.escape(str(item.get('availability_status') or '—'))}</td>"
                f"<td>{html.escape(str(item.get('courier_date') or item.get('pickup_date') or '—'))}</td>"
                f"<td>{'В счёте' if _included(item) else 'Исключено'}</td>"
                "</tr>"
            )
        customer = normalize_customer(result.get("customer"))
        customer_error = customer_validation_error(customer)
        customer_notice = (
            f"<p style='color:#b3261e'><b>{html.escape(customer_error)}</b></p>"
            if customer_error else "<p>Реквизиты плательщика заполнены. Проверьте их перед отправкой.</p>"
        )
        customer_form = (
            "<section><h2>Реквизиты плательщика</h2>" + customer_notice
            + ("<p>Новые реквизиты будут использованы при скачивании исправленного счёта. Уже отправленный файл в Закупай не изменится.</p>" if offer_created else "")
            + f"<form method='post' action='/dashboard/automation/jobs/{job_id}/customer'>"
            + f"<p><label>Наименование <input name='customer_name' value='{html.escape(customer['name'], quote=True)}' required maxlength='500'></label></p>"
            + f"<p><label>ИНН <input name='customer_inn' value='{html.escape(customer['inn'], quote=True)}' required inputmode='numeric' pattern='[0-9]{{10}}|[0-9]{{12}}' maxlength='12'></label> "
            + f"<label>КПП организации <input name='customer_kpp' value='{html.escape(customer['kpp'], quote=True)}' maxlength='9'></label></p>"
            + f"<p><label>Юридический адрес (если известен) <input name='customer_address' value='{html.escape(customer['legalAddress'], quote=True)}' maxlength='1000'></label></p>"
            + "<p><label><input type='checkbox' name='confirm_customer' value='CONFIRMED' required> Реквизиты сверены с заявкой или сообщением плательщика</label></p>"
            + "<button type='submit'>Сохранить реквизиты плательщика</button></form></section>"
        )
        invoice_link = (
            "<p><b>Формирование счёта заблокировано до заполнения реквизитов плательщика.</b></p>"
            if customer_error else
            f"<p><a href='/dashboard/automation/jobs/{job_id}/invoice.xlsx'>Скачать сформированный счёт</a></p>"
            if result.get("status") == "ready_for_review" else
            "<p><b>Счёт пока не сформирован: имеются позиции для проверки.</b></p>"
        )
        if offer_created:
            offer_id = html.escape(str(result.get("live_offer_id") or "—"))
            sent_notice = (
                "<div class='sent-notice'><b>Предложение уже выставлено в Закупай.</b><br>"
                f"ID предложения: {offer_id}. Повторная отправка заблокирована.</div>"
            )
            offer_link = ""
            top_controls = ""
            save_control = ""
        else:
            sent_notice = ""
            offer_link = (
                "<p><b>Ручная заявка:</b> счёт можно скачать, но отправка предложения в Закупай не выполняется.</p>"
                if is_manual else
                "<p>Отправка предложения заблокирована до сохранения реквизитов плательщика.</p>" if customer_error else
                f"<p><a href='/dashboard/order/{row['order_id']}/offer'>Перейти к подтверждению предложения в Закупай</a></p>"
            )
            top_controls = f"<form method='post' action='/dashboard/automation/jobs/{job_id}/refresh'><p><button class='button' type='submit'>Повторить поиск у поставщиков</button></p></form>"
            save_control = "<p><button form='review-form' type='submit'>Сохранить и пересчитать счёт</button></p>"
        search_report = ""
        if result.get("deferred_suppliers"):
            search_report += (
                "<p style='padding:10px;background:#fff7df;border-radius:7px'>"
                + html.escape("; ".join(result.get("deferred_suppliers") or []))
                + ". Текущий результат сформирован без ожидания загрузки большого каталога.</p>"
            )
        if result.get("last_search_at"):
            search_report += (
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
                "th{background:#eee}select{min-width:280px}.qty{width:90px}.price{width:100px}button,.button{display:inline-block;padding:11px 16px;background:#1a73e8;color:white;border:0;border-radius:7px;text-decoration:none;font-weight:bold}.row-search{margin-top:7px;padding:7px 10px;background:#5f6368}.sent-notice{padding:14px;background:#e6f4ea;color:#137333;border-radius:8px;margin:14px 0}</style><body><main>"
                "<p><a href='/dashboard/automation'>← Все заявки</a></p>"
                f"<h1>{request_heading}</h1>"
                f"<p>Счёт № {row['invoice_number']} · статус: {'предложение выставлено' if offer_created else html.escape(str(row['status']))}</p>"
                f"{sent_notice}{customer_form}{top_controls}{search_report}"
                f"<form id='review-form' method='post' action='/dashboard/automation/jobs/{job_id}/review'></form>{''.join(row_search_forms)}<table><tr><th>Включить</th><th>№</th><th>Заявка</th><th>Подбор поставщика<br><small>(закупочная цена)</small></th><th>Количество</th><th>Наша цена<br><small>за единицу заявки (+5%)</small></th>"
                "<th>Статус подбора</th><th>Замена</th><th>Наличие</th><th>Срок</th><th>Решение</th></tr>"
                + "".join(table_rows) + "</table>" + save_control + invoice_link + offer_link + "</main></body></html>"
            ),
            media_type="text/html",
            headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
        )

    @app.post("/dashboard/automation/jobs/{job_id}/customer")
    async def automation_save_customer(job_id: int, request: Request):
        form = await request.form()
        if form.get("confirm_customer") != "CONFIRMED":
            raise HTTPException(status_code=400, detail="Подтвердите проверку реквизитов плательщика")
        try:
            save_invoice_customer(job_id, {
                "name": str(form.get("customer_name") or "")[:500],
                "inn": str(form.get("customer_inn") or ""),
                "kpp": str(form.get("customer_kpp") or ""),
                "legalAddress": str(form.get("customer_address") or "")[:1000],
            })
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return Response(status_code=303, headers={"Location": f"/dashboard/automation/jobs/{job_id}/review"})

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
        _preserve_invoice_customer(result, old_result)
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

    @app.post("/dashboard/automation/jobs/{job_id}/items/{position}/refresh")
    def automation_review_refresh_position(job_id: int, position: int):
        with _connect() as conn:
            row = _execute(conn, "SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
        if not row or not row["result_json"]:
            raise HTTPException(status_code=404, detail="Заявка или результат подбора не найдены")
        result = json.loads(row["result_json"])
        if result.get("live_offer_created"):
            raise HTTPException(status_code=409, detail="Предложение уже отправлено; повторный подбор заблокирован")
        result_items = result.get("items") or []
        current_index = next(
            (idx for idx, item in enumerate(result_items) if int(item.get("position") or 0) == position),
            None,
        )
        current = result_items[current_index] if current_index is not None else None
        if not current:
            raise HTTPException(status_code=404, detail="Позиция заявки не найдена")
        order = _saved_order_snapshot(row, result)
        if not order:
            raise HTTPException(status_code=409, detail="Состав заявки не был сохранён")
        order_items = list(order.get("orderItems") or [])
        if position < 1 or position > len(order_items):
            raise HTTPException(status_code=404, detail="Исходная позиция заявки не найдена")

        excluded = set(current.get("excluded_candidate_keys") or [])
        for candidate in current.get("candidates") or []:
            if not candidate.get("error"):
                excluded.add(_candidate_key(candidate))
        if current.get("selected"):
            excluded.add(_candidate_key(current["selected"]))
        excluded |= _excluded_feedback_keys(current.get("requested_name") or "")

        suppliers = [VseinstrumentiAdapter()]
        krep_komp = KrepKompAdapter()
        if krep_komp.enabled:
            suppliers.append(krep_komp)
        refreshed = _build_match_row(position, order_items[position - 1], suppliers, excluded)
        refreshed["search_history"] = list(current.get("search_history") or []) + [{
            "searched_at": datetime.now(timezone.utc).isoformat(),
            "excluded_candidate_keys": sorted(excluded),
            "previous_selected": current.get("selected"),
        }]
        result["items"][current_index] = refreshed
        result["last_search_at"] = datetime.now(timezone.utc).isoformat()
        result["last_search_position"] = position
        _refresh_summary(result)
        with _lock, _connect() as conn:
            _execute(
                conn,
                "UPDATE automation_jobs SET status=?, result_json=?, updated_at=? WHERE id=?",
                (result["status"], json.dumps(result, ensure_ascii=False), result["last_search_at"], job_id),
            )
        return Response(status_code=303, headers={"Location": f"/dashboard/automation/jobs/{job_id}/review"})

    @app.post("/dashboard/automation/jobs/{job_id}/review")
    async def automation_review_save(job_id: int, request: Request):
        form = await request.form()
        feedback = []
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
                    selected = item["selected"]
                    conflicts = _hard_conflicts(item.get("requested_name") or "", selected)
                    item["match_status"] = _match_status(
                        item.get("requested_name") or "",
                        selected,
                        float(selected.get("match_score") or 0),
                        conflicts,
                    )
                    item["replacement_details"] = conflicts
                    item["purchase_price"] = selected.get("price")
                try:
                    item["quantity"] = float(form.get(f"quantity_{pos}") or item.get("quantity") or 0)
                    item["proposed_unit_price"] = float(form.get(f"price_{pos}") or 0)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"Некорректное количество или цена в позиции {pos}")
                include = form.get(f"include_{pos}") == "1"
                item["decision"] = "approved" if include and item.get("selected") and item.get("proposed_unit_price") is not None else "excluded"
                item["operator_included"] = include
                if item.get("selected"):
                    feedback.append((item.get("requested_name") or "", item["selected"], item["decision"]))
            _refresh_summary(result)
            _execute(conn,
                "UPDATE automation_jobs SET status=?, result_json=?, updated_at=? WHERE id=?",
                (result["status"], json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), job_id),
            )
        for requested_name, candidate, action in feedback:
            _save_match_feedback(requested_name, candidate, action)
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
