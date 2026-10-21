"""Read the managed KeramaHall catalogue; retrieval never refreshes stock dates."""
import hashlib
import io
import json
import os
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

ORIGIN = "https://keramahall-delivery.mehmans.chatgpt.site"
PROJECT_ID = "appgprj_6ad35e29facc8191a65ec9adde8ec75b"
SCHEMA = "keramahall.catalog.v1"


def configured():
    return bool(os.getenv("KERAMAHALL_CATALOG_API_KEY", "").strip())


def is_managed(data):
    return (data.get("managed_source") or {}).get("project_id") == PROJECT_ID


def _date(value):
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return str(value)
    except (ValueError, TypeError):
        pass
    raise ValueError("КерамаХолл вернул некорректную дату обновления данных.")


def _get(url, maximum, image=False):
    if not url.startswith(ORIGIN + "/api/v1/catalog"):
        raise ValueError("Недопустимый адрес API КерамаХолл.")
    key = os.getenv("KERAMAHALL_CATALOG_API_KEY", "").strip()
    if not key:
        raise ValueError("Не настроен API-ключ управляемого каталога КерамаХолл.")
    headers = {"Authorization": "Bearer " + key, "Accept": "image/jpeg" if image else "application/json"}
    access = os.getenv("KERAMAHALL_SITE_ACCESS_TOKEN", "").strip()
    if access:
        headers["OAI-Sites-Authorization"] = "Bearer " + access
    try:
        with requests.get(url, headers=headers, timeout=(10, 35), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise ValueError(f"API КерамаХолл не отдал данные (HTTP {response.status_code}).")
            expected = "image/jpeg" if image else "application/json"
            if not response.headers.get("Content-Type", "").startswith(expected):
                raise ValueError("API КерамаХолл вернул неверный формат ответа.")
            output = bytearray()
            for chunk in response.iter_content(65536):
                output.extend(chunk)
                if len(output) > maximum:
                    raise ValueError("Ответ API КерамаХолл превышает допустимый размер.")
            return bytes(output)
    except requests.RequestException:
        raise ValueError("Не удалось связаться с API КерамаХолл. Сохранённые версии не изменены.") from None


def sync_catalog():
    from porcelain import MAX_IMPORT, import_catalog, load_catalog
    raw = _get(ORIGIN + "/api/v1/catalog", 4 * 1024 * 1024)
    try:
        source = json.loads(raw)
        if source.get("schema_version") != SCHEMA or source.get("source", {}).get("project_id") != PROJECT_ID:
            raise ValueError("Неизвестный источник каталога КерамаХолл.")
        fingerprint = source["content_sha256"]
        if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise ValueError("Неверная версия каталога КерамаХолл.")
        products = source["products"]
        if not isinstance(products, list) or not 1 <= len(products) <= 10000:
            raise ValueError("API КерамаХолл не вернул товары.")
        _date(source.get("catalog_updated_at"))
        _date(source.get("stock_as_of"))
        seen = set()
        for p in products:
            key = p["id"]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", key) or key in seen or not p.get("name"):
                raise ValueError("Некорректные идентификаторы товаров КерамаХолл.")
            seen.add(key)
            # Never fall back to retrieved_at or the local clock for data validity.
            p["stock_as_of"] = _date(p.get("stock_updated_at"))
            p["price_updated_at"] = _date(p.get("price_updated_at"))
            if p.get("image_url") and p["image_url"] != ORIGIN + "/api/v1/catalog/images/" + key:
                raise ValueError("Недопустимый адрес фото КерамаХолл.")
            p["api_image_sha256"] = p.get("image_sha256")
            if p.get("image_url") and not re.fullmatch(r"[a-f0-9]{64}", p.get("api_image_sha256") or ""):
                raise ValueError("Не указана версия фото КерамаХолл.")
            p["image"] = "assets/" + key + ".jpg" if p.get("image_url") else None
    except (KeyError, TypeError, json.JSONDecodeError):
        raise ValueError("API КерамаХолл вернул неполный каталог.") from None
    previous = load_catalog()
    if previous and is_managed(previous["data"]) and previous["data"].get("source_fingerprint") == fingerprint:
        return {"id": previous["id"], "changed": False, "products": len(products)}
    cached = {}
    if previous:
        previous = load_catalog(previous["id"], with_archive=True)
        with zipfile.ZipFile(io.BytesIO(bytes(previous["archive_bytes"]))) as archive:
            for p in previous["data"]["products"]:
                if p.get("image") and p.get("api_image_sha256"):
                    cached[p["id"]] = (p["api_image_sha256"], archive.read(p["image"]))

    def photo(p):
        if not p.get("image"):
            return None
        prior = cached.get(p["id"])
        content = prior[1] if prior and prior[0] == p["api_image_sha256"] else _get(p["image_url"], 2 * 1024 * 1024, image=True)
        if not content.startswith(b"\xff\xd8") or hashlib.sha256(content).hexdigest() != p["api_image_sha256"]:
            raise ValueError("Фото КерамаХолл не соответствует версии каталога.")
        return p["image"], content

    data = {
        "products": products, "shipping": source.get("shipping") or {},
        "stock_as_of": source.get("stock_as_of"), "currency": source.get("currency"),
        "managed_source": source["source"], "source_fingerprint": fingerprint,
        "source_revision": source.get("revision"), "catalog_updated_at": source.get("catalog_updated_at"),
        "retrieved_at": source.get("retrieved_at"),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("catalog.json", json.dumps(data, ensure_ascii=False, allow_nan=False))
        total = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            for result in pool.map(photo, products):
                if result:
                    name, content = result
                    total += len(content)
                    if total > MAX_IMPORT:
                        raise ValueError("Слишком большой каталог фотографий КерамаХолл.")
                    archive.writestr(name, content)
    key = import_catalog(output.getvalue())
    return {"id": key, "changed": True, "products": len(products)}
