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
