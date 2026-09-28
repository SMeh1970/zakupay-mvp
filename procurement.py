"""Supplier purchase drafts. No live supplier order is sent from this module."""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone

from fastapi import HTTPException
from fastapi.responses import Response

from automation_pipeline import DATABASE_URL, _connect, _execute, _included, _lock


def ensure_procurement_schema() -> None:
    with _lock, _connect() as conn:
        draft_id = "BIGSERIAL PRIMARY KEY" if DATABASE_URL else "INTEGER PRIMARY KEY AUTOINCREMENT"
        line_id = "BIGSERIAL PRIMARY KEY" if DATABASE_URL else "INTEGER PRIMARY KEY AUTOINCREMENT"
        _execute(conn, f"""CREATE TABLE IF NOT EXISTS procurement_drafts (
            id {draft_id}, automation_job_id BIGINT NOT NULL, order_id BIGINT NOT NULL,
            supplier TEXT NOT NULL, status TEXT NOT NULL, total REAL NOT NULL DEFAULT 0,
            external_order_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(automation_job_id, supplier))""")
        _execute(conn, f"""CREATE TABLE IF NOT EXISTS procurement_lines (
            id {line_id}, draft_id BIGINT NOT NULL, position INTEGER NOT NULL,
            requested_name TEXT NOT NULL, supplier_sku TEXT, supplier_name TEXT NOT NULL,
            quantity REAL NOT NULL, purchase_price REAL NOT NULL, line_total REAL NOT NULL,
            availability TEXT, delivery_date TEXT, payload_json TEXT NOT NULL)""")


