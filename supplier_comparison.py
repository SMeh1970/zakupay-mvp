"""Saved procurement matrix. Decisions apply to values, never supplier wording alone.

No supplier requests on GET. All mutations use a session-bound CSRF token and a
compare-and-set on the saved result. The original request and quotes are immutable.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import html
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

import security

ASSETS = Path(__file__).with_name("comparison_assets")
N = r"\d+(?:[.,]\d+)?"


def norm(value):
    return " ".join(str(value or "").lower().replace("ё", "е").split())


def number(value):
    try:
        value = float(str(value).replace(" ", "").replace(",", "."))
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def unit(value):
    value = re.sub(r"[.\s]", "", norm(value).replace("²", "2").replace("³", "3"))
    return {"погм": "м", "метр": "м", "метры": "м", "мп": "м", "пм": "м",
            "штука": "шт", "штук": "шт", "штуки": "шт", "ед": "шт",
            "уп": "упак", "упаковка": "упак", "квм": "м2", "кубм": "м3",
            "тонна": "т", "тонн": "т", "килограмм": "кг", "литр": "л"}.get(value, value)


def feature_id(field, value):
    return hashlib.sha256((field + "\0" + norm(value)).encode()).hexdigest()[:24]


def attributes(name):
    """Finite extraction: dimensions, standards and common categorical attributes.

    Values are typed by parameter: a 2 mm wall never rejects another 2 mm field.
    Unparsed name differences are exposed separately as exact whole-word rules.
    """
    text = str(name or "")
    values = {}

    def add(field, value):
        values.setdefault(field, set()).add(str(value))

    dims = re.search(r"(?<!\d)(" + N + r")\s*[xх×*]\s*(" + N + r")(?:\s*[xх×*]\s*(" + N + r"))?\s*(мм|см|м)?", text, re.I)
    if dims:
        factor = {"см": 10, "м": 1000}.get((dims[4] or "").lower(), 1)
        nums = [number(dims[i]) * factor for i in (1, 2, 3) if dims[i]]
        if re.search(r"труб", text, re.I):
            fields = ["Ширина профиля", "Высота профиля", "Толщина стенки"] if len(nums) == 3 else ["Наружный диаметр", "Толщина стенки"]
        else:
            fields = ["Длина", "Ширина", "Толщина"] if len(nums) == 3 else ["Размер 1", "Размер 2"]
        for field, value in zip(fields, nums):
            add(field, f"{value:g} мм")
    for field, pattern in [
        ("Толщина стенки" if re.search("труб", text, re.I) else "Толщина", r"(?:толщин\w*|стенка|\bs\s*=)\s*(" + N + r")\s*(мм|см)"),
        ("Диаметр", r"(?:диаметр\w*|[Ø⌀фФ]|\bd\s*=)\s*(" + N + r")\s*(мм|см)"),
        ("Длина изделия", r"(?:длина\s*|\b[lL]\s*=)\s*(" + N + r")\s*(мм|см|м)\b"),
    ]:
        for match in re.finditer(pattern, text, re.I):
            add(field, f"{number(match[1]) * {'мм': 1, 'см': 10, 'м': 1000}[match[2].lower()]:g} мм")
    for match in re.finditer(r"\b(ГОСТ(?:\s+Р)?|ТУ)\s*([\d][\w./–-]*)", text, re.I):
        add(match[1].upper(), match[2].upper().replace("–", "-"))
    for match in re.finditer(r"\b(?:ст(?:аль|\.)?\s*)(\d+[а-яa-z]*)(?!\w)", text, re.I):
        add("Марка стали", match[1].upper())
    for match in re.finditer(r"\b(?:Ду|DN)\s*(" + N + r")", text, re.I):
        add("Условный проход", f"{number(match[1]):g}")
    for match in re.finditer(r"\bRAL\s*(\d{4})", text, re.I):
        add("Цвет RAL", match[1])
    for field, patterns in {
        "Цвет": {"Белый": r"\bбел(?:ый|ая|ое|ые)\b", "Чёрный": r"\bч[её]рн(?:ый|ая|ое|ые)\b", "Серый": r"\bсер(?:ый|ая|ое|ые)\b"},
        "Материал": {"Нержавеющая сталь": r"нержаве", "Алюминий": r"алюмин", "ПВХ": r"\bПВХ\b", "Полипропилен": r"полипропилен|\bПП\b"},
        "Поверхность": {"Матовая": r"матов", "Полированная": r"(?<!не)полирован", "Ламинированная": r"ламиниров"},
        "Покрытие": {"Оцинкованное": r"оцинков", "ВУС 2 слоя": r"2\s*ВУС|2[-\s]*(?:х\s*)?(?:слоя|слойн)", "ВУС 3 слоя": r"3\s*ВУС|3[-\s]*(?:х\s*)?(?:слоя|слойн)"},
        "Тип трубы": {"Бесшовная": r"бесшов|б/ш", "Электросварная": r"электросвар|э/с"},
        "Рисунок": {"Мрамор": r"под мрамор", "Дерево": r"под дерево"},
    }.items():
        for value, pattern in patterns.items():
            if re.search(pattern, text, re.I):
                add(field, value)
    return {field: sorted(v) for field, v in values.items()}


def quote_key(candidate):
    from automation_pipeline import _candidate_key
    return _candidate_key(candidate)


def lexical_tokens(name):
    """Don't ask twice about a dimension already represented by a typed field."""
    text = str(name or "")
    patterns = [
        r"(?<!\d)" + N + r"\s*[xх×*]\s*" + N + r"(?:\s*[xх×*]\s*" + N + r")?\s*(?:мм|см|м)?",
        r"\b(?:ГОСТ(?:\s+Р)?|ТУ)\s*[\d][\w./–-]*",
        r"\bст(?:аль|\.)?\s*\d+[а-яa-z]*\b",
        r"\b(?:Ду|DN)\s*" + N,
        r"(?:толщин\w*|стенка|\bs\s*=|диаметр\w*|[Ø⌀фФ]|\bd\s*=|длина\s*|\b[lL]\s*=)\s*" + N + r"\s*(?:мм|см|м)\b",
        r"\bRAL\s*\d{4}",
        r"\b(?:бел(?:ый|ая|ое|ые)|ч[её]рн(?:ый|ая|ое|ые)|сер(?:ый|ая|ое|ые))\b",
        r"нержаве\w*|алюмин\w*|\bПВХ\b|полипропилен\w*|\bПП\b",
        r"матов\w*|(?<!не)полирован\w*|ламиниров\w*|оцинков\w*",
        r"[23]\s*ВУС|[23][-\s]*(?:х\s*)?(?:слоя|слойн\w*)",
        r"бесшов\w*|б/ш|электросвар\w*|э/с|под мрамор|под дерево",
    ]
    for pattern in patterns:
        text = re.sub(pattern, " ", text, flags=re.I)
    return re.findall(r"[\w]+(?:[./+-][\w]+)*", norm(text))


