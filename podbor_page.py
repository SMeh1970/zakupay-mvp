"""Подбор товаров (стенд В2) inside the app.

The page is the fixed stand ``comparison_assets/podbor.html`` (logic and design of version В2,
see stand/SPEC-v2.md on the ``stand`` branch). The app only supplies the data:
positions of the request with the found offers, the saved decisions of the page and
what is currently in the invoice. «Выбрать» goes through the existing comparison action
(``value_accept_select`` / ``value_unselect``), so the invoice path is unchanged.
"""
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

import requests
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

ASSET = Path(__file__).with_name("comparison_assets") / "podbor.html"
SCHEMA = "CREATE TABLE IF NOT EXISTS podbor_state (job_id INTEGER PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
STATE_LIMIT = 1_000_000
AI_URL = "https://api.anthropic.com/v1/messages"


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def positions(result, heading):
    """Request positions in the stand's format: [name, price, unit, {key, supplier, url}] per product."""
    from supplier_comparison import candidates, quote_key
    out, chosen = [], {}
    for item in result.get("items") or []:
        source = item.get("comparison_source") or item
        position = item.get("position")
        pid = f"p{position}"
        products = []
        for candidate in candidates(item):
            price = _number(candidate.get("price"))
            products.append([
                str(candidate.get("name") or ""),
                price if price is not None else 0,
                str(candidate.get("unit") or "шт"),
                {"key": quote_key(candidate), "supplier": str(candidate.get("supplier") or "ВсеИнструменты.ру"),
                 "url": str(candidate.get("url") or "")},
            ])
        out.append({
            "id": pid, "app": True, "position": position,
            "name": str(source.get("requested_name") or item.get("requested_name") or ""),
            "qty": _number(source.get("quantity")) or 0,
            "unit": str(source.get("unit") or item.get("unit") or "шт"),
            "example": f"{heading} · позиция {position}",
            "products": products,
        })
        selected = item.get("selected")
        if selected and selected.get("name") and item.get("decision") == "approved":
            chosen[pid] = str(selected["name"])
    return out, chosen


