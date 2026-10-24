import re
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from supplier_adapters import VseinstrumentiAdapter


def _norm(s):
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[^a-zа-я0-9]+", " ", s)
    return " ".join(s.split())


def _tokens(s):
    return {x for x in _norm(s).split() if len(x) >= 2}


_GENERIC_TOKENS = {
    "для", "шт", "штук", "мм", "см", "м", "набор", "комплект", "упаковка",
    "инструмент", "расходный", "материал", "профессиональный",
    # Units are compared by _measurements(); as words they only add noise.
    "дюйм", "дюйма", "дюймов", "дюймовый", "inch",
}

# Explicit, reviewed synonym groups. Both the request and the candidate are
# rewritten to the same canonical token, so a match appears only when both
# sides state the feature. This does not lower any score threshold.
_FEATURE_SYNONYMS = (
    (re.compile(r"\b(?:прорезинен\w*|epdm|резинов\w*\s+(?:уплотнител\w*|прокладк\w*|вкладыш\w*|вставк\w*))"), "feat_rubber_lined"),
)

_RANGE_RE = re.compile(r"(?<!\d)(?<!\d[.,])(\d{1,3})\s*[-–—]\s*(\d{1,3})(?!\d)(?![.,]\d)(?:\s*(мм|mm|см|cm|м|m)(?![а-яa-z]))?", re.I)


def _canonical_features(normalized):
    for pattern, token in _FEATURE_SYNONYMS:
        normalized = re.sub(r"\b(?:без|нет|не)\s+(?:" + pattern.pattern + r")", "neg_" + token, normalized)
        normalized = re.sub(r"(?:" + pattern.pattern + r")\s+(?:нет|отсутствует|отсутствуют)\b", "neg_" + token, normalized)
        normalized = pattern.sub(token, normalized)
    return normalized


def _range_records(s):
    """Only locally identified dimensions; unrelated units do not identify articles."""
    text = str(s or "").lower()
    records = []
    for match in _RANGE_RE.finditer(text):
        if (match.start() and re.match(r"[\w./–—-]", text[match.start()-1])) or (match.end() < len(text) and re.match(r"[\w/–—-]", text[match.end()])):
            continue
        low, high, unit = match.groups()
        if int(low) >= int(high):
            continue
        prefix = text[:match.start()]
        label = re.search(r"\b(диаметр|длина|ширина|высота|толщина|dn|ду)\s*[:=]?\s*$", prefix)
        article = re.search(r"\b(?:артикул|арт|модель|код)\.?\s*[:=]?\s*$", prefix)
        if article:
            continue
        kind = label.group(1) if label else ""
        # A single unlabelled clamp range conventionally describes its diameter.
        if not kind and re.search(r"\bхомут\w*\b", text) and len(list(_RANGE_RE.finditer(text))) == 1:
            kind = "диаметр"
        if not kind and not unit:
            continue
        unit = {"mm": "мм", "cm": "см", "m": "м"}.get((unit or "").lower(), (unit or "").lower())
        records.append((int(low), int(high), unit, kind, match.start(), match.end()))
    return records


def _ranges(s):
    return {record[:3] for record in _range_records(s)}


def _range_relation(requested, candidate_text):
    """Compare size ranges of the request and the candidate.

    None: no comparable ranges. same / different (overlap) / disjoint (same
    explicit unit on both sides) / disjoint_unverified (a unit is missing, so it
    is not certain that the same characteristic is compared).
    """
    wanted, offered = _range_records(requested), _range_records(candidate_text)
    if not wanted:
        return None
    results = []
    for w in wanted:
        peers = [o for o in offered if w[3] and w[3] == o[3]]
        if len(peers) != 1 or sum(r[3] == w[3] for r in wanted) != 1:
            results.append("disjoint_unverified")
            continue
        o = peers[0]
        if w[2] and o[2] and w[2] != o[2]:
            results.append("disjoint_unverified")
        elif w[:2] == o[:2]:
            results.append("same")
        elif w[0] <= o[1] and o[0] <= w[1]:
            results.append("different")
        else:
            results.append("disjoint" if w[2] and w[2] == o[2] else "disjoint_unverified")
    if "disjoint" in results:
        return "disjoint"
    if "disjoint_unverified" in results:
        return "disjoint_unverified"
    if "different" in results:
        return "different"
    return "same"