def candidates(item):
    seen, out = set(), []
    for candidate in [item.get("selected")] + list(item.get("candidates") or []) + list(item.get("rejected_candidates") or []):
        if not candidate or candidate.get("error") or not candidate.get("name"):
            continue
        key = quote_key(candidate)
        if key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def signature(item):
    # quantity in comparison_source is the original requested quantity, even if a
    # partial delivery was deliberately applied to the commercial invoice.
    source = item.get("comparison_source") or item
    return hashlib.sha256(json.dumps([source.get("requested_name"), source.get("quantity"), source.get("unit")], ensure_ascii=False).encode()).hexdigest()


def settings(item):
    state = item.get("comparison") or {}
    return state if state.get("request_signature") == signature(item) else {}


def conversion(item, candidate):
    from automation_pipeline import _candidate_pack_size
    source = item.get("comparison_source") or item
    requested, supplied = unit(source.get("unit")), unit(candidate.get("unit"))
    manual = (settings(item).get("conversions") or {}).get(quote_key(candidate))
    if manual and manual.get("supplier_unit") == supplied and manual.get("request_unit") == requested:
        return manual["factor"], "Задан вручную"
    if requested and supplied == requested:
        # Name may mention a pack, but a price explicitly per piece is per piece.
        return 1.0, "Одинаковые единицы"
    if requested == "шт" and supplied == "упак":
        pack = _candidate_pack_size(candidate, source.get("unit") or "")
        if pack > 1:
            return float(pack), "Количество в упаковке"
    factors = {("т", "кг"): 1000, ("кг", "т"): .001, ("г", "кг"): .001,
               ("м", "мм"): 1000, ("мм", "м"): .001, ("см", "м"): .01,
               ("л", "мл"): 1000, ("мл", "л"): .001}
    if (supplied, requested) in factors:
        return factors[(supplied, requested)], "Перевод единиц"
    lengths = attributes(candidate.get("name")).get("Длина изделия") or []
    if supplied == "шт" and requested == "м" and len(lengths) == 1:
        return number(lengths[0].split()[0]) / 1000, "Длина изделия из наименования"
    return None, "Коэффициент не указан"


