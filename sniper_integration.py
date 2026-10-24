"""Manual Sniper Search integration. Never send mail from a read or retry.

Contract: https://sniper-search.ru/api-docs/openapi.json (2026-10-14).
Credentials stay encrypted on the server; login passwords are never retained.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from price_lists import database, execute, page, safe_url
from security import SESSION_COOKIE, _decode_payload, _sign_payload, _valid_session

API_BASE = "https://sniper-search.ru"
# /user/login is relative to the website's BFF base, not its public API base.
# Verified in Sniper's /app/assets/index-BufPZ8VP.js on 2026-10-14.
LOGIN_URL = API_BASE + "/api/bff/v1/user/login"
MAX_RESPONSE = 4 * 1024 * 1024
logger = logging.getLogger("zakupay.sniper")


def initialize_schema(conn, postgres):
    conn.execute("""CREATE TABLE IF NOT EXISTS sniper_settings (
        id INTEGER PRIMARY KEY, encrypted_token TEXT NOT NULL,
        expires_at TEXT, verified_at TEXT, languages_json TEXT,
        email_blacklist TEXT NOT NULL DEFAULT '[]', site_blacklist TEXT NOT NULL DEFAULT '[]'
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sniper_requests (
        id TEXT PRIMARY KEY, job_id BIGINT, kind TEXT NOT NULL, name TEXT NOT NULL,
        payload_json TEXT NOT NULL, state TEXT NOT NULL, task_id TEXT,
        remote_status TEXT, result_json TEXT, replies_json TEXT, error TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sniper_requests_job ON sniper_requests(job_id,created_at)")


def now():
    return datetime.now(timezone.utc).isoformat()


def cipher():
    source = os.getenv("APP_SIGNING_SECRET") or os.getenv("ZAKUPAY_API_KEY")
    if not source:
        raise ValueError("На сервере не настроен постоянный ключ для сохранения доступа.")
    key = hashlib.sha256(("sniper-credentials-v1:" + source).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def settings():
    with database() as conn:
        row = execute(conn, "SELECT * FROM sniper_settings WHERE id=1").fetchone()
    return dict(row) if row else {}


def token():
    saved = settings()
    if saved.get("encrypted_token"):
        try:
            return cipher().decrypt(saved["encrypted_token"].encode()).decode()
        except InvalidToken:
            raise ValueError("Ключ сервера изменился. Подключите Sniper повторно.") from None
    supplied = os.getenv("SNIPER_API_TOKEN", "").strip()
    if not supplied:
        raise ValueError("Сначала подключите аккаунт Sniper.")
    return supplied


def save_token(value, expires_at="", languages=None):
    if not value or len(value) > 8192 or any(c.isspace() for c in value):
        raise ValueError("Некорректный API-токен.")
    encrypted = cipher().encrypt(value.encode()).decode()
    with database() as conn:
        execute(conn, """INSERT INTO sniper_settings(id,encrypted_token,expires_at,verified_at,languages_json)
            VALUES (1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET encrypted_token=excluded.encrypted_token,
            expires_at=excluded.expires_at,verified_at=excluded.verified_at,languages_json=excluded.languages_json""",
            (encrypted, expires_at, now() if languages is not None else None,
             json.dumps(languages, ensure_ascii=False) if languages is not None else None))


class SniperError(ValueError):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class SniperClient:
    def __init__(self, pat=None):
        self.pat = pat

    def call(self, method, path, payload=None, params=None, bearer=None):
        # Paths are generated exclusively by the adapter, never supplied URLs.
        headers = {"Authorization": "Bearer " + (bearer or self.pat or token()), "Accept": "application/json"}
        creating = method == "POST"
        try:
            with requests.request(method, API_BASE + path, json=payload, params=params,
                                  headers=headers, timeout=(8, 30), allow_redirects=False, stream=True) as response:
                if response.status_code not in {200, 201, 202}:
                    messages = {400: "Sniper отклонил запрос. При подключении: возможно, уже существует активный PAT — введите его в поле токена.",
                                401: "Sniper отклонил авторизацию. Проверьте токен или логин и пароль.",
                                403: "Sniper не разрешил доступ к API для этого аккаунта.",
                                402: "Sniper требует пополнить баланс.",
                                422: "Sniper отклонил параметры запроса.",
                                429: "Достигнуто ограничение Sniper. Повторите позже."}
                    raise SniperError(messages.get(response.status_code, f"Sniper вернул HTTP {response.status_code}."),
                                      uncertain=creating and response.status_code >= 500)
                body = bytearray()
                for part in response.iter_content(65536):
                    body.extend(part)
                    if len(body) > MAX_RESPONSE:
                        raise SniperError("Ответ Sniper слишком большой.", uncertain=creating)
                try:
                    result = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    raise SniperError("Sniper вернул ответ в неизвестном формате.", uncertain=creating) from None
                if not isinstance(result, dict):
                    raise SniperError("Sniper вернул ответ в неизвестном формате.", uncertain=creating)
                return result
        except requests.RequestException:
            raise SniperError("Связь со Sniper прервана. Для созданного запроса проверьте личный кабинет перед повтором.", uncertain=creating) from None

    def login(self, login, password):
        # The public login method has no PAT authentication.
        # Match Sniper's Login-Bq9a_33M.js: both input values are trimmed.
        login, password = login.strip(), password.strip()
        try:
            with requests.post(LOGIN_URL, json={"login": login, "password": password, "fingerprint": ""},
                               timeout=(8, 30), allow_redirects=False) as response:
                if response.status_code != 200:
                    # Only recognized non-secret codes may leave this method.
                    # Arbitrary upstream detail can echo credentials/input.
                    reasons = {
                        "invalid credentials": ("invalid_credentials", "Sniper отклонил переданные данные входа. Если с ними работает вход на сайте, требуется проверка серверного входа со стороны Sniper."),
                        "different fingerprints": ("different_fingerprints", "Sniper заблокировал вход с другого устройства. Требуется разрешение серверного доступа со стороны Sniper."),
                        "multiple login": ("multiple_login", "Sniper ограничил одновременные входы в аккаунт."),
                        "user banned": ("user_banned", "Sniper заблокировал аккаунт. Обратитесь в поддержку Sniper."),
                        "password login not available": ("password_login_unavailable", "Для этого аккаунта Sniper недоступен вход по паролю. Используйте существующий API-токен."),
                        "api access is restricted": ("api_access_restricted", "Sniper не разрешил доступ к API для этого аккаунта."),
                    }
                    reason = None
                    try:
                        rejected = response.json()
                        detail = rejected.get("detail") if isinstance(rejected, dict) else None
                        if isinstance(detail, str):
                            reason = reasons.get(detail.strip().lower())
                    except ValueError:
                        pass
                    logger.warning("Sniper login rejected: HTTP %s reason=%s", response.status_code, reason[0] if reason else "unknown")
                    if reason:
                        raise SniperError(f"{reason[1]} (HTTP {response.status_code}; {reason[0]})")
                    messages = {
                        400: "Sniper не принял данные входа (HTTP 400).",
                        401: "Sniper отклонил авторизацию (HTTP 401), не сообщив распознаваемую причину.",
                        403: "Sniper запретил вход или доступ с сервера (HTTP 403).",
                        404: "Адрес авторизации Sniper не найден (HTTP 404). Это ошибка подключения, а не проверка пароля.",
                        422: "Sniper отклонил формат данных авторизации (HTTP 422).",
                        429: "Sniper временно ограничил попытки входа (HTTP 429).",
                    }
                    raise SniperError(messages.get(response.status_code, f"Вход в Sniper не выполнен: HTTP {response.status_code}."))
                data = response.json()
                jwt = (data.get("access_token") or data.get("accessToken") or data.get("token")) if isinstance(data, dict) else None
                if not isinstance(jwt, str) or not jwt:
                    raise SniperError("Sniper не вернул токен авторизации.")
        except (requests.RequestException, json.JSONDecodeError):
            raise SniperError("Не удалось выполнить вход в Sniper.") from None
        return self.call("POST", "/api/v0/token/issue", bearer=jwt)

    def languages(self):
        data = self.call("GET", "/api/v0/requests/languages")
        if not isinstance(data.get("languages"), list):
            raise SniperError("Не удалось проверить доступ: неверный ответ справочника языков.")
        return data


def require_session(request: Request):
    if not _valid_session(request):
        raise HTTPException(401, "Войдите в закрытую панель.")


def csrf(request):
    return _sign_payload({"exp": int(time.time()) + 3600,
                          "session": hashlib.sha256(request.cookies.get(SESSION_COOKIE, "").encode()).hexdigest()},
                         "sniper-form", "sn_form")


async def checked_form(request):
    if int(request.headers.get("content-length", "0") or 0) > 65536:
        raise HTTPException(413, "Слишком большой запрос.")
    body = await request.body()
    if len(body) > 65536:
        raise HTTPException(413, "Слишком большой запрос.")
    form = await request.form()
    supplied = _decode_payload(str(form.get("csrf", "")), "sniper-form", "sn_form")
    expected = hashlib.sha256(request.cookies.get(SESSION_COOKIE, "").encode()).hexdigest()
    if not supplied or not hmac.compare_digest(str(supplied.get("session", "")), expected):
        raise HTTPException(403, "Обновите страницу и повторите действие.")
    return form


def esc(value):
    return html.escape(str(value or ""), quote=True)


def start_form(request, action):
    return f"<form method='post' action='{action}'><input type='hidden' name='csrf' value='{esc(csrf(request))}'>"


def redirect(message="", target="/dashboard/sniper"):
    return RedirectResponse(target + ("?" + urlencode({"message": message}) if message else ""), status_code=303)


def lines(text, limit=100):
    result = list(dict.fromkeys(line.strip() for line in str(text or "").splitlines() if line.strip()))
    if len(result) > limit or any(len(line) > 255 for line in result):
        raise ValueError(f"Список должен содержать не более {limit} строк, каждая до 255 символов.")
    return result


def parse_goods(text):
    result = []
    for line in str(text or "").splitlines():
        if not line.strip():
            continue
        columns = line.strip().rsplit(";", 2)
        item = {"name": columns[0].strip()}
        if not 1 <= len(item["name"]) <= 255:
            raise ValueError("Наименование должно содержать от 1 до 255 символов.")
        if len(columns) >= 2 and columns[1].strip():
            try:
                quantity = Decimal(columns[1].strip().replace(",", "."))
                if not quantity.is_finite() or quantity <= 0 or quantity > Decimal('1e17'):
                    raise InvalidOperation
            except InvalidOperation:
                raise ValueError("Укажите положительное количество после точки с запятой.") from None
            # Sniper accepts integer quantities only. Fractional quantities remain
            # verbatim in the reviewed email; never round or invent a quantity.
            if quantity == quantity.to_integral_value():
                item["quantity"] = int(quantity)
        if len(columns) == 3 and columns[2].strip():
            if len(columns[2].strip()) > 50:
                raise ValueError("Единица измерения длиннее 50 символов.")
            item["uom"] = columns[2].strip()
        result.append(item)
    if not 1 <= len(result) <= 20:
        raise ValueError("В одном запросе Sniper допускает от 1 до 20 позиций. Разделите большую заявку на партии.")
    return result


def prepare(form):
    kind = str(form.get("kind", "scan"))
    if kind not in {"scan", "wizard"}:
        raise ValueError("Неизвестный режим Sniper.")
    name = str(form.get("name", "")).strip()
    if not 1 <= len(name) <= 100:
        raise ValueError("Укажите название запроса, до 100 символов.")
    payload = {"search_goods": parse_goods(form.get("goods")), "geo_targets": [1], "languages": [1], "dry_run": False}
    saved = settings()
    sites = json.loads(saved.get("site_blacklist") or "[]")
    emails = json.loads(saved.get("email_blacklist") or "[]")
    if sites:
        payload["site_blacklist"] = sites
    if kind == "wizard":
        subject = str(form.get("email_subject", "")).strip()
        body = str(form.get("email_body", "")).strip()
        if not 1 <= len(subject) <= 100 or not 1 <= len(body) <= 10000:
            raise ValueError("Для рассылки заполните тему (до 100 символов) и текст письма (до 10000 символов).")
        payload.update(name=name, email_subject=subject, email_body=body, generate_email={"generate": False})
        if emails:
            payload["email_blacklist"] = emails
    try:
        job_id = int(form.get("job_id") or 0) or None
    except ValueError:
        raise ValueError("Некорректный номер исходной заявки.") from None
    identifier = str(uuid.uuid4())
    with database() as conn:
        if job_id and not execute(conn, "SELECT id FROM automation_jobs WHERE id=?", (job_id,)).fetchone():
            raise ValueError("Исходная заявка не найдена.")
        execute(conn, """INSERT INTO sniper_requests(id,job_id,kind,name,payload_json,state,created_at,updated_at)
            VALUES (?,?,?,?,?,'draft',?,?)""", (identifier, job_id, kind, name, json.dumps(payload, ensure_ascii=False), now(), now()))
    return identifier


def get_request(identifier):
    with database() as conn:
        row = execute(conn, "SELECT * FROM sniper_requests WHERE id=?", (identifier,)).fetchone()
    if not row:
        raise HTTPException(404, "Запрос не найден.")
    return dict(row)


def submit(identifier, client=None):
    record = get_request(identifier)
    client = client or SniperClient()
    # Validate configuration before claiming the one-shot submission.
    if not client.pat:
        token()
    with database() as conn:
        changed = execute(conn, "UPDATE sniper_requests SET state='submitting',updated_at=? WHERE id=? AND state='draft'", (now(), identifier))
        if changed.rowcount != 1:
            raise ValueError("Этот запрос уже запускался. Проверьте его состояние; повторная отправка заблокирована.")
    try:
        data = client.call("POST", "/api/v0/requests/" + record["kind"], json.loads(record["payload_json"]))
        task_id = str(uuid.UUID(str(data.get("task_id"))))
    except (SniperError, ValueError) as error:
        uncertain = not isinstance(error, SniperError) or error.uncertain
        message = str(error) if isinstance(error, SniperError) else "Sniper не вернул идентификатор. Проверьте личный кабинет перед новым запуском."
        with database() as conn:
            execute(conn, "UPDATE sniper_requests SET state=?,error=?,updated_at=? WHERE id=?",
                    ("uncertain" if uncertain else "rejected", message, now(), identifier))
        raise ValueError(message) from None
    with database() as conn:
        execute(conn, "UPDATE sniper_requests SET state='submitted',task_id=?,remote_status=?,error=NULL,updated_at=? WHERE id=?",
                (task_id, str(data.get("status") or "created"), now(), identifier))


def refresh(identifier, client=None):
    record = get_request(identifier)
    if not record.get("task_id"):
        raise ValueError("У этого запроса ещё нет идентификатора Sniper.")
    client = client or SniperClient()
    data = client.call("GET", "/api/v0/requests/" + str(uuid.UUID(record["task_id"])))
    with database() as conn:
        execute(conn, "UPDATE sniper_requests SET remote_status=?,result_json=?,updated_at=? WHERE id=?",
                (str(data.get("status") or "unknown"), json.dumps(data, ensure_ascii=False), now(), identifier))


def install_sniper(app):
    router = APIRouter(prefix="/dashboard/sniper", dependencies=[Depends(require_session)])

    @router.get("")
    def dashboard(request: Request, message: str = "", job_id: int = 0, login: str = "", connection_error: bool = False, positions: str = "", batch: int = 1):
        saved = settings()
        configured = bool(saved.get("encrypted_token") or os.getenv("SNIPER_API_TOKEN"))
        state = "Доступ проверен " + esc(saved["verified_at"]) if saved.get("verified_at") else "Ключ настроен, доступ ещё не проверен" if configured else "Аккаунт ещё не подключён"
        goods, name = "", "Запрос поставщикам"
        batches = ""
        if job_id:
            from automation_pipeline import _saved_order_snapshot
            with database() as conn:
                row = execute(conn, "SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Заявка не найдена.")
            order = _saved_order_snapshot(row, json.loads(row["result_json"] or "{}")) or {}
            name = str(order.get("name") or row["subject"] or name)[:100]
            entries = []
            selected = {int(v) for v in positions.split(",") if v.isdigit() and int(v)>0}
            if positions and not selected:
                raise HTTPException(400, "Не выбраны позиции")
            source_items = order.get("orderItems") or []
            if selected and not selected.issubset(set(range(1,len(source_items)+1))):
                raise HTTPException(400, "Позиция отсутствует в заявке")
            chosen = sorted(selected or set(range(1, len(source_items)+1)))
            count = (len(chosen)+19)//20
            if batch < 1 or batch > max(1,count):
                raise HTTPException(400, "Партия отсутствует")
            if count > 1:
                params = {"job_id": job_id, "positions": ",".join(map(str, chosen))}
                batches = (f"<p>Выбрано {len(chosen)} позиций. Sniper принимает до 20 за запрос. "
                    f"Сейчас партия {batch} из {count}; каждую партию запускайте отдельно.</p>"
                    + " ".join(f"<a href='/dashboard/sniper?{esc(urlencode({**params, 'batch':n}))}'>Партия {n}</a>" for n in range(1,count+1)))
            selected = set(chosen[(batch-1)*20:batch*20])
            for position, item in enumerate(source_items, 1):
                if selected and position not in selected:
                    continue
                unit = item.get("unit") or {}
                entries.append(f"{item.get('goodName') or ''}; {item.get('count') or ''}; {unit.get('name', '') if isinstance(unit, dict) else unit}")
            goods = "\n".join(entries)
        with database() as conn:
            rows = execute(conn, "SELECT id,name,kind,state,remote_status,created_at FROM sniper_requests ORDER BY created_at DESC LIMIT 100").fetchall()
        email_text = "Добрый день.\nПросим сообщить цены, наличие и сроки поставки:\n" + goods if goods else ""
        history = "".join(f"<tr><td><a href='/dashboard/sniper/requests/{esc(row['id'])}'>{esc(row['name'])}</a></td><td>{'Поиск' if row['kind']=='scan' else 'Рассылка'}</td><td>{esc(row['remote_status'] or row['state'])}</td><td>{esc(row['created_at'])}</td></tr>" for row in rows)
        notice = f"<div role='{'alert' if connection_error else 'status'}' style='padding:16px;border:2px solid {'#b3261e' if connection_error else '#1a73e8'};border-radius:8px;margin:12px 0'><b>{esc(message)}</b></div>" if message else ""
        body = f"<h1>Sniper Search</h1><section><h2>Подключение</h2>{notice}<p>{state}</p>"
        if configured and not connection_error:
            body += "<details><summary>Изменить подключение Sniper</summary>"
        body += start_form(request, "/dashboard/sniper/connect").replace("<form ", "<form autocomplete='off' ", 1) + f"<label>Логин Sniper <input name='sniper_account_login' value='{esc(login[:255])}' autocomplete='off' maxlength='255'></label><label>Пароль Sniper <input type='password' name='sniper_account_password' autocomplete='new-password' maxlength='1000'></label><p>Или укажите уже выпущенный API-токен:</p><label>API-токен <input type='password' name='sniper_pat' autocomplete='new-password' maxlength='8192'></label><p class='note'>Используйте пароль аккаунта Sniper, а не пароль почтового ящика. Пароль не сохраняется. Действующий токен автоматически не перевыпускается.</p><button>Подключить и проверить доступ</button></form>"
        if configured and not connection_error:
            body += "</details>"
        if configured:
            body += start_form(request, "/dashboard/sniper/check") + "<button>Проверить доступ без поиска и рассылки</button></form>"
        body += "</section><section><h2>Новый запрос</h2>" + batches + "<p>Поиск собирает контакты поставщиков. Рассылка дополнительно ищет поставщиков и отправляет им письмо от вашего аккаунта Sniper.</p>"
        body += start_form(request, "/dashboard/sniper/prepare") + f"<input type='hidden' name='job_id' value='{job_id or ''}'><label>Название <input name='name' value='{esc(name)}' maxlength='100' required></label><label>Действие <select name='kind'><option value='scan'>Только поиск поставщиков</option><option value='wizard'>Поиск и рассылка</option></select></label><label>Позиции: наименование; количество; единица<br><textarea name='goods' rows='12' cols='90' required>{esc(goods)}</textarea></label><p class='note'>До 20 позиций за один запуск. Для заявки большего размера оставьте нужную партию. Дробные количества укажите также в тексте письма. Поиск по умолчанию: русский язык, весь мир — согласно настройкам Sniper.</p><label>Тема письма (для рассылки) <input name='email_subject' maxlength='100' value='Запрос цен и наличия'></label><label>Текст письма (для рассылки)<br><textarea name='email_body' rows='8' cols='90' maxlength='10000'>{esc(email_text)}</textarea></label><button>Проверить запрос перед запуском</button></form></section>"
        emails = "\n".join(json.loads(saved.get("email_blacklist") or "[]"))
        sites = "\n".join(json.loads(saved.get("site_blacklist") or "[]"))
        body += "<section><h2>Исключить из новых запросов</h2>" + start_form(request, "/dashboard/sniper/exclusions") + f"<label>Email или домены почты, по одному на строку<br><textarea name='emails' rows='4' cols='70'>{esc(emails)}</textarea></label><label>Сайты поставщиков, по одному на строку<br><textarea name='sites' rows='4' cols='70'>{esc(sites)}</textarea></label><p class='note'>Исключения применяются к новым запросам. Sniper фильтрует email только при рассылке.</p><button>Сохранить исключения</button></form></section>"
        body += f"<section><h2>История</h2><table><tr><th>Запрос</th><th>Действие</th><th>Состояние</th><th>Дата</th></tr>{history}</table></section>"
        return page(body, "Sniper Search")

    @router.post("/connect")
    async def connect(request: Request):
        form = await checked_form(request)
        entered_login = str(form.get("sniper_account_login", form.get("login", ""))).strip()
        def work():
            supplied_pat = str(form.get("sniper_pat", form.get("pat", ""))).strip()
            if supplied_pat:
                pat = supplied_pat
                data = SniperClient(pat).languages()
                save_token(pat, languages=data)
            else:
                login, password = entered_login, str(form.get("sniper_account_password", form.get("password", "")))
                if not login or not password or len(login) > 255 or len(password) > 1000:
                    raise ValueError("Введите логин и пароль Sniper или API-токен.")
                cipher()  # Check durable encryption before issuing a one-time token.
                issued = SniperClient().login(login, password)
                save_token(str(issued.get("token") or ""), str(issued.get("expires_at") or ""))
                data = SniperClient().languages()
                with database() as conn:
                    execute(conn, "UPDATE sniper_settings SET verified_at=?,languages_json=? WHERE id=1", (now(), json.dumps(data)))
        try:
            await run_in_threadpool(work)
            return redirect("Sniper подключён. Доступ к API проверен. Поиск и рассылка не запускались.")
        except ValueError as error:
            return await run_in_threadpool(dashboard, request, message=str(error), login=entered_login, connection_error=True)

    @router.post("/check")
    async def check(request: Request):
        await checked_form(request)
        try:
            data = await run_in_threadpool(SniperClient().languages)
            with database() as conn:
                execute(conn, "UPDATE sniper_settings SET verified_at=?,languages_json=? WHERE id=1", (now(), json.dumps(data)))
            return redirect("Доступ к Sniper подтверждён. Поиск и рассылка не запускались.")
        except ValueError as error:
            return redirect(str(error))

    @router.post("/exclusions")
    async def exclusions(request: Request):
        form = await checked_form(request)
        try:
            emails, sites = lines(form.get("emails")), lines(form.get("sites"))
            with database() as conn:
                execute(conn, """INSERT INTO sniper_settings(id,encrypted_token,email_blacklist,site_blacklist)
                    VALUES (1,'',?,?) ON CONFLICT(id) DO UPDATE SET email_blacklist=excluded.email_blacklist,
                    site_blacklist=excluded.site_blacklist""", (json.dumps(emails), json.dumps(sites)))
            return redirect("Исключения сохранены для новых запросов.")
        except ValueError as error:
            return redirect(str(error))

    @router.post("/prepare")
    async def prepare_route(request: Request):
        form = await checked_form(request)
        try:
            identifier = await run_in_threadpool(prepare, form)
            return redirect(target="/dashboard/sniper/requests/" + identifier)
        except ValueError as error:
            return redirect(str(error))

    @router.get("/requests/{identifier}")
    def request_page(identifier: str, request: Request, message: str = ""):
        record = get_request(identifier)
        payload = json.loads(record["payload_json"])
        body = f"<h1>{esc(record['name'])}</h1><p><a href='/dashboard/sniper'>← Sniper</a></p><p>{esc(message)}</p>"
        if record["job_id"]:
            body += f"<p><a href='/dashboard/automation/jobs/{record['job_id']}/review'>Исходная заявка</a></p>"
        body += "<section><h2>Позиции</h2><ul>" + "".join(f"<li>{esc(g['name'])} — {esc(g.get('quantity'))} {esc(g.get('uom'))}</li>" for g in payload["search_goods"]) + "</ul>"
        if record["kind"] == "wizard":
            body += f"<h2>{esc(payload['email_subject'])}</h2><pre style='white-space:pre-wrap'>{esc(payload['email_body'])}</pre><p>Sniper заново найдёт получателей. Отправка по нашему готовому списку email сейчас недоступна.</p>"
        body += f"<p>Исключённые сайты: {esc(', '.join(payload.get('site_blacklist') or [])) or 'нет'}. Исключённые email: {esc(', '.join(payload.get('email_blacklist') or [])) or 'нет'}.</p></section>"
        if record["state"] == "draft":
            action = "поиск и отправку писем найденным поставщикам" if record["kind"] == "wizard" else "поиск поставщиков без отправки писем"
            body += "<section><h2>Подтверждение запуска</h2><p>Запуск использует тариф Sniper. Стоимость определяется вашим аккаунтом.</p>" + start_form(request, f"/dashboard/sniper/requests/{identifier}/submit") + f"<label><input type='checkbox' name='confirm' value='YES' required> Подтверждаю {action}</label><button>Запустить {'поиск и рассылку' if record['kind']=='wizard' else 'поиск'}</button></form></section>"
        else:
            body += f"<section><p>Состояние: {esc(record['remote_status'] or record['state'])}. Идентификатор Sniper: {esc(record['task_id']) or 'не получен'}.</p><p>{esc(record['error'])}</p>"
            if record["state"] in {"submitting", "uncertain"}:
                body += "<p>Повторный запуск заблокирован. Проверьте запрос в личном кабинете Sniper.</p>"
            if record["task_id"]:
                body += start_form(request, f"/dashboard/sniper/requests/{identifier}/refresh") + "<button>Получить состояние и контакты</button></form>"
                if record["kind"] == "wizard":
                    body += start_form(request, f"/dashboard/sniper/requests/{identifier}/replies") + "<button>Получить первые 100 ответов поставщиков</button></form>"
            body += "</section>"
        data = json.loads(record["result_json"] or "{}")
        contacts = data.get("result") or []
        contact_rows, seen = [], set()
        for contact in contacts:
            url, email = safe_url(contact.get("url")), str(contact.get("email") or "")
            key = (email.lower(), url.lower())
            if key in seen:
                continue
            seen.add(key)
            site = f"<a href='{esc(url)}' target='_blank' rel='noopener noreferrer'>{esc(url)}</a>" if url else "—"
            contact_rows.append(f"<tr><td>{site}</td><td>{esc(email)}</td><td>{esc(', '.join(contact.get('phone') or []))}</td><td>{esc(contact.get('site_class'))}</td></tr>")
        body += "<section><h2>Найденные контакты</h2><p class='note'>Это поставщики по всей партии. Соответствие каждому товару, цены и наличие требуют проверки.</p><table><tr><th>Сайт</th><th>Email</th><th>Телефон</th><th>Тип</th></tr>" + "".join(contact_rows) + "</table></section>"
        replies = json.loads(record["replies_json"] or "{}")
        if replies:
            body += f"<section><h2>Ответы поставщиков</h2><p>Всего: {esc(replies.get('total'))}; отображается до 100.</p><pre style='white-space:pre-wrap'>{esc(json.dumps(replies.get('email_list', []), ensure_ascii=False, indent=2))}</pre></section>"
        return page(body, "Запрос Sniper")

    @router.post("/requests/{identifier}/submit")
    async def submit_route(identifier: str, request: Request):
        form = await checked_form(request)
        if form.get("confirm") != "YES":
            raise HTTPException(400, "Подтвердите запуск.")
        try:
            await run_in_threadpool(submit, identifier)
            message = "Запрос принят Sniper."
        except ValueError as error:
            message = str(error)
        return redirect(message, "/dashboard/sniper/requests/" + identifier)

    @router.post("/requests/{identifier}/refresh")
    async def refresh_route(identifier: str, request: Request):
        await checked_form(request)
        try:
            await run_in_threadpool(refresh, identifier)
            message = "Состояние обновлено."
        except ValueError as error:
            message = str(error)
        return redirect(message, "/dashboard/sniper/requests/" + identifier)

    @router.post("/requests/{identifier}/replies")
    async def replies_route(identifier: str, request: Request):
        await checked_form(request)
        record = get_request(identifier)
        if record["kind"] != "wizard" or not record["task_id"]:
            raise HTTPException(400, "Запрос рассылки ещё не создан.")
        try:
            data = await run_in_threadpool(SniperClient().call, "GET", "/api/v0/mailbox/replies", params={"task_id": str(uuid.UUID(record["task_id"])), "entry": 100, "offset": 0})
            with database() as conn:
                execute(conn, "UPDATE sniper_requests SET replies_json=?,updated_at=? WHERE id=?", (json.dumps(data, ensure_ascii=False), now(), identifier))
            message = "Ответы получены."
        except ValueError as error:
            message = str(error)
        return redirect(message, "/dashboard/sniper/requests/" + identifier)

    app.include_router(router)
