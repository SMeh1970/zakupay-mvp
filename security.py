"""Authentication for the private Zakupay browser panel and MCP server.

The browser uses a signed, HTTP-only session cookie. ChatGPT uses OAuth 2.1
Authorization Code with PKCE. The Zakupay API token never leaves the server.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

PANEL_USERNAME = os.getenv("PANEL_USERNAME", "mekhman")
PANEL_PASSWORD_HASH = os.getenv(
    "PANEL_PASSWORD_HASH",
    "pbkdf2_sha256$310000$gOewHb9B9fKWTcxvmHVmQNq_MjA$juaSV1rksI9gQNQS_94TudsIpWS3v_WejP7BXioGy6U",
)
SESSION_COOKIE = "zakupay_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
ACCESS_TOKEN_TTL_SECONDS = 60 * 60
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60
OAUTH_SCOPE = "orders:read"
MAX_FORM_BYTES = 16_384

_signing_source = os.getenv("APP_SIGNING_SECRET") or os.getenv("ZAKUPAY_API_KEY")
if not _signing_source:
    # Local development still starts safely, but sessions reset on every restart.
    _signing_source = secrets.token_urlsafe(48)
_master_key = hashlib.sha256(
    ("zakupay-private-app:" + _signing_source).encode("utf-8")
).digest()

_authorization_codes: dict[str, dict[str, Any]] = {}
_failed_logins: dict[str, list[float]] = {}
_client_metadata_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _purpose_key(purpose: str) -> bytes:
    return hmac.new(_master_key, purpose.encode("utf-8"), hashlib.sha256).digest()


def _sign_payload(payload: dict[str, Any], purpose: str, prefix: str) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = _b64encode(raw)
    signature = _b64encode(
        hmac.new(_purpose_key(purpose), encoded.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{prefix}.{encoded}.{signature}"


def _decode_payload(token: str, purpose: str, prefix: str) -> dict[str, Any] | None:
    try:
        token_prefix, encoded, supplied_signature = token.split(".", 2)
        if token_prefix != prefix:
            return None
        expected_signature = _b64encode(
            hmac.new(
                _purpose_key(purpose), encoded.encode("ascii"), hashlib.sha256
            ).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        payload = json.loads(_b64decode(encoded))
        if not isinstance(payload, dict) or float(payload.get("exp", 0)) <= time.time():
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _verify_password(password: str) -> bool:
    try:
        algorithm, iterations, salt_b64, expected_b64 = PANEL_PASSWORD_HASH.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        calculated = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            _b64decode(salt_b64),
            int(iterations),
        )
        return hmac.compare_digest(_b64encode(calculated), expected_b64)
    except (ValueError, TypeError):
        return False


def _valid_credentials(username: str, password: str) -> bool:
    user_ok = hmac.compare_digest(username, PANEL_USERNAME)
    password_ok = _verify_password(password)
    return user_ok and password_ok


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _login_allowed(key: str) -> bool:
    cutoff = time.time() - 5 * 60
    attempts = [stamp for stamp in _failed_logins.get(key, []) if stamp >= cutoff]
    _failed_logins[key] = attempts
    return len(attempts) < 8


def _record_failed_login(key: str) -> None:
    _failed_logins.setdefault(key, []).append(time.time())


def _clear_failed_logins(key: str) -> None:
    _failed_logins.pop(key, None)


def _session_token() -> str:
    return _sign_payload(
        {
            "sub": PANEL_USERNAME,
            "iat": int(time.time()),
            "exp": int(time.time()) + SESSION_TTL_SECONDS,
        },
        "browser-session",
        "za_session",
    )


def _valid_session(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE, "")
    payload = _decode_payload(token, "browser-session", "za_session")
    return bool(payload and payload.get("sub") == PANEL_USERNAME)


def _origin(request: Request) -> str:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0]
    scheme = forwarded_proto.strip() or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}".rstrip("/")


def current_mcp_resource(request: Request) -> str:
    return f"{_origin(request)}/mcp"


def _safe_next(value: str | None) -> str:
    if not value:
        return "/dashboard"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//"):
        return "/dashboard"
    return value


async def _read_form(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        return {}
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items() if values}


def _login_page(next_path: str, error: str = "") -> HTMLResponse:
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    content = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Вход — Закупай</title>
<style>
body{{font-family:Arial,sans-serif;background:#f2f4f8;margin:0;min-height:100vh;display:grid;place-items:center;color:#17202a}}
.card{{width:min(390px,calc(100% - 40px));background:#fff;padding:28px;border-radius:16px;box-shadow:0 10px 35px rgba(0,0,0,.12)}}
h1{{margin:0 0 8px}}p{{color:#64748b;margin:0 0 22px}}label{{display:block;margin:13px 0 5px;font-size:13px;font-weight:700}}
input{{width:100%;box-sizing:border-box;padding:11px;border:1px solid #cbd5e1;border-radius:8px;font-size:16px}}
button{{width:100%;margin-top:18px;padding:12px;border:0;border-radius:8px;background:#4737cf;color:#fff;font-size:16px;font-weight:700;cursor:pointer}}
.error{{background:#fee2e2;color:#991b1b;padding:10px;border-radius:8px;margin-bottom:12px}}
</style></head><body><main class="card">
<h1>Панель «Закупай»</h1><p>Закрытый доступ к заявкам поставщика</p>{error_html}
<form method="post" action="/login">
<input type="hidden" name="next" value="{html.escape(next_path, quote=True)}">
<label for="username">Логин</label><input id="username" name="username" autocomplete="username" required>
<label for="password">Пароль</label><input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Войти</button>
</form></main></body></html>"""
    return HTMLResponse(content, headers={"Cache-Control": "no-store"})