def _features(text):
    """Canonical feature tokens (e.g. feat_rubber_lined) stated in a text."""
    return {t for t in _canonical_features(_norm(text)).split() if t.startswith("feat_")}


def _negative_features(text):
    return {t[4:] for t in _canonical_features(_norm(text)).split() if t.startswith("neg_feat_")}

# Supplier brand fields extend this vocabulary at each search, including brands
# that were not known when the application was deployed.
_KNOWN_BRANDS = {
    "karbosan", "matrix", "gigant", "runtec", "izeltas", "rockforce", "сибртех",
    "практика", "skole", "voll", "makita", "bosch", "dewalt", "metabo", "hilti",
    "stanley", "knipex", "stayer", "зубр", "dexx", "denzel", "patriot", "iek",
    "tdm electric", "ekf", "litokol", "knauf", "soudal", "3m",
}


def _contains_brand(text, brand):
    normalized = _norm(str(brand or ""))
    return bool(normalized and f" {normalized} " in f" {_norm(str(text or ''))} ")


def _requested_brands(requested, brands=()):
    vocabulary = _KNOWN_BRANDS | {_norm(str(brand)) for brand in brands if brand}
    found = {brand for brand in vocabulary if _contains_brand(requested, brand)}
    return {brand for brand in found if not any(brand != other and _contains_brand(other, brand) for other in found)}


def _identifiers(s):
    """Model/article-like fragments: RT-IB150, 80661, DIN7504-O, RF-TC7005."""
    raw = s or ""
    for record in reversed(_range_records(raw)):
        raw = raw[:record[4]] + " " + raw[record[5]:]
    raw = raw.upper().replace("Ё", "Е")
    values = set(re.findall(r"(?<![A-ZА-Я0-9])(?=[A-ZА-Я0-9./_-]{4,})(?=[A-ZА-Я0-9./_-]*\d)[A-ZА-Я0-9]+(?:[-_/][A-ZА-Я0-9]+)*(?![A-ZА-Я0-9])", raw))
    normalized = {re.sub(r"[^A-ZА-Я0-9]", "", x) for x in values}
    measurement_only = re.compile(r"^\d+(?:ММ|СМ|М|МЛ|Л|Г|КГ|ВТ|КВТ|В|А|БАР)$")
    return {x for x in normalized if len(x) >= 4 and not measurement_only.fullmatch(x)}


def _measurements(s):
    """Comparable measurements while keeping the unit (10x160 mm, 2 inch, 500 g)."""
    text = (s or "").lower().replace("×", "x").replace("*", "x").replace(",", ".")
    for record in reversed(_range_records(text)):
        text = text[:record[4]] + " " + text[record[5]:]
    found = set()
    for dims, unit in re.findall(r"(\d+(?:\.\d+)?(?:\s*x\s*\d+(?:\.\d+)?){0,2})\s*(мм|mm|см|cm|мл|ml|м|m|л|l|г|гр|g|кг|kg|вт|w|квт|kw|в|v|а|a|бар|bar|дюйм(?:а|ов)?|inch|\")", text):
        nums = "x".join(part.strip().lstrip("0") or "0" for part in dims.split("x"))
        unit = {
            "mm": "мм", "cm": "см", "m": "м", "ml": "мл", "l": "л",
            "гр": "г", "g": "г", "kg": "кг", "w": "вт", "kw": "квт",
            "v": "в", "a": "а", "bar": "бар", "inch": "дюйм", '"': "дюйм",
        }.get(unit, unit)
        if unit.startswith("дюйм"):
            unit = "дюйм"
        found.add(f"{nums}{unit}")
    return found


