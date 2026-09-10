import os
from dataclasses import dataclass, asdict
from typing import Any

import requests


@dataclass
class SupplierQuote:
    supplier: str
    name: str
    sku: str | None = None
    article: str | None = None
    brand: str | None = None
    unit: str | None = None
    price: float | None = None
    base_price: float | None = None
    stock: float | None = None
    pickup_date: str | None = None
    courier_date: str | None = None
    url: str | None = None
    image_url: str | None = None
    country: str | None = None
    keyword: str | None = None
    breadcrumbs: list[str] | None = None
    technical_specifications: Any = None
    weight: float | None = None
    length: float | None = None
    width: float | None = None
    height: float | None = None
    price_type: str | None = None
    score: float | None = None
    error: str | None = None

    def to_dict(self):
        return asdict(self)


class SupplierAdapter:
    code = "base"
    name = "Base supplier"

    @property
    def enabled(self) -> bool:
        return False

    def status(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name, "enabled": self.enabled}

    def search(self, query: str, limit: int = 5) -> list[SupplierQuote]:
        raise NotImplementedError


class VseinstrumentiAdapter(SupplierAdapter):
    code = "vseinstrumenti"
    name = "ВсеИнструменты.ру"

    def __init__(self):
        self.token = os.getenv("VSEINSTRUMENTI_API_TOKEN", "").strip()
        self.base_url = os.getenv(
            "VSEINSTRUMENTI_API_BASE_URL",
            "https://api.vseinstrumenti.ru/open-api",
        ).rstrip("/")
        self.region_id = os.getenv(
            "VSEINSTRUMENTI_REGION_ID",
            "0c5b2444-70a0-4932-980c-b4dc0d3f02b5",
        ).strip()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.region_id)

    def status(self) -> dict[str, Any]:
        data = super().status()
        data.update({
            "region_id": self.region_id,
            "configured_token": bool(self.token),
            "base_url": self.base_url,
            "auth": "Bearer",
            "price_field": "prices.price",
            "price_semantics": "Цена ОПТ контрагента",
        })
        return data

    @staticmethod
    def _num(value):
        if value is None or value == "":
            return None
        try:
            return float(str(value).replace(" ", "").replace(",", "."))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _error_text(response: requests.Response) -> str:
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("error")
            details = payload.get("details")
            if isinstance(details, dict):
                detail_message = details.get("message")
                field = details.get("field")
                detail_bits = [str(x) for x in (field, detail_message) if x]
                if detail_bits:
                    return f"{message or 'Ошибка API'}: {'; '.join(detail_bits)}"
            if message:
                return str(message)
            return str(payload)
        text = (response.text or "").strip()
        return text[:1000] if text else "Пустой ответ API"

    @staticmethod
    def _extract_products(data: Any) -> list[dict[str, Any]]:
        """Support both the documented nested shape and the actual PROD top-level shape."""
        if not isinstance(data, dict):
            return []
        products = data.get("products")
        if isinstance(products, list):
            return products
        result = data.get("result")
        if isinstance(result, dict):
            products = result.get("products")
            if isinstance(products, list):
                return products
        return []

    def _request_products(self, query: str, limit: int = 5) -> requests.Response:
        limit = min(max(int(limit), 1), 40)
        url = f"{self.base_url}/v1/products"
        params = {
            "search": query,
            "regionId": self.region_id,
            "limit": limit,
            "offset": 0,
            "orderBy": "price",
            "sort": "asc",
        }
        return requests.get(
            url,
            params=params,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
            timeout=25,
        )

    def diagnose(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Safe OpenAPI diagnostics. Never returns Authorization/token contents."""
        query = (query or "").strip()
        base = {
            "supplier": self.name,
            "query": query,
            "configured_token": bool(self.token),
            "region_id": self.region_id,
            "base_url": self.base_url,
        }
        if not query:
            return {**base, "ok": False, "error": "Пустой поисковый запрос"}
        if not self.enabled:
            return {**base, "ok": False, "error": "Токен или regionId не настроен"}

        try:
            response = self._request_products(query, limit)
        except requests.RequestException as exc:
            return {**base, "ok": False, "error": f"Ошибка сети API ВИ: {exc}"}

        diagnostic: dict[str, Any] = {
            **base,
            "http_status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "ok": response.ok,
        }
        try:
            payload = response.json()
        except ValueError:
            diagnostic["body_type"] = "non_json"
            diagnostic["body_preview"] = (response.text or "")[:2000]
            return diagnostic

        diagnostic["body_type"] = type(payload).__name__
        if isinstance(payload, dict):
            diagnostic["top_level_keys"] = sorted(payload.keys())
            products = self._extract_products(payload)
            diagnostic["products_type"] = "list" if isinstance(products, list) else type(products).__name__
            diagnostic["products_count"] = len(products)
            diagnostic["products_preview"] = products[: min(len(products), 3)]
            result = payload.get("result")
            diagnostic["result_type"] = type(result).__name__ if result is not None else None
            if isinstance(result, dict):
                diagnostic["result_keys"] = sorted(result.keys())
            if not response.ok:
                diagnostic["api_error"] = self._error_text(response)
        else:
            diagnostic["payload_preview"] = payload
        return diagnostic

    def search(self, query: str, limit: int = 5) -> list[SupplierQuote]:
        query = (query or "").strip()
        if not query:
            return [SupplierQuote(supplier=self.name, name="", error="Пустой поисковый запрос")]
        if not self.enabled:
            return [SupplierQuote(
                supplier=self.name,
                name=query,
                error="Не настроен VSEINSTRUMENTI_API_TOKEN или VSEINSTRUMENTI_REGION_ID",
            )]

        limit = min(max(int(limit), 1), 40)
        try:
            response = self._request_products(query, limit)
        except requests.RequestException as exc:
            return [SupplierQuote(supplier=self.name, name=query, error=f"Ошибка сети API ВИ: {exc}")]

        if not response.ok:
            return [SupplierQuote(
                supplier=self.name,
                name=query,
                error=f"HTTP {response.status_code}: {self._error_text(response)}",
            )]

        try:
            data = response.json()
        except ValueError:
            return [SupplierQuote(
                supplier=self.name,
                name=query,
                error="API ВИ вернул не-JSON ответ",
            )]

        products = self._extract_products(data)
        result: list[SupplierQuote] = []
        for product in products[:limit]:
            prices = product.get("prices") or {}
            stock = product.get("stock") or {}
            delivery = product.get("deliveryDates") or {}
            dimensions = product.get("weightAndDimensions") or {}
            breadcrumbs = product.get("breadcrumbs") or []
            if not isinstance(breadcrumbs, list):
                breadcrumbs = [str(breadcrumbs)]

            result.append(SupplierQuote(
                supplier=self.name,
                name=product.get("name") or query,
                sku=str(product.get("sku")) if product.get("sku") is not None else None,
                article=product.get("productCode"),
                brand=product.get("brandName"),
                unit=product.get("unit"),
                price=self._num(prices.get("price")),
                base_price=self._num(prices.get("basePrice")),
                stock=self._num(stock.get("atWarehouse")),
                pickup_date=delivery.get("pickup"),
                courier_date=delivery.get("courier"),
                url=product.get("siteUrl"),
                image_url=product.get("ImageUrl") or product.get("imageUrl"),
                country=product.get("madeInCountry"),
                keyword=product.get("keyword"),
                breadcrumbs=[str(x) for x in breadcrumbs],
                technical_specifications=product.get("technicalSpecifications"),
                weight=self._num(dimensions.get("weight")),
                length=self._num(dimensions.get("length")),
                width=self._num(dimensions.get("width")),
                height=self._num(dimensions.get("height")),
                price_type="contractor_wholesale",
            ))
        return result


class DisabledAdapter(SupplierAdapter):
    def __init__(self, code: str, name: str, env_hint: str = ""):
        self.code = code
        self.name = name
        self.env_hint = env_hint

    @property
    def enabled(self) -> bool:
        return False

    def status(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name, "enabled": False, "note": self.env_hint or "Адаптер ожидает API-документацию/доступ"}

    def search(self, query: str, limit: int = 5) -> list[SupplierQuote]:
        return [SupplierQuote(supplier=self.name, name=query, error="Адаптер ещё не подключён")]


def get_supplier_adapters() -> list[SupplierAdapter]:
    return [
        VseinstrumentiAdapter(),
        DisabledAdapter("ozon", "Ozon", "Нужен официальный API/партнёрский доступ для цен"),
        DisabledAdapter("stroy_dvor", "Строительный двор", "Нужна документация API/прайс-фид"),
        DisabledAdapter("teharmatura", "Техарматура", "Нужна документация API/прайс-фид"),
    ]


def enabled_adapters() -> list[SupplierAdapter]:
    return [a for a in get_supplier_adapters() if a.enabled]


def supplier_statuses() -> list[dict[str, Any]]:
    return [a.status() for a in get_supplier_adapters()]


def vseinstrumenti_diagnostic(query: str, limit: int = 5) -> dict[str, Any]:
    return VseinstrumentiAdapter().diagnose(query, limit)


def compare_suppliers(query: str, limit_per_supplier: int = 5) -> dict[str, Any]:
    quotes: list[SupplierQuote] = []
    for adapter in enabled_adapters():
        quotes.extend(adapter.search(query, limit=limit_per_supplier))

    valid = [q for q in quotes if q.price is not None and not q.error]
    valid.sort(key=lambda q: q.price)
    errors = [q.to_dict() for q in quotes if q.error]

    return {
        "query": query,
        "suppliers": supplier_statuses(),
        "quotes": [q.to_dict() for q in valid],
        "errors": errors,
        "best": valid[0].to_dict() if valid else None,
    }
