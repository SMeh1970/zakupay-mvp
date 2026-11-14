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
import re
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
# Цвет 0 — «без цвета»; остальные — метки групп, как цветные метки в почте. Номер виден на точке, цвет не единственный признак.
GROUP_COLORS = ["#9aa0a6", "#d93025", "#f29900", "#188038", "#1a73e8", "#9334e6", "#e52592", "#00897b"]
_running: set[str] = set()
_running_lock = threading.Lock()


def initialize_schema(conn, postgres):
    conn.execute("""CREATE TABLE IF NOT EXISTS web_supplier_runs (
        run_key TEXT PRIMARY KEY, job_id BIGINT, position INTEGER, item_name TEXT NOT NULL, query TEXT NOT NULL,
        state_json TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_web_supplier_runs_job ON web_supplier_runs(job_id,position)")
    conn.execute("""CREATE TABLE IF NOT EXISTS web_supplier_marks (
        job_id BIGINT NOT NULL, position INTEGER NOT NULL, color INTEGER NOT NULL DEFAULT 0, selected INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL, PRIMARY KEY (job_id, position)
    )""")
    # v2: цвет = группа, phrase = позиция-«поисковая фраза» группы (раньше «искать»; старые отметки не переносятся, смысл другой)
    conn.execute("""CREATE TABLE IF NOT EXISTS web_supplier_marks_v2 (
        job_id BIGINT NOT NULL, position INTEGER NOT NULL, color INTEGER NOT NULL DEFAULT 0, phrase INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL, PRIMARY KEY (job_id, position)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS web_supplier_groups (
        job_id BIGINT NOT NULL, color INTEGER NOT NULL, keyword TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (job_id, color)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS web_supplier_emails (
        email TEXT PRIMARY KEY, selected INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
    )""")
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

def order_details(job_id):
    """Название заявки, названия позиций и их количество с единицей измерения («12 шт»)."""
    from automation_pipeline import _saved_order_snapshot
    with database() as conn:
        row = execute(conn, "SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Заявка не найдена.")
    order = _saved_order_snapshot(row, json.loads(row["result_json"] or "{}")) or {}
    items = order.get("orderItems") or []
    names = [str(i.get("goodName") or i.get("name") or "").strip() for i in items]
    quantities = []
    for item in items:
        count = item.get("count") if item.get("count") not in (None, "") else item.get("quantity")
        unit = item.get("unit")
        unit = unit.get("name") if isinstance(unit, dict) else unit
        quantities.append(" ".join(str(v) for v in (count, unit) if v not in (None, "")))
    return str(order.get("name") or row["subject"] or f"Заявка {job_id}"), names, quantities


def order_positions(job_id):
    title, names, _ = order_details(job_id)
    return title, names


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


def load_marks(job_id, count):
    """{номер позиции: (цвет, фраза)}. Цвет 0 — позиция ни в какой группе и в поиск не попадает."""
    with database() as conn:
        rows = execute(conn, "SELECT position,color,phrase FROM web_supplier_marks_v2 WHERE job_id=?", (job_id,)).fetchall()
    saved = {r["position"]: (int(r["color"]), bool(r["phrase"])) for r in rows}
    return {n: saved.get(n, (0, False)) for n in range(1, count + 1)}


def save_marks(job_id, marks):
    stamp = now()
    with database() as conn:
        for position, (color, phrase) in marks.items():
            execute(conn, """INSERT INTO web_supplier_marks_v2(job_id,position,color,phrase,updated_at) VALUES(?,?,?,?,?)
                ON CONFLICT(job_id,position) DO UPDATE SET color=excluded.color,phrase=excluded.phrase,updated_at=excluded.updated_at""",
                    (job_id, position, color, 1 if phrase else 0, stamp))


def build_groups(marks):
    """{цвет: [номера позиций]}: группа = все позиции этого цвета. Без цвета позиция в группу не входит."""
    groups = {}
    for position, (color, _) in sorted(marks.items()):
        if color:
            groups.setdefault(color, []).append(position)
    return dict(sorted(groups.items()))


def group_phrases(marks, members):
    """Позиции группы, отмеченные галочкой как поисковая фраза."""
    return [n for n in members if marks[n][1]]


# Рубрики собраны по существующему определителю категорий (product_characteristics): рубрика -> категории.
RUBRICS = {
    "Трубопровод и соединительные детали": ["Трубы", "Профильные трубы", "Отводы", "Переходы", "Тройники"],
    "Крепёж и монтаж": ["Крепёж", "Подвесы"],
    "Инструмент и оснастка": ["Сверла", "Зенковки", "Метчики", "Фрезы", "Измерительный инструмент", "Абразивные круги", "Подшипники"],
    "Краски, отделка, строительные смеси": ["Краски и эмали", "Шпаклёвки", "Штукатурки", "Клеи", "Вяжущие", "Плёнки"],
    "Сантехника": ["Дозаторы", "Инсталляции", "Писсуары", "Раковины", "Унитазы", "Смесители"],
    "Электро и безопасность": ["Кабели", "Огнетушители"],
    "Двери, панели, профили, фанера": ["Двери", "Панели", "Профили", "Фанера"],
}
UNKNOWN_CATEGORY = "Не определена"
CATEGORY_RUBRIC = {c: r for r, cats in RUBRICS.items() for c in cats}


def position_category(name):
    try:
        import product_characteristics
        return product_characteristics.extract(name).get("category") or UNKNOWN_CATEGORY
    except Exception:  # определитель не должен ронять страницу
        return UNKNOWN_CATEGORY


def position_blocks(names):
    """[(рубрика, [(категория, [номера позиций])])]: блоки по порядку первого появления, «Не определена» в конце.
    Внутри категории позиции идут по номерам; сквозной порядок нумерации не сохраняется."""
    rubrics = {}
    for number, name in enumerate(names, 1):
        category = position_category(name)
        rubric = CATEGORY_RUBRIC.get(category, "Без рубрики")
        rubrics.setdefault(rubric, {}).setdefault(category, []).append(number)
    ordered = sorted(rubrics.items(), key=lambda kv: kv[0] == "Без рубрики")
    return [(rubric, sorted(cats.items(), key=lambda kv: kv[0] == UNKNOWN_CATEGORY)) for rubric, cats in ordered]


def group_title(color):
    return f"Группа {color}"


def load_keywords(job_id):
    with database() as conn:
        rows = execute(conn, "SELECT color,keyword FROM web_supplier_groups WHERE job_id=?", (job_id,)).fetchall()
    return {int(r["color"]): r["keyword"] for r in rows}


def save_keyword(job_id, color, keyword):
    with database() as conn:
        execute(conn, """INSERT INTO web_supplier_groups(job_id,color,keyword,updated_at) VALUES(?,?,?,?)
            ON CONFLICT(job_id,color) DO UPDATE SET keyword=excluded.keyword,updated_at=excluded.updated_at""",
                (job_id, color, keyword[:300], now()))


def group_run_key(job_id, color):
    return f"{job_id}:g{color}"


def aggregate(job_id, names=None):
    """Сайты заявки по доменам: сколько позиций покрывает сайт, чем он лучше оценён, откуда найден.
    Поиск по группе (position < 0) засчитывается всем позициям группы, которые сейчас отмечены «искать»."""
    with database() as conn:
        runs = execute(conn, "SELECT * FROM web_supplier_runs WHERE job_id=? ORDER BY position", (job_id,)).fetchall()
    groups = build_groups(load_marks(job_id, len(names))) if names else {}
    domains = {}
    for run in runs:
        color = -run["position"] - 1 if run["position"] < 0 else None
        for site in json.loads(run["state_json"]).get("sites", []):
            d = sd.site_domain(site["url"])
            entry = domains.setdefault(d, {"domain": d, "positions": {}, "seller": "unknown", "reason": "", "url": site["url"],
                                           "title": site["title"], "engines": set(), "groups": set()})
            if color is None:
                entry["positions"].setdefault(run["position"], {"name": run["item_name"], "url": site["url"], "title": site["title"]})
            else:
                entry["groups"].add(color)
                for number in groups.get(color, []):
                    entry["positions"].setdefault(number, {"name": names[number - 1], "url": site["url"], "title": site["title"]})
            entry["engines"].add(site.get("engine", ""))
            status, reason = sd.merchant_assessment(site["title"], site.get("description", ""), site["url"])
            if SELLER_RANK[status] < SELLER_RANK[entry["seller"]] or not entry["reason"]:
                entry["seller"], entry["reason"], entry["url"], entry["title"] = status, reason, site["url"], site["title"]
    return runs, domains


# ------------------------------------------------------------------ поиск

def execute_search(key, job_id, position, item_name, query, engines):
    cfg = settings()
    serpapi_key, firecrawl_key = sd.env_keys()
    yandex_key, yandex_folder = sd.yandex_env()
    blocked = excluded_domains()
    row, state = load_run(key)
    if not state or state["query"] != query or sorted(state["engines"]) != sorted(engines):
        state = sd.new_state(query, engines)
    state.setdefault("operations", {})

    def fetch(engine, page_number, text, seen):
        return sd.search_page(engine, page_number, text, serpapi_key=serpapi_key, firecrawl_key=firecrawl_key,
                              limit=cfg["max_sites"], exclude_domains=[*blocked, *seen],
                              yandex_key=yandex_key, yandex_folder=yandex_folder, operations=state["operations"],
                              save=lambda: save_run(key, job_id, position, item_name, state))

    sd.collect_sites(fetch, state, limit=cfg["max_sites"], max_requests=cfg["max_requests"], max_pages=cfg["pages_per_click"],
                     accept=lambda r: not sd.excluded_by(r["url"], blocked),
                     on_state=lambda s: save_run(key, job_id, position, item_name, s))
    return state


def default_engines():
    """Официальный Яндекс (отложенный запрос, в разы дешевле) важнее SerpAPI; без ключей остаётся Firecrawl."""
    serpapi_key, _ = sd.env_keys()
    yandex_key, yandex_folder = sd.yandex_env()
    if yandex_key and yandex_folder:
        return ["yandex_cloud"]
    return ["yandex", "google"] if serpapi_key else ["firecrawl"]


def waiting_note(state):
    """«Запрос отправлен N мин назад» для ожидающей операции Яндекса."""
    for operation in (state.get("operations") or {}).values():
        try:
            started = datetime.fromisoformat(operation["started"])
        except (KeyError, ValueError):
            continue
        minutes = max(0, int((datetime.now(timezone.utc) - started).total_seconds() // 60))
        return f"запрос отправлен {minutes} мин назад"
    return ""


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


def recent_orders_section():
    from automation_pipeline import _saved_order_snapshot
    with database() as conn:
        rows = execute(conn, "SELECT * FROM automation_jobs ORDER BY id DESC LIMIT 30").fetchall()
    items = ""
    for row in rows:
        try:
            order = _saved_order_snapshot(row, json.loads(row["result_json"] or "{}")) or {}
        except (ValueError, TypeError):
            order = {}
        count = len(order.get("orderItems") or [])
        label = str(order.get("name") or row["subject"] or f"Заявка {row['id']}")
        items += f"<li><a href='/dashboard/web-suppliers?job_id={int(row['id'])}'>{esc(label)}</a> · позиций: {count}</li>"
    return ("<section><details><summary><b>Поиск по позициям заявки</b></summary>"
            "<p class='note'>Выберите заявку: её позиции попадут в многопозиционный поиск с группами.</p>"
            f"<ul>{items or '<li class=note>Заявок пока нет.</li>'}</ul></details></section>")


STYLE = ("<style>.mark{display:inline-flex;align-items:center;margin-right:8px}.dot{width:30px;height:30px;border-radius:50%;padding:0;"
         "color:#fff;font-weight:bold;border:2px solid #fff;box-shadow:0 0 0 1px #5f6368;cursor:pointer;display:inline-block;text-align:center;line-height:26px}"
         ".dot.static{width:22px;height:22px;line-height:18px;cursor:default;font-size:12px}"
         ".members{margin:6px 0;padding-left:20px}.group.warn{background:#fff4e5}.warnline,.warnmark{color:#b45309;font-weight:bold}.warnmark{font-size:12px}tr.hdr td{background:#eef1f5}tr.hdr.rub td{background:#dfe6ef;font-size:16px}tr.hdr.cat td{padding-left:24px}tr.pos td:nth-child(3){padding-left:12px}.group{background:#f8f9fa;padding:12px 16px;margin:12px 0;border-radius:8px}input[type=checkbox]{width:20px;height:20px;vertical-align:middle}"
         ".notice{padding:14px;border:2px solid #1a73e8;border-radius:8px;margin:12px 0}"
         ".searchbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.searchbar label{font-weight:bold;color:#5f6368}.searchbar input{flex:1;min-width:260px;font-size:16px}"
         ".chips{display:flex;gap:8px;flex-wrap:wrap}.chip{background:#e8f0fe;border-radius:14px;padding:4px 10px;text-decoration:none;font-size:13px}"
         ".layout{display:grid;grid-template-columns:230px 1fr;gap:20px;align-items:start}@media(max-width:800px){.layout{grid-template-columns:1fr}}"
         "aside{background:#fff;padding:12px;border-radius:10px}aside h3{margin:6px 0}.word{display:flex;gap:8px;align-items:center;border:1px solid #ddd;border-radius:6px;padding:5px 8px;margin:5px 0}"
         ".word span:first-child{flex:1}.word a{text-decoration:none;font-weight:bold;padding:0 6px}.active div{background:#fce8e6;border-radius:6px;padding:5px 8px;margin:4px 0}"
         ".tabs{display:flex;gap:16px;flex-wrap:wrap;border-bottom:1px solid #ddd;margin-bottom:10px}.tab{padding:8px 2px;text-decoration:none;color:#5f6368}.tab.on{font-weight:bold;color:#202124;border-bottom:3px solid #1a73e8}"
         ".actions{display:flex;gap:10px;flex-wrap:wrap}.actions form{margin:0}button.secondary{background:#e8f0fe;color:#174ea6}"
         "article{border-bottom:1px solid #e0e0e0;padding:14px 0}.title{font-size:19px}.domain{color:#188038;margin:4px 0}.tag{background:#f1f3f4;color:#5f6368;border-radius:4px;padding:1px 6px;font-size:12px}"
         ".mail{display:inline-block;margin-right:14px}.snippet{color:#3c4043;margin:6px 0 0}button.link{background:none;color:#b3261e;padding:0 8px;font-size:13px}"
         "</style>")


def respond(body, script_js=""):
    nonce = hashlib.sha256(os.urandom(16)).hexdigest()[:24]
    script = f"<script nonce='{nonce}'>(function(){{{script_js}}})();</script>" if script_js else ""
    response = page(body + STYLE + script, "Поставщики в интернете")
    response.headers["Content-Security-Policy"] = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
                                                   "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
    return response


COPY_JS = ("var b=document.getElementById('copy-emails'),t=document.getElementById('emails');"
           "if(b&&t){b.addEventListener('click',function(){t.select();if(navigator.clipboard){navigator.clipboard.writeText(t.value);}else{document.execCommand('copy');}b.textContent='Скопировано';});}")


def render_page(request, job_id, message, show_all, run="", seller="all", inc=(), exc=(), view="cat"):
    if not job_id:
        return respond(render_search_page(request, run, message, seller, list(inc), list(exc), show_all), COPY_JS)
    cfg = settings()
    serpapi_key, firecrawl_key = sd.env_keys()
    title, names, runs, domains = "Поиск без заявки", [], [], {}
    marks, groups = {}, {}
    if job_id:
        title, names, quantities = order_details(job_id)
        runs, domains = aggregate(job_id, names)
        marks = load_marks(job_id, len(names))
        groups = build_groups(marks)
    records = supplier_records()
    yandex_key, yandex_folder = sd.yandex_env()
    keys_note = ("Яндекс: официальный Search API, отложенный режим (результат через 5 минут и больше)." if yandex_key and yandex_folder else
                 "Google и Яндекс: ключ SerpAPI подключён." if serpapi_key else
                 "Ключ SerpAPI не задан (SERPAPI_API_KEY): работает только Firecrawl" + (" с ключом." if firecrawl_key else " без ключа, в пределах бесплатного лимита сервиса."))
    notice = f"<div role='status' style='padding:14px;border:2px solid #1a73e8;border-radius:8px;margin:12px 0'><b>{esc(message)}</b></div>" if message else ""
    back_link = (f"<p><a href='/dashboard/automation/jobs/{int(job_id)}/review'>← К заявке</a> · "
                 "<a href='/dashboard/automation'>Все заявки</a></p>") if job_id else "<p><a href='/dashboard/automation'>← Все заявки</a></p>"
    body = (f"{back_link}<h1>Поставщики в интернете</h1>{notice}<section><p>Находит сайты профильных поставщиков по названию позиции. "
            "Цены с сайтов не читаются и в подбор или счёт не попадают. Найденный сайт — кандидат: профильность и работу с ним проверяет оператор.</p>"
            f"<p class='note'>{esc(keys_note)} Бюджет: до {cfg['max_requests']} запросов на позицию, "
            f"{cfg['pages_per_click']} страниц за одно нажатие, до {cfg['max_sites']} сайтов.</p></section>")
    if job_id:
        view = "seq" if view == "seq" else "cat"

        def position_row(number, name, classes=""):
            color, phrase = marks[number]
            warn = "<br><span class='warnmark'>⚠ галочка без цвета: позиция никуда не попадёт</span>" if phrase and not color else ""
            return (f"<tr class='pos {classes}'><td style='white-space:nowrap'><span class='mark'><input type='hidden' class='color' name='color_{number}' value='{color}'>"
                    f"<button type='button' class='dot' title='Нажимайте, чтобы сменить цвет группы' aria-label='Цвет группы позиции {number}'>{color or '·'}</button></span>"
                    f"<input type='checkbox' name='phrase_{number}' value='1' {'checked' if phrase else ''} title='Эта позиция — поисковая фраза группы' aria-label='Поисковая фраза: позиция {number}'></td>"
                    f"<td>{number}</td><td>{esc(name)}{warn}</td><td style='white-space:nowrap'>{esc(quantities[number - 1]) or '—'}</td></tr>")

        def header_row(selector, label, count, level):
            return (f"<tr class='hdr {level}'><td><span class='mark'><button type='button' class='dot block' data-sel='{selector}' "
                    f"title='Цвет сразу всему блоку' aria-label='Цвет блока {esc(label)}'>·</button></span></td>"
                    f"<td colspan='3'><b>{esc(label)}</b> <span class='note'>· позиций: {count}</span></td></tr>")

        rows = ""
        if view == "seq":
            rows = "".join(position_row(n, name) for n, name in enumerate(names, 1))
        else:
            for ri, (rubric, categories) in enumerate(position_blocks(names)):
                rows += header_row(f".r{ri}", rubric, sum(len(m) for _, m in categories), "rub")
                for ci, (category, members_of) in enumerate(categories):
                    rows += header_row(f".c{ri}_{ci}", category, len(members_of), "cat")
                    rows += "".join(position_row(n, names[n - 1], f"r{ri} c{ri}_{ci}") for n in members_of)
        view_links = (f"<a class='tab{' on' if view == 'cat' else ''}' href='/dashboard/web-suppliers?{esc(urlencode({'job_id': job_id}))}'>По рубрикам</a>"
                      f"<a class='tab{' on' if view == 'seq' else ''}' href='/dashboard/web-suppliers?{esc(urlencode({'job_id': job_id, 'view': 'seq'}))}'>По порядку номеров</a>")
        body += (f"<section><h2>{esc(title)}: позиции</h2><div class='tabs'>{view_links}</div>"
                 "<p class='note'><b>Цвет</b> (точка, нажатие меняет цвет по кругу; точка на заголовке рубрики или категории красит весь блок) — в какую группу идёт позиция. "
                 "<b>Галочка</b> — эта позиция и есть поисковая фраза своей группы. "
                 "Позиция без цвета никуда не попадает и не ищется.</p>"
                 + form_start(request, "/dashboard/web-suppliers/groups", f"<input type='hidden' name='job_id' value='{job_id}'><input type='hidden' name='view' value='{view}'>")
                 + "<div class='scroll'><table><tr><th>Группа · фраза</th><th>№</th><th>Позиция</th><th>Кол-во</th></tr>"
                 f"{rows}</table></div><p><button>Сохранить группы</button></p></form></section>")
        keywords = load_keywords(job_id)
        group_cards = ""
        for color, members in groups.items():
            key = group_run_key(job_id, color)
            row, state = load_run(key)
            saved_phrase = keywords.get(color, "")
            ticked = group_phrases(marks, members)
            status = (f"{len(state['sites'])} сайтов · запросов {state['requests']} из {cfg['max_requests']} · {esc(sd.STOP_LABELS.get(state['reason'], state['reason']))}"
                      if state else "Не искали")
            if state and state["reason"] == "waiting" and waiting_note(state):
                status += f" ({esc(waiting_note(state))})"
            errors = f"<br><span class='error'>{esc(state['errors'][-1])}</span>" if state and state["errors"] else ""
            can_continue = bool(state) and state["reason"] in ("paused", "economy", "credits", "error", "waiting")
            hidden_fields = (f"<input type='hidden' name='job_id' value='{job_id}'><input type='hidden' name='color' value='{color}'>"
                             f"<input type='hidden' name='view' value='{view}'>")
            members_html = "".join(f"<li>{n}. {esc(names[n - 1])}" + (f" — {esc(quantities[n - 1])}" if quantities[n - 1] else "")
                                   + (" <span class='tag'>фраза</span>" if n in ticked else "") + "</li>" for n in members)
            if len(ticked) == 1:
                phrase_line = f"<p>Поисковая фраза (отмечена галочкой): <b>«{esc(names[ticked[0] - 1])}»</b></p>"
                warn = False
            elif not ticked:
                phrase_line = "<p class='warnline'>⚠ Введите поисковую фразу ниже или отметьте галочкой позицию этой группы и сохраните группы.</p>"
                warn = True
            else:
                phrase_line = (f"<p class='warnline'>⚠ В группе отмечено фраз: {len(ticked)}. Оставьте одну галочку и сохраните группы "
                               "или впишите свою фразу ниже.</p>")
                warn = True
            can_search = len(ticked) == 1
            search_form = (form_start(request, "/dashboard/web-suppliers/group-search", hidden_fields)
                           + f"<label>{'Своя фраза вместо отмеченной' if can_search else 'Поисковая фраза'} "
                             f"<input name='keyword' maxlength='300' size='50' placeholder='например: хомут спринклерный Ду 25'></label>"
                           + f"<button>{'Найти сайты по новой фразе' if state else 'Найти сайты'}</button></form>")
            continue_form = (form_start(request, "/dashboard/web-suppliers/group-search", hidden_fields + f"<input type='hidden' name='keyword' value='{esc(saved_phrase)}'>")
                             + f"<button class='secondary'>{'Забрать результат' if state['reason'] == 'waiting' else 'Искать ещё по этой фразе'}</button></form>") if can_continue and saved_phrase else ""
            used = f"<p>Ищем по фразе: <b>«{esc(state['query'])}»</b></p>" if state else ""
            contacts_form = (form_start(request, "/dashboard/web-suppliers/group-contacts", hidden_fields)
                             + "<button style='background:#5f6368'>Найти e-mail у сайтов группы</button></form>") if state and state["sites"] else ""
            group_cards += (f"<div class='group{' warn' if warn else ''}' style='border-left:8px solid {GROUP_COLORS[color]}'><b>{esc(group_title(color))}</b> · позиций: {len(members)}"
                            f"<ul class='members'>{members_html}</ul>{phrase_line}{search_form}{used}<p>{status}{errors}</p>"
                            f"<div class='actions'>{continue_form}{contacts_form}</div></div>")
        body += ("<section><h2>Поиск по группам</h2>"
                 + (group_cards or "<p class='note'>Групп нет. Выберите цвет у позиций (или у целого блока) и сохраните группы.</p>") + "</section>")
        visible = [d for d in domains.values() if show_all or records.get(d["domain"], {}).get("policy") != "excluded"]
        visible.sort(key=lambda d: (records.get(d["domain"], {}).get("policy") != "trusted", -len(d["positions"]),
                                    SELLER_RANK[d["seller"]], d["domain"]))
        total = sum(len(m) for m in groups.values()) or len(names)
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
            dots = " ".join(f"<span class='dot static' style='background:{GROUP_COLORS[c]}' title='{esc(group_title(c))}'>{c or '·'}</span>" for c in sorted(d["groups"]))
            trs += (f"<tr><td><b><a href='{esc(d['url'])}' rel='noopener noreferrer' target='_blank'>{esc(d['domain'])}</a></b>"
                    f"<br>{dots}<br><span class='note'>{esc(POLICIES[policy])}</span></td>"
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
    colors_js = ("var C=" + json.dumps(GROUP_COLORS) + ";"
                 "function paint(b,n){b.style.background=C[n];b.textContent=n||'\\u00b7';}"
                 "function rows(sel){return document.querySelectorAll('tr.pos'+sel+' input.color');}"
                 "function refresh(){document.querySelectorAll('tr.pos .mark').forEach(function(m){paint(m.querySelector('.dot'),parseInt(m.querySelector('input.color').value)||0);});"
                 "document.querySelectorAll('.dot.block').forEach(function(b){var v=null,same=true;rows(b.getAttribute('data-sel')).forEach(function(i){if(v===null){v=i.value;}else if(v!==i.value){same=false;}});paint(b,same&&v!==null?(parseInt(v)||0):0);});}"
                 "document.querySelectorAll('tr.pos .mark').forEach(function(m){var i=m.querySelector('input.color');"
                 "m.querySelector('.dot').addEventListener('click',function(){i.value=((parseInt(i.value)||0)+1)%C.length;refresh();});});"
                 "document.querySelectorAll('.dot.block').forEach(function(b){b.addEventListener('click',function(){"
                 "var list=rows(b.getAttribute('data-sel')),cur=0;if(list.length){var same=true;list.forEach(function(i){if(i.value!==list[0].value){same=false;}});cur=same?(parseInt(list[0].value)||0):0;}"
                 "var n=(cur+1)%C.length;list.forEach(function(i){i.value=n;});refresh();});});refresh();")
    return respond(body, colors_js)


def search_message(state):
    message = f"Найдено сайтов: {len(state['sites'])}. {sd.STOP_LABELS.get(state['reason'], state['reason'])}."
    if state["reason"] == "waiting":
        message = ("Запрос отправлен в Яндекс. Результат обычно готов через 5 минут и больше: вернитесь на эту страницу и нажмите «Забрать результат». "
                   "Повторные нажатия новых запросов не создают." + (f" Сейчас: {waiting_note(state)}." if waiting_note(state) else ""))
    if state["errors"] and state["reason"] == "error":
        message = state["errors"][-1]
    return message


async def guarded_search(key, job_id, position, item_name, query):
    """Запускает поиск, если по этому ключу он ещё не идёт; возвращает состояние или None."""
    engines = default_engines()
    with _running_lock:
        busy = key in _running
        _running.add(key)
    if busy:
        return None
    try:
        return await run_in_threadpool(execute_search, key, job_id, position, item_name, query, engines)
    finally:
        with _running_lock:
            _running.discard(key)


CONTACTS_PER_CLICK = 12


def collect_contacts_for(todo):
    """Контакты у сайтов из списка (не больше CONTACTS_PER_CLICK за раз). Возвращает (проверено, с e-mail, осталось)."""
    from concurrent.futures import ThreadPoolExecutor
    blocked = excluded_domains()
    todo = sorted(todo, key=lambda d: (SELLER_RANK[d["seller"]], d["domain"]))
    batch = todo[:CONTACTS_PER_CLICK]

    def work(d):
        try:
            found = sd.read_domain_contacts(d["url"], blocked=blocked)
            save_contacts(d["domain"], found["contacts"], "; ".join(found["errors"]))
            return any(c["kind"] == "email" for c in found["contacts"])
        except sd.ProviderError as exc:
            save_contacts(d["domain"], [], str(exc))
            return False
    if batch:
        with ThreadPoolExecutor(max_workers=6) as pool:
            with_email = sum(1 for ok in pool.map(work, batch) if ok)
    else:
        with_email = 0
    return len(batch), with_email, len(todo) - len(batch)


def unchecked(domains, records):
    return [d for d in domains if records.get(d["domain"], {}).get("policy") != "excluded"
            and records.get(d["domain"], {}).get("contacts_json") is None]


def collect_group_contacts(job_id, color, names):
    """E-mail и контакты у сайтов группы, у которых контакты ещё не искали."""
    _, domains = aggregate(job_id, names)
    return collect_contacts_for(unchecked([d for d in domains.values() if color in d["groups"]], supplier_records()))


# ------------------------------------------------------------------ поиск без заявки (страница как в СнабПоиске)

SELLER_TABS = [("all", "Все"), ("seller", "Продавец"), ("possible", "Возможно продавец"), ("unknown", "Не подтверждено")]
STOP_WORDS = {"купить", "цена", "цены", "цену", "заказать", "каталог", "магазин", "интернет", "интернет-магазин", "стоимость", "москве",
              "доставкой", "доставка", "оптом", "рейтинг", "отзывы", "отзыв", "также", "которые", "после", "более", "всех", "этого", "наши",
              "наш", "наша", "нашем", "ваш", "ваши", "через", "можно", "только", "самые", "лучшие", "компания", "россии", "России".lower()}


def page_words(site):
    text = f"{site.get('title', '')} {site.get('description', '')}".lower()
    return {w for w in re.findall(r"[a-zа-яё][a-zа-яё\-]{3,}", text) if w not in STOP_WORDS}


def clean_words(raw):
    return [w for w in dict.fromkeys(re.findall(r"[a-zа-яё][a-zа-яё\-]*", str(raw or "").lower())) if len(w) <= 40][:20]


def email_choice():
    with database() as conn:
        rows = execute(conn, "SELECT email,selected FROM web_supplier_emails").fetchall()
    return {r["email"]: bool(r["selected"]) for r in rows}


def save_email_choice(listed, selected):
    stamp = now()
    chosen = set(selected)
    with database() as conn:
        for email in dict.fromkeys(listed):
            execute(conn, """INSERT INTO web_supplier_emails(email,selected,updated_at) VALUES(?,?,?)
                ON CONFLICT(email) DO UPDATE SET selected=excluded.selected,updated_at=excluded.updated_at""",
                    (email, 1 if email in chosen else 0, stamp))


def recent_searches(limit=12):
    with database() as conn:
        rows = execute(conn, "SELECT run_key,item_name,state_json,updated_at FROM web_supplier_runs WHERE job_id=0 "
                             "ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        try:
            count = len(json.loads(r["state_json"]).get("sites", []))
        except ValueError:
            count = 0
        out.append((r["run_key"], r["item_name"], count))
    return out


def search_items(state):
    """Сайты одного поиска: оценка, контакты и отметки (одна запись на домен)."""
    records = supplier_records()
    items = []
    for site in state.get("sites", []):
        domain = sd.site_domain(site["url"])
        status, reason = sd.merchant_assessment(site["title"], site.get("description", ""), site["url"])
        record = records.get(domain) or {}
        contacts = json.loads(record["contacts_json"]) if record.get("contacts_json") else None
        items.append({"domain": domain, "url": site["url"], "title": site["title"], "description": site.get("description", ""),
                      "seller": status, "reason": reason, "policy": record.get("policy", "neutral"),
                      "contacts": contacts, "contacts_error": record.get("contacts_error", ""), "words": page_words(site)})
    return items


def state_query(run, seller="all", inc=(), exc=(), show_all=False, **extra):
    query = {"run": run, "seller": seller, "inc": ",".join(inc), "exc": ",".join(exc), "all": 1 if show_all else 0, **extra}
    return urlencode({k: v for k, v in query.items() if v not in ("", 0, None)})


def render_search_page(request, run, message, seller, inc, exc, show_all):
    seller = seller if seller in dict(SELLER_TABS) else "all"
    searches = recent_searches()
    if not run and searches:
        run = searches[0][0]
    state = load_run(run)[1] if run else None
    notice = f"<div role='status' class='notice'><b>{esc(message)}</b></div>" if message else ""
    serpapi_key, firecrawl_key = sd.env_keys()
    yandex_key, yandex_folder = sd.yandex_env()
    engine_note = ("Яндекс: официальный Search API, отложенный режим (результат через 5 минут и больше)." if yandex_key and yandex_folder else
                   "Google и Яндекс: ключ SerpAPI подключён." if serpapi_key else
                   "Ключ SerpAPI не задан (SERPAPI_API_KEY): работает только Firecrawl" + (" с ключом." if firecrawl_key else " без ключа, в пределах бесплатного лимита сервиса."))
    current_name = state["query"] if state else ""
    head = ("<h1>Сайты и Email</h1>" + notice
            + form_start(request, "/dashboard/web-suppliers/search", "") +
            "<div class='searchbar'><label for='q'>НАИМЕНОВАНИЕ</label>"
            f"<input id='q' name='query' maxlength='300' value='{esc(current_name)}' placeholder='Например: Керамогранит 600*600 соль/перец' required>"
            "<button>Найти</button></div></form>"
            f"<p class='note'>{esc(engine_note)} Цены с сайтов не читаются.</p>")
    history = "".join(f"<a class='chip' href='/dashboard/web-suppliers?{esc(urlencode({'run': k}))}'>{esc(n[:60])} · {c}</a>" for k, n, c in searches)
    head += f"<p class='chips'>{history}</p>" if history else ""
    if not state:
        return head + ("<p class='note'>Введите наименование и нажмите «Найти»: приложение соберёт сайты поставщиков и их e-mail.</p>"
                       + recent_orders_section())
    items = search_items(state)
    records_excluded = [i for i in items if i["policy"] == "excluded"]
    pool = [i for i in items if show_all or i["policy"] != "excluded"]
    for word in inc:
        pool = [i for i in pool if word in i["words"]]
    for word in exc:
        pool = [i for i in pool if word not in i["words"]]
    counts = {key: sum(1 for i in pool if key == "all" or i["seller"] == key) for key, _ in SELLER_TABS}
    shown = [i for i in pool if seller == "all" or i["seller"] == seller]
    shown.sort(key=lambda i: (i["policy"] != "trusted", SELLER_RANK[i["seller"]], i["domain"]))
    choice = email_choice()
    emails_of = lambda i: [c["value"] for c in (i["contacts"] or []) if c["kind"] == "email"][:6]
    chosen_emails = list(dict.fromkeys(e for i in shown for e in emails_of(i) if choice.get(e, True)))
    todo = [i for i in shown if i["contacts"] is None and i["policy"] != "excluded"]
    base = dict(run=run, inc=inc, exc=exc, show_all=show_all)

    tabs = "".join(f"<a class='tab{' on' if key == seller else ''}' href='/dashboard/web-suppliers?{esc(state_query(seller=key, **{k: v for k, v in base.items()}))}'>{label} · {counts[key]}</a>"
                   for key, label in SELLER_TABS)
    summary = (f"<p class='summary'><b>Сайтов: {len(shown)} / {len(items)}</b>"
               + (f" · <a href='/dashboard/web-suppliers?{esc(state_query(seller=seller, **{**base, 'show_all': not show_all}))}'>"
                  f"{'Скрыть исключённые' if show_all else f'Исключённые · {len(records_excluded)}'}</a>" if records_excluded else "")
               + (f" · <a href='/dashboard/web-suppliers?{esc(state_query(run=run))}'>Сбросить отбор</a>" if inc or exc or seller != "all" or show_all else "")
               + f"</p><p class='note'>Запросов к поиску: {state['requests']}. {esc(sd.STOP_LABELS.get(state['reason'], state['reason']))}.</p>"
               + (f"<p class='error'>{esc(state['errors'][-1])}</p>" if state["errors"] else ""))
    can_continue = state["reason"] in ("paused", "economy", "credits", "error", "waiting")
    more_label = "Забрать результат" if state["reason"] == "waiting" else "Искать ещё"
    more_button = (form_start(request, "/dashboard/web-suppliers/search", f"<input type='hidden' name='query' value='{esc(state['query'])}'>")
                   + f"<button class='secondary'>{more_label}</button></form>") if can_continue else ""
    find_button = (form_start(request, "/dashboard/web-suppliers/find-emails", f"<input type='hidden' name='run' value='{esc(run)}'>")
                   + f"<button class='secondary'>Найти Email · {len(todo)}</button></form>") if todo else ""
    copy_block = (f"<details><summary>Копировать Email · {len(chosen_emails)}</summary>"
                  f"<textarea id='emails' readonly rows='4' style='width:100%'>{esc('; '.join(chosen_emails))}</textarea>"
                  "<p><button type='button' id='copy-emails' class='secondary'>Скопировать в буфер</button></p></details>") if chosen_emails else ""

    # боковая панель слов: «+» оставить, «−» исключить
    freq = {}
    for i in pool:
        for w in i["words"]:
            freq[w] = freq.get(w, 0) + 1
    top = [(w, n) for w, n in sorted(freq.items(), key=lambda x: (-x[1], x[0])) if n >= 2 and w not in inc and w not in exc][:25]
    active = "".join(f"<div>{'+' if w in inc else '−'} {esc(w)} <a href='/dashboard/web-suppliers?{esc(state_query(seller=seller, **{**base, 'inc': [x for x in inc if x != w], 'exc': [x for x in exc if x != w]}))}' aria-label='Убрать'>×</a></div>"
                     for w in [*inc, *exc])
    word_rows = "".join(
        f"<div class='word'><span>{esc(w)} <span class='note'>({n})</span></span>"
        f"<a href='/dashboard/web-suppliers?{esc(state_query(seller=seller, **{**base, 'inc': [*inc, w]}))}' title='Оставить'>+</a>"
        f"<a href='/dashboard/web-suppliers?{esc(state_query(seller=seller, **{**base, 'exc': [*exc, w]}))}' title='Исключить'>−</a></div>" for w, n in top)
    side = ("<aside><h3>Отбор сайтов</h3>" + (f"<div class='active'>{active}</div>" if active else "<p class='note'>+ оставить · − исключить</p>")
            + "<h3>Слова</h3>" + (word_rows or "<p class='note'>Недостаточно текста для выбора слов.</p>") + "</aside>")

    # результаты: одна форма с галочками, чтобы выбор e-mail сохранялся одним нажатием
    cards = ""
    for i in shown:
        mails = emails_of(i)
        if i["contacts"] is None:
            contacts_html = "<span class='note'>Email ещё не искали</span>"
        elif mails:
            contacts_html = " ".join(
                f"<label class='mail'><input type='hidden' name='listed' value='{esc(e)}'>"
                f"<input type='checkbox' name='selected' value='{esc(e)}' {'checked' if choice.get(e, True) else ''}> {esc(e)}</label>" for e in mails)
        else:
            contacts_html = "<span class='note'>Email не найден</span>"
        extra = ""
        phones = [c["value"] for c in (i["contacts"] or []) if c["kind"] == "phone"][:2]
        forms = [c["value"] for c in (i["contacts"] or []) if c["kind"] == "form"][:1]
        if phones:
            extra += f" · {esc(', '.join(phones))}"
        if forms:
            extra += f" · <a href='{esc(forms[0])}' rel='noopener noreferrer' target='_blank'>форма заявки</a>"
        error = f"<br><span class='error'>{esc(i['contacts_error'])}</span>" if i["contacts_error"] else ""
        cards += (f"<article><a class='title' href='{esc(i['url'])}' rel='noopener noreferrer' target='_blank'>{esc(i['title'])}</a>"
                  f"<div class='domain'>{esc(i['domain'])} <span class='tag'>{esc(SELLER_LABEL[i['seller']])}</span>"
                  + (f" <span class='tag'>{esc(POLICIES[i['policy']])}</span>" if i["policy"] != "neutral" else "")
                  + f"<button class='link' name='exclude_domain' value='{esc(i['domain'])}'>исключить</button></div>"
                  f"<div>{contacts_html}{extra}{error}</div><p class='snippet'>{esc(i['description'])}</p></article>")
    hidden = "".join(f"<input type='hidden' name='{k}' value='{esc(v)}'>" for k, v in
                     (("run", run), ("seller", seller), ("inc", ",".join(inc)), ("exc", ",".join(exc)), ("all", "1" if show_all else "0")))
    results = (form_start(request, "/dashboard/web-suppliers/emails", hidden)
               + ("<p><button>Сохранить выбор e-mail</button></p>" if shown else "")
               + (cards or "<p class='note'>Нет сайтов по выбранному отбору.</p>") + "</form>")
    main = (f"<div class='tabs'>{tabs}</div><div class='actions'>{find_button}{more_button}</div>{copy_block}{summary}{results}")
    return head + f"<div class='layout'>{side}<div>{main}</div></div>" + recent_orders_section()


def install_supplier_discovery(app):
    router = APIRouter(prefix="/dashboard/web-suppliers", dependencies=[Depends(require_session)])

    def back(job_id, message, run="", **state):
        query = {"message": message}
        if job_id:
            query["job_id"] = job_id
        elif run:
            query["run"] = run
        query.update({k: v for k, v in state.items() if v not in ("", None, "0")})
        return RedirectResponse("/dashboard/web-suppliers?" + urlencode(query), status_code=303)

    @router.get("")
    def dashboard(request: Request, job_id: int = 0, message: str = "", all: int = 0, run: str = "", seller: str = "all",
                  inc: str = "", exc: str = "", view: str = "cat"):
        return render_page(request, job_id, message[:500], bool(all), run[:80], seller, clean_words(inc), clean_words(exc), view)

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
        state = await guarded_search(key, job_id, position, item_name, query)
        if state is None:
            return back(job_id, "Поиск по этому наименованию уже выполняется. Подождите и обновите страницу.", key)
        return back(job_id, search_message(state), key)

    def return_state(form):
        seller = str(form.get("seller", "all"))
        return {"run": str(form.get("run", ""))[:80], "seller": seller if seller in dict(SELLER_TABS) else "all",
                "inc": ",".join(clean_words(form.get("inc"))), "exc": ",".join(clean_words(form.get("exc"))),
                "all": "1" if str(form.get("all")) == "1" else ""}

    @router.post("/emails")
    async def emails(request: Request):
        form = await checked_form(request)
        save_email_choice([str(v) for v in form.getlist("listed")], [str(v) for v in form.getlist("selected")])
        message = "Выбор e-mail сохранён."
        domain = str(form.get("exclude_domain", "")).strip().lower()
        if domain:
            set_policy(domain, "excluded")
            message = f"{domain}: сайт исключён из поиска и списка."
        return back(0, message, **return_state(form))

    @router.post("/find-emails")
    async def find_emails(request: Request):
        form = await checked_form(request)
        run = str(form.get("run", ""))[:80]
        _, state = load_run(run)
        if not state:
            raise HTTPException(404, "Поиск не найден.")
        records = supplier_records()
        sites = [{"domain": sd.site_domain(x["url"]), "url": x["url"],
                  "seller": sd.merchant_assessment(x["title"], x.get("description", ""), x["url"])[0]} for x in state["sites"]]
        checked, with_email, left = await run_in_threadpool(collect_contacts_for, unchecked(sites, records))
        if not checked:
            return back(0, "У всех найденных сайтов контакты уже проверены.", run=run)
        return back(0, f"Проверено сайтов {checked}, с e-mail {with_email}." + (f" Осталось непроверенных: {left}, нажмите ещё раз." if left else ""), run=run)


    @router.post("/groups")
    async def groups(request: Request):
        form = await checked_form(request)
        job_id = int(form.get("job_id") or 0)
        view = "seq" if form.get("view") == "seq" else ""
        _, names = order_positions(job_id)
        marks = {}
        for number in range(1, len(names) + 1):
            try:
                color = int(form.get(f"color_{number}") or 0)
            except ValueError:
                color = 0
            marks[number] = (min(max(color, 0), len(GROUP_COLORS) - 1), bool(form.get(f"phrase_{number}")))
        save_marks(job_id, marks)
        built = build_groups(marks)
        lost = [n for n, (color, phrase) in marks.items() if phrase and not color]
        message = f"Группы сохранены. Групп: {len(built)}."
        if lost:
            message += f" Галочка без цвета у позиций {', '.join(map(str, lost))}: они никуда не попадут."
        no_phrase = [c for c, members in built.items() if not group_phrases(marks, members)]
        if no_phrase:
            message += f" В группах {', '.join(map(str, no_phrase))} нет галочки: впишите поисковую фразу."
        return back(job_id, message, view=view)

    @router.post("/group-search")
    async def group_search(request: Request):
        form = await checked_form(request)
        job_id, color = int(form.get("job_id") or 0), int(form.get("color") or 0)
        view = "seq" if form.get("view") == "seq" else ""
        _, names = order_positions(job_id)
        marks = load_marks(job_id, len(names))
        members = build_groups(marks).get(color)
        if not members:
            raise HTTPException(400, "В этой группе нет позиций. Выберите цвет у позиций и сохраните группы.")
        keyword = " ".join(str(form.get("keyword", "")).split())
        if not keyword:
            ticked = group_phrases(marks, members)
            if len(ticked) == 1:
                keyword = " ".join(names[ticked[0] - 1].split())
            elif not ticked:
                return back(job_id, f"{group_title(color)}: нет поисковой фразы. Впишите её или отметьте галочкой позицию группы и сохраните группы.", view=view)
            else:
                return back(job_id, f"{group_title(color)}: отмечено фраз {len(ticked)}. Оставьте одну галочку или впишите свою фразу.", view=view)
        try:
            query = sd.build_query(keyword)
        except ValueError as exc:
            return back(job_id, str(exc), view=view)
        save_keyword(job_id, color, keyword)
        state = await guarded_search(group_run_key(job_id, color), job_id, -(color + 1), keyword, query)
        if state is None:
            return back(job_id, "Поиск по этой группе уже выполняется. Подождите и обновите страницу.", view=view)
        return back(job_id, f"{group_title(color)}: {search_message(state)}", view=view)

    @router.post("/group-contacts")
    async def group_contacts(request: Request):
        form = await checked_form(request)
        job_id, color = int(form.get("job_id") or 0), int(form.get("color") or 0)
        _, names = order_positions(job_id)
        checked, with_email, left = await run_in_threadpool(collect_group_contacts, job_id, color, names)
        if not checked:
            return back(job_id, f"{group_title(color)}: у всех найденных сайтов контакты уже проверены.")
        return back(job_id, f"{group_title(color)}: проверено сайтов {checked}, с e-mail {with_email}."
                            + (f" Осталось непроверенных: {left}, нажмите ещё раз." if left else ""))

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
