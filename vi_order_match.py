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
}


def _identifiers(s):
    """Model/article-like fragments: RT-IB150, 80661, DIN7504-O, RF-TC7005."""
    raw = (s or "").upper().replace("Ё", "Е")
    values = set(re.findall(r"(?<![A-ZА-Я0-9])(?=[A-ZА-Я0-9./_-]{4,})(?=[A-ZА-Я0-9./_-]*\d)[A-ZА-Я0-9]+(?:[-_/][A-ZА-Я0-9]+)*(?![A-ZА-Я0-9])", raw))
    return {re.sub(r"[^A-ZА-Я0-9]", "", x) for x in values if len(re.sub(r"[^A-ZА-Я0-9]", "", x)) >= 4}


def _measurements(s):
    """Comparable measurements while keeping the unit (10x160 mm, 2 inch, 500 g)."""
    text = (s or "").lower().replace("×", "x").replace("*", "x").replace(",", ".")
    found = set()
    for dims, unit in re.findall(r"(\d+(?:\.\d+)?(?:\s*x\s*\d+(?:\.\d+)?){0,2})\s*(мм|mm|см|cm|м|m|г|гр|g|кг|kg|дюйм(?:а|ов)?|inch|\")", text):
        nums = "x".join(part.strip().lstrip("0") or "0" for part in dims.split("x"))
        unit = {"mm": "мм", "cm": "см", "m": "м", "гр": "г", "g": "г", "kg": "кг", "inch": "дюйм", '"': "дюйм"}.get(unit, unit)
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
    ta = _tokens(a) - _GENERIC_TOKENS
    tb = _tokens(b) - _GENERIC_TOKENS
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
    orders = fetch_all_orders()
    order = next((x for x in orders if x.get("id") == order_id), None)
    if not order:
        orders = fetch_all_orders(force=True)
        order = next((x for x in orders if x.get("id") == order_id), None)
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
