import json
import os
import re
import tempfile
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import automation_pipeline as pipeline
import sniper_integration as sniper
from security import SESSION_COOKIE, _session_token, install_security


@pytest.fixture
def panel():
    with tempfile.TemporaryDirectory() as directory, patch.multiple(
            pipeline, DB_PATH=os.path.join(directory, "sniper.db"), DATABASE_URL="", _initialized_database_key=None), \
            patch.dict(os.environ, {"APP_SIGNING_SECRET": "test-only-secret", "SNIPER_API_TOKEN": ""}):
        app = FastAPI()
        sniper.install_sniper(app)
        install_security(app)
        with TestClient(app, base_url="https://testserver") as client:
            client.cookies.set(SESSION_COOKIE, _session_token())
            yield client


def csrf(client, path="/dashboard/sniper"):
    response = client.get(path)
    assert response.status_code == 200
    return re.search(r"name='csrf' value='([^']+)'", response.text).group(1)


def draft(kind="scan", **changes):
    form = {"name": "Проверка", "kind": kind, "goods": "Маркер черный; 10; шт",
            "email_subject": "Запрос цены", "email_body": "Добрый день. Маркер черный, 10 шт."}
    form.update(changes)
    return sniper.prepare(form)


def client_with_reply(data):
    client = Mock(spec=sniper.SniperClient)
    client.pat = "test-pat"
    client.call.return_value = data
    return client


def test_private_panel_blocks_anonymous_and_csrf(panel):
    panel.cookies.clear()
    assert panel.get("/dashboard/sniper", follow_redirects=False).status_code == 303
    assert panel.post("/dashboard/sniper/connect", data={"pat": "secret"}, follow_redirects=False).status_code == 303
    panel.cookies.set(SESSION_COOKIE, _session_token())
    with patch.object(sniper.SniperClient, "languages") as call:
        assert panel.post("/dashboard/sniper/connect", data={"pat": "secret"}).status_code == 403
        call.assert_not_called()


def test_credential_ciphertext_and_render_do_not_expose_secret(panel):
    secret = "very-private-pat"
    with patch.object(sniper.SniperClient, "languages", return_value={"languages": [{"id": 1, "name": "Русский"}]}):
        response = panel.post("/dashboard/sniper/connect", data={"csrf": csrf(panel), "pat": secret})
    assert "Доступ к API проверен" in response.text
    assert secret not in response.text
    assert secret not in json.dumps(sniper.settings())
    assert sniper.token() == secret
    with patch.dict(os.environ, {"APP_SIGNING_SECRET": "different-server-key"}):
        with pytest.raises(ValueError, match="изменился"):
            sniper.token()


def test_bad_pat_does_not_overwrite_existing_connection(panel):
    sniper.save_token("working-pat", languages={"languages": []})
    with patch.object(sniper.SniperClient, "languages", side_effect=sniper.SniperError("Авторизация отклонена")):
        panel.post("/dashboard/sniper/connect", data={"csrf": csrf(panel), "pat": "wrong-pat"})
    assert sniper.token() == "working-pat"


def test_login_issues_only_and_persists_pat_before_followup_check(panel):
    with patch.object(sniper.SniperClient, "login", return_value={"token": "new-pat", "expires_at": "2027-04-12"}) as login, \
            patch.object(sniper.SniperClient, "languages", side_effect=sniper.SniperError("Справочник недоступен")):
        response = panel.post("/dashboard/sniper/connect", data={"csrf": csrf(panel), "login": "smeh", "password": "private-password"})
    login.assert_called_once_with("smeh", "private-password")
    assert sniper.token() == "new-pat"
    assert "private-password" not in response.text + json.dumps(sniper.settings())
    assert sniper.settings()["verified_at"] is None


def test_get_and_prepare_never_start_search_or_mail(panel):
    with patch.object(sniper.SniperClient, "call") as call:
        response = panel.post("/dashboard/sniper/prepare", data={"csrf": csrf(panel), "name": "Тест", "kind": "wizard",
            "goods": "Маркер; 10; шт", "email_subject": "Запрос", "email_body": "Маркер 10 шт"})
        assert "Подтверждение запуска" in response.text
        assert "Маркер 10 шт" in response.text
        panel.get("/dashboard/sniper")
        call.assert_not_called()