def build_procurement_drafts(job_id: int) -> list[int]:
    ensure_procurement_schema()
    with _connect() as conn:
        job = _execute(conn, "SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
    if not job or not job["result_json"]:
        raise ValueError("Результат подбора не найден")
    result = json.loads(job["result_json"])
    grouped: dict[str, list[dict]] = {}
    for row in result.get("items") or []:
        candidate = row.get("selected") or {}
        if not _included(row) or not candidate or candidate.get("price") is None:
            continue
        grouped.setdefault(candidate.get("supplier") or "Неизвестный поставщик", []).append(row)
    if not grouped:
        raise ValueError("Нет включённых позиций с выбранным поставщиком и закупочной ценой")
    now = datetime.now(timezone.utc).isoformat()
    draft_ids = []
    with _lock, _connect() as conn:
        for supplier, rows in grouped.items():
            existing = _execute(conn, "SELECT id,status FROM procurement_drafts WHERE automation_job_id=? AND supplier=?", (job_id, supplier)).fetchone()
            total = sum(float((row.get("selected") or {}).get("price") or 0) * float(row.get("purchase_units") or row.get("quantity") or 0) for row in rows)
            if existing:
                if existing["status"] not in {"draft", "approved_for_submission"}:
                    raise ValueError(f"Черновик {existing['id']} уже нельзя пересобрать")
                draft_id = existing["id"]
                _execute(conn, "UPDATE procurement_drafts SET status='draft',total=?,updated_at=? WHERE id=?", (total, now, draft_id))
                _execute(conn, "DELETE FROM procurement_lines WHERE draft_id=?", (draft_id,))
            else:
                sql = """INSERT INTO procurement_drafts
                    (automation_job_id,order_id,supplier,status,total,created_at,updated_at)
                    VALUES (?,?,?,'draft',?,?,?)"""
                values = (job_id, job["order_id"], supplier, total, now, now)
                if DATABASE_URL:
                    draft_id = _execute(conn, sql + " RETURNING id", values).fetchone()["id"]
                else:
                    draft_id = _execute(conn, sql, values).lastrowid
            draft_ids.append(draft_id)
            for row in rows:
                candidate = row["selected"]
                quantity = float(row.get("purchase_units") or row.get("quantity") or 0)
                price = float(candidate.get("price") or 0)
                delivery = row.get("courier_date") or row.get("pickup_date")
                _execute(conn, """INSERT INTO procurement_lines
                    (draft_id,position,requested_name,supplier_sku,supplier_name,quantity,purchase_price,line_total,availability,delivery_date,payload_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
                    draft_id, row.get("position"), row.get("requested_name") or "",
                    str(candidate.get("sku") or candidate.get("article") or ""), candidate.get("name") or "",
                    quantity, price, quantity * price, row.get("availability_status"), delivery,
                    json.dumps(row, ensure_ascii=False),
                ))
    return draft_ids


def install_procurement(app) -> None:
    @app.post("/dashboard/automation/jobs/{job_id}/procurement/build")
    def procurement_build(job_id: int):
        try:
            build_procurement_drafts(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return Response(status_code=303, headers={"Location": f"/dashboard/automation/jobs/{job_id}/procurement"})

    @app.get("/dashboard/automation/jobs/{job_id}/procurement")
    def procurement_view(job_id: int):
        ensure_procurement_schema()
        with _connect() as conn:
            drafts = _execute(conn, "SELECT * FROM procurement_drafts WHERE automation_job_id=? ORDER BY id", (job_id,)).fetchall()
            job = _execute(conn, "SELECT order_id FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
            sections = []
            for draft in drafts:
                lines = _execute(conn, "SELECT * FROM procurement_lines WHERE draft_id=? ORDER BY position", (draft["id"],)).fetchall()
                rows = "".join(
                    f"<tr><td>{line['position']}</td><td>{html.escape(line['requested_name'])}</td><td>{html.escape(line['supplier_name'])}</td>"
                    f"<td>{line['quantity']:g}</td><td>{line['purchase_price']:.2f}</td><td>{line['line_total']:.2f}</td>"
                    f"<td>{html.escape(line['availability'] or 'не подтверждено')}</td><td>{html.escape(line['delivery_date'] or '—')}</td></tr>"
                    for line in lines
                )
                action = "<b>Подтверждён к будущей отправке по API</b>" if draft["status"] == "approved_for_submission" else (
                    f"<form method='post' action='/dashboard/automation/procurement/{draft['id']}/confirm'><button>Подтвердить заказ поставщику</button></form>"
                )
                sections.append(f"<section><h2>{html.escape(draft['supplier'])}</h2><p>Статус: {draft['status']} · сумма: {draft['total']:.2f} ₽</p>"
                    f"<table><tr><th>№</th><th>Заявка</th><th>Товар поставщика</th><th>Кол-во</th><th>Цена</th><th>Сумма</th><th>Наличие</th><th>Срок</th></tr>{rows}</table>{action}</section>")
        body = "".join(sections) or "<p>Черновики ещё не созданы.</p>"
        order_id = job["order_id"] if job else None
        next_action = (
            f"<a class='button primary' href='/dashboard/order/{order_id}/offer'>Перейти к выставлению счёта в Закупай →</a>"
            if order_id else ""
        )
        navigation = (
            f"<div class='actions'><a class='button secondary' href='/dashboard/automation/jobs/{job_id}/review'>← Вернуться к проверке</a>"
            f"{next_action}</div>"
        )
        return Response(content=f"<!doctype html><meta charset='utf-8'><style>body{{font-family:Arial;margin:24px;background:#f4f6f8}}section{{background:white;padding:18px;margin:15px 0;border-radius:10px}}table{{width:100%;border-collapse:collapse}}td,th{{border:1px solid #ddd;padding:8px}}button,.button{{display:inline-block;padding:11px 16px;background:#188038;color:white;border:0;border-radius:7px;text-decoration:none;font-weight:bold;cursor:pointer}}.actions{{display:flex;gap:12px;flex-wrap:wrap;margin:18px 0}}.primary{{background:#1a73e8}}.secondary{{background:#5f6368}}</style>{navigation}<h1>Черновики заказов поставщикам</h1><p>Подтверждение пока не отправляет заказ поставщику: подключение методов создания/отмены заказа выполняется отдельно после проверки API.</p>{body}{navigation}", media_type="text/html")

    @app.post("/dashboard/automation/procurement/{draft_id}/confirm")
    def procurement_confirm(draft_id: int):
        ensure_procurement_schema()
        now = datetime.now(timezone.utc).isoformat()
        with _lock, _connect() as conn:
            draft = _execute(conn, "SELECT * FROM procurement_drafts WHERE id=?", (draft_id,)).fetchone()
            if not draft:
                raise HTTPException(status_code=404, detail="Черновик не найден")
            _execute(conn, "UPDATE procurement_drafts SET status='approved_for_submission',updated_at=? WHERE id=?", (now, draft_id))
        return Response(status_code=303, headers={"Location": f"/dashboard/automation/jobs/{draft['automation_job_id']}/procurement"})