def offer_view(item, candidate):
    source = item.get("comparison_source") or item
    state = settings(item)
    decisions = state.get("features") or {}
    required = attributes(source.get("requested_name"))
    found = attributes(candidate.get("name"))
    if candidate.get("brand"):
        found["Бренд"] = [str(candidate["brand"])]
    features = []
    for field in sorted(set(required) | set(found)):
        for value in found.get(field) or ["Не указано"]:
            identifier = feature_id(field, value)
            same = value in required.get(field, [])
            differs = field in required and not same
            features.append(dict(id=identifier, field=field, value=value, required=required.get(field, []),
                                 state=decisions.get(identifier, "pending" if differs else "matched"), differs=differs))
    # A fallback for arbitrary products/parameters. Whole tokens avoid substring
    # exclusions (e.g. 20 must not reject 200). Unknown text remains reviewable.
    wanted = set(lexical_tokens(source.get("requested_name")))
    extras = [t for t in dict.fromkeys(lexical_tokens(candidate.get("name"))) if t not in wanted and len(t) > 1]
    for token in extras:
        field, value = "Слово в наименовании", token
        identifier = feature_id(field, value)
        features.append(dict(id=identifier, field=field, value=value, required=[],
                             state=decisions.get(identifier, "pending"), differs=True))
    for token in sorted(wanted - set(lexical_tokens(candidate.get("name")))):
        identifier = feature_id("Нет слова из заявки", token)
        features.append(dict(id=identifier, field="Нет слова из заявки", value=token, required=[token],
                             state=decisions.get(identifier, "pending"), differs=True))
    key = quote_key(candidate)
    factor, factor_source = conversion(item, candidate)
    price, need = number(candidate.get("price")), number(source.get("quantity"))
    supplier_unit = str(candidate.get("unit") or "не указана")
    requested_unit = str(source.get("unit") or "ед.")
    explicit_qty = number(candidate.get("offered_quantity"))
    if explicit_qty is None:
        explicit_qty = number(candidate.get("qty"))
    quantity = explicit_qty
    historical = candidate.get("source_type") in {"price_list", "own_catalog"}
    stock = number(candidate.get("stock")) if not historical else None
    if quantity is None and factor and need is not None:
        quantity = need / factor
        if unit(candidate.get("unit")) in {"шт", "упак", "компл"}:
            quantity = math.ceil(round(quantity, 9))
        if stock is not None:
            quantity = min(quantity, stock)
    converted = quantity * factor if quantity is not None and factor else None
    differs_qty = need is not None and converted is not None and not math.isclose(need, converted, abs_tol=1e-7)
    # Quantity approval is bound to the actual proposal, its units and original
    # demand. A price/stock refresh cannot reuse approval of another amount.
    qty_id = feature_id("Количество", json.dumps([quantity, supplier_unit, converted, need, requested_unit]))
    qty_state = (state.get("quantities") or {}).get(key, {})
    qty_decision = qty_state.get("decision", "pending") if qty_state.get("id") == qty_id else "pending"
    if not differs_qty:
        qty_decision = "matched"
    pending = [x for x in features if x["state"] == "pending"]
    rejected = [x for x in features if x["state"] == "rejected"]
    if candidate.get("hard_conflicts") and not features:
        # Don't silently admit an algorithmically rejected product with no
        # reviewable field. This synthetic decision is still operator-controlled.
        identifier = feature_id("Проверка соответствия", "; ".join(candidate["hard_conflicts"]))
        entry = dict(id=identifier, field="Проверка соответствия", value="; ".join(candidate["hard_conflicts"]), required=[], state=decisions.get(identifier, "pending"), differs=True)
        features.append(entry)
        if entry["state"] == "pending":
            pending.append(entry)
        if entry["state"] == "rejected":
            rejected.append(entry)
    reasons = []
    if rejected:
        reasons.append("Отклонена характеристика")
    if pending:
        reasons.append("Проверьте отличия в наименовании")
    if differs_qty and qty_decision != "accepted":
        reasons.append("Количество отклонено" if qty_decision == "rejected" else "Подтвердите количество")
    if not factor:
        reasons.append("Нужен коэффициент пересчёта")
    if price is None or price <= 0 or quantity is None or quantity <= 0:
        reasons.append("Нет положительной цены или количества")
    if state.get("cells", {}).get(key) is False:
        reasons.append("Предложение выключено")
    admissible = not reasons
    total = round(price * quantity, 2) if price is not None and quantity is not None else None
    return dict(key=key, supplier=str(candidate.get("supplier") or "ВсеИнструменты.ру"), name=candidate.get("name"),
                price=price, unit=supplier_unit, requested_unit=requested_unit, quantity=quantity,
                converted_quantity=converted, shortage=max(0, need - converted) if converted is not None and need is not None else None,
                quantity_differs=differs_qty, quantity_id=qty_id, quantity_state=qty_decision,
                factor=factor, factor_source=factor_source, unit_price=round(price / factor, 6) if price is not None and factor else None,
                total=total, features=features, eligible=admissible, reasons=reasons,
                enabled=state.get("cells", {}).get(key) is not False,
                selected=quote_key(item.get("selected") or {}) == key and item.get("decision") in {"approved", "auto_ready"},
                stock=stock, availability="Наличие требует подтверждения" if historical or stock is None else "Остаток поставщика",
                source_date=candidate.get("price_date"), url=safe_url(candidate.get("url") or candidate.get("source_url")))


