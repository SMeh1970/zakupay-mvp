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
        self.base_url = os.getenv("VSEINSTRUMENTI_API_BASE_URL", "https://api.vseinstrumenti.ru/open-api").rstrip("/")
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
        })
        return data

    def search(self, query: str, limit: int = 5) -> list[SupplierQuote]:
        if not self.enabled:
            return [SupplierQuote(supplier=self.name, name=query, error="Не настроен VSEINSTRUMENTI_API_TOKEN")]

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
        try:
            response = requests.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
                timeout=25,
            )
            data = response.json()
        except Exception as exc:
            return [SupplierQuote(supplier=self.name, name=query, error=f"Ошибка API: {exc}")]

        if not response.ok:
            return [SupplierQuote(supplier=self.name, name=query, error=f"HTTP {response.status_code}: {data}")]

        products = ((data or {}).get("result") or {}).get("products") or []
        result = []
        for product in products[:limit]:
            prices = product.get("prices") or {}
            stock = product.get("stock") or {}
            delivery = product.get("deliveryDates") or {}

            def num(value):
                if value is None or value == "":
                    return None
                try:
                    return float(str(value).replace(" ", "").replace(",", "."))
                except (TypeError, ValueError):
                    return None

            result.append(SupplierQuote(
                supplier=self.name,
                name=product.get("name") or query,
                sku=str(product.get("sku")) if product.get("sku") is not None else None,
                article=product.get("productCode"),
                brand=product.get("brandName"),
                unit=product.get("unit"),
                price=num(prices.get("price")),
                base_price=num(prices.get("basePrice")),
                stock=num(stock.get("atWarehouse")),
                pickup_date=delivery.get("pickup"),
                courier_date=delivery.get("courier"),
                url=product.get("siteUrl"),
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