def _chatgpt_client_id(client_id: str) -> bool:
    return bool(
        re.fullmatch(
            r"https://chatgpt\.com/oauth/(?:client\.json|[A-Za-z0-9_-]+/client\.json)",
            client_id,
        )
    )


def _valid_redirect_uri(uri: str) -> bool:
    return bool(
        re.fullmatch(
            r"https://chatgpt\.com/(?:connector_platform_oauth_redirect|connector/oauth/[A-Za-z0-9_-]+)",
            uri,
        )
    )


def _validated_client_metadata(client_id: str, redirect_uri: str) -> bool:
    """Fetch and validate the ChatGPT CIMD document without allowing SSRF."""
    if not _chatgpt_client_id(client_id) or not _valid_redirect_uri(redirect_uri):
        return False
    cached = _client_metadata_cache.get(client_id)
    if cached and cached[0] > time.time():
        document = cached[1]
    else:
        try:
            response = requests.get(
                client_id,
                headers={"Accept": "application/json"},
                timeout=8,
                allow_redirects=False,
            )
            if response.status_code != 200 or len(response.content) > 65_536:
                return False
            document = response.json()
            if not isinstance(document, dict):
                return False
        except (requests.RequestException, ValueError):
            return False
        _client_metadata_cache[client_id] = (time.time() + 60 * 60, document)

    redirect_uris = document.get("redirect_uris")
    supported_auth = document.get("token_endpoint_auth_methods_supported")
    if not isinstance(supported_auth, list):
        supported_auth = [document.get("token_endpoint_auth_method")]
    return (
        hmac.compare_digest(str(document.get("client_id", "")), client_id)
        and isinstance(redirect_uris, list)
        and redirect_uri in redirect_uris
        and "none" in supported_auth
        and "authorization_code" in document.get("grant_types", [])
        and "code" in document.get("response_types", [])
    )


def _valid_resource(resource: str, request: Request) -> bool:
    return hmac.compare_digest(resource.rstrip("/"), current_mcp_resource(request))


