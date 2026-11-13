import pytest

import supplier_discovery as sd


@pytest.fixture(autouse=True)
def offline_dns(monkeypatch):
    """Тесты не ходят в DNS: имя «internal-trap.ru» ведёт в закрытую сеть, остальные имена публичные."""
    monkeypatch.setattr(sd, "resolves_publicly", lambda host: host != "internal-trap.ru")


class Reply:
    def __init__(self, data=None, status=200, headers=None, body=b""):
        self._data, self.status_code, self.headers, self._body = data, status, headers or {}, body
        self.ok = status < 400

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data

    def iter_content(self, size):
        yield self._body


class Session:
    """Подставляет ответы по порядку и запоминает параметры запросов."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.replies.pop(0)

    get = lambda self, url, **kwargs: self.request("GET", url, **kwargs)  # noqa: E731


def serp(*links, nxt=None):
    data = {"search_metadata": {"status": "Success"},
            "organic_results": [{"link": u, "title": t, "snippet": s} for u, t, s in links]}
    if nxt is not None:
        data["serpapi_pagination"] = {"next": f"https://serpapi.com/search.json?engine=yandex&p={nxt}"}
    return Reply(data)


# ------------------------------------------------------------------ адреса

def test_domains_group_subdomains_and_keep_third_level_suffixes():
    assert sd.site_domain("https://www.example.ru/catalog") == "example.ru"
    assert sd.site_domain("https://shop.example.ru/") == "example.ru"
    assert sd.site_domain("https://magazin.com.ru/") == "magazin.com.ru"
    assert sd.site_domain("not a url") == ""


def test_only_public_addresses_are_allowed():
    for bad in ("http://localhost/x", "http://127.0.0.1/", "http://10.0.0.5/", "http://192.168.1.1/",
                "https://example.ru:8443/", "https://user:pw@example.ru/", "ftp://example.ru/", "https://intranet/"):
        assert not sd.is_public_url(bad), bad
    assert sd.is_public_url("https://example.ru/catalog")


def test_tracking_parameters_are_removed_but_the_rest_is_kept():
    assert sd.canonical_url("https://a.ru/p?id=5&utm_source=x&yclid=1#top") == "https://a.ru/p?id=5"


# ------------------------------------------------------------------ отсев страниц

@pytest.mark.parametrize("url,title", [
    ("https://forum.stroy.ru/t/1", ""), ("https://vk.com/club1", ""), ("https://a.ru/files/price.xlsx", ""),
    ("https://a.ru/blog/kak-vybrat", ""), ("https://a.ru/cart", ""), ("https://a.ru/x", "Как выбрать профиль"),
    ("https://docs.google.com/spreadsheets/d/1", ""), ("https://a.ru/x", "Профиль своими руками"),
])
def test_non_product_pages_are_rejected(url, title):
    assert sd.non_product_reason(url, title)


def test_a_supplier_without_price_is_not_rejected():
    assert sd.non_product_reason("https://stroy-profil.ru/catalog/profil/", "Профили для гипсокартона — купить") is None


def test_merchant_assessment_levels():
    assert sd.merchant_assessment("Профиль ПП купить оптом", "доставка по России", "https://a.ru/catalog/x")[0] == "seller"
    assert sd.merchant_assessment("Профиль ПП", "Мы производитель", "https://a.ru/")[0] == "possible"
    assert sd.merchant_assessment("О компании", "Наша история", "https://a.ru/about")[0] == "unknown"


def test_query_joins_size_numbers_with_one_letter_and_keeps_decimals_and_fractions():
    assert sd.build_query("Профиль потолочный 60*27") == "Профиль потолочный 60х27"
    assert sd.build_query("Саморез 4,2 x 16", "Москва") == "Саморез 4,2х16 Москва"
    assert sd.build_query("Труба 1/2 дюйма") == "Труба 1/2 дюйма"
    assert sd.build_query("Профиль 60/27") == "Профиль 60/27"
    with pytest.raises(ValueError):
        sd.build_query("слово " * 100)


# ------------------------------------------------------------------ провайдеры

def test_yandex_page_uses_the_documented_parameters_and_filters_results():
    session = Session(serp(("https://stroy-profil.ru/catalog/profil/?utm_source=ya", "Профили — купить", "доставка"),
                           ("https://www.ozon.ru/product/1", "Профиль", ""), ("https://vk.com/g", "Профиль", ""),
                           ("https://a.ru/price.pdf", "Прайс", ""), ("http://127.0.0.1/", "x", ""), nxt=1))
    page = sd.search_page("yandex", 0, "профиль 60х27", serpapi_key="K", session=session)
    params = session.calls[0][2]["params"]
    assert params["engine"] == "yandex" and params["text"] == "профиль 60х27" and params["p"] == 0
    assert params["lang"] == "ru" and params["yandex_domain"] == "yandex.ru" and params["groups_on_page"] == 20
    assert [r["url"] for r in page["results"]] == ["https://stroy-profil.ru/catalog/profil/"]
    assert page["diagnostics"] == {"raw": 5, "invalid": 1, "excluded": 3}
    assert page["next"] == 1 and len(page["raw"]) == 5


def test_google_page_uses_start_offset_and_russian_locale():
    session = Session(serp(("https://a-profil.ru/", "Профили", "")))
    sd.search_page("google", 10, "профиль", serpapi_key="K", session=session)
    params = session.calls[0][2]["params"]
    assert params["q"] == "профиль" and params["start"] == 10 and params["hl"] == "ru" and params["gl"] == "ru"


def test_missing_key_and_provider_errors_do_not_leak_the_key():
    with pytest.raises(sd.ProviderError, match="SERPAPI_API_KEY"):
        sd.search_page("yandex", 0, "профиль", serpapi_key="", session=Session())
    secret = "SECRET-123456"
    reply = Reply({"error": f"Invalid API key: api_key={secret} see https://serpapi.com/x"}, status=401)
    with pytest.raises(sd.ProviderError) as caught:
        sd.search_page("google", 0, "профиль", serpapi_key=secret, session=Session(reply))
    assert secret not in str(caught.value) and "serpapi.com" not in str(caught.value) and "HTTP 401" in str(caught.value)


def test_empty_result_is_reported_as_empty_not_as_error():
    empty = Reply({"search_metadata": {"status": "Success"}, "search_information": {"organic_results_state": "Fully empty"}})
    page = sd.search_page("yandex", 0, "несуществующее", serpapi_key="K", session=Session(empty))
    assert page["results"] == [] and page["next"] is None and "не нашёл" in page["notice"]


def test_response_without_organic_results_is_not_taken_for_an_empty_search():
    with pytest.raises(sd.ProviderError, match="organic_results"):
        sd.search_page("yandex", 0, "профиль", serpapi_key="K",
                       session=Session(Reply({"search_metadata": {"status": "Success"}})))


def test_firecrawl_search_sends_the_excluded_domains_without_a_key():
    reply = Reply({"success": True, "data": {"web": [{"url": "https://a-profil.ru/", "title": "Профили", "description": ""}]}})
    session = Session(reply)
    page = sd.search_page("firecrawl", 0, "профиль", exclude_domains=["bad.ru"], session=session)
    sent = session.calls[0][2]
    assert sent["json"]["excludeDomains"] == ["bad.ru"] and "Authorization" not in sent["headers"]
    assert page["results"][0]["url"] == "https://a-profil.ru/" and page["next"] == 1


# ------------------------------------------------------------------ бюджет и сбор

def pages(*batches):
    """fetch_page, отдающий по одной странице на вызов."""
    queue = list(batches)
    calls = []

    def fetch(engine, page, query, seen):
        calls.append((engine, page, tuple(seen)))
        urls, nxt = queue.pop(0)
        results = [{"url": u, "title": "t", "description": ""} for u in urls]
        return {"results": results, "raw": results, "next": nxt, "diagnostics": {"raw": len(results), "invalid": 0, "excluded": 0}}
    fetch.calls = calls
    return fetch


def test_sites_are_unique_by_domain_across_engines_and_pages():
    state = sd.new_state("профиль", ["yandex", "google"])
    fetch = pages((["https://a.ru/1", "https://www.a.ru/2", "https://b.ru/"], 1), (["https://a.ru/3", "https://c.ru/"], 1))
    sd.collect_sites(fetch, state, max_pages=2)
    assert [site_domain for site_domain in (sd.site_domain(x["url"]) for x in state["sites"])] == ["a.ru", "b.ru", "c.ru"]
    assert state["duplicates"] == 2 and [x["engine"] for x in state["sites"]] == ["yandex", "yandex", "google"]


def test_budget_is_reserved_before_the_request_and_stops_the_search():
    state = sd.new_state("профиль", ["yandex"])
    seen = []
    fetch = pages((["https://a.ru/"], 1), (["https://b.ru/"], 2))

    def on_state(s):
        seen.append((s["requests"], s["pending"]))
    sd.collect_sites(fetch, state, max_pages=5, max_requests=2, on_state=on_state)
    assert (1, {"engine": "yandex", "page": 0}) in seen   # расход записан до ответа провайдера
    assert state["requests"] == 2 and state["reason"] == "credits" and len(state["sites"]) == 2


def test_unfinished_request_after_a_crash_is_not_charged_twice():
    state = sd.new_state("профиль", ["yandex"])
    state["requests"], state["pending"] = 1, {"engine": "yandex", "page": 0}
    sd.collect_sites(pages((["https://a.ru/"], None)), state, max_pages=1, max_requests=1)
    assert state["requests"] == 1 and state["pending"] is None and state["reason"] == "exhausted"


def test_search_pauses_after_the_requested_pages_and_can_continue():
    state = sd.new_state("профиль", ["yandex"])
    fetch = pages((["https://a.ru/"], 1), (["https://b.ru/"], 2), (["https://c.ru/"], None))
    sd.collect_sites(fetch, state, max_pages=1)
    assert state["reason"] == "paused" and state["cursors"] == {"yandex": 1}
    sd.collect_sites(fetch, state, max_pages=5)
    assert state["reason"] == "exhausted" and len(state["sites"]) == 3 and fetch.calls[-1][1] == 2


def test_low_yield_stops_the_search_to_save_requests():
    state = sd.new_state("профиль", ["yandex"])
    same = ["https://a.ru/", "https://b.ru/"]
    fetch = pages((same, 1), (same, 2), (same, 3), (same, 4), (same, 5), (same, 6))
    sd.collect_sites(fetch, state, max_pages=10, max_requests=20)
    assert state["reason"] == "economy" and state["pages"] == 4 and len(fetch.calls) == 4


def test_provider_error_keeps_found_sites_and_does_not_count_as_a_page():
    state = sd.new_state("профиль", ["yandex", "google"])
    calls = []

    def fetch(engine, page, query, seen):
        calls.append(engine)
        if engine == "google":
            raise sd.ProviderError("Лимит исчерпан")
        r = [{"url": "https://a.ru/", "title": "t", "description": ""}]
        return {"results": r, "raw": r, "next": None, "diagnostics": {"raw": 1, "invalid": 0, "excluded": 0}}
    sd.collect_sites(fetch, state, max_pages=3)
    assert [x["url"] for x in state["sites"]] == ["https://a.ru/"] and state["pages"] == 1
    assert state["errors"] == ["google: Лимит исчерпан"] and state["requests"] == 2


def test_site_limit_stops_the_collection():
    state = sd.new_state("профиль", ["yandex"])
    fetch = pages((["https://a.ru/", "https://b.ru/", "https://c.ru/"], 1))
    sd.collect_sites(fetch, state, limit=2, max_pages=3)
    assert len(state["sites"]) == 2 and state["reason"] == "limit" and state["overflow"] == 1


# ------------------------------------------------------------------ контакты

PAGE = """<html><body>
<script>var x = "hidden@script.ru"</script>
<p>Отдел продаж: <a href="mailto:Sales@Stroy-Profil.ru?subject=1">написать</a>, +7 (495) 123-45-67</p>
<p>logo@2x.png info@stroy-profil.ru.</p>
<a href="/contacts/">Контакты</a><a href="https://other.ru/contacts/">Контакты</a>
<form id="request-form"><input name="name"><textarea name="message"></textarea></form>
<form><input name="login"><input type="password" name="p"></form>
</body></html>"""


def test_contacts_are_extracted_from_text_mailto_forms_and_same_site_links():
    result = sd.extract_contacts(PAGE, "https://stroy-profil.ru/")
    values = {(c["kind"], c["value"]) for c in result["contacts"]}
    assert ("email", "sales@stroy-profil.ru") in values and ("email", "info@stroy-profil.ru") in values
    assert not any(v == "hidden@script.ru" or v.endswith(".png") for _, v in values)
    assert ("phone", "+7 (495) 123-45-67") in values
    assert ("form", "https://stroy-profil.ru/") in values and sum(k == "form" for k, _ in values) == 1
    assert result["pages"] == ["https://stroy-profil.ru/contacts/"]


def test_domain_contacts_read_the_contact_page_and_refuse_private_redirects():
    home = Reply(headers={"content-type": "text/html"}, body="<a href='/contacts/'>Контакты</a>".encode())
    contacts = Reply(headers={"content-type": "text/html"}, body="<p>zakaz@stroy-profil.ru</p>".encode())
    session = Session(home, contacts)
    result = sd.read_domain_contacts("https://stroy-profil.ru/", session=session)
    assert [(c["kind"], c["value"]) for c in result["contacts"] if c["kind"] == "email"] == [("email", "zakaz@stroy-profil.ru")]

    redirect = Reply(status=302, headers={"location": "http://127.0.0.1/admin"})
    with pytest.raises(sd.ProviderError, match="публичные"):
        sd.read_domain_contacts("https://stroy-profil.ru/", session=Session(redirect))
    with pytest.raises(sd.ProviderError, match="исключён"):
        sd.read_domain_contacts("https://stroy-profil.ru/", blocked=["stroy-profil.ru"], session=Session())


def test_a_site_name_that_resolves_to_a_private_network_is_not_read():
    with pytest.raises(sd.ProviderError, match="закрытую сеть"):
        sd.read_domain_contacts("https://internal-trap.ru/", session=Session())


def test_real_name_resolution_rejects_private_addresses(monkeypatch):
    monkeypatch.undo()
    import socket
    answers = {"public.example": ["93.184.216.34"], "mixed.example": ["93.184.216.34", "10.0.0.7"], "meta.example": ["169.254.169.254"]}
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: [(2, 1, 6, "", (a, 0)) for a in answers[host]])
    assert sd.resolves_publicly("public.example") is True
    assert sd.resolves_publicly("mixed.example") is False and sd.resolves_publicly("meta.example") is False


# ---------------------------------------------------------------- Яндекс Search API, отложенный запрос

import base64
import json as _json
from datetime import datetime, timedelta, timezone


def yandex_xml(*docs, groups=None, error=None):
    """XML-выдача в формате Яндекса: yandexsearch/response/results/grouping/group/doc."""
    if error is not None:
        return f'<?xml version="1.0" encoding="utf-8"?><yandexsearch><response><error code="{error[0]}">{error[1]}</error></response></yandexsearch>'.encode()
    body = "".join(
        f"<group><doccount>1</doccount><doc><url>{u}</url><domain>d</domain><title>{t}</title>"
        f"<headline>{h}</headline><passages><passage>{h}</passage></passages></doc></group>" for u, t, h in docs)
    return ('<?xml version="1.0" encoding="utf-8"?><yandexsearch><request/><response><results><grouping>' + body +
            "</grouping></results></response></yandexsearch>").encode()


def done_reply(xml):
    return Reply({"id": "op1234567", "done": True, "response": {"rawData": base64.b64encode(xml).decode()}})


KEY, FOLDER = "AQVN-SECRET-KEY", "b1gfolder0000"


def start(session, ops, **kw):
    return sd.yandex_cloud_page(0, "профиль 60х27", key=KEY, folder=FOLDER, operations=ops, session=session, **kw)


def test_yandex_first_call_creates_the_operation_and_saves_its_id_before_waiting():
    ops, saved = {}, []
    session = Session(Reply({"id": "op1234567", "done": False}))
    with pytest.raises(sd.PendingOperation):
        start(session, ops, save=lambda: saved.append(dict(ops)))
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("POST", sd.YANDEX_SEARCH_URL)
    assert kwargs["headers"]["Authorization"] == f"Api-Key {KEY}"
    body = kwargs["json"]
    assert body["folderId"] == FOLDER and body["query"]["queryText"] == "профиль 60х27" and body["responseFormat"] == "FORMAT_XML"
    assert body["groupSpec"] == {"groupMode": "GROUP_MODE_DEEP", "groupsOnPage": "100", "docsInGroup": "1"}
    assert ops["yandex_cloud:0"]["id"] == "op1234567" and saved and saved[0]["yandex_cloud:0"]["id"] == "op1234567"


def test_yandex_waiting_polls_for_free_and_never_starts_a_second_operation():
    ops = {"yandex_cloud:0": {"id": "op1234567", "started": datetime.now(timezone.utc).isoformat()}}
    session = Session(Reply({"id": "op1234567", "done": False}), Reply({"id": "op1234567", "done": False}))
    for _ in range(2):
        with pytest.raises(sd.PendingOperation):
            start(session, ops)
    assert [c[0] for c in session.calls] == ["GET", "GET"] and session.calls[0][1] == sd.YANDEX_OPERATION_URL + "op1234567"
    assert ops["yandex_cloud:0"]["id"] == "op1234567"


def test_yandex_done_operation_is_parsed_and_removed():
    ops = {"yandex_cloud:0": {"id": "op1234567", "started": datetime.now(timezone.utc).isoformat()}}
    xml = yandex_xml(("https://stroy-profil.ru/catalog/", "Профили <hlword>60х27</hlword> — купить", "Купить профиль с доставкой"),
                     ("https://ozon.ru/p/1", "Профиль на Ozon", "marketplace"),
                     ("not a url", "мусор", ""))
    page = start(Session(done_reply(xml)), ops)
    assert ops == {}
    assert [r["url"] for r in page["results"]] == ["https://stroy-profil.ru/catalog/"]
    assert page["results"][0]["title"] == "Профили 60х27 — купить" and page["results"][0]["description"] == "Купить профиль с доставкой"
    assert page["diagnostics"] == {"raw": 3, "invalid": 1, "excluded": 1} and page["next"] is None


def test_yandex_full_page_has_a_next_page_until_the_250_result_limit():
    docs = [(f"https://shop{i}.ru/", f"Магазин {i}", "купить") for i in range(100)]
    for page_number, expected in ((0, 1), (1, 2), (2, None)):
        ops = {f"yandex_cloud:{page_number}": {"id": "op1234567", "started": datetime.now(timezone.utc).isoformat()}}
        page = sd.yandex_cloud_page(page_number, "q", key=KEY, folder=FOLDER, operations=ops, session=Session(done_reply(yandex_xml(*docs))))
        assert page["next"] == expected


def test_yandex_nothing_found_is_an_empty_page_not_an_error():
    ops = {"yandex_cloud:0": {"id": "op1234567", "started": datetime.now(timezone.utc).isoformat()}}
    page = start(Session(done_reply(yandex_xml(error=(15, "Искомая комбинация слов нигде не встречается")))), ops)
    assert page["results"] == [] and page["next"] is None and "не нашёл" in page["notice"]


@pytest.mark.parametrize("xml, text", [
    (yandex_xml(error=(32, "Превышен дневной лимит")), "ошибка 32"),
    (b"<html><body>captcha</body></html>", "формат ответа не распознан"),
    (b"<yandexsearch><response><results><url>http://a.ru</url></results></response></yandexsearch>", "формат ответа не распознан"),
    (b"<!DOCTYPE x [<!ENTITY a 'b'>]><yandexsearch/>", "недопустимые объявления"),
    (b"<yandexsearch><response>", "не удалось разобрать"),
])
def test_yandex_unknown_or_broken_answer_is_an_error_never_a_silent_empty_result(xml, text):
    ops = {"yandex_cloud:0": {"id": "op1234567", "started": datetime.now(timezone.utc).isoformat()}}
    with pytest.raises(sd.ProviderError, match=text):
        start(Session(done_reply(xml)), ops)


def test_yandex_failed_operation_is_dropped_so_the_next_click_does_not_poll_it_forever():
    ops = {"yandex_cloud:0": {"id": "op1234567", "started": datetime.now(timezone.utc).isoformat()}}
    failed = Reply({"id": "op1234567", "done": True, "error": {"code": 13, "message": f"internal {KEY}"}})
    with pytest.raises(sd.ProviderError) as caught:
        start(Session(failed), ops)
    assert ops == {} and KEY not in str(caught.value)


def test_yandex_unknown_or_expired_operation_is_dropped():
    old = (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat()
    ops = {"yandex_cloud:0": {"id": "op1234567", "started": old}}
    with pytest.raises(sd.ProviderError, match="12 часов"):
        start(Session(Reply({"id": "op1234567", "done": False})), ops)
    assert ops == {}
    ops = {"yandex_cloud:0": {"id": "op1234567", "started": datetime.now(timezone.utc).isoformat()}}
    with pytest.raises(sd.ProviderError, match="не найдена"):
        start(Session(Reply({"code": 5, "message": "not found"}, status=404)), ops)
    assert ops == {}


def test_yandex_needs_both_keys_and_a_short_query_and_hides_the_key_in_http_errors():
    with pytest.raises(sd.ProviderError, match="YANDEX_SEARCH_API_KEY"):
        sd.yandex_cloud_page(0, "q", key="", folder=FOLDER, operations={}, session=Session())
    with pytest.raises(sd.ProviderError, match="40 слов"):
        sd.yandex_cloud_page(0, " ".join(["слово"] * 41), key=KEY, folder=FOLDER, operations={}, session=Session())
    with pytest.raises(sd.ProviderError) as caught:
        start(Session(Reply({"code": 16, "message": f"bad key {KEY} at https://x.example/y"}, status=401)), {})
    assert KEY not in str(caught.value) and "https://" not in str(caught.value)


def test_collect_sites_waits_without_charging_twice_and_finishes_on_the_second_click():
    ops_session = Session(Reply({"id": "op1234567", "done": False}), Reply({"id": "op1234567", "done": False}),
                          done_reply(yandex_xml(("https://stroy-profil.ru/catalog/", "Профили — купить", "купить"))))
    state = sd.new_state("профиль 60х27", ["yandex_cloud"])

    def fetch(engine, page, query, seen):
        return sd.search_page(engine, page, query, yandex_key=KEY, yandex_folder=FOLDER, operations=state["operations"], session=ops_session)

    sd.collect_sites(fetch, state, max_requests=6, max_pages=2)
    assert state["reason"] == "waiting" and state["requests"] == 1 and state["pending"] == {"engine": "yandex_cloud", "page": 0}
    sd.collect_sites(fetch, state, max_requests=6, max_pages=2)           # ещё не готово
    assert state["reason"] == "waiting" and state["requests"] == 1
    sd.collect_sites(fetch, state, max_requests=6, max_pages=2)           # готово
    assert state["requests"] == 1 and [s["url"] for s in state["sites"]] == ["https://stroy-profil.ru/catalog/"]
    assert state["reason"] == "exhausted" and state["operations"] == {} and state["pending"] is None
    assert [c[0] for c in ops_session.calls] == ["POST", "GET", "GET"]