def test_submission_requires_confirmation_and_only_once(panel):
    sniper.save_token("test-pat")
    identifier = draft("wizard")
    path = "/dashboard/sniper/requests/" + identifier
    response_data = {"task_id": "6b584e44-dee8-4012-b8c0-962cd51d053b", "status": "created"}
    with patch.object(sniper.SniperClient, "call", return_value=response_data) as call:
        assert panel.post(path + "/submit", data={"csrf": csrf(panel, path)}).status_code == 400
        call.assert_not_called()
        form = {"csrf": csrf(panel, path), "confirm": "YES"}
        assert panel.post(path + "/submit", data=form).status_code == 200
        panel.post(path + "/submit", data=form)
        call.assert_called_once()
        assert call.call_args.args[1] == "/api/v0/requests/wizard"
        assert call.call_args.args[2]["dry_run"] is False
    assert sniper.get_request(identifier)["state"] == "submitted"


def test_scan_has_no_mail_parameters(panel):
    identifier = draft()
    client = client_with_reply({"task_id": "6b584e44-dee8-4012-b8c0-962cd51d053b"})
    sniper.submit(identifier, client)
    _, path, payload = client.call.call_args.args
    assert path == "/api/v0/requests/scan"
    assert not any(key in payload for key in ["email_body", "email_subject", "generate_email", "email_blacklist"])


@pytest.mark.parametrize("error", [sniper.SniperError("timeout", uncertain=True), sniper.SniperError("500", uncertain=True)])
def test_uncertain_submission_is_durable_and_not_retried(panel, error):
    identifier = draft("wizard")
    client = client_with_reply({})
    client.call.side_effect = error
    with pytest.raises(ValueError):
        sniper.submit(identifier, client)
    assert sniper.get_request(identifier)["state"] == "uncertain"
    client.call.reset_mock()
    with pytest.raises(ValueError, match="уже запускался"):
        sniper.submit(identifier, client)
    client.call.assert_not_called()


def test_missing_task_id_is_uncertain_not_a_success(panel):
    identifier = draft()
    with pytest.raises(ValueError):
        sniper.submit(identifier, client_with_reply({"status": "created"}))
    assert sniper.get_request(identifier)["state"] == "uncertain"


def test_refresh_uses_read_method_and_renders_contacts_safely(panel):
    identifier = draft()
    client = client_with_reply({"task_id": "6b584e44-dee8-4012-b8c0-962cd51d053b"})
    sniper.submit(identifier, client)
    client.call.reset_mock()
    contact = {"url": "javascript:alert(1)", "email": "<script>alert(1)</script>", "phone": [], "site_class": "торговая"}
    client.call.return_value = {"status": "finished", "result": [contact, contact]}
    sniper.refresh(identifier, client)
    assert client.call.call_args.args[0] == "GET"
    rendered = panel.get("/dashboard/sniper/requests/" + identifier).text
    assert "href='javascript:" not in rendered
    assert "<script>alert(1)</script>" not in rendered
    assert rendered.count("&lt;script&gt;alert(1)&lt;/script&gt;") == 1


def test_exclusions_snapshotted_with_draft(panel):
    panel.post("/dashboard/sniper/exclusions", data={"csrf": csrf(panel), "emails": "bad@example.com", "sites": "bad.example"})
    identifier = draft("wizard")
    payload = json.loads(sniper.get_request(identifier)["payload_json"])
    assert payload["site_blacklist"] == ["bad.example"]
    assert payload["email_blacklist"] == ["bad@example.com"]
    assert "email_blacklist" not in json.loads(sniper.get_request(draft())["payload_json"])


def test_input_limits_preserve_fractional_quantities_and_name(panel):
    assert sniper.parse_goods("Фанера 21 мм; 2; лист")[0] == {"name": "Фанера 21 мм", "quantity": 2, "uom": "лист"}
    assert sniper.parse_goods("Труба 21 мм; 2,5; м")[0] == {"name": "Труба 21 мм", "uom": "м"}
    with pytest.raises(ValueError, match="20"):
        sniper.parse_goods("\n".join(["Маркер"] * 21))
    for quantity in ["0", "-2", "NaN", "Infinity"]:
        with pytest.raises(ValueError):
            sniper.parse_goods("Маркер; " + quantity + "; шт")
    with pytest.raises(ValueError, match="тему"):
        draft("wizard", email_body="")


