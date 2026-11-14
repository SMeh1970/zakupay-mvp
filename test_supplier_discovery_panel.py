import json
import os
import re
import tempfile
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import automation_pipeline as pipeline
import supplier_discovery as sd
import supplier_discovery_panel as panel_module
from security import SESSION_COOKIE, _session_token, install_security

SECRET = "SERP-SECRET-0123456789"


@pytest.fixture
def panel():
    with tempfile.TemporaryDirectory() as directory, patch.multiple(
            pipeline, DB_PATH=os.path.join(directory, "web.db"), DATABASE_URL="", _initialized_database_key=None), \
            patch.dict(os.environ, {"APP_SIGNING_SECRET": "test-only-secret", "SERPAPI_API_KEY": SECRET,
                                    "FIRECRAWL_API_KEY": "", "WEB_SEARCH_PAGES_PER_CLICK": "3"}):
        app = FastAPI()
        panel_module.install_supplier_discovery(app)
        install_security(app)
        with TestClient(app, base_url="https://testserver") as client:
            client.cookies.set(SESSION_COOKIE, _session_token())
            yield client


def add_job(items=("Профиль потолочный 60*27", "Саморез 4,2*16")):
    order = {"name": "Тестовая заявка", "orderItems": [{"goodName": n, "count": 1} for n in items]}
    with pipeline._connect() as conn:
        cursor = conn.execute(
            "INSERT INTO automation_jobs (dedupe_key,order_id,event_type,status,order_json,result_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)", ("job", 1, "test", "ready", json.dumps(order), "{}", "now", "now"))
        return cursor.lastrowid


def token(client, job_id):
    response = client.get(f"/dashboard/web-suppliers?job_id={job_id}")
    assert response.status_code == 200
    assert f"/dashboard/automation/jobs/{job_id}/review" in response.text
    return re.search(r"name='csrf' value='([^']+)'", response.text).group(1)


def fake_search(by_query):
    """search_page, отдающий сайты по тексту запроса и запоминающий вызовы."""
    calls = []

    def search(engine, page, query, **kwargs):
        calls.append((engine, page, query, kwargs))
        urls = by_query.get(query, [])
        results = [{"url": u, "title": t, "description": "купить с доставкой"} for u, t in urls] if page == 0 else []
        return {"results": results, "raw": results, "next": None, "notice": "",
                "diagnostics": {"raw": len(results), "invalid": 0, "excluded": 0}}
    search.calls = calls
    return search


PROFILE = [("https://stroy-profil.ru/catalog/profil/", "Профили — купить"), ("https://gkl-shop.ru/catalog/", "ГКЛ и профиль")]
SCREWS = [("https://gkl-shop.ru/catalog/samorez/", "Саморезы — купить"), ("https://krepezh.ru/catalog/", "Крепёж")]


def post(client, path, job_id, **data):
    return client.post(path, data={"csrf": token(client, job_id), "job_id": job_id, **data}, follow_redirects=False)


def test_panel_is_private_and_forms_need_a_token(panel):
    job = add_job()
    panel.cookies.clear()
    assert panel.get("/dashboard/web-suppliers", follow_redirects=False).status_code == 303
    panel.cookies.set(SESSION_COOKIE, _session_token())
    with patch.object(sd, "search_page") as call:
        response = panel.post("/dashboard/web-suppliers/search", data={"job_id": job, "position": 1})
        assert response.status_code == 403
        call.assert_not_called()


def test_order_page_lists_positions_with_a_search_button(panel):
    job = add_job()
    html = panel.get(f"/dashboard/web-suppliers?job_id={job}").text
    assert "Профиль потолочный 60*27" in html and "Саморез 4,2*16" in html
    assert html.count("class='dot'") == 2 and html.count("type='checkbox'") == 2 and "Сохранить группы" in html
    assert "Найти сайты" in html and "Позиции без цвета образуют" in html
    assert SECRET not in html
    # скрипт смены цвета разрешён только своим одноразовым кодом
    response = panel.get(f"/dashboard/web-suppliers?job_id={job}")
    nonce = re.search(r"<script nonce='([^']+)'", response.text).group(1)
    assert f"script-src 'nonce-{nonce}'" in response.headers["content-security-policy"]


def test_search_stores_sites_and_aggregates_domains_across_positions(panel):
    job = add_job()
    search = fake_search({"Профиль потолочный 60х27": PROFILE, "Саморез 4,2х16": SCREWS})
    with patch.object(sd, "search_page", search):
        assert post(panel, "/dashboard/web-suppliers/groups", job, color_1=1, sel_1=1, color_2=2, sel_2=1).status_code == 303
        assert post(panel, "/dashboard/web-suppliers/group-search", job, color=1, keyword="Профиль потолочный 60*27").status_code == 303
        assert post(panel, "/dashboard/web-suppliers/group-search", job, color=2, keyword="Саморез 4,2*16").status_code == 303
    engines = {c[0] for c in search.calls}
    assert engines == {"yandex", "google"}
    assert all(c[3]["serpapi_key"] == SECRET for c in search.calls)
    html = panel.get(f"/dashboard/web-suppliers?job_id={job}").text
    assert "Найденные сайты (3)" in html, re.findall(r"Найденные сайты \(\d+\)|<b><a [^>]*>[^<]+</a></b>", html)
    # gkl-shop.ru найден по обеим позициям и поэтому стоит первым
    assert html.index("gkl-shop.ru") < html.index("stroy-profil.ru") and "2 из 2" in html and "1 из 2" in html
    assert SECRET not in html


def test_size_in_the_query_is_written_in_one_spelling(panel):
    job = add_job(("Профиль 60 * 27",))
    search = fake_search({})
    with patch.object(sd, "search_page", search):
        post(panel, "/dashboard/web-suppliers/search", job, position=1)
    assert {c[2] for c in search.calls} == {"Профиль 60х27"}


def test_second_click_continues_and_does_not_charge_finished_pages_again(panel):
    job = add_job(("Профиль 60х27",))
    pages = iter([(PROFILE[:1], 1), (PROFILE[1:], 2)])

    def search(engine, page, query, **kwargs):
        if engine == "google":
            return {"results": [], "raw": [], "next": None, "diagnostics": {"raw": 0, "invalid": 0, "excluded": 0}}
        urls, nxt = next(pages)
        results = [{"url": u, "title": t, "description": ""} for u, t in urls]
        return {"results": results, "raw": results, "next": nxt, "diagnostics": {"raw": 1, "invalid": 0, "excluded": 0}}
    with patch.object(sd, "search_page", search), patch.dict(os.environ, {"WEB_SEARCH_PAGES_PER_CLICK": "1"}):
        post(panel, "/dashboard/web-suppliers/search", job, position=1)
        key = panel_module.run_key(job, 1, "Профиль 60х27")
        first = panel_module.load_run(key)[1]
        post(panel, "/dashboard/web-suppliers/search", job, position=1)
        second = panel_module.load_run(key)[1]
    assert len(second["sites"]) > len(first["sites"]) and second["requests"] > first["requests"]
    assert len(second["completed"]) == len(set(second["completed"]))


def test_excluded_domain_disappears_and_is_skipped_by_new_searches(panel):
    job = add_job(("Профиль 60х27",))
    search = fake_search({"Профиль 60х27": PROFILE})
    with patch.object(sd, "search_page", search):
        post(panel, "/dashboard/web-suppliers/search", job, position=1)
        response = post(panel, "/dashboard/web-suppliers/policy", job, domain="gkl-shop.ru", policy="excluded")
        assert response.status_code == 303
        assert "gkl-shop.ru" not in panel.get(f"/dashboard/web-suppliers?job_id={job}").text.split("Найденные сайты")[1]
        assert "gkl-shop.ru" in panel.get(f"/dashboard/web-suppliers?job_id={job}&all=1").text
    other = fake_search({"Профиль 60х27": PROFILE})
    with patch.object(sd, "search_page", other):
        state = panel_module.execute_search("m:fresh", 0, 0, "Профиль 60х27", "Профиль 60х27", ["yandex"])
    assert [sd.site_domain(s["url"]) for s in state["sites"]] == ["stroy-profil.ru"]
    assert "gkl-shop.ru" in other.calls[0][3]["exclude_domains"]


def test_policy_rejects_unknown_marks(panel):
    job = add_job()
    assert post(panel, "/dashboard/web-suppliers/policy", job, domain="a.ru", policy="boss").status_code == 400