def _valid_code_challenge(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{43,128}", value))


def _authorize_params(values: dict[str, str], request: Request) -> tuple[dict[str, str] | None, str | None]:
    params = {
        "response_type": values.get("response_type", ""),
        "client_id": values.get("client_id", ""),
        "redirect_uri": values.get("redirect_uri", ""),
        "state": values.get("state", ""),
        "code_challenge": values.get("code_challenge", ""),
        "code_challenge_method": values.get("code_challenge_method", ""),
        "resource": values.get("resource", ""),
        "scope": values.get("scope", OAUTH_SCOPE) or OAUTH_SCOPE,
    }
    if params["response_type"] != "code":
        return None, "Поддерживается только Authorization Code."
    if not _chatgpt_client_id(params["client_id"]):
        return None, "Неизвестный OAuth-клиент."
    if not _valid_redirect_uri(params["redirect_uri"]):
        return None, "Недопустимый адрес возврата."
    if params["code_challenge_method"] != "S256" or not _valid_code_challenge(
        params["code_challenge"]
    ):
        return None, "Требуется PKCE S256."
    if not _valid_resource(params["resource"], request):
        return None, "Недопустимый MCP resource."
    requested_scopes = set(params["scope"].split())
    if not requested_scopes or not requested_scopes.issubset({OAUTH_SCOPE}):
        return None, "Запрошена недопустимая область доступа."
    return params, None


def _authorize_page(params: dict[str, str], error: str = "") -> HTMLResponse:
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(key, quote=True)}" value="{html.escape(value, quote=True)}">'
        for key, value in params.items()
    )
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    content = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Подключение ChatGPT — Закупай</title>
<style>
body{{font-family:Arial,sans-serif;background:#f2f4f8;margin:0;min-height:100vh;display:grid;place-items:center;color:#17202a}}
.card{{width:min(430px,calc(100% - 40px));background:#fff;padding:28px;border-radius:16px;box-shadow:0 10px 35px rgba(0,0,0,.12)}}
h1{{margin:0 0 8px}}p,li{{color:#64748b;line-height:1.45}}label{{display:block;margin:13px 0 5px;font-size:13px;font-weight:700}}
input{{width:100%;box-sizing:border-box;padding:11px;border:1px solid #cbd5e1;border-radius:8px;font-size:16px}}
button{{width:100%;margin-top:18px;padding:12px;border:0;border-radius:8px;background:#4737cf;color:#fff;font-size:16px;font-weight:700;cursor:pointer}}
.error{{background:#fee2e2;color:#991b1b;padding:10px;border-radius:8px;margin-bottom:12px}}
</style></head><body><main class="card">
<h1>Подключить «Закупай» к ChatGPT</h1>
<p>ChatGPT получит доступ только на чтение к заявкам. Ключ ZakupayToken останется на сервере.</p>
<ul><li>Просмотр и фильтрация заявок</li><li>Просмотр одной заявки</li><li>Без изменения цен и предложений</li></ul>
{error_html}<form method="post" action="/oauth/authorize">{hidden}
<label for="username">Логин</label><input id="username" name="username" autocomplete="username" required>
<label for="password">Пароль</label><input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Разрешить доступ</button>
</form></main></body></html>"""
    return HTMLResponse(content, headers={"Cache-Control": "no-store"})


def _issue_oauth_tokens(client_id: str, resource: str, scope: str) -> dict[str, Any]:
    now = int(time.time())
    issuer = resource.removesuffix("/mcp")
    common = {
        "sub": PANEL_USERNAME,
        "client_id": client_id,
        "aud": resource,
        "iss": issuer,
        "scope": scope,
        "iat": now,
        "nbf": now - 5,
    }
    access_token = _sign_payload(
        {**common, "exp": now + ACCESS_TOKEN_TTL_SECONDS, "jti": secrets.token_urlsafe(12)},
        "oauth-access",
        "za_at",
    )
    refresh_token = _sign_payload(
        {**common, "exp": now + REFRESH_TOKEN_TTL_SECONDS, "jti": secrets.token_urlsafe(16)},
        "oauth-refresh",
        "za_rt",
    )
    return {
        "access_token": access_token,
        "token_type": "Bearer",  # nosec B105
        "expires_in": ACCESS_TOKEN_TTL_SECONDS,
        "refresh_token": refresh_token,
        "scope": scope,
    }


def validate_bearer(request: Request) -> dict[str, Any] | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    payload = _decode_payload(token.strip(), "oauth-access", "za_at")
    if not payload:
        return None
    if payload.get("sub") != PANEL_USERNAME:
        return None
    if payload.get("iss", "").rstrip("/") != _origin(request):
        return None
    if payload.get("aud", "").rstrip("/") != current_mcp_resource(request):
        return None
    if OAUTH_SCOPE not in str(payload.get("scope", "")).split():
        return None
    return payload


def mcp_unauthorized(request: Request) -> JSONResponse:
    metadata_url = f"{_origin(request)}/.well-known/oauth-protected-resource/mcp"
    challenge = f'Bearer resource_metadata="{metadata_url}", scope="{OAUTH_SCOPE}"'
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "error": {"code": -32001, "message": "Требуется авторизация OAuth."},
            "id": None,
        },
        status_code=401,
        headers={"WWW-Authenticate": challenge, "Cache-Control": "no-store"},
    )


def install_security(app: FastAPI) -> None:
    @app.middleware("http")
    async def private_routes(request: Request, call_next):
        path = request.url.path
        browser_protected = path == "/dashboard" or path.startswith("/dashboard/")
        api_protected = path.startswith(("/analysis/", "/zakupay/")) or path in {
            "/orders",
            "/order",
        }

        if browser_protected and not _valid_session(request):
            target = path
            if request.url.query:
                target += "?" + request.url.query
            response: Response = RedirectResponse(
                "/login?" + urlencode({"next": target}), status_code=303
            )
        elif api_protected and not _valid_session(request):
            response = JSONResponse(
                {"detail": "Требуется вход в закрытую панель."},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        else:
            response = await call_next(request)

        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if browser_protected or api_protected or path.startswith("/oauth/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(next: str = "/dashboard"):
        return _login_page(_safe_next(next))

    @app.post("/login")
    async def login(request: Request):
        form = await _read_form(request)
        next_path = _safe_next(form.get("next"))
        key = _client_key(request)
        if not _login_allowed(key):
            return _login_page(next_path, "Слишком много попыток. Повторите через 5 минут.")
        if not _valid_credentials(form.get("username", ""), form.get("password", "")):
            _record_failed_login(key)
            return _login_page(next_path, "Неверный логин или пароль.")

        _clear_failed_logins(key)
        response = RedirectResponse(next_path, status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            _session_token(),
            max_age=SESSION_TTL_SECONDS,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/logout")
    async def logout():
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/.well-known/oauth-protected-resource")
    @app.get("/.well-known/oauth-protected-resource/mcp")
    async def protected_resource_metadata(request: Request):
        origin = _origin(request)
        return {
            "resource": f"{origin}/mcp",
            "authorization_servers": [origin],
            "scopes_supported": [OAUTH_SCOPE],
            "bearer_methods_supported": ["header"],
        }

    @app.get("/.well-known/oauth-authorization-server")
    async def authorization_server_metadata(request: Request):
        origin = _origin(request)
        return {
            "issuer": origin,
            "authorization_response_iss_parameter_supported": True,
            "authorization_endpoint": f"{origin}/oauth/authorize",
            "token_endpoint": f"{origin}/oauth/token",
            "client_id_metadata_document_supported": True,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": [OAUTH_SCOPE],
            "token_endpoint_auth_methods_supported": ["none"],
        }

    @app.get("/oauth/authorize", response_class=HTMLResponse)
    async def oauth_authorize_page(request: Request):
        values = dict(request.query_params)
        params, error = _authorize_params(values, request)
        if error or not params:
            return HTMLResponse(
                f"<h1>OAuth-запрос отклонён</h1><p>{html.escape(error or 'Ошибка запроса')}</p>",
                status_code=400,
            )
        if not await run_in_threadpool(
            _validated_client_metadata, params["client_id"], params["redirect_uri"]
        ):
            return HTMLResponse(
                "<h1>OAuth-запрос отклонён</h1><p>Не удалось проверить метаданные клиента.</p>",
                status_code=400,
            )
        return _authorize_page(params)

    @app.post("/oauth/authorize")
    async def oauth_authorize(request: Request):
        form = await _read_form(request)
        params, error = _authorize_params(form, request)
        if error or not params:
            return HTMLResponse(
                f"<h1>OAuth-запрос отклонён</h1><p>{html.escape(error or 'Ошибка запроса')}</p>",
                status_code=400,
            )
        if not await run_in_threadpool(
            _validated_client_metadata, params["client_id"], params["redirect_uri"]
        ):
            return HTMLResponse(
                "<h1>OAuth-запрос отклонён</h1><p>Не удалось проверить метаданные клиента.</p>",
                status_code=400,
            )

        key = _client_key(request)
        if not _login_allowed(key):
            return _authorize_page(params, "Слишком много попыток. Повторите через 5 минут.")
        if not _valid_credentials(form.get("username", ""), form.get("password", "")):
            _record_failed_login(key)
            return _authorize_page(params, "Неверный логин или пароль.")
        _clear_failed_logins(key)

        code = secrets.token_urlsafe(40)
        for old_code, old_record in list(_authorization_codes.items()):
            if old_record.get("used") or float(old_record.get("exp", 0)) <= time.time():
                _authorization_codes.pop(old_code, None)
        _authorization_codes[code] = {
            **params,
            "exp": time.time() + 5 * 60,
            "used": False,
        }
        redirect_query = {
            "code": code,
            "iss": _origin(request),
        }
        if params["state"]:
            redirect_query["state"] = params["state"]
        separator = "&" if "?" in params["redirect_uri"] else "?"
        return RedirectResponse(
            params["redirect_uri"] + separator + urlencode(redirect_query),
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/oauth/token")
    async def oauth_token(request: Request):
        form = await _read_form(request)
        grant_type = form.get("grant_type", "")

        if grant_type == "authorization_code":
            code = form.get("code", "")
            record = _authorization_codes.get(code)
            if (
                not record
                or record.get("used")
                or float(record.get("exp", 0)) <= time.time()
                or not hmac.compare_digest(form.get("client_id", ""), record["client_id"])
                or not hmac.compare_digest(form.get("redirect_uri", ""), record["redirect_uri"])
                or not hmac.compare_digest(form.get("resource", ""), record["resource"])
            ):
                return JSONResponse(
                    {"error": "invalid_grant", "error_description": "Код недействителен."},
                    status_code=400,
                )
            verifier = form.get("code_verifier", "")
            calculated = _b64encode(hashlib.sha256(verifier.encode("ascii", errors="ignore")).digest())
            if not verifier or not hmac.compare_digest(calculated, record["code_challenge"]):
                return JSONResponse(
                    {"error": "invalid_grant", "error_description": "PKCE-проверка не пройдена."},
                    status_code=400,
                )
            record["used"] = True
            return JSONResponse(
                _issue_oauth_tokens(record["client_id"], record["resource"], record["scope"]),
                headers={"Cache-Control": "no-store"},
            )

        if grant_type == "refresh_token":
            payload = _decode_payload(
                form.get("refresh_token", ""), "oauth-refresh", "za_rt"
            )
            if (
                not payload
                or not hmac.compare_digest(form.get("client_id", ""), str(payload.get("client_id", "")))
                or not hmac.compare_digest(form.get("resource", ""), str(payload.get("aud", "")))
            ):
                return JSONResponse(
                    {"error": "invalid_grant", "error_description": "Refresh token недействителен."},
                    status_code=400,
                )
            return JSONResponse(
                _issue_oauth_tokens(
                    str(payload["client_id"]), str(payload["aud"]), str(payload["scope"])
                ),
                headers={"Cache-Control": "no-store"},
            )

        return JSONResponse(
            {"error": "unsupported_grant_type"},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )
