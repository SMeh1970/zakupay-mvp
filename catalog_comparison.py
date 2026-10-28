"""Bridge prepared product evidence to the comparison, without external calls.

Product corrections belong to the catalogue. Accept/reject decisions belong to
one request position; price/quantity confirmations belong to one quote version.
"""
import copy
import hashlib
import json
import math
from datetime import date, datetime, timezone

from product_characteristics import extract

ALIASES = {"Номинальный диаметр": "Условный проход", "Стандарт ГОСТ": "ГОСТ",
    "Стандарт ГОСТ Р": "ГОСТ Р", "Стандарт ТУ": "ТУ", "Длина": "Длина изделия",
    "Высота сечения": "Ширина профиля", "Ширина сечения": "Высота профиля"}


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(str(value).replace(",", "."))
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def label(value, unit=None):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = f"{value:g}"
    elif isinstance(value, bool):
        value = "Да" if value else "Нет"
    else:
        value = str(value)
    return value + (" " + unit if unit and unit != "DN" else "")


def records(name, candidate=None):
    """Stored structured data wins, including an explicitly empty result."""
    candidate = candidate or {}
    analysis = candidate.get("analysis")
    if isinstance(analysis, dict) and isinstance(analysis.get("attributes"), list):
        raw = analysis["attributes"]
    elif isinstance(candidate.get("characteristics"), list):
        raw = candidate["characteristics"]
    else:
        raw = extract(name)["attributes"]
    from product_identity import kind, records as identity_records
    identity = identity_records(name)
    product_kind = kind(name)
    typed_fields = {r["field"] for r in identity}
    result = []
    for a in raw:
        if not isinstance(a, dict) or not a.get("parameter"):
            continue
        field = ALIASES.get(a["parameter"], a["parameter"])
        if field in typed_fields or (product_kind == "наконечник" and field in {"Марка кабеля", "Число жил", "Сечение жилы"}):
            continue
        result.append(dict(field=field, value=label(a.get("value"), a.get("unit")),
            raw_value=a.get("value"), unit=a.get("unit"), evidence=str(a.get("evidence") or ""),
            origin=a.get("origin") or "name", ambiguous=bool(a.get("ambiguous") or a.get("needs_review"))))
    # Existing API responses still use finite additional comparison rules.
    # Never let those rules replace authoritative values from a prepared file.
    if not candidate.get("catalog_prepared"):
        from supplier_comparison import attributes
        known = {r["field"] for r in result}
        for field, values in attributes(name).items():
            if field in typed_fields or (product_kind in {"бита", "рулетка", "плоскогубцы", "очиститель пены", "монтажная пена", "гофрированная труба"} and field in {"Размер 1", "Размер 2", "Длина", "Ширина", "Толщина", "Наружный диаметр", "Толщина стенки"}):
                continue
            if extract(name)['category'] == 'Кабели' and field in {'Размер 1', 'Размер 2', 'Длина', 'Ширина', 'Толщина'}:
                continue
            field = ALIASES.get(field, field)
            if field == "Диаметр" and "Наружный диаметр" in known:
                continue
            if field not in known:
                result.extend(dict(field=field, value=v, raw_value=v, unit=None,
                    evidence=v, origin="name", ambiguous=False) for v in values)
    if candidate.get("brand") and not any(r["field"] == "Бренд" for r in result):
        result.append(dict(field="Бренд", value=str(candidate["brand"]), raw_value=candidate["brand"],
            unit=None, evidence=str(candidate["brand"]), origin="supplier", ambiguous=False))
    return result + identity


def without_evidence(name, attrs):
    text = str(name or "")
    for r in sorted(attrs, key=lambda a: -len(a["evidence"])):
        if r["origin"] == "name" and r["evidence"]:
            text = text.replace(r["evidence"], " ")
    return text