def test_contacts_are_saved_per_domain_and_shown(panel):
    job = add_job(("Профиль 60х27",))
    with patch.object(sd, "search_page", fake_search({"Профиль 60х27": PROFILE})):
        post(panel, "/dashboard/web-suppliers/search", job, position=1)
    found = {"contacts": [{"kind": "email", "value": "zakaz@stroy-profil.ru", "source": "x"},
                          {"kind": "phone", "value": "+7 (495) 123-45-67", "source": "x"}], "errors": [], "url": "x"}
    with patch.object(sd, "read_domain_contacts", return_value=found) as read:
        response = post(panel, "/dashboard/web-suppliers/contacts", job, domain="stroy-profil.ru", url="https://stroy-profil.ru/catalog/profil/")
        assert response.status_code == 303 and read.call_args[0][0] == "https://stroy-profil.ru/catalog/profil/"
    html = panel.get(f"/dashboard/web-suppliers?job_id={job}").text
    assert "zakaz@stroy-profil.ru" in html and "+7 (495) 123-45-67" in html


def test_contacts_for_a_foreign_address_are_refused(panel):
    job = add_job()
    with patch.object(sd, "read_domain_contacts") as read:
        response = post(panel, "/dashboard/web-suppliers/contacts", job, domain="stroy-profil.ru", url="https://evil.ru/")
        assert response.status_code == 400
        read.assert_not_called()


def test_provider_error_is_shown_without_a_server_error_and_without_the_key(panel):
    job = add_job(("Профиль 60х27",))

    def failing(engine, page, query, **kwargs):
        raise sd.ProviderError("SerpAPI (поиск) (HTTP 402): Лимит поиска или баланс исчерпан.")
    with patch.object(sd, "search_page", failing):
        response = post(panel, "/dashboard/web-suppliers/search", job, position=1)
    assert response.status_code == 303
    html = panel.get(response.headers["location"]).text
    assert "Лимит поиска или баланс исчерпан" in html and SECRET not in html


def test_position_outside_the_order_is_rejected(panel):
    job = add_job()
    assert post(panel, "/dashboard/web-suppliers/search", job, position=9).status_code == 400
    assert post(panel, "/dashboard/web-suppliers/search", job, position=0).status_code == 400


def test_manual_search_without_an_order_works_and_needs_a_name(panel):
    page = panel.get("/dashboard/web-suppliers")
    csrf_value = re.search(r"name='csrf' value='([^']+)'", page.text).group(1)
    with patch.object(sd, "search_page", fake_search({"Профиль 60х27": PROFILE})) as search:
        response = panel.post("/dashboard/web-suppliers/search", data={"csrf": csrf_value, "query": "Профиль 60*27"}, follow_redirects=False)
        assert response.status_code == 303 and "message=" in response.headers["location"]
        assert panel.post("/dashboard/web-suppliers/search", data={"csrf": csrf_value, "query": " "}).status_code == 400


def test_a_second_click_during_a_running_search_does_not_spend_the_budget_twice(panel):
    job = add_job(("Профиль 60х27",))
    key = panel_module.run_key(job, 1, "Профиль 60х27")
    panel_module._running.add(key)
    try:
        with patch.object(sd, "search_page") as call:
            response = post(panel, "/dashboard/web-suppliers/search", job, position=1)
            assert response.status_code == 303 and "уже выполняется" in panel.get(response.headers["location"]).text
            call.assert_not_called()
    finally:
        panel_module._running.discard(key)


def test_default_budget_is_small(panel):
    """Без переменных окружения: до 6 запросов на позицию и 2 страницы выдачи за одно нажатие."""
    with patch.dict(os.environ):
        os.environ.pop("WEB_SEARCH_MAX_REQUESTS", None)
        os.environ.pop("WEB_SEARCH_PAGES_PER_CLICK", None)
        cfg = panel_module.settings()
    assert cfg["max_requests"] == 6 and cfg["pages_per_click"] == 2


def test_yandex_keys_switch_the_default_engine_to_the_official_api(panel):
    with patch.dict(os.environ, {"YANDEX_SEARCH_API_KEY": "YK", "YANDEX_FOLDER_ID": "folder"}):
        assert panel_module.default_engines() == ["yandex_cloud"]
    with patch.dict(os.environ, {"YANDEX_SEARCH_API_KEY": "YK", "YANDEX_FOLDER_ID": ""}):
        assert panel_module.default_engines() == ["yandex", "google"]


def test_yandex_deferred_click_waits_then_collects_without_a_second_charge(panel):
    job = add_job(("Профиль 60х27",))
    env = {"YANDEX_SEARCH_API_KEY": "YK-SECRET", "YANDEX_FOLDER_ID": "folder", "SERPAPI_API_KEY": ""}
    calls = []
    answers = iter(["wait", "wait", "ready"])

    def search(engine, page, query, **kwargs):
        calls.append((engine, page, kwargs["yandex_key"], kwargs["yandex_folder"]))
        step = next(answers)
        if step == "wait":
            kwargs["operations"]["yandex_cloud:0"] = {"id": "op1234567", "started": "2026-11-13T10:00:00+00:00"}
            kwargs["save"]()
            raise sd.PendingOperation("2026-11-13T10:00:00+00:00")
        kwargs["operations"].pop("yandex_cloud:0", None)   # как делает настоящая функция, когда результат забран
        results = [{"url": u, "title": t, "description": "купить"} for u, t in PROFILE]
        return {"results": results, "raw": results, "next": None, "notice": "", "diagnostics": {"raw": 2, "invalid": 0, "excluded": 0}}

    with patch.dict(os.environ, env), patch.object(sd, "search_page", search):
        first = post(panel, "/dashboard/web-suppliers/group-search", job, color=0, keyword="Профиль 60х27")
        assert "Запрос отправлен в Яндекс" in panel.get(first.headers["location"]).text
        html = panel.get(f"/dashboard/web-suppliers?job_id={job}").text
        assert "Забрать результат" in html and "YK-SECRET" not in html
        post(panel, "/dashboard/web-suppliers/group-search", job, color=0, keyword="Профиль 60х27")       # ещё не готово
        post(panel, "/dashboard/web-suppliers/group-search", job, color=0, keyword="Профиль 60х27")       # готово
    state = panel_module.load_run(panel_module.group_run_key(job, 0))[1]
    assert state["requests"] == 1 and state["reason"] == "exhausted" and state["operations"] == {}
    assert {sd.site_domain(s["url"]) for s in state["sites"]} == {"stroy-profil.ru", "gkl-shop.ru"}
    assert all(c[:2] == ("yandex_cloud", 0) and c[2:] == ("YK-SECRET", "folder") for c in calls)


def test_marks_make_groups_and_unchecked_positions_stay_out_of_search(panel):
    job = add_job(("Профиль 60х27", "Саморез 4,2х16", "Дюбель 6х40"))
    assert post(panel, "/dashboard/web-suppliers/groups", job, color_1=1, sel_1=1, color_2=1, sel_2=1, color_3=2).status_code == 303
    assert panel_module.build_groups(panel_module.load_marks(job, 3)) == {1: [1, 2]}
    html = panel.get(f"/dashboard/web-suppliers?job_id={job}").text
    assert "Группа 1" in html and "Группа 2" not in html and "позиций: 2" in html
    search = fake_search({"Профиль 60х27": PROFILE})
    with patch.object(sd, "search_page", search):
        post(panel, "/dashboard/web-suppliers/group-search", job, color=1, keyword="Профиль 60х27")
    assert {c[2] for c in search.calls} == {"Профиль 60х27"}
    html = panel.get(f"/dashboard/web-suppliers?job_id={job}").text
    # сайты группы засчитаны обеим её позициям, а не отключённой третьей
    assert "2 из 3" in html and "1. " in html and "2. " in html and "3. <a" not in html


def test_group_without_selected_positions_cannot_be_searched(panel):
    job = add_job(("Профиль 60х27", "Саморез 4,2х16"))
    post(panel, "/dashboard/web-suppliers/groups", job, color_1=1, color_2=2, sel_2=1)
    with patch.object(sd, "search_page", fake_search({})) as search:
        assert post(panel, "/dashboard/web-suppliers/group-search", job, color=1, keyword="Профиль").status_code == 400
    assert not search.calls if hasattr(search, "calls") else True
    assert post(panel, "/dashboard/web-suppliers/group-search", job, color=2, keyword="  ").status_code == 400


