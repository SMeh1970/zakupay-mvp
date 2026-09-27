"""Durable PostgreSQL/SQLite task queue used by the background worker."""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timedelta, timezone

from automation_pipeline import DATABASE_URL, _connect, _execute, _lock


WORKER_ID = os.getenv("WORKER_ID", socket.gethostname())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_queue_schema() -> None:
    with _lock, _connect() as conn:
        id_declaration = "BIGSERIAL PRIMARY KEY" if DATABASE_URL else "INTEGER PRIMARY KEY AUTOINCREMENT"
        _execute(conn, f"""CREATE TABLE IF NOT EXISTS task_queue (
            id {id_declaration},
            task_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            dedupe_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            available_at TEXT NOT NULL,
            locked_at TEXT,
            locked_by TEXT,
            result_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_task_queue_ready ON task_queue(status, available_at, id)")


def enqueue_task(task_type: str, payload: dict, dedupe_key: str, max_attempts: int = 3) -> dict:
    ensure_queue_schema()
    now = _now()
    with _lock, _connect() as conn:
        existing = _execute(conn, "SELECT id,status FROM task_queue WHERE dedupe_key=?", (dedupe_key,)).fetchone()
        if existing and existing["status"] in {"queued", "running"}:
            return {"id": existing["id"], "status": existing["status"], "duplicate": True}
        if existing:
            _execute(conn, """UPDATE task_queue SET task_type=?,payload_json=?,status='queued',attempts=0,
                max_attempts=?,available_at=?,locked_at=NULL,locked_by=NULL,result_json=NULL,error=NULL,updated_at=?
                WHERE id=?""", (task_type, json.dumps(payload, ensure_ascii=False), max_attempts, now, now, existing["id"]))
            return {"id": existing["id"], "status": "queued", "duplicate": False}
        sql = """INSERT INTO task_queue
            (task_type,payload_json,dedupe_key,status,attempts,max_attempts,available_at,created_at,updated_at)
            VALUES (?,?,?,'queued',0,?,?,?,?)"""
        values = (task_type, json.dumps(payload, ensure_ascii=False), dedupe_key, max_attempts, now, now, now)
        if DATABASE_URL:
            row = _execute(conn, sql + " RETURNING id", values).fetchone()
            task_id = row["id"]
        else:
            task_id = _execute(conn, sql, values).lastrowid
    return {"id": task_id, "status": "queued", "duplicate": False}


def claim_task(worker_id: str = WORKER_ID) -> dict | None:
    ensure_queue_schema()
    now = _now()
    with _lock, _connect() as conn:
        stale = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        _execute(conn, """UPDATE task_queue SET status='queued',locked_at=NULL,locked_by=NULL,
            error='worker lease expired; task returned to queue',updated_at=?
            WHERE status='running' AND locked_at<?""", (now, stale))
        if DATABASE_URL:
            row = _execute(conn, """SELECT * FROM task_queue
                WHERE status='queued' AND available_at<=?
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1""", (now,)).fetchone()
        else:
            row = _execute(conn, "SELECT * FROM task_queue WHERE status='queued' AND available_at<=? ORDER BY id LIMIT 1", (now,)).fetchone()
        if not row:
            return None
        _execute(conn, """UPDATE task_queue SET status='running',attempts=attempts+1,
            locked_at=?,locked_by=?,updated_at=? WHERE id=? AND status='queued'""",
            (now, worker_id, now, row["id"]))
        current = _execute(conn, "SELECT * FROM task_queue WHERE id=?", (row["id"],)).fetchone()
    task = dict(current)
    task["payload"] = json.loads(task.pop("payload_json"))
    return task


def complete_task(task_id: int, result: dict | None = None) -> None:
    now = _now()
    with _lock, _connect() as conn:
        _execute(conn, """UPDATE task_queue SET status='succeeded',result_json=?,error=NULL,
            locked_at=NULL,locked_by=NULL,updated_at=? WHERE id=?""",
            (json.dumps(result or {}, ensure_ascii=False), now, task_id))


def fail_task(task: dict, error: str) -> None:
    now = datetime.now(timezone.utc)
    retry = int(task.get("attempts") or 0) < int(task.get("max_attempts") or 3)
    status = "queued" if retry else "failed"
    available = (now + timedelta(seconds=min(300, 15 * (2 ** max(0, int(task.get("attempts") or 1) - 1))))).isoformat()
    with _lock, _connect() as conn:
        _execute(conn, """UPDATE task_queue SET status=?,available_at=?,error=?,locked_at=NULL,
            locked_by=NULL,updated_at=? WHERE id=?""", (status, available, error[:4000], now.isoformat(), task["id"]))


def queue_status_for_job(job_id: int) -> str | None:
    ensure_queue_schema()
    prefix = f"match-job:{job_id}:"
    with _connect() as conn:
        row = _execute(conn, "SELECT status FROM task_queue WHERE dedupe_key LIKE ? ORDER BY id DESC LIMIT 1", (prefix + "%",)).fetchone()
    return row["status"] if row else None