def fingerprint(candidate):
    if candidate.get("catalog_fingerprint"):
        return candidate["catalog_fingerprint"]
    keys = ("source_list_id", "source_sheet", "source_row", "original_price", "price_list_stock",
        "price_date", "warehouse", "unit", "characteristics")
    return hashlib.sha256(json.dumps([candidate.get(k) for k in keys], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def version_setting(state, field, key, candidate):
    setting = (state.get(field) or {}).get(key) or {}
    return setting if setting.get("fingerprint") == fingerprint(candidate) else {}


def effective_quote(candidate, state, key):
    quote = copy.deepcopy(candidate)
    if not candidate.get("catalog_prepared"):
        return quote
    terms = version_setting(state, "terms", key, candidate)
    if terms:
        quote.update({k: v for k, v in terms.items() if k != "fingerprint"})
    proposal = version_setting(state, "proposals", key, candidate)
    from supplier_comparison import unit
    if proposal and unit(proposal.get("unit")) == unit(quote.get("unit")):
        quote.update(offered_quantity=proposal["quantity"], quantity_confirmed=True,
            quantity_confirmed_at=proposal["confirmed_at"], quantity_evidence=proposal["note"])
    return quote


def confirm_terms(state, key, candidate, payload):
    if not candidate.get("catalog_prepared"):
        raise ValueError("Условия подтверждаются здесь только для подготовленного прайса")
    price, rate = finite(payload.get("price")), finite(payload.get("vat_rate"))
    mode = payload.get("vat_mode")
    supplied_unit = str(payload.get("unit") or "").strip()
    note = str(payload.get("note") or "").strip()
    when = str(payload.get("price_date") or "")
    if price is None or price <= 0 or payload.get("currency") != "RUB":
        raise ValueError("Подтвердите положительную цену в RUB")
    if mode not in {"included", "excluded", "exempt"} or rate is None or not 0 <= rate <= 100:
        raise ValueError("Подтвердите режим и ставку НДС")
    if mode == "exempt" and rate != 0:
        raise ValueError("Для продажи без НДС укажите ставку 0")
    try:
        valid_date = date.fromisoformat(when)
    except ValueError:
        raise ValueError("Укажите дату подтверждённой цены")
    if valid_date > date.today() or not supplied_unit or len(supplied_unit) > 50 or not note or len(note) > 2000:
        raise ValueError("Проверьте дату, единицу цены и основание подтверждения")
    gross = round(price * (1 + rate / 100), 6) if mode == "excluded" else price
    if not math.isfinite(gross):
        raise ValueError("Слишком большая цена")
    state.setdefault("terms", {})[key] = dict(fingerprint=fingerprint(candidate), price=gross,
        confirmed_raw_price=price, unit=supplied_unit, currency="RUB", vat_mode=mode, vat_rate=rate,
        price_date=when, pricing_ready=True, terms_evidence=note,
        terms_confirmed_at=datetime.now(timezone.utc).isoformat())


def confirm_proposal(state, key, candidate, payload):
    if not candidate.get("catalog_prepared"):
        raise ValueError("Количество подтверждается здесь для предложения из прайса")
    qty = finite(payload.get("quantity"))
    note = str(payload.get("note") or "").strip()
    if qty is None or qty < 0 or not note or len(note) > 2000:
        raise ValueError("Укажите количество поставщика и основание подтверждения")
    from supplier_comparison import unit
    effective = effective_quote(candidate, state, key)
    if unit(effective.get("unit")) in {"шт", "упак", "компл", "лист"} and not qty.is_integer():
        raise ValueError("Для целых изделий и упаковок укажите целое количество")
    state.setdefault("proposals", {})[key] = dict(fingerprint=fingerprint(candidate), quantity=qty,
        unit=effective.get("unit"), note=note, confirmed_at=datetime.now(timezone.utc).isoformat())


def hydrate_request(result, raw_order):
    if result.get("items"):
        return
    order = json.loads(raw_order or "{}") if isinstance(raw_order, str) else (raw_order or {})
    source = order.get("orderItems") or order.get("items") or []
    result["items"] = [dict(position=n, order_item_id=i.get("id"),
        requested_name=i.get("goodName") or i.get("name") or "", quantity=i.get("count"),
        unit=i["unit"].get("name") if isinstance(i.get("unit"), dict) else i.get("unit"),
        selected=None, candidates=[], rejected_candidates=[], decision="manual")
        for n, i in enumerate(source, 1) if not i.get("deleted")]
    result.setdefault("order_name", order.get("name"))
    result.setdefault("order_id", order.get("id"))


def load_offers(result, conn, position=None):
    """Explicit local search only. Keep original request/API candidates intact."""
    from prepared_catalog import search
    from supplier_comparison import activate, quote_key
    rows = result.get("items") or []
    if position is not None:
        rows = [r for r in rows if str(r.get("position")) == str(position)]
        if not rows:
            raise ValueError("Позиция не найдена")
    if len(rows) > 100:
        raise ValueError("Для заявки больше 100 строк запускайте подбор по отдельной позиции")
    total = 0
    for row in rows:
        source = row.get("comparison_source") or row
        matched = search(source.get("requested_name") or "", 12, include_conflicts=True, conn=conn)
        projected = []
        for item in matched["candidates"]:
            quote = copy.deepcopy(item)
            quote.update(catalog_prepared=True, characteristics=item["analysis"]["attributes"],
                category=item["analysis"]["category"], catalog_product_id=item["product_id"],
                sku=item.get("sku") if item.get("identity_status") == "supplier_sku" else f"row:{item['source_list_id']}:{item['source_sheet']}:{item['source_row']}",
                price_type="Прайс поставщика", stock=None)
            if quote.get("pricing_ready") and quote.get("vat_mode") in {"included", "excluded", "exempt"}:
                quote["currency"] = quote.get("currency") or "RUB"
            quote["catalog_fingerprint"] = fingerprint(quote)
            projected.append(quote)
        row["catalog_candidates"] = projected
        replacements = {quote_key(c): c for c in projected}
        for field in ("candidates", "rejected_candidates"):
            row[field] = [c for c in row.get(field, []) if not c.get("catalog_prepared") and quote_key(c) not in replacements]
        row["catalog_search"] = dict(at=datetime.now(timezone.utc).isoformat(), count=len(projected),
            seconds=matched["seconds"], category=matched["requirements"]["category"])
        selected = row.get("selected") or {}
        if selected.get("source_type") == "price_list" and not selected.get("catalog_prepared") and quote_key(selected) in replacements:
            row.setdefault("catalog_previous_selection", copy.deepcopy(selected))
            row["selected"] = replacements[quote_key(selected)]
            row.update(decision="excluded", operator_included=False)
            selected = row["selected"]
        if selected.get("catalog_prepared"):
            current = next((c for c in projected if quote_key(c) == quote_key(selected)), None)
            if not current or fingerprint(current) != fingerprint(selected):
                row["selected"] = current or {**selected, "catalog_unavailable": True}
                row.update(decision="excluded", operator_included=False)
        activate(row)
        total += len(projected)
    result["catalog_search"] = dict(positions=len(rows), candidates=total, at=datetime.now(timezone.utc).isoformat())
    return total