def test_group_keyword_is_saved_and_shown_again(panel):
    job = add_job()
    post(panel, "/dashboard/web-suppliers/groups", job, color_1=3, sel_1=1)
    with patch.object(sd, "search_page", fake_search({"гипсокартон лист": PROFILE})):
        post(panel, "/dashboard/web-suppliers/group-search", job, color=3, keyword="гипсокартон лист")
    assert "value=\"гипсокартон лист\"" in panel.get(f"/dashboard/web-suppliers?job_id={job}").text or \
        "value='гипсокартон лист'" in panel.get(f"/dashboard/web-suppliers?job_id={job}").text


def test_group_contacts_are_collected_for_sites_of_that_group_only(panel):
    job = add_job(("Профиль 60х27", "Саморез 4,2х16"))
    post(panel, "/dashboard/web-suppliers/groups", job, color_1=1, sel_1=1, color_2=2, sel_2=1)
    with patch.object(sd, "search_page", fake_search({"Профиль": PROFILE, "Саморез": SCREWS})):
        post(panel, "/dashboard/web-suppliers/group-search", job, color=1, keyword="Профиль")
        post(panel, "/dashboard/web-suppliers/group-search", job, color=2, keyword="Саморез")
    visited = []

    def contacts(url, blocked=(), session=None):
        visited.append(sd.site_domain(url))
        return {"contacts": [{"kind": "email", "value": f"info@{sd.site_domain(url)}"}], "errors": [], "url": url}
    with patch.object(sd, "read_domain_contacts", contacts):
        response = post(panel, "/dashboard/web-suppliers/group-contacts", job, color=2)
    assert response.status_code == 303
    assert sorted(visited) == ["gkl-shop.ru", "krepezh.ru"]      # сайты группы 2; stroy-profil.ru из группы 1 не тронут
    html = panel.get(f"/dashboard/web-suppliers?job_id={job}").text
    assert "info@krepezh.ru" in html and "info@stroy-profil.ru" not in html
    with patch.object(sd, "read_domain_contacts", contacts):
        again = post(panel, "/dashboard/web-suppliers/group-contacts", job, color=2)
    assert "уже проверены" in panel.get(again.headers["location"]).text


def test_standalone_page_lists_recent_orders_to_open_in_the_search(panel):
    job = add_job()
    html = panel.get("/dashboard/web-suppliers").text
    assert f"/dashboard/web-suppliers?job_id={job}" in html and "Тестовая заявка" in html and "позиций: 2" in html


TILE_SITES = [
    ("https://tile-a.ru/catalog/keramogranit/", "Керамогранит соль-перец 600х600", "Керамогранит матовый, плиты со склада, купить с доставкой, каталог, оптом"),
    ("https://tile-b.ru/catalog/granit/", "Гранит полированный плита", "Гранит природный, плиты со склада, каталог"),
    ("https://tile-c.ru/about/", "О компании", "Информация о нас"),
]


