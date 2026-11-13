"""Поиск профильных поставщиков в интернете. Цены не читаются и в расчёт не попадают.

Контракты SerpAPI (Google, Яндекс) и Firecrawl, правила отсева страниц, оценка «продавец»,
бюджет запросов и извлечение контактов перенесены из СнабПоиска v43 от 13.11.2026
(lib/network.ts, search-economy.ts, search-page-kind.ts, merchant.ts, contacts.ts).
Найденный поисковиком сайт — кандидат: профильность и работу с нами проверяет оператор.
"""
from __future__ import annotations

import base64
import ipaddress
import os
import re
import socket
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit, urlencode

import requests


SERPAPI_URL = "https://serpapi.com/search.json"
FIRECRAWL_SEARCH_URL = "https://api.firecrawl.dev/v2/search"
YANDEX_SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/web/searchAsync"
YANDEX_OPERATION_URL = "https://operation.api.cloud.yandex.net/operations/"
YANDEX_GROUPS_ON_PAGE = 100       # максимум для XML: по одному сайту (домену) на группу
YANDEX_MAX_RESULTS = 250          # предел Яндекса на один поисковый запрос
YANDEX_RETENTION = timedelta(hours=12)  # столько Яндекс хранит готовый результат
USER_AGENT = "ZakupayMVP/1.0 (supplier research)"
ENGINES = ("yandex", "google")
ECONOMY = {"auto": True, "min_pages": 4, "new_domain_percent": 15, "low_page_count": 2}

MARKETPLACES = (
    "ozon.ru", "wildberries.ru", "market.yandex.ru", "aliexpress.ru", "aliexpress.com", "avito.ru",
    "promportal.su", "pulscen.ru", "tiu.ru", "blizko.ru", "youtube.com", "wikipedia.org", "vk.com", "dzen.ru",
)
SOCIAL = (
    "vk.com", "vk.ru", "vkontakte.ru", "ok.ru", "odnoklassniki.ru", "youtube.com", "youtu.be", "rutube.ru", "dzen.ru",
    "zen.yandex.ru", "t.me", "telegram.me", "facebook.com", "instagram.com", "tiktok.com", "twitter.com", "x.com",
    "pinterest.com", "linkedin.com", "reddit.com", "vc.ru", "otvet.mail.ru",
)
_STORAGES = ("docs.google.com", "drive.google.com", "docs.yandex.ru", "disk.yandex.ru", "cloud.mail.ru")
# Регистрируемые домены третьего уровня: без этого «shop.com.ru» превратился бы в «com.ru».
_SECOND_LEVEL = {"com.ru", "net.ru", "org.ru", "spb.ru", "msk.ru", "pp.ru", "nov.ru", "ru.com", "com.ua", "co.uk"}


class ProviderError(Exception):
    """Ошибка внешнего сервиса; текст безопасен для показа (без ключей и ссылок)."""


class PendingOperation(Exception):
    """Отложенный запрос принят сервисом, результата ещё нет. Это не ошибка и не новый расход."""


# ---------------------------------------------------------------- адреса и домены