def _score_details(request_name, quote):
    a = _norm(request_name)
    b = _norm(quote.name)
    if not a or not b:
        return 0.0, ["пустое наименование"]
    seq = SequenceMatcher(None, a, b).ratio()
    ta = _tokens(_canonical_features(a)) - _GENERIC_TOKENS
    tb = _tokens(_canonical_features(b)) - _GENERIC_TOKENS
    overlap = len(ta & tb) / max(1, len(ta))
    bonus = 0.0
    reasons = []
    for label, needle in (("SKU", quote.sku), ("артикул", quote.article), ("бренд", quote.brand)):
        n = _norm(str(needle or ""))
        if n and n in a:
            bonus += 0.16 if label != "бренд" else 0.10
            reasons.append(f"совпал {label}")

    req_ids = _identifiers(request_name)
    quote_ids = _identifiers(" ".join(str(x or "") for x in (quote.name, quote.sku, quote.article)))
    shared_ids = req_ids & quote_ids
    if shared_ids:
        bonus += 0.22
        reasons.append("совпала модель/артикул: " + ", ".join(sorted(shared_ids)))

    penalty = 0.0
    req_measures = _measurements(request_name)
    quote_measures = _measurements(quote.name)
    if req_measures and quote_measures:
        shared_measures = req_measures & quote_measures
        if shared_measures:
            bonus += min(0.18, 0.06 * len(shared_measures))
            reasons.append("совпали размеры: " + ", ".join(sorted(shared_measures)))
        # A requested dimension that is absent while the candidate states another
        # dimension is a strong warning (e.g. 10x160 instead of 12x160).
        if not shared_measures:
            penalty += 0.38
            reasons.append("конфликт размеров")

    relation = _range_relation(request_name, quote.name)
    if relation == "same":
        bonus += 0.06
        reasons.append("совпал диапазон размеров")
    elif relation == "different":
        reasons.append("диапазон размеров отличается")

    req_brand = _norm(str(quote.brand or ""))
    if req_brand and req_brand not in a and any(x in a for x in ("karbosan", "matrix", "gigant", "runtec", "izeltas", "rockforce", "сибртех", "практика", "skole", "voll")):
        penalty += 0.25
        reasons.append("заявлен другой бренд")

    score = max(0.0, min(1.0, 0.48 * seq + 0.42 * overlap + bonus - penalty))
    if not reasons:
        reasons.append("текстовое сходство")
    return round(score, 3), reasons


def _score(request_name, quote):
    return _score_details(request_name, quote)[0]


def _label(score):
    if score >= 0.88:
        return "Точное"
    if score >= 0.72:
        return "Хорошее/вероятное"
    if score >= 0.48:
        return "Сомнительное"
    return "Не соответствует"


def _get_order(fetch_all_orders, order_id):
    order = fetch_all_orders(order_id=order_id)
    if not order:
        order = fetch_all_orders(order_id=order_id, force=True)
    if not order:
        raise HTTPException(status_code=404, detail="Заявка не найдена среди актуальных")
    return order


def _unit_name(item):
    u = item.get("unit") or {}
    return u.get("name") if isinstance(u, dict) else str(u or "")


def match_order(fetch_all_orders, order_id, limit=5):
    order = _get_order(fetch_all_orders, order_id)
    vi = VseinstrumentiAdapter()
    order_items = list(order.get("orderItems") or [])

    # A large order used to make one external VI request after another. For 38
    # positions that could keep the HTTP request open for minutes. Search a
    # conservative number in parallel: fast enough for the UI without flooding
    # the supplier API.
    try:
        configured_workers = int(os.getenv("VI_MATCH_WORKERS", "8"))
    except ValueError:
        configured_workers = 8
    workers = max(1, min(configured_workers, 12, len(order_items) or 1))
    quotes_by_position = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(vi.search, item.get("goodName") or "", limit): pos
            for pos, item in enumerate(order_items, 1)
        }
        for future in as_completed(futures):
            pos = futures[future]
            try:
                quotes_by_position[pos] = future.result()
            except Exception as exc:
                quotes_by_position[pos] = [{"error": f"Ошибка поиска ВИ: {type(exc).__name__}: {exc}"}]

    items = []
    for pos, item in enumerate(order_items, 1):
        name = item.get("goodName") or ""
        quotes = quotes_by_position.get(pos) or []
        candidates = []
        for q in quotes:
            if isinstance(q, dict) and q.get("error"):
                candidates.append(q)
                continue
            if q.error:
                candidates.append({"error": q.error})
                continue
            score, reasons = _score_details(name, q)
            d = q.to_dict()
            d["match_score"] = score
            d["match_level"] = _label(score)
            d["match_reasons"] = reasons
            candidates.append(d)
        candidates.sort(key=lambda x: (x.get("error") is not None, -(x.get("match_score") or 0), x.get("price") or 10**18))
        top_candidate = next((x for x in candidates if not x.get("error")), None)
        accepted_candidate = top_candidate if top_candidate and (top_candidate.get("match_score") or 0) >= 0.48 else None
        items.append({
            "position": pos,
            "order_item_id": item.get("id"),
            "requested_name": name,
            "quantity": item.get("count"),
            "unit": _unit_name(item),
            "candidates": candidates,
            "top_candidate": top_candidate,
            "best_candidate": accepted_candidate,
        })
    return {"order_id": order_id, "order_name": order.get("name"), "items": items}