def manual_search(panel, query="Керамогранит 600*600 соль/перец", sites=TILE_SITES):
    page = panel.get("/dashboard/web-suppliers")
    csrf_value = re.search(r"name='csrf' value='([^']+)'", page.text).group(1)
    results = [{"url": u, "title": t, "description": d} for u, t, d in sites]
    search = lambda engine, page, q, **kw: {"results": results if page == 0 else [], "raw": results, "next": None, "notice": "",
                                            "diagnostics": {"raw": len(results), "invalid": 0, "excluded": 0}}
    with patch.object(sd, "search_page", search):
        response = panel.post("/dashboard/web-suppliers/search", data={"csrf": csrf_value, "query": query}, follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"]


def run_of(location):
    from urllib.parse import parse_qs, urlsplit
    return parse_qs(urlsplit(location).query)["run"][0]


def page_token(panel, url):
    return re.search(r"name='csrf' value='([^']+)'", panel.get(url).text).group(1)


def test_standalone_page_shows_search_results_with_tabs(panel):
    location = manual_search(panel)
    assert "run=" in location
    html = panel.get(location).text
    assert "Сайты и Email" in html and "НАИМЕНОВАНИЕ" in html and "Керамогранит 600х600 соль/перец" in html
    assert "Сайтов: 3 / 3" in html and "Все · 3" in html
    assert "tile-a.ru" in html and "tile-b.ru" in html and "Керамогранит соль-перец 600х600" in html
    assert "Продавец ·" in html and "Возможно продавец ·" in html and "Не подтверждено ·" in html
    assert SECRET not in html


def test_standalone_page_without_a_search_shows_the_form_not_a_list_of_orders_first(panel):
    add_job()
    html = panel.get("/dashboard/web-suppliers").text
    assert "НАИМЕНОВАНИЕ" in html and "Найти</button>" in html
    assert "<details>" in html and html.index("НАИМЕНОВАНИЕ") < html.index("Поиск по позициям заявки")


def test_word_filters_keep_or_drop_sites(panel):
    location = manual_search(panel)
    run = run_of(location)
    html = panel.get(location).text
    assert "плиты" in html.split("<aside>")[1].split("</aside>")[0]     # слово, общее для двух найденных текстов
    only = panel.get("/dashboard/web-suppliers", params={"run": run, "inc": "керамогранит"}).text
    assert "tile-a.ru" in only and "tile-b.ru" not in only and "Сайтов: 1 / 3" in only
    without = panel.get("/dashboard/web-suppliers", params={"run": run, "exc": "гранит"}).text
    assert "tile-a.ru" in without and "tile-c.ru" in without and "tile-b.ru" not in without


def test_find_emails_then_choose_which_to_copy_and_exclude_a_site(panel):
    location = manual_search(panel)
    token_value = page_token(panel, location)
    run = run_of(location)

    def contacts(url, blocked=(), session=None):
        domain = sd.site_domain(url)
        return {"contacts": [{"kind": "email", "value": f"sales@{domain}"}, {"kind": "email", "value": f"info@{domain}"}], "errors": [], "url": url}
    with patch.object(sd, "read_domain_contacts", contacts):
        response = panel.post("/dashboard/web-suppliers/find-emails", data={"csrf": token_value, "run": run}, follow_redirects=False)
    html = panel.get(response.headers["location"]).text
    assert "Проверено сайтов 3, с e-mail 3" in html and "sales@tile-a.ru" in html and "Копировать Email · 6" in html
    # снимаем галочку у одного адреса и исключаем второй сайт
    token_value = page_token(panel, response.headers["location"])
    data = {"csrf": token_value, "run": run, "exclude_domain": "tile-b.ru",
            "listed": ["sales@tile-a.ru", "info@tile-a.ru", "sales@tile-b.ru", "info@tile-b.ru", "sales@tile-c.ru", "info@tile-c.ru"],
            "selected": ["sales@tile-a.ru", "sales@tile-c.ru", "info@tile-c.ru"]}
    saved = panel.post("/dashboard/web-suppliers/emails", data=data, follow_redirects=False)
    assert saved.status_code == 303
    html = panel.get(saved.headers["location"]).text
    assert "sales@tile-a.ru; sales@tile-c.ru; info@tile-c.ru" in html and "info@tile-a.ru;" not in html and "tile-b.ru</div>" not in html
    assert "Исключённые · 1" in html


def test_emails_route_needs_the_form_token(panel):
    assert panel.post("/dashboard/web-suppliers/emails", data={"listed": "a@b.ru"}).status_code in (400, 403)


def test_positions_show_quantity_and_unit(panel):
    order = {"name": "З", "orderItems": [{"goodName": "Хомут Ду 25", "count": 12, "unit": {"name": "шт"}},
                                         {"goodName": "Труба 57х3,5", "quantity": "40", "unit": "м"}, {"goodName": "Краска"}]}
    with pipeline._connect() as conn:
        job = conn.execute("INSERT INTO automation_jobs (dedupe_key,order_id,event_type,status,order_json,result_json,created_at,updated_at) "
                           "VALUES (?,?,?,?,?,?,?,?)", ("q", 2, "test", "ready", json.dumps(order), "{}", "now", "now")).lastrowid
    html = panel.get(f"/dashboard/web-suppliers?job_id={job}").text
    assert "<th>Кол-во</th>" in html and ">12 шт<" in html and ">40 м<" in html
    assert "<td style='white-space:nowrap'>—</td>" in html
    post(panel, "/dashboard/web-suppliers/groups", job, color_1=1, sel_1=1)
    assert "Хомут Ду 25 — 12 шт" in panel.get(f"/dashboard/web-suppliers?job_id={job}").text