def site_domain(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    host = host[4:] if host.startswith("www.") else host
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def is_public_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if parts.scheme not in {"http", "https"} or parts.username or parts.password:
            return False
        if parts.port not in (None, 80, 443):
            return False
        if "." not in host or re.search(r"(?:^|\.)(localhost|local|internal|test|invalid)$", host):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return True
        return address.is_global
    except ValueError:
        return False


def canonical_url(value: str) -> str:
    parts = urlsplit(value)
    query = [(k, v) for k, vs in parse_qs(parts.query, keep_blank_values=True).items() for v in vs
             if not re.match(r"(utm_|yclid|gclid|fbclid)", k)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def domain_matches(url_or_domain: str, domain: str) -> bool:
    host = (urlsplit(url_or_domain).hostname or url_or_domain).lower().removeprefix("www.")
    return bool(host) and (host == domain or host.endswith("." + domain))


def excluded_by(url: str, domains) -> bool:
    return any(domain_matches(url, d) for d in domains)


def non_product_reason(url: str, title: str = "") -> str | None:
    """Известные нетоварные страницы. Сайт без цены сюда не попадает."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "Некорректный адрес"
    host = (parts.hostname or "").lower()
    path = unquote(parts.path).lower()
    if host in _STORAGES:
        return "Хранилище документов"
    if re.match(r"(?:forum|forums)\.", host):
        return "Форум"
    if any(host == d or host.endswith("." + d) for d in SOCIAL):
        return "Соцсеть, видеоплатформа или обсуждение"
    if re.search(r"\.(?:pdf|docx?|xlsx?|pptx?|rtf|txt|csv|od[ts]|zip|rar|7z|jpe?g|png)(?:$|[/.])", path):
        return "Документ или файл"
    if re.search(r"(?:^|/)(?:blog|blogs|news|novosti|articles?|stati|forum|forums|wiki|tenders?|zakupki|"
                 r"sertifikaty|certificates|gost|downloads?)(?:/|$)", path):
        return "Статья, новость, форум, закупка или документация"
    if re.search(r"(?:^|/)(?:cart|basket|checkout|login|register|privacy|policy|sitemap)(?:/|$)", path):
        return "Служебная страница"
    heading = title.strip()
    if re.match(r"(?:скачать\s|гост\s|отказное письмо|сертификат\s|как\s|почему\s|чем отличается|в чем разница|"
                r"своими руками|инструкция\s|новости\s|форум\s|тендер\s|закупка\s|рейтинг производителей|нормы расхода)",
                heading, re.I):
        return "Информационный материал или документ"
    if re.search(r"для чего (?:примен|использ)|своими руками|пошагов(?:ая|ое) инструкц", title, re.I):
        return "Информационный материал"
    return None


def merchant_assessment(title: str, description: str, url: str) -> tuple[str, str]:
    """seller / possible / unknown по признакам на найденной странице. Предварительная оценка."""
    text = f"{title} {description}".lower()
    path = unquote(urlsplit(url).path).lower()
    catalogue = bool(re.search(r"(?:^|/)(?:catalog|catalogue|katalog|product|products|product-category|"
                               r"product_cat|goods|shop)(?:/|$)", path))
    commerce = bool(re.search(r"купить|в корзину|заказать|запросить (?:цену|предложение)|интернет[- ]магазин|"
                              r"отдел продаж", text))
    if (catalogue and commerce) or (commerce and re.search(r"доставк|оплат|оптом|производител|поставщик", text)):
        return "seller", "Признаки продажи: каталог / заказ / доставка. Предварительно по найденной странице"
    if catalogue or commerce or re.search(r"производител|поставщик|дистрибьютор|прода[её]м|отгруз|завод|продажа", text):
        return "possible", "Есть признаки поставщика, но продажа не подтверждена"
    return "unknown", "Данных о продаже пока недостаточно"


_SIZE_JOINER = re.compile(r"(?<=\d)\s*[*×xXхХ]\s*(?=\d)")


def build_query(name: str, region: str = "", extra: str = "") -> str:
    """Название позиции для поисковика. Между числами размера ставится «х»; десятичная запятая и
    дроби вида 1/2 не меняются. Для Яндекса запрос короче 400 символов."""
    text = " ".join(_SIZE_JOINER.sub("х", p).strip() for p in (name, region, extra) if p and p.strip())
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 399:
        raise ValueError("Слишком длинный запрос: Яндекс принимает до 399 символов. Сократите название позиции.")
    return text


# ---------------------------------------------------------------- провайдеры

def provider_detail(data, secrets=()) -> str:
    error = data.get("error") if isinstance(data, dict) else None
    detail = error if isinstance(error, str) else (error or {}).get("message") if isinstance(error, dict) else ""
    detail = detail or (data.get("message") if isinstance(data, dict) and isinstance(data.get("message"), str) else "")
    for secret in filter(None, secrets):
        detail = detail.replace(secret, "[скрыто]")
    detail = re.sub(r"https?://[^\s<>\"']+", "[ссылка сервиса]", detail, flags=re.I)
    detail = re.sub(r"((?:api[_-]?key|token|authorization)\s*[=:]\s*)[^\s,;]+", r"\1[скрыто]", detail, flags=re.I)
    return re.sub(r"\s+", " ", re.sub(r"[\r\n\t<>]", " ", detail)).strip()[:450]


def provider_error(service: str, status: int, detail: str) -> str:
    if status == 401 or re.search(r"invalid api.?key|no valid api.?key", detail, re.I):
        action = "Ключ не принят. Проверьте ключ этого сервиса в настройках Render."
    elif status == 403:
        action = "Доступ к сервису запрещён. Проверьте состояние аккаунта и права ключа."
    elif status == 402 or re.search(r"run out of searches|no searches remaining|exhausted|insufficient credits|"
                                    r"credit balance", detail, re.I):
        action = "Лимит поиска или баланс исчерпан. Проверьте остаток в аккаунте сервиса; повтор сейчас не поможет."
    elif re.search(r"hourly|throughput|concurrent|rate limit", detail, re.I):
        action = "Превышена частота или параллельность запросов. Повторите поиск позже."
    elif status == 429:
        action = "Достигнут лимит сервиса: частота запросов или остаток поиска."
    elif status >= 500 or re.search(r"timeout|try again later|internal error", detail, re.I):
        action = "Временный сбой сервиса. Повторите поиск позже."
    elif status == 400:
        action = "Сервис отклонил параметры поиска."
    else:
        action = "Запрос не выполнен."
    return f"{service} (HTTP {status}): {action}" + (f" Ответ сервиса: {detail}" if detail else " Сервис не передал пояснение.")


def _empty_serp(data: dict) -> bool:
    info = data.get("search_information") or {}
    return ((data.get("search_metadata") or {}).get("status") == "Success"
            and not data.get("organic_results")
            and (str(info.get("organic_results_state", "")).lower() == "fully empty"
                 or re.search(r"has(?:n't| not) returned any results for this query", str(data.get("error", "")), re.I)))


def _next_serp_page(data: dict, engine: str, current: int) -> int | None:
    if engine == "firecrawl":
        return None
    param = "p" if engine == "yandex" else "start"
    links = []
    for block in (data.get("serpapi_pagination") or {}, data.get("pagination") or {}):
        links += [block.get("next"), block.get("next_link"), *(block.get("other_pages") or {}).values()]
    pages = []
    for link in links:
        if not isinstance(link, str):
            continue
        raw = parse_qs(urlsplit(link).query).get(param)
        if raw and raw[0].isdigit() and current < int(raw[0]) <= 10000:
            pages.append(int(raw[0]))
    return min(pages) if pages else None


def _json_request(method: str, url: str, *, service: str, secrets=(), timeout=90, session=requests, keep_error_body=False, **kwargs) -> dict:
    try:
        response = session.request(method, url, timeout=timeout, **kwargs)
    except requests.Timeout:
        raise ProviderError(f"{service}: истекло время ожидания ответа. Повторите поиск позже.") from None
    except requests.RequestException:
        raise ProviderError(f"{service}: не удалось соединиться с сервисом. Повторите запрос позже.") from None
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ProviderError(f"{service} (HTTP {response.status_code}): сервис вернул нечитаемый ответ. Повторите запрос позже.")
    if response.ok and (_empty_serp(data) or keep_error_body):
        return data
    if not response.ok or data.get("success") is False or data.get("error"):
        raise ProviderError(provider_error(service, response.status_code, provider_detail(data, secrets)))
    return data


def _shaped(raw: list, nxt, notice: str = "") -> dict:
    """Общий отсев выдачи: недопустимые адреса, страницы-не-товары и маркетплейсы."""
    counts = {"raw": len(raw), "invalid": 0, "excluded": 0}
    results = []
    for item in raw:
        if not item["url"] or not is_public_url(item["url"]):
            counts["invalid"] += 1
        elif non_product_reason(item["url"], item["title"]) or excluded_by(item["url"], MARKETPLACES):
            counts["excluded"] += 1
        else:
            results.append({"url": canonical_url(item["url"]), "title": re.sub(r"\s+", " ", item["title"]).strip()[:2000],
                            "description": re.sub(r"\s+", " ", item["description"]).strip()[:4000]})
    return {"results": results, "raw": raw, "next": nxt, "notice": notice, "diagnostics": counts}


def parse_yandex_xml(xml_bytes: bytes) -> tuple[list, int]:
    """Разбор XML-выдачи Яндекса: ([{url, title, description}], число групп на странице).

    Код ошибки 15 («ничего не найдено») — пустая выдача, а не сбой. Неизвестный формат — ошибка,
    а не молчаливая пустая выдача: иначе оператор решит, что сайтов нет.
    """
    if re.search(rb"<!(?:DOCTYPE|ENTITY)", xml_bytes, re.I):
        raise ProviderError("Яндекс Search API: ответ содержит недопустимые объявления XML и отклонён.")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        raise ProviderError("Яндекс Search API: не удалось разобрать XML-ответ.") from None
    for error in root.iter("error"):
        code = (error.get("code") or "").strip()
        if code == "15":
            return [], 0
        text = re.sub(r"\s+", " ", "".join(error.itertext())).strip()[:300]
        raise ProviderError(f"Яндекс Search API: ошибка {code or '?'}" + (f": {text}" if text else "."))
    if root.tag != "yandexsearch" and next(root.iter("response"), None) is None:
        raise ProviderError("Яндекс Search API: формат ответа не распознан.")
    docs = []
    for doc in root.iter("doc"):
        title = doc.find("title")
        headline = doc.find("headline")
        passages = [" ".join("".join(p.itertext()).split()) for p in doc.iter("passage")]
        docs.append({"url": (doc.findtext("url") or "").strip(),
                     "title": " ".join("".join(title.itertext()).split()) if title is not None else "",
                     "description": " ".join("".join(headline.itertext()).split()) if headline is not None and "".join(headline.itertext()).strip()
                     else " ".join(x for x in passages if x)})
    if not docs and b"<url" in xml_bytes:
        raise ProviderError("Яндекс Search API: формат ответа не распознан (адреса есть, документов не нашли).")
    groups = len(list(root.iter("group"))) or len(docs)
    return docs, groups


def yandex_cloud_page(page: int, query: str, *, key: str, folder: str, operations: dict, save=None,
                      session=requests, now=None) -> dict:
    """Одна страница выдачи через отложенный запрос Yandex Search API.

    Первый вызов создаёт операцию и сразу сохраняет её id (save): это единственный платный шаг.
    Следующие вызовы только спрашивают готовность (бесплатно) и поднимают PendingOperation, пока Яндекс готовит ответ.
    """
    if not key.strip() or not folder.strip():
        raise ProviderError("Для Яндекса нужны YANDEX_SEARCH_API_KEY и YANDEX_FOLDER_ID (настройки Render).")
    service = "Яндекс Search API"
    headers = {"Authorization": f"Api-Key {key.strip()}", "Content-Type": "application/json"}
    secrets = [key, folder]
    now = now or datetime.now(timezone.utc)
    slot = f"yandex_cloud:{page}"
    operation = operations.get(slot)
    if not operation:
        if len(query) >= 400 or len(query.split()) > 40:
            raise ProviderError("Яндекс принимает запрос до 400 символов и 40 слов. Сократите название позиции.")
        body = {"query": {"searchType": "SEARCH_TYPE_RU", "queryText": query, "page": str(page), "fixTypoMode": "FIX_TYPO_MODE_OFF"},
                "groupSpec": {"groupMode": "GROUP_MODE_DEEP", "groupsOnPage": str(YANDEX_GROUPS_ON_PAGE), "docsInGroup": "1"},
                "maxPassages": "2", "l10n": "LOCALIZATION_RU", "folderId": folder.strip(), "responseFormat": "FORMAT_XML"}
        data = _json_request("POST", YANDEX_SEARCH_URL, service=service, secrets=secrets, session=session, headers=headers, json=body)
        operation_id = data.get("id")
        if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{5,100}", operation_id):
            raise ProviderError(f"{service}: сервис не вернул идентификатор операции.")
        operations[slot] = {"id": operation_id, "started": now.isoformat()}
        if save:
            save()
        raise PendingOperation(operations[slot]["started"])
    try:
        started = datetime.fromisoformat(operation["started"])
    except (KeyError, ValueError):
        started = now
    try:
        data = _json_request("GET", YANDEX_OPERATION_URL + operation["id"], service=service, secrets=secrets, session=session,
                             headers={"Authorization": headers["Authorization"]}, keep_error_body=True)
    except ProviderError as exc:
        if "HTTP 404" in str(exc):
            operations.pop(slot, None)
            if save:
                save()
            raise ProviderError(f"{service}: операция не найдена (возможно, истёк срок хранения результата). Запустите поиск заново.") from None
        raise
    if not data.get("done"):
        if now - started > YANDEX_RETENTION:
            operations.pop(slot, None)
            if save:
                save()
            raise ProviderError(f"{service}: результат не получен за 12 часов. Запустите поиск заново.")
        raise PendingOperation(operation["started"])
    operations.pop(slot, None)
    if save:
        save()
    if data.get("error"):
        raise ProviderError(provider_error(service, 200, provider_detail(data, secrets)))
    encoded = (data.get("response") or {}).get("rawData")
    if not isinstance(encoded, str) or not encoded:
        raise ProviderError(f"{service}: в ответе нет данных выдачи.")
    try:
        xml_bytes = base64.b64decode(encoded, validate=False)
    except ValueError:
        raise ProviderError(f"{service}: данные выдачи не удалось декодировать.") from None
    docs, groups = parse_yandex_xml(xml_bytes)
    nxt = page + 1 if groups >= YANDEX_GROUPS_ON_PAGE and (page + 1) * YANDEX_GROUPS_ON_PAGE < YANDEX_MAX_RESULTS else None
    shaped = _shaped(docs, nxt)
    if not docs:
        shaped["notice"] = "Следующих результатов в поисковой выдаче нет." if page else "Поисковик не нашёл сайтов по этой фразе."
    return shaped


def search_page(engine: str, page: int, query: str, *, serpapi_key: str = "", firecrawl_key: str = "",
                limit: int = 100, exclude_domains=(), session=requests, yandex_key: str = "", yandex_folder: str = "",
                operations: dict | None = None, save=None) -> dict:
    """Одна страница выдачи: {results, raw, next, notice, diagnostics}. Ключи берутся из окружения Render."""
    notice = ""
    if engine == "yandex_cloud":
        return yandex_cloud_page(page, query, key=yandex_key, folder=yandex_folder,
                                 operations=operations if operations is not None else {}, save=save, session=session)
    if engine in ("google", "yandex"):
        if not serpapi_key.strip():
            raise ProviderError("Для Google и Яндекса нужен ключ SerpAPI (SERPAPI_API_KEY в настройках Render).")
        if engine == "yandex" and len(query) >= 400:
            raise ProviderError("Яндекс принимает запрос короче 400 символов. Сократите название позиции.")
        params = {"engine": engine, "api_key": serpapi_key.strip()}
        if engine == "yandex":
            params.update(text=query, p=page, lang="ru", yandex_domain="yandex.ru", groups_on_page=20, sort_mode="relevance")
        else:
            params.update(q=query, hl="ru", gl="ru", start=page)
        data = _json_request("GET", SERPAPI_URL, service="SerpAPI (поиск)", secrets=[serpapi_key], session=session, params=params)
        if _empty_serp(data):
            return {"results": [], "raw": [], "next": None, "diagnostics": {"raw": 0, "invalid": 0, "excluded": 0},
                    "notice": "Следующих результатов в поисковой выдаче нет." if page else "Поисковик не нашёл сайтов по этой фразе."}
        organic = data.get("organic_results")
        if not isinstance(organic, list):
            raise ProviderError("SerpAPI: в ответе нет organic_results. Это не подтверждённая пустая выдача.")
        raw = [{"url": x.get("link") or "", "title": x.get("title") or "", "description": x.get("snippet") or ""} for x in organic]
        nxt = _next_serp_page(data, engine, page)
    else:
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {firecrawl_key}"} if firecrawl_key else {})}
        body = {"query": query, "limit": min(100, max(1, limit)), "country": "ru", "sources": [{"type": "web"}],
                "excludeDomains": list(exclude_domains)}
        data = _json_request("POST", FIRECRAWL_SEARCH_URL, service="Firecrawl (поиск)", secrets=[firecrawl_key], session=session, headers=headers, json=body)
        found = data.get("data")
        found = found if isinstance(found, list) else (found or {}).get("web") or []
        raw = [{"url": x.get("url") or "", "title": x.get("title") or "", "description": x.get("description") or ""} for x in found]
        nxt = page + 1 if raw else None
        notice = "Firecrawl: продолжение исключает уже найденные домены; это не пагинация Firecrawl."
    return _shaped(raw, nxt, notice)


# ---------------------------------------------------------------- сбор с бюджетом

def new_state(query: str, engines) -> dict:
    return {"version": 1, "query": query, "engines": [e for e in engines if e in ("google", "yandex", "yandex_cloud", "firecrawl")],
            "cursors": {e: 0 for e in engines}, "completed": [], "sites": [], "raw_domains": [], "seen_domains": [],
            "requests": 0, "pages": 0, "duplicates": 0, "ignored": 0, "overflow": 0,
            "diagnostics": {"raw": 0, "invalid": 0, "excluded": 0}, "low_streaks": {}, "reason": "new",
            "errors": [], "pending": None, "operations": {}}


STOP_LABELS = {
    "new": "Поиск ещё не запускался", "paused": "Выполнено запрошенное число страниц; можно продолжить",
    "economy": "Две страницы подряд дали мало новых доменов", "credits": "Достигнут лимит запросов на позицию",
    "limit": "Достигнут лимит уникальных сайтов", "exhausted": "Доступная выдача закончилась",
    "error": "Ошибка сервиса; найденные результаты сохранены", "stopped": "Остановлено",
    "waiting": "Яндекс готовит результат: обычно от 5 минут. Нажмите «Забрать результат» позже",
}


def collect_sites(fetch_page, state: dict, *, limit: int = 100, max_requests: int = 20, max_pages: int = 3,
                  accept=None, on_state=None, economy=None) -> dict:
    """Выполняет не более max_pages страниц и возвращает состояние (его можно продолжить).

    Запрос резервируется в бюджете и сохраняется ДО обращения к провайдеру: перезагрузка
    страницы не сбрасывает расход. Неудачный запрос расход не возвращает и не считается страницей.
    """
    cfg = {**ECONOMY, **(economy or {})}
    s = state
    s["reason"], s["low_streaks"] = "paused", {}
    domains = {site_domain(x["url"]) for x in s["sites"]}
    raw_domains, seen_domains = set(s["raw_domains"]), set(s["seen_domains"])
    paused, errors, done, waiting = set(), [], 0, set()

    def persist():
        s["raw_domains"], s["seen_domains"] = sorted(raw_domains), sorted(seen_domains)
        if on_state:
            on_state(s)

    while done < max_pages and len(s["sites"]) < limit and any(s["cursors"].get(e) is not None and e not in paused for e in s["engines"]):
        progressed = False
        for engine in s["engines"]:
            page = s["cursors"].get(engine)
            if page is None or engine in paused or done >= max_pages:
                continue
            key = f"{engine}:{page}"
            if key in s["completed"]:
                raise RuntimeError("Повтор уже обработанной страницы заблокирован.")
            reserved = bool(s["pending"]) and s["pending"] == {"engine": engine, "page": page}
            if not reserved and s["requests"] >= max_requests:
                s["reason"] = "credits"
                persist()
                return s
            if not reserved:
                s["requests"] += 1
                s["pending"] = {"engine": engine, "page": page}
            persist()
            try:
                data = fetch_page(engine, page, s["query"], sorted(seen_domains))
            except PendingOperation:
                # Запрос уже учтён и сохранён: ожидание не стоит ничего, повторный клик его не удваивает.
                waiting.add(engine)
                paused.add(engine)
                persist()
                continue
            except ProviderError as exc:
                message = f"{engine}: {exc}"
                errors.append(message)
                s["errors"] = (s["errors"] + [message])[-50:]
                s["pending"] = None
                paused.add(engine)
                persist()
                continue
            raw = data.get("raw") or data["results"]
            raw_count = (data.get("diagnostics") or {}).get("raw", len(raw))
            new_raw = 0
            for link in raw:
                d = site_domain(link["url"])
                if d and d not in raw_domains:
                    raw_domains.add(d)
                    new_raw += 1
            for k in s["diagnostics"]:
                s["diagnostics"][k] += (data.get("diagnostics") or {}).get(k, raw_count if k == "raw" else 0)
            for result in data["results"]:
                d = site_domain(result["url"])
                if d:
                    seen_domains.add(d)
                if accept and not accept(result):
                    s["ignored"] += 1
                elif not d:
                    continue
                elif d in domains:
                    s["duplicates"] += 1
                elif len(s["sites"]) >= limit:
                    s["overflow"] += 1
                else:
                    domains.add(d)
                    s["sites"].append({**result, "engine": engine})
            s["pages"] += 1
            done += 1
            progressed = True
            s["completed"].append(key)
            s["pending"] = None
            nxt = data.get("next")
            s["cursors"][engine] = nxt if raw_count > 0 and nxt is not None and nxt > page else None
            low = raw_count > 0 and 100 * new_raw / raw_count < cfg["new_domain_percent"]
            s["low_streaks"]["_phrase"] = s["low_streaks"].get("_phrase", 0) + 1 if low else 0
            if cfg["auto"] and s["pages"] >= cfg["min_pages"] and s["low_streaks"]["_phrase"] >= cfg["low_page_count"]:
                s["reason"] = "economy"
                paused.update(s["engines"])
            persist()
            if len(s["sites"]) >= limit:
                break
        if not progressed:
            break
    if len(s["sites"]) >= limit:
        s["reason"] = "limit"
    elif waiting:
        s["reason"] = "waiting"
    elif s["reason"] == "economy":
        pass
    elif errors and done == 0:
        s["reason"] = "error"
    elif not any(s["cursors"].get(e) is not None for e in s["engines"]):
        s["reason"] = "exhausted"
    else:
        s["reason"] = "paused"
    persist()
    return s


# ---------------------------------------------------------------- контакты

_EMAIL = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", re.I)
_PHONE = re.compile(r"(?<![\d.])(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)")
_CONTACT_LABEL = re.compile(r"контакт|contact|обратная связь|отправить заявку|запросить предложени", re.I)
_FORM_CONTEXT = re.compile(r"заявк|обратн.*связ|задать.*вопрос|запрос|сообщени|расч[её]т|request|feedback|inquiry", re.I)


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.mailto, self.links, self.forms = [], [], [], []
        self._skip = 0
        self._link = None
        self._form = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag == "a" and a.get("href"):
            href = a["href"]
            if href.lower().startswith("mailto:"):
                self.mailto.append(unquote(href[7:].split("?")[0]))
            else:
                self._link = [href, ""]
        elif tag == "form":
            self._form = {"context": f"{a.get('id', '')} {a.get('class', '')}", "message": False, "inputs": 0, "password": False}
        elif self._form is not None and tag in ("input", "textarea"):
            kind, name = (a.get("type") or "text").lower(), (a.get("name") or "").lower()
            if kind == "password":
                self._form["password"] = True
            elif kind != "hidden":
                self._form["inputs"] += 1
            if tag == "textarea" or "message" in name or "comment" in name:
                self._form["message"] = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1
        elif tag == "a" and self._link:
            self.links.append(tuple(self._link))
            self._link = None
        elif tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None

    def handle_data(self, data):
        if self._skip:
            return
        self.text.append(data)
        if self._link is not None:
            self._link[1] += data
        if self._form is not None:
            self._form["context"] += " " + data


def extract_contacts(html: str, url: str) -> dict:
    """Почты, телефоны, формы заявки и ссылки на страницы контактов одного сайта."""
    parser = _Page()
    parser.feed(html or "")
    body = " ".join(parser.text) + " " + " ".join(parser.mailto)
    found, pages = [], []

    def add(kind, value):
        if not any(c["kind"] == kind and c["value"] == value for c in found):
            found.append({"kind": kind, "value": value, "source": url})

    for email in _EMAIL.findall(body):
        value = email.lower().rstrip(".,;")
        if len(value) <= 254 and not re.search(r"\.(png|jpe?g|webp|svg|gif)$", value):
            add("email", value)
    for phone in _PHONE.findall(body):
        add("phone", re.sub(r"\s+", " ", phone).strip())
    for form in parser.forms:
        if not form["password"] and form["inputs"] and (form["message"] or _FORM_CONTEXT.search(form["context"])):
            add("form", url)
    for href, label in parser.links:
        try:
            target = urljoin(url, href)
        except ValueError:
            continue
        if is_public_url(target) and site_domain(target) == site_domain(url) and \
                _CONTACT_LABEL.search(f"{label} {urlsplit(target).path}"):
            target = target.split("#")[0]
            add("contact", target)
            if target != url and target not in pages:
                pages.append(target)
    return {"contacts": found[:40], "pages": pages[:8]}


def merge_contacts(*groups) -> list:
    merged = {}
    for group in groups:
        for contact in group:
            merged.setdefault((contact["kind"], contact["value"]), contact)
    return list(merged.values())[:40]


def resolves_publicly(host: str) -> bool:
    """Все адреса, в которые превращается имя сайта, публичные (не локальная сеть и не служебные адреса).

    Проверка до запроса; подмену ответа DNS между проверкой и соединением она не исключает."""
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    return bool(addresses) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses)


def public_get(url: str, *, blocked=(), session=requests, max_bytes=1_000_000, timeout=18):
    """Чтение публичной страницы с проверкой каждого перехода. Возвращает (url, html) или бросает ProviderError."""
    current = url
    for _ in range(4):
        if excluded_by(current, blocked):
            raise ProviderError("Домен исключён.")
        if not is_public_url(current):
            raise ProviderError("Разрешены только публичные сайты.")
        if not resolves_publicly(urlsplit(current).hostname or ""):
            raise ProviderError("Адрес сайта не определился или указывает на закрытую сеть.")
        try:
            response = session.get(current, timeout=timeout, allow_redirects=False, stream=True,
                                   headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
        except requests.RequestException:
            raise ProviderError("Не удалось загрузить страницу.") from None
        if response.status_code in (301, 302, 303, 307, 308):
            current = urljoin(current, response.headers.get("location", ""))
            continue
        if response.status_code >= 400:
            raise ProviderError(f"Сайт вернул HTTP {response.status_code}.")
        if "html" not in response.headers.get("content-type", "html").lower():
            raise ProviderError("Это не страница сайта.")
        body = bytearray()
        for chunk in response.iter_content(65536):
            body += chunk
            if len(body) >= max_bytes:
                break
        encoding = "windows-1251" if "windows-1251" in response.headers.get("content-type", "").lower() else "utf-8"
        return current, bytes(body[:max_bytes]).decode(encoding, errors="replace")
    raise ProviderError("Слишком много перенаправлений.")


def read_domain_contacts(url: str, *, blocked=(), session=requests) -> dict:
    """Контакты с адреса сайта и не более двух его страниц контактов. Товар и цены не проверяются."""
    final, html = public_get(url, blocked=blocked, session=session)
    result = extract_contacts(html, final)
    contacts, errors = result["contacts"], []
    pages = [p for p in result["pages"] if not excluded_by(p, blocked)]
    if not any(c["kind"] == "email" for c in contacts):
        pages.append(urljoin(final, "/"))
    for page_url in list(dict.fromkeys(pages))[:2]:
        if page_url == final:
            continue
        if any(c["kind"] == "email" for c in contacts) and any(c["kind"] == "form" for c in contacts):
            break
        try:
            read_url, text = public_get(page_url, blocked=blocked, session=session)
            if site_domain(read_url) == site_domain(final):
                contacts = merge_contacts(contacts, extract_contacts(text, read_url)["contacts"])
        except ProviderError as exc:
            errors.append(str(exc))
    return {"contacts": contacts, "errors": errors, "url": final}


def yandex_env() -> tuple[str, str]:
    return os.getenv("YANDEX_SEARCH_API_KEY", "").strip(), os.getenv("YANDEX_FOLDER_ID", "").strip()


def env_keys() -> tuple[str, str]:
    return os.getenv("SERPAPI_API_KEY", "").strip(), os.getenv("FIRECRAWL_API_KEY", "").strip()