def safe_url(value):
    from urllib.parse import urlsplit
    value = str(value or "")
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    return value if parsed.scheme in {"https", "http"} and parsed.netloc and not parsed.username else None


def supplier_enabled(result, supplier):
    return (result.get("comparison") or {}).get("suppliers", {}).get(supplier) is not False


def selected_allowed(item):
    """Used by the invoice/procurement path, not just visual highlighting."""
    if not item.get("comparison"):
        return True
    candidate = item.get("selected")
    if not candidate or item.get("comparison_supplier_disabled"):
        return False
    view = offer_view(item, candidate)
    state = settings(item)
    applied_matches = not state.get("applied_key") or (
        state.get("applied_key") == view["key"] and state.get("applied_quantity_id") == view["quantity_id"])
    return bool(view["eligible"] and applied_matches
                and number(item.get("quantity")) == view["converted_quantity"]
                and number(item.get("purchase_units")) == view["quantity"]
                and number(item.get("pack_size")) == view["factor"])


def revision(raw):
    return hashlib.sha256(str(raw).encode()).hexdigest()


def model(result, raw=None):
    rows, names = [], set()
    for item in result.get("items") or []:
        views = [offer_view(item, c) for c in candidates(item)]
        selection_valid = selected_allowed(item)
        for v in views:
            names.add(v["supplier"])
            v["selected"] = v["selected"] and selection_valid
            if not supplier_enabled(result, v["supplier"]):
                v["eligible"] = False
                v["reasons"].append("Поставщик выключен")
        source = item.get("comparison_source") or item
        rows.append(dict(position=item.get("position"), name=source.get("requested_name"), quantity=source.get("quantity"),
                         unit=source.get("unit"), offers=views, active=bool(item.get("comparison")),
                         rules=[dict(id=k, state=v) for k, v in (settings(item).get("features") or {}).items()]))
    return dict(revision=revision(raw) if raw is not None else None, order_name=result.get("order_name"),
                order_id=result.get("order_id"), source_label=result.get("source_label") or "Закупай",
                read_only=bool(result.get("live_offer_created")), rows=rows,
                suppliers=[dict(name=n, enabled=supplier_enabled(result, n)) for n in sorted(names)],
                mode=(result.get("comparison") or {}).get("mode", "sum"))