def test_client_never_follows_redirect_or_exposes_upstream_secrets(panel):
    response = Mock()
    response.status_code = 500
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.text = "private-token"
    with patch.object(requests_module := sniper.requests, "request", return_value=response) as call:
        with pytest.raises(sniper.SniperError) as captured:
            sniper.SniperClient("private-token").call("POST", "/api/v0/requests/wizard", {})
    assert "private-token" not in str(captured.value)
    assert captured.value.uncertain
    assert call.call_args.kwargs["allow_redirects"] is False


def test_credentials_use_documented_login_and_do_not_renew(panel):
    response = Mock(status_code=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.json.return_value = {"access_token": "jwt"}
    with patch.object(sniper.requests, "post", return_value=response) as login, \
            patch.object(sniper.SniperClient, "call", return_value={"token": "pat"}) as issue:
        assert sniper.SniperClient().login("smeh", "password") == {"token": "pat"}
    assert login.call_args.args == ("https://sniper-search.ru/api/bff/v1/user/login",)
    assert login.call_args.kwargs["json"] == {"login": "smeh", "password": "password", "fingerprint": ""}
    assert issue.call_args.args == ("POST", "/api/v0/token/issue")
    assert issue.call_args.kwargs == {"bearer": "jwt"}


def test_failed_connection_preserves_login_and_shows_visible_result(panel):
    with patch.object(sniper.SniperClient, "login", side_effect=sniper.SniperError("Ошибка подключения HTTP 404")):
        response = panel.post("/dashboard/sniper/connect", data={"csrf": csrf(panel),
            "sniper_account_login": "smeh", "sniper_account_password": "private-password"}, follow_redirects=False)
    assert response.status_code == 200
    assert "role='alert'" in response.text
    assert "HTTP 404" in response.text
    assert "name='sniper_account_login' value='smeh'" in response.text
    assert "autocomplete='current-password'" not in response.text
    assert "private-password" not in response.text
    assert "Аккаунт ещё не подключён" in response.text


def test_unknown_login_route_is_not_reported_as_wrong_password(panel):
    response = Mock(status_code=404)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    with patch.object(sniper.requests, "post", return_value=response), \
            patch.object(sniper.SniperClient, "call") as issue:
        with pytest.raises(sniper.SniperError, match="ошибка подключения"):
            sniper.SniperClient().login("smeh", "private-password")
    issue.assert_not_called()


def test_login_normalizes_spaces_like_sniper_website(panel):
    response = Mock(status_code=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.json.return_value = {"access_token": "jwt"}
    with patch.object(sniper.requests, "post", return_value=response) as login, \
            patch.object(sniper.SniperClient, "call", return_value={"token": "pat"}):
        sniper.SniperClient().login(" smeh ", " password \n")
    assert login.call_args.kwargs["json"]["login"] == "smeh"
    assert login.call_args.kwargs["json"]["password"] == "password"


@pytest.mark.parametrize("detail,code", [("different fingerprints", "different_fingerprints"),
    ("multiple login", "multiple_login"), ("invalid credentials", "invalid_credentials"),
    ("password login not available", "password_login_unavailable")])
def test_login_classifies_upstream_denial_without_inventing_password_error(panel, detail, code):
    response = Mock(status_code=401)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.json.return_value = {"detail": detail}
    with patch.object(sniper.requests, "post", return_value=response), \
            patch.object(sniper.SniperClient, "call") as issue:
        with pytest.raises(sniper.SniperError, match=code):
            sniper.SniperClient().login("smeh", "password")
    issue.assert_not_called()


def test_unknown_upstream_login_detail_cannot_leak_credentials(panel, caplog):
    response = Mock(status_code=401)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.json.return_value = {"detail": "Rejected password: private-password, token: private-token"}
    with patch.object(sniper.requests, "post", return_value=response):
        with pytest.raises(sniper.SniperError) as error:
            sniper.SniperClient().login("smeh", "private-password")
    assert "private-password" not in str(error.value) + caplog.text
    assert "private-token" not in str(error.value) + caplog.text
