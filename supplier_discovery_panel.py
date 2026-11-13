"""Закрытая страница «Поставщики в интернете»: поиск профильных сайтов по позициям заявки.

Результат — список сайтов-кандидатов с контактами. Цены не читаются, в подбор и счёт ничего не попадает.
Одна позиция — один запуск поиска (run) с сохранённым бюджетом запросов; сайты считаются по доменам.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

import supplier_discovery as sd
from price_lists import database, execute, page
from security import SESSION_COOKIE, _decode_payload, _sign_payload, _valid_session

POLICIES = {"neutral": "Без отметки", "in_work": "В работе", "trusted": "Доверенный", "excluded": "Исключён"}
SELLER_RANK = {"seller": 0, "possible": 1, "unknown": 2}
SELLER_LABEL = {"seller": "Продавец", "possible": "Возможно продавец", "unknown": "Неизвестно"}
FORM_PURPOSE = "websup-form"
_running: set[str] = set()
_running_lock = threading.Lock()


def initialize_schema(conn, postgres):
    conn.execute("""CREATE TABLE IF NOT EXISTS web_supplier_runs (
        run_key TEXT PRIMARY KEY, job_id BIGINT, position INTEGER, item_name TEXT NOT NULL, query TEXT NOT NULL,
        state_json TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_web_supplier_runs_job ON web_supplier_runs(job_id,position)")
    conn.execute("""CREATE TABLE IF NOT EXISTS web_suppliers (
        domain TEXT PRIMARY KEY, policy TEXT NOT NULL DEFAULT 'neutral', contacts_json TEXT,
        contacts_checked_at TEXT, contacts_error TEXT, updated_at TEXT NOT NULL
    )""")


def now():
    return datetime.now(timezone.utc).isoformat()


def esc(value):
    return html.escape(str(value or ""), quote=True)


def settings():
    def number(name, default, low, high):
        try:
            return min(high, max(low, int(os.getenv(name, default))))
        except ValueError:
            return default
    return {"max_requests": number("WEB_SEARCH_MAX_REQUESTS", 6, 1, 200),
            "max_sites": number("WEB_SEARCH_MAX_SITES", 100, 10, 200),
            "pages_per_click": number("WEB_SEARCH_PAGES_PER_CLICK", 2, 1, 10)}


# ------------------------------------------------------------------ защита форм

def require_session(request: Request):
    if not _valid_session(request):
        raise HTTPException(401, "Войдите в закрытую панель.")


def csrf(request):
    return _sign_payload({"exp": int(time.time()) + 3600,
                          "session": hashlib.sha256(request.cookies.get(SESSION_COOKIE, "").encode()).hexdigest()},
                         FORM_PURPOSE, "ws_form")


async def checked_form(request):
    body = await request.body()
    if len(body) > 65536:
        raise HTTPException(413, "Слишком большой запрос.")
    form = await request.form()
    supplied = _decode_payload(str(form.get("csrf", "")), FORM_PURPOSE, "ws_form")
    expected = hashlib.sha256(request.cookies.get(SESSION_COOKIE, "").encode()).hexdigest()
    if not supplied or not hmac.compare_digest(str(supplied.get("session", "")), expected):
        raise HTTPException(403, "Обновите страницу и повторите действие.")
    return form


def form_start(request, action, extra=""):
    return (f"<form method='post' action='{esc(action)}' style='display:inline'>"
            f"<input type='hidden' name='csrf' value='{esc(csrf(request))}'>{extra}")


# ------------------------------------------------------------------ данные

def order_positions(job_id):
    from automation_pipeline import _saved_order_snapshot
    with database() as conn:
        row = execute(conn, "SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Заявка не найдена.")
    order = _saved_order_snapshot(row, json.loads(row["result_json"] or "{}")) or {}
    names = [str(i.get("goodName") or i.get("name") or "").strip() for i in order.get("orderItems") or []]
    return str(order.get("name") or row["subject"] or f"Заявка {job_id}"), names


def run_key(job_id, position, query):
    return f"{job_id}:{position}" if job_id and position else "m:" + hashlib.sha1(query.encode()).hexdigest()[:16]


def load_run(key):
    with database() as conn:
        row = execute(conn, "SELECT * FROM web_supplier_runs WHERE run_key=?", (key,)).fetchone()
    return (dict(row), json.loads(row["state_json"])) if row else (None, None)


def save_run(key, job_id, position, item_name, state):
    stamp = now()
    with database() as conn:
        execute(conn, """INSERT INTO web_supplier_runs(run_key,job_id,position,item_name,query,state_json,reason,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(run_key) DO UPDATE SET query=excluded.query,
            state_json=excluded.state_json,reason=excluded.reason,updated_at=excluded.updated_at""",
                (key, job_id or 0, position or 0, item_name[:500], state["query"], json.dumps(state, ensure_ascii=False),
                 state["reason"], stamp, stamp))


def supplier_records():
    with database() as conn:
        rows = execute(conn, "SELECT * FROM web_suppliers").fetchall()
    return {r["domain"]: dict(r) for r in rows}


def set_policy(domain, policy):
    if policy not in POLICIES or not domain:
        raise ValueError("Неизвестная отметка.")
    with database() as conn:
        execute(conn, """INSERT INTO web_suppliers(domain,policy,updated_at) VALUES(?,?,?)
            ON CONFLICT(domain) DO UPDATE SET policy=excluded.policy,updated_at=excluded.updated_at""", (domain, policy, now()))


def save_contacts(domain, contacts, error=""):
    with database() as conn:
        execute(conn, """INSERT INTO web_suppliers(domain,contacts_json,contacts_checked_at,contacts_error,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(domain) DO UPDATE SET contacts_json=excluded.contacts_json,
            contacts_checked_at=excluded.contacts_checked_at,contacts_error=excluded.contacts_error,updated_at=excluded.updated_at""",
                (domain, json.dumps(contacts, ensure_ascii=False), now(), error[:500], now()))


def excluded_domains():
    return sorted(d for d, r in supplier_records().items() if r.get("policy") == "excluded")


def aggregate(job_id):
    """Сайты заявки по доменам: сколько позиций покрывает сайт, чем он лучше оценён, откуда найден."""
    with database() as conn:
        runs = execute(conn, "SELECT * FROM web_supplier_runs WHERE job_id=? ORDER BY position", (job_id,)).fetchall()
    domains = {}
    for run in runs:
        for site in json.loads(run["state_json"]).get("sites", []):
            d = sd.site_domain(site["url"])
            entry = domains.setdefault(d, {"domain": d, "positions": {}, "seller": "unknown", "reason": "", "url": site["url"],
                                           "title": site["title"], "engines": set()})
            entry["positions"].setdefault(run["position"], {"name": run["item_name"], "url": site["url"], "title": site["title"]})
            entry["engines"].add(site.get("engine", ""))
            status, reason = sd.merchant_assessment(site["title"], site.get("description", ""), site["url"])
            if SELLER_RANK[status] < SELLER_RANK[entry["seller"]] or not entry["reason"]:
                entry["seller"], entry["reason"], entry["url"], entry["title"] = status, reason, site["url"], site["title"]
    return runs, domains


# ------------------------------------------------------------------ поиск

def execute_search(key, job_id, position, item_name, query, engines):
    cfg = settings()
    serpapi_key, firecrawl_key = sd.env_keys()
    blocked = excluded_domains()
    row, state = load_run(key)
    if not state or state["query"] != query or sorted(state["engines"]) != sorted(engines):
        state = sd.new_state(query, engines)

    def fetch(engine, page_number, text, seen):
        return sd.search_page(engine, page_number, text, serpapi_key=serpapi_key, firecrawl_key=firecrawl_key,
                              limit=cfg["max_sites"], exclude_domains=[*blocked, *seen])

    sd.collect_sites(fetch, state, limit=cfg["max_sites"], max_requests=cfg["max_requests"], max_pages=cfg["pages_per_click"],
                     accept=lambda r: not sd.excluded_by(r["url"], blocked),
                     on_state=lambda s: save_run(key, job_id, position, item_name, s))
    return state


def default_engines():
    serpapi_key, _ = sd.env_keys()
    return ["yandex", "google"] if serpapi_key else ["firecrawl"]


# ------------------------------------------------------------------ страница

def contacts_cell(record):
    if not record or record.get("contacts_json") is None:
        return "<span class='note'>Не искали</span>"
    contacts = json.loads(record["contacts_json"])
    out = []
    for kind, label in (("email", ""), ("phone", ""), ("form", "форма заявки"), ("contact", "страница контактов")):
        for c in [c for c in contacts if c["kind"] == kind][:3]:
            out.append(esc(c["value"]) if kind in ("email", "phone") else f"<a href='{esc(c['value'])}' rel='noopener noreferrer' target='_blank'>{label}</a>")
    note = f"<br><span class='error'>{esc(record['contacts_error'])}</span>" if record.get("contacts_error") else ""
    return ("<br>".join(out) or "<span class='note'>Контакты не найдены</span>") + note


def render_page(request, job_id, message, show_all):
    cfg = settings()
    serpapi_key, firecrawl_key = sd.env_keys()
    title, names, runs, domains = "Поиск без заявки", [], [], {}
    if job_id:
        title, names = order_positions(job_id)
        runs, domains = aggregate(job_id)
    run_by_position = {r["position"]: r for r in runs}
    records = supplier_records()
    keys_note = ("Google и Яндекс: ключ SerpAPI подключён." if serpapi_key else
                 "Ключ SerpAPI не задан (SERPAPI_API_KEY): работает только Firecrawl" + (" с ключом." if firecrawl_key else " без ключа, в пределах бесплатного лимита сервиса."))
    notice = f"<div role='status' style='padding:14px;border:2px solid #1a73e8;border-radius:8px;margin:12px 0'><b>{esc(message)}</b></div>" if message else ""
    body = (f"<h1>Поставщики в интернете</h1>{notice}<section><p>Находит сайты профильных поставщиков по названию позиции. "
            "Цены с сайтов не читаются и в подбор или счёт не попадают. Найденный сайт — кандидат: профильность и работу с ним проверяет оператор.</p>"
            f"<p class='note'>{esc(keys_note)} Бюджет: до {cfg['max_requests']} запросов на позицию, "
            f"{cfg['pages_per_click']} страниц за одно нажатие, до {cfg['max_sites']} сайтов.</p></section>")
    if job_id:
        rows = ""
        for number, name in enumerate(names, 1):
            run = run_by_position.get(number)
            state = json.loads(run["state_json"]) if run else None
            status = (f"{len(state['sites'])} сайтов · запросов {state['requests']} из {cfg['max_requests']} · {esc(sd.STOP_LABELS.get(state['reason'], state['reason']))}"
                      if state else "Не искали")
            errors = f"<br><span class='error'>{esc(state['errors'][-1])}</span>" if state and state["errors"] else ""
            can_continue = not state or state["reason"] in ("paused", "economy", "credits", "error")
            label = "Найти" if not state else "Искать ещё"
            button = (form_start(request, "/dashboard/web-suppliers/search",
                                 f"<input type='hidden' name='job_id' value='{job_id}'><input type='hidden' name='position' value='{number}'>")
                      + f"<button>{label}</button></form>") if can_continue else ""
            rows += f"<tr><td>{number}</td><td>{esc(name)}</td><td>{status}{errors}</td><td>{button}</td></tr>"
        body += (f"<section><h2>{esc(title)}: позиции</h2><div class='scroll'><table><tr><th>№</th><th>Позиция</th><th>Поиск</th><th></th></tr>"
                 f"{rows}</table></div></section>")
        visible = [d for d in domains.values() if show_all or records.get(d["domain"], {}).get("policy") != "excluded"]
        visible.sort(key=lambda d: (records.get(d["domain"], {}).get("policy") != "trusted", -len(d["positions"]),
                                    SELLER_RANK[d["seller"]], d["domain"]))
        total = len(names)
        trs = ""
        for d in visible:
            record, policy = records.get(d["domain"]), records.get(d["domain"], {}).get("policy", "neutral")
            covered = "<br>".join(f"{n}. <a href='{esc(p['url'])}' rel='noopener noreferrer' target='_blank'>{esc(p['name'][:60])}</a>"
                                  for n, p in sorted(d["positions"].items())[:6])
            more = f"<br><span class='note'>и ещё {len(d['positions']) - 6}</span>" if len(d["positions"]) > 6 else ""
            buttons = "".join(form_start(request, "/dashboard/web-suppliers/policy",
                                         f"<input type='hidden' name='job_id' value='{job_id}'><input type='hidden' name='domain' value='{esc(d['domain'])}'>"
                                         f"<input type='hidden' name='policy' value='{p}'>")
                              + f"<button style='margin:2px;padding:6px 9px;{'opacity:.55' if policy == p else ''}'>{esc(label)}</button></form>"
                              for p, label in POLICIES.items() if p != "neutral" or policy != "neutral")
            contacts_button = (form_start(request, "/dashboard/web-suppliers/contacts",
                                          f"<input type='hidden' name='job_id' value='{job_id}'><input type='hidden' name='domain' value='{esc(d['domain'])}'>"
                                          f"<input type='hidden' name='url' value='{esc(d['url'])}'>") + "<button style='padding:6px 9px'>Найти контакты</button></form>")
            trs += (f"<tr><td><b><a href='{esc(d['url'])}' rel='noopener noreferrer' target='_blank'>{esc(d['domain'])}</a></b>"
                    f"<br><span class='note'>{esc(POLICIES[policy])}</span></td>"
                    f"<td>{len(d['positions'])} из {total}<br>{covered}{more}</td>"
                    f"<td>{esc(SELLER_LABEL[d['seller']])}<br><span class='note'>{esc(d['reason'])}</span></td>"
                    f"<td>{contacts_cell(record)}<br>{contacts_button}</td><td>{buttons}</td></tr>")
        hidden = len(domains) - len(visible)
        toggle = (f"<p><a href='/dashboard/web-suppliers?{urlencode({'job_id': job_id, 'all': 0 if show_all else 1})}'>"
                  f"{'Скрыть исключённые' if show_all else f'Показать исключённые ({hidden})' if hidden else ''}</a></p>")
        body += (f"<section><h2>Найденные сайты ({len(visible)})</h2>{toggle}"
                 "<p class='note'>Порядок: доверенные, затем сайты, найденные по большему числу позиций заявки. "
                 "«Исключён» убирает сайт из новых поисков и из этого списка.</p><div class='scroll'><table>"
                 "<tr><th>Сайт</th><th>Позиции заявки</th><th>Оценка по найденной странице</th><th>Контакты</th><th>Отметка</th></tr>"
                 f"{trs}</table></div></section>")
    else:
        body += ("<section><h2>Поиск по названию</h2>"
                 + form_start(request, "/dashboard/web-suppliers/search")
                 + "<label>Название товара <input name='query' maxlength='300' size='60' required></label><button>Найти</button></form>"
                 "<p class='note'>Для поиска по позициям заявки откройте эту страницу из заявки.</p></section>")
    return page(body, "Поставщики в интернете")


def install_supplier_discovery(app):
    router = APIRouter(prefix="/dashboard/web-suppliers", dependencies=[Depends(require_session)])

    def back(job_id, message):
        query = {"message": message}
        if job_id:
            query["job_id"] = job_id
        return RedirectResponse("/dashboard/web-suppliers?" + urlencode(query), status_code=303)

    @router.get("")
    def dashboard(request: Request, job_id: int = 0, message: str = "", all: int = 0):
        return render_page(request, job_id, message[:500], bool(all))

    @router.post("/search")
    async def search(request: Request):
        form = await checked_form(request)
        job_id = int(form.get("job_id") or 0)
        position = int(form.get("position") or 0)
        if job_id:
            _, names = order_positions(job_id)
            if not 1 <= position <= len(names) or not names[position - 1]:
                raise HTTPException(400, "Позиция отсутствует в заявке.")
            item_name = names[position - 1]
        else:
            item_name = str(form.get("query", "")).strip()
            if not item_name:
                raise HTTPException(400, "Введите название товара.")
        try:
            query = sd.build_query(item_name)
        except ValueError as exc:
            return back(job_id, str(exc))
        key = run_key(job_id, position, query)
        engines = default_engines()
        with _running_lock:
            busy = key in _running
            _running.add(key)
        if busy:
            return back(job_id, "Поиск по этой позиции уже выполняется. Подождите и обновите страницу.")
        try:
            state = await run_in_threadpool(execute_search, key, job_id, position, item_name, query, engines)
        finally:
            with _running_lock:
                _running.discard(key)
        message = f"Найдено сайтов: {len(state['sites'])}. {sd.STOP_LABELS.get(state['reason'], state['reason'])}."
        if state["errors"] and state["reason"] == "error":
            message = state["errors"][-1]
        return back(job_id, message)

    @router.post("/policy")
    async def policy(request: Request):
        form = await checked_form(request)
        try:
            set_policy(str(form.get("domain", "")).strip().lower(), str(form.get("policy", "")))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return back(int(form.get("job_id") or 0), "Отметка сохранена.")

    @router.post("/contacts")
    async def contacts(request: Request):
        form = await checked_form(request)
        domain, url = str(form.get("domain", "")).strip().lower(), str(form.get("url", ""))
        if not domain or sd.site_domain(url) != domain:
            raise HTTPException(400, "Адрес не относится к этому сайту.")
        blocked = excluded_domains()

        def work():
            try:
                found = sd.read_domain_contacts(url, blocked=blocked)
                save_contacts(domain, found["contacts"], "; ".join(found["errors"]))
                return f"{domain}: контактов найдено {len(found['contacts'])}."
            except sd.ProviderError as exc:
                save_contacts(domain, [], str(exc))
                return f"{domain}: {exc}"
        return back(int(form.get("job_id") or 0), await run_in_threadpool(work))

    app.include_router(router)