def activate(item):
    if not item.get("comparison_source"):
        item["comparison_source"] = {k: copy.deepcopy(item.get(k)) for k in ("requested_name", "quantity", "unit")}
    current = settings(item)
    item["comparison"] = current or {"request_signature": signature(item)}
    return item["comparison"]


def apply_selection(item, candidate, result):
    from automation_pipeline import DEFAULT_MARKUP
    view = offer_view(item, candidate)
    if not view["eligible"] or not supplier_enabled(result, view["supplier"]):
        raise ValueError("Сначала примите отличия и количество, включите предложение и поставщика.")
    activate(item)
    item.update(selected=copy.deepcopy(candidate), quantity=view["converted_quantity"], purchase_units=view["quantity"],
                pack_size=view["factor"], purchase_price=view["price"],
                proposed_unit_price=round(view["unit_price"] * (1 + result.get("markup", DEFAULT_MARKUP)), 2),
                decision="approved", operator_included=True, selection_rule="operator_comparison",
                match_status="подтверждено оператором", replacement_details=[],
                stock_confirmed=view["stock"] is not None and view["stock"] >= view["quantity"],
                availability_status=view["availability"], pickup_date=candidate.get("pickup_date"), courier_date=candidate.get("courier_date"))
    item["comparison"]["applied_key"] = view["key"]
    item["comparison"]["applied_quantity_id"] = view["quantity_id"]


def reconcile(result):
    """Any disallowed selection is removed from commercial totals immediately."""
    for item in result.get("items") or []:
        if not item.get("comparison"):
            continue
        selected = item.get("selected") or {}
        item["comparison_supplier_disabled"] = not supplier_enabled(result, str(selected.get("supplier") or "ВсеИнструменты.ру"))
        if not selected_allowed(item):
            item.update(decision="excluded", operator_included=False)


def preserve_comparison(new, old):
    """Carry rules across re-search by original line identity, not list ordering."""
    if old.get("comparison"):
        new["comparison"] = copy.deepcopy(old["comparison"])
    old_rows = {str(x.get("position")): x for x in old.get("items") or []}
    for item in new.get("items") or []:
        previous = old_rows.get(str(item.get("position")))
        if not previous or norm(item.get("requested_name")) != norm(previous.get("requested_name")):
            continue
        source = previous.get("comparison_source") or previous
        if number(source.get("quantity")) != number(item.get("quantity")) or unit(source.get("unit")) != unit(item.get("unit")):
            continue
        for field in ("comparison", "comparison_source"):
            if previous.get(field):
                item[field] = copy.deepcopy(previous[field])
    reconcile(new)