def page_html(data, nonce):
    text = ASSET.read_text(encoding="utf-8")
    serialized = json.dumps(data, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c").replace("&", "\\u0026")
    for tag in ("<script>",):
        if text.count(tag) != 1:
            raise RuntimeError(f"podbor.html: ожидался один {tag}")
    text = text.replace("<script>", (
        f'<script nonce="{nonce}" type="application/json" id="podbor-data">{serialized}</script>'
        f'<script nonce="{nonce}">{AI_SHIM}</script><script nonce="{nonce}">'))
    if text.count('<div class="wrap">') != 1:
        raise RuntimeError('podbor.html: ожидался один <div class="wrap">')
    return ('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            + text.replace('<div class="wrap">', '</head><body><div class="wrap">') + "</body></html>")


# The stand asks Claude through window.claude.use('sample'); in the app the same calls go to the server.
AI_SHIM = r"""(()=>{const data=JSON.parse(document.getElementById('podbor-data').textContent);
if(!data.ai)return;
const call=async(body,signal)=>{let r;try{r=await fetch(data.ai_url,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Requested-With':'podbor'},body:JSON.stringify(body),signal})}catch(e){const x=new Error('cancelled');x.code=e?.name==='AbortError'?'cancelled':'error';throw x}
const d=await r.json().catch(()=>({}));if(!r.ok){const x=new Error(d.detail||'Ошибка ИИ');x.code=r.status===429?'rate_limited':'error';x.text=d.detail;throw x}return d};
const fn=async(turns,o={})=>{const d=await call({mode:'text',turns},o.signal);o.onText?.({text:d.text});return {text:d.text}};
fn.json=async(prompt,o={})=>(await call({mode:'json',prompt},o.signal)).json;
window.claude={use:async name=>name==='sample'?fn:null};})();"""


def _ai_enabled():
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def ask_ai(payload):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise HTTPException(503, "ИИ не подключён: на сервере не задан ключ ANTHROPIC_API_KEY.")
    mode = payload.get("mode")
    if mode == "json":
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise HTTPException(400, "Пустой запрос")
        messages = [{"role": "user", "content": prompt}]
        system = "Ответь только одним JSON-объектом, без пояснений и без разметки."
    elif mode == "text":
        turns = payload.get("turns")
        if not isinstance(turns, list) or not turns or len(turns) > 20:
            raise HTTPException(400, "Неверный диалог")
        messages = []
        for turn in turns:
            if not isinstance(turn, dict) or turn.get("role") not in {"user", "assistant"} or not isinstance(turn.get("content"), str):
                raise HTTPException(400, "Неверный диалог")
            if messages and messages[-1]["role"] == turn["role"]:
                messages[-1]["content"] += "\n\n" + turn["content"]
            else:
                messages.append({"role": turn["role"], "content": turn["content"]})
        system = None
    else:
        raise HTTPException(400, "Неизвестный режим")
    body = {"model": os.getenv("PODBOR_AI_MODEL", "claude-sonnet-5-5"), "max_tokens": 8000, "messages": messages}
    if system:
        body["system"] = system
    try:
        response = requests.post(AI_URL, json=body, timeout=110, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    except requests.RequestException as exc:
        raise HTTPException(502, f"ИИ недоступен: {type(exc).__name__}")
    if response.status_code == 429:
        raise HTTPException(429, "Слишком много запросов к ИИ, повторите позже.")
    if not response.ok:
        raise HTTPException(502, f"ИИ ответил ошибкой {response.status_code}")
    text = "".join(part.get("text", "") for part in (response.json().get("content") or []) if part.get("type") == "text")
    if mode == "text":
        return {"text": text}
    match = re.search(r"\{.*\}", text, re.S)
    try:
        return {"json": json.loads(match[0] if match else text)}
    except ValueError:
        raise HTTPException(502, "ИИ вернул ответ не в формате JSON")


def install(app):
    from automation_pipeline import _connect, _execute, _lock
    from supplier_comparison import _csrf, _session, revision

    def ensure(conn):
        _execute(conn, SCHEMA)

    def load(job_id, conn):
        row = _execute(conn, "SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
        if not row or not row["result_json"]:
            raise HTTPException(404, "Сохранённый результат не найден. Сначала выполните поиск по заявке.")
        result = json.loads(row["result_json"])
        from catalog_comparison import hydrate_request
        hydrate_request(result, row["order_json"])
        return row, result

    def ajax(request):
        _session(request)
        if request.headers.get("x-requested-with") != "podbor":
            raise HTTPException(403, "Обновите страницу и повторите действие")

    async def body(request, limit):
        raw = await request.body()
        if len(raw) > limit:
            raise HTTPException(413, "Слишком большой запрос")
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            raise HTTPException(400, "Неверный формат запроса")
        if not isinstance(payload, dict):
            raise HTTPException(400, "Неверный формат запроса")
        return payload

    @app.get("/dashboard/automation/jobs/{job_id}/podbor", response_class=HTMLResponse)
    def podbor(job_id: int, request: Request):
        _session(request)
        with _connect() as conn:
            row, result = load(job_id, conn)
            ensure(conn)
            saved = _execute(conn, "SELECT state_json FROM podbor_state WHERE job_id=?", (job_id,)).fetchone()
        is_manual = row["event_type"] == "manual_order"
        heading = (f"Ручная заявка {result.get('manual_reference') or 'без номера'}" if is_manual
                   else f"Заявка Закупай № {row['order_id']}")
        items, chosen = positions(result, heading)
        base = f"/dashboard/automation/jobs/{job_id}"
        data = {
            "title": "Подбор товаров · " + heading, "positions": items, "chosen": chosen,
            "state": json.loads(saved["state_json"]) if saved else None,
            "revision": revision(row["result_json"]), "csrf": _csrf(request),
            "action_url": base + "/comparison/action", "state_url": base + "/podbor/state", "ai_url": base + "/podbor/ai",
            "ai": _ai_enabled(),
            "links": [["/dashboard/automation", "← Все заявки"], [base + "/review", "Проверка заявки и счёт"]],
        }
        nonce = secrets.token_urlsafe(18)
        return HTMLResponse(page_html(data, nonce), headers={"Cache-Control": "no-store", "Content-Security-Policy": (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' https:; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'")})

    @app.post("/dashboard/automation/jobs/{job_id}/podbor/state")
    async def podbor_state(job_id: int, request: Request):
        ajax(request)
        state = (await body(request, STATE_LIMIT)).get("state")
        if not isinstance(state, dict):
            raise HTTPException(400, "Неверное состояние")
        raw = json.dumps(state, ensure_ascii=False, allow_nan=False)
        with _lock, _connect() as conn:
            if not _execute(conn, "SELECT id FROM automation_jobs WHERE id=?", (job_id,)).fetchone():
                raise HTTPException(404, "Заявка не найдена")
            ensure(conn)
            _execute(conn, "INSERT INTO podbor_state (job_id, state_json, updated_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(job_id) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at",
                     (job_id, raw, datetime.now(timezone.utc).isoformat()))
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    @app.post("/dashboard/automation/jobs/{job_id}/podbor/ai")
    async def podbor_ai(job_id: int, request: Request):
        ajax(request)
        payload = await body(request, 400_000)
        from starlette.concurrency import run_in_threadpool
        return JSONResponse(await run_in_threadpool(ask_ai, payload), headers={"Cache-Control": "no-store"})