def install_vi_order_match(app, fetch_all_orders, esc):
    @app.get("/analysis/order/{order_id}/vseinstrumenti")
    def vi_order_match_json(order_id: int, limit: int = 5):
        return JSONResponse(match_order(fetch_all_orders, order_id, max(1, min(limit, 10))))

    @app.get("/dashboard/analysis/order/{order_id}/vseinstrumenti", response_class=HTMLResponse)
    def vi_order_match_html(order_id: int, limit: int = 5):
        data = match_order(fetch_all_orders, order_id, max(1, min(limit, 10)))
        rows = ""
        for item in data["items"]:
            best = item.get("best_candidate") or {}
            price = best.get("price")
            stock = best.get("stock")
            delivery = best.get("courier_date") or best.get("pickup_date") or "—"
            score = best.get("match_score")
            level = best.get("match_level") or "—"
            reasons = "; ".join(best.get("match_reasons") or [])
            product = best.get("name") or "—"
            sku = best.get("sku") or "—"
            article = best.get("article") or "—"
            link = best.get("url")
            product_html = f"<a target='_blank' href='{esc(link)}'>{esc(product)}</a>" if link else esc(product)
            rows += f"<tr><td>{item['position']}</td><td>{esc(item['requested_name'])}</td><td>{esc(item['quantity'])} {esc(item['unit'])}</td><td>{product_html}</td><td>{esc(sku)}</td><td>{esc(article)}</td><td>{level} ({score if score is not None else '—'})<div class='note'>{esc(reasons)}</div></td><td><b>{f'{price:,.2f} ₽'.replace(',', ' ') if isinstance(price,(int,float)) else '—'}</b></td><td>{esc(stock) if stock is not None else '—'}</td><td>{esc(delivery)}</td></tr>"
        html = f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ВИ — заявка {order_id}</title><style>body{{font-family:Arial;margin:24px;background:#f5f5f5;color:#222}}.card{{background:#fff;border-radius:12px;padding:18px;overflow:auto}}table{{width:100%;border-collapse:collapse;font-size:13px}}th{{background:#eee;text-align:left;padding:9px}}td{{padding:9px;border-bottom:1px solid #eee;vertical-align:top}}a{{color:#4c39d4;font-weight:bold;text-decoration:none}}.note{{color:#666;font-size:12px}}</style></head><body><p><a href='/dashboard/analysis/order/{order_id}'>← К заявке</a></p><h1>ВсеИнструменты — заявка {order_id}</h1><p class='note'>Автопоиск по полному наименованию. Совпадение — предварительная оценка; неоднозначные позиции требуют проверки оператором.</p><div class='card'><table><thead><tr><th>№</th><th>Позиция заявки</th><th>Кол-во</th><th>Товар ВИ</th><th>SKU</th><th>Артикул</th><th>Соответствие</th><th>ОПТ цена</th><th>Остаток</th><th>Доставка</th></tr></thead><tbody>{rows}</tbody></table></div></body></html>"""
        return HTMLResponse(html)