def mutate(result, payload):
    action = payload.get("action")
    if result.get("live_offer_created"):
        raise ValueError("Предложение уже отправлено. Сравнение доступно только для просмотра.")
    if action == "mode":
        if payload.get("value") not in {"sum", "price"}:
            raise ValueError("Неизвестный режим")
        result.setdefault("comparison", {})["mode"] = payload["value"]
    elif action == "supplier":
        name = payload.get("supplier")
        known = {v["supplier"] for row in model(result)["rows"] for v in row["offers"]}
        if name not in known or not isinstance(payload.get("enabled"), bool):
            raise ValueError("Поставщик не найден")
        result.setdefault("comparison", {}).setdefault("suppliers", {})[name] = payload["enabled"]
        for item in result.get("items") or []:
            if any(str(c.get("supplier") or "ВсеИнструменты.ру") == name for c in candidates(item)):
                activate(item)
    else:
        item = next((i for i in result.get("items") or [] if str(i.get("position")) == str(payload.get("position"))), None)
        if item is None:
            raise ValueError("Позиция не найдена")
        state = activate(item)
        views = [offer_view(item, c) for c in candidates(item)]
        if action == "feature":
            identifier, decision = payload.get("feature"), payload.get("decision")
            if decision not in {"accepted", "rejected", "pending"} or not any(f["id"] == identifier for v in views for f in v["features"]):
                raise ValueError("Характеристика не найдена")
            state.setdefault("features", {})[identifier] = decision
        elif action == "reset":
            # Reset decisions, retain the immutable original request.
            item["comparison"] = {"request_signature": signature(item)}
            item.update(decision="excluded", operator_included=False)
        else:
            candidate = next((c for c in candidates(item) if quote_key(c) == payload.get("key")), None)
            if candidate is None:
                raise ValueError("Предложение не найдено")
            view = offer_view(item, candidate)
            if action == "quantity":
                decision = payload.get("decision")
                if decision not in {"accepted", "rejected", "pending"} or not view["quantity_differs"]:
                    raise ValueError("Количество не отличается")
                state.setdefault("quantities", {})[view["key"]] = {"id": view["quantity_id"], "decision": decision}
            elif action == "cell":
                if not isinstance(payload.get("enabled"), bool):
                    raise ValueError("Некорректный переключатель")
                state.setdefault("cells", {})[view["key"]] = payload["enabled"]
            elif action == "conversion":
                factor = number(payload.get("factor"))
                if factor is None or factor <= 0 or factor > 1e9:
                    raise ValueError("Введите положительный коэффициент")
                state.setdefault("conversions", {})[view["key"]] = dict(factor=factor, supplier_unit=unit(candidate.get("unit")), request_unit=unit((item.get("comparison_source") or item).get("unit")))
            elif action == "select":
                apply_selection(item, candidate, result)
            else:
                raise ValueError("Неизвестное действие")
    reconcile(result)
    from automation_pipeline import _refresh_summary
    _refresh_summary(result)


def _session(request):
    if not security._valid_session(request):
        raise HTTPException(401, "Войдите в закрытую панель")


def _csrf(request):
    return security._sign_payload({"exp": int(time.time()) + 3600, "session": hashlib.sha256(request.cookies.get(security.SESSION_COOKIE, "").encode()).hexdigest()}, "comparison-form", "cmp_form")


def install_supplier_comparison(app):
    from automation_pipeline import _connect, _execute, _lock

    def load(job_id, conn):
        row = _execute(conn, "SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
        if not row or not row["result_json"]:
            raise HTTPException(404, "Сохранённый результат не найден. Сначала выполните поиск по заявке.")
        return row, json.loads(row["result_json"])

    @app.get("/dashboard/comparison/assets/{name}")
    def asset(name: str, request: Request):
        _session(request)
        if name not in {"comparison.js", "comparison.css"}:
            raise HTTPException(404)
        return Response((ASSETS / name).read_text(), media_type="text/javascript" if name.endswith(".js") else "text/css", headers={"Cache-Control": "no-cache"})

    @app.get("/dashboard/automation/jobs/{job_id}/comparison", response_class=HTMLResponse)
    def page(job_id: int, request: Request):
        _session(request)
        with _connect() as conn:
            row, result = load(job_id, conn)
        data = model(result, row["result_json"])
        data.update(csrf=_csrf(request), job_id=job_id, read_only=data["read_only"] or row["status"] in {"processing", "offer_created"})
        serialized = json.dumps(data, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c").replace("&", "\\u0026")
        nonce = security.secrets.token_urlsafe(18)
        content = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Сравнение поставщиков · Закупай</title><link rel="stylesheet" href="/dashboard/comparison/assets/comparison.css"></head><body>
        <nav><a href="/dashboard/automation">Закупай <span>MVP</span></a><a href="/dashboard/automation/jobs/{job_id}/review">← К проверке заявки</a><a href="/dashboard/prices">Прайсы</a></nav>
        <main><div class="eyebrow">ЗАКУПКИ / СРАВНЕНИЕ ПРЕДЛОЖЕНИЙ</div><h1>Выбрать поставщиков</h1><p id="order-title" class="subtitle"></p>
        <div id="message" role="status" aria-live="polite"></div><section id="summary" class="summary"></section>
        <section class="toolbar"><label>Показывать и выбирать минимум по <select id="mode"><option value="sum">сумме предложения</option><option value="price">цене за единицу заявки</option></select></label><label><input type="checkbox" id="show-excluded" checked> Показывать исключённые</label><button id="show-all">Показать все столбцы</button><button id="export">Скачать CSV</button></section>
        <p class="hint">✓ принимает значение характеристики у всех поставщиков этой позиции. ✗ исключает все предложения с этим значением. Решение по количеству относится к конкретному предложению. Пересчёт единиц показан отдельно.</p>
        <div id="supplier-toggles" class="supplier-toggles"></div><section class="matrix-wrap" aria-label="Сравнительная таблица"><table id="matrix"></table></section>
        <p class="legend"><span class="dot green"></span> Минимум среди допущенных <span class="dot orange"></span> Требует решения <span class="dot gray"></span> Исключено</p>
        <p class="hint">Итоги — по сохранённым закупочным ценам. Доставка в общий минимум не добавляется: её стоимость нужно подтвердить отдельно. Выбор переносится в проверку заявки; отправка выполняется из существующей формы подтверждения.</p>
        <noscript>Для интерактивного сравнения включите JavaScript. Обычная проверка заявки доступна по ссылке выше.</noscript></main>
        <script nonce="{nonce}" type="application/json" id="comparison-data">{serialized}</script><script nonce="{nonce}" src="/dashboard/comparison/assets/comparison.js"></script></body></html>"""
        return HTMLResponse(content, headers={"Cache-Control": "no-store", "Content-Security-Policy": f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"})

    @app.post("/dashboard/automation/jobs/{job_id}/comparison/action")
    async def action(job_id: int, request: Request):
        _session(request)
        body = await request.body()
        if len(body) > 16384:
            raise HTTPException(413, "Слишком большой запрос")
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            raise HTTPException(400, "Неверный формат запроса")
        if not isinstance(payload, dict):
            raise HTTPException(400, "Неверный формат запроса")
        token = security._decode_payload(str(payload.get("csrf") or ""), "comparison-form", "cmp_form")
        expected = hashlib.sha256(request.cookies.get(security.SESSION_COOKIE, "").encode()).hexdigest()
        if not token or not hmac.compare_digest(str(token.get("session") or ""), expected):
            raise HTTPException(403, "Обновите страницу и повторите действие")
        with _lock, _connect() as conn:
            row, result = load(job_id, conn)
            if row["status"] in {"processing", "offer_created"}:
                raise HTTPException(409, "Заявка обрабатывается или предложение уже отправлено")
            if payload.get("revision") != revision(row["result_json"]):
                raise HTTPException(409, "Заявка изменена в другом окне. Обновите страницу.")
            submission = _execute(conn, "SELECT status FROM offer_submissions WHERE job_id=? AND status IN ('sending','unknown') LIMIT 1", (job_id,)).fetchone()
            if submission:
                raise HTTPException(409, "Отправка предложения выполняется или требует проверки")
            try:
                mutate(result, payload)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            invoice_number = row["invoice_number"]
            if payload.get("action") == "select" and invoice_number is None:
                from automation_pipeline import INVOICE_NUMBER_START
                last = _execute(conn, "SELECT MAX(invoice_number) AS maximum FROM automation_jobs").fetchone()
                invoice_number = max(INVOICE_NUMBER_START, (last["maximum"] or INVOICE_NUMBER_START - 1) + 1)
                result["invoice_number"] = invoice_number
            raw = json.dumps(result, ensure_ascii=False, allow_nan=False)
            cursor = _execute(conn, "UPDATE automation_jobs SET result_json=?,status=?,invoice_number=?,updated_at=? WHERE id=? AND result_json=? AND status=?", (raw, result["status"], invoice_number, datetime.now(timezone.utc).isoformat(), job_id, row["result_json"], row["status"]))
            if cursor.rowcount != 1:
                raise HTTPException(409, "Заявка изменена. Обновите страницу.")
        data = model(result, raw)
        return JSONResponse(data, headers={"Cache-Control": "no-store"})
