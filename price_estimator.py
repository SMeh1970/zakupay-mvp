import json
import os
import re
from difflib import SequenceMatcher
from statistics import median

from price_debug import extract_best_price

PRICE_CATALOG_PATH = os.getenv("PRICE_CATALOG_PATH", "price_catalog.json")


def _norm(text):
    text = str(text or "").lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9.+/-]+", " ", text, flags=re.I)
    return " ".join(text.split())


def _tokens(text):
    return {x for x in _norm(text).split() if len(x) >= 2}


def _model_tokens(text):
    return {
        x for x in _tokens(text)
        if any(ch.isdigit() for ch in x) and len(x) >= 4
    }


def name_similarity(request_name, offered_name):
    a, b = _norm(request_name), _norm(offered_name)
    if not a or not b:
        return 0.0
    seq = SequenceMatcher(None, a, b).ratio()
    ta, tb = _tokens(a), _tokens(b)
    overlap = len(ta & tb) / max(1, len(ta | tb))
    ma, mb = _model_tokens(a), _model_tokens(b)
    model_bonus = 0.18 if ma and mb and (ma & mb) else 0.0
    containment = 0.08 if a in b or b in a else 0.0
    return round(min(1.0, seq * 0.55 + overlap * 0.37 + model_bonus + containment), 3)


def _to_number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        s = value.replace("\xa0", " ").replace("₽", "").replace("руб.", "").replace("руб", "").strip()
        s = s.replace(" ", "").replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _walk(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            yield path, v
            yield from _walk(v, path)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{prefix}[{i}]")


def _extract_offer_name(best_offer):
    if isinstance(best_offer, str) and best_offer.strip():
        return best_offer.strip()
    if not isinstance(best_offer, dict):
        return None
    preferred = (
        "goodName", "name", "productName", "offerName", "nomenclatureName",
        "title", "description"
    )
    for key in preferred:
        value = best_offer.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for path, value in _walk(best_offer):
        leaf = path.split(".")[-1].lower()
        if isinstance(value, str) and value.strip() and any(x in leaf for x in ("name", "title", "good")):
            return value.strip()
    return None


def _extract_offer_price(best_offer):
    if not isinstance(best_offer, dict):
        return None
    preferred = (
        "price", "unitPrice", "pricePerUnit", "offerPrice", "bestPrice",
        "cost", "unitCost"
    )
    for key in preferred:
        if key in best_offer:
            n = _to_number(best_offer.get(key))
            if n is not None and n > 0:
                return n
    candidates = []
    for path, value in _walk(best_offer):
        leaf = path.split(".")[-1].lower()
        n = _to_number(value)
        if n is None or n <= 0:
            continue
        if "price" in leaf or "cost" in leaf:
            penalty = 1 if any(x in leaf for x in ("total", "sum", "amount")) else 0
            candidates.append((penalty, len(path), n))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[0][2]


def _load_catalog():
    try:
        with open(PRICE_CATALOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _catalog_best(name, catalog):
    best = None
    best_score = 0.0
    for row in catalog:
        if not isinstance(row, dict):
            continue
        candidate = row.get("name") or row.get("product_name") or row.get("title")
        price = _to_number(row.get("price"))
        if not candidate or not price or price <= 0:
            continue
        score = name_similarity(name, candidate)
        if score > best_score:
            best_score = score
            best = row
    if not best:
        return None
    return {
        "price": _to_number(best.get("price")),
        "name": best.get("name") or best.get("product_name") or best.get("title"),
        "supplier": best.get("supplier"),
        "article": best.get("article") or best.get("sku"),
        "similarity": round(best_score, 3),
    }


def _category_name(item):
    return ((item.get("category") or {}).get("name") or "").strip()


def estimate_order(order):
    items = order.get("orderItems") or []
    catalog = _load_catalog()

    reliable = []
    category_prices = {}
    preliminary = []
    for item in items:
        req_name = item.get("goodName") or ""
        offer = item.get("bestOfferItem")
        offer_name = _extract_offer_name(offer)
        offer_price = _extract_offer_price(offer)
        price_source_path = None
        if offer_price is None:
            candidate = extract_best_price(item)
            if candidate:
                offer_price = candidate["value"]
                price_source_path = candidate["path"]
        similarity = name_similarity(req_name, offer_name) if offer_name else 0.0
        competitor_ok = bool(offer_name and offer_price and similarity >= 0.58)
        pre = {
            "request_name": req_name,
            "competitor_name": offer_name,
            "competitor_price": offer_price,
            "competitor_similarity": similarity,
            "competitor_accepted": competitor_ok,
            "competitor_price_path": price_source_path,
        }
        preliminary.append(pre)
        if competitor_ok:
            reliable.append(float(offer_price))
            cat = _category_name(item)
            if cat:
                category_prices.setdefault(cat, []).append(float(offer_price))

    order_median = median(reliable) if reliable else None
    category_medians = {k: median(v) for k, v in category_prices.items() if v}

    rows = []
    total = 0.0
    priced_positions = 0
    high_conf_positions = 0

    for item, pre in zip(items, preliminary):
        qty = _to_number(item.get("count")) or 0.0
        req_name = pre["request_name"]
        unit_price = None
        source = None
        confidence = "none"
        source_name = None
        similarity = pre["competitor_similarity"]

        if pre["competitor_accepted"]:
            unit_price = float(pre["competitor_price"])
            source = "competitor"
            source_name = pre["competitor_name"]
            confidence = "high" if similarity >= 0.72 else "medium"

        if unit_price is None and catalog:
            cm = _catalog_best(req_name, catalog)
            if cm and cm["similarity"] >= 0.62:
                unit_price = cm["price"]
                source = "supplier_catalog"
                source_name = cm.get("name")
                similarity = cm["similarity"]
                confidence = "high" if similarity >= 0.75 else "medium"

        if unit_price is None:
            cat = _category_name(item)
            if cat and cat in category_medians:
                unit_price = float(category_medians[cat])
                source = "category_median"
                source_name = cat
                confidence = "low"
            elif order_median is not None:
                unit_price = float(order_median)
                source = "order_median"
                source_name = "медиана известных цен этой заявки"
                confidence = "low"

        line_total = round(unit_price * qty, 2) if unit_price is not None else None
        if line_total is not None:
            total += line_total
            priced_positions += 1
            if confidence in ("high", "medium"):
                high_conf_positions += 1

        rows.append({
            "item_id": item.get("id"),
            "request_name": req_name,
            "quantity": item.get("count"),
            "unit": ((item.get("unit") or {}).get("name")),
            "category": _category_name(item),
            "estimated_unit_price": round(unit_price, 2) if unit_price is not None else None,
            "estimated_line_total": line_total,
            "source": source,
            "source_name": source_name,
            "confidence": confidence,
            "match_score": round(similarity, 3) if similarity else None,
            "competitor_name": pre["competitor_name"],
            "competitor_price": pre["competitor_price"],
            "competitor_price_path": pre["competitor_price_path"],
            "competitor_accepted": pre["competitor_accepted"],
        })

    count = len(items)
    coverage = priced_positions / count if count else 0.0
    reliable_coverage = high_conf_positions / count if count else 0.0
    return {
        "estimated_total": round(total, 2) if priced_positions else None,
        "coverage": round(coverage, 3),
        "reliable_coverage": round(reliable_coverage, 3),
        "priced_positions": priced_positions,
        "positions_count": count,
        "items": rows,
    }


def analyze_order_v2(order, has_my_offer, max_competitors):
    items = order.get("orderItems") or []
    delay = order.get("delay")
    competitors = max_competitors(order)
    estimate = estimate_order(order)
    score, reasons = 0, []

    if delay == 0:
        score += 30
        reasons.append("предоплата / без отсрочки")
    elif isinstance(delay, (int, float)) and delay <= 14:
        score += 16
        reasons.append(f"короткая отсрочка {delay} дн.")
    elif isinstance(delay, (int, float)) and delay <= 30:
        score += 8

    if competitors == 0:
        score += 22
        reasons.append("конкурентов пока нет")
    elif competitors <= 2:
        score += 16
        reasons.append("мало конкурентов")
    elif competitors <= 5:
        score += 8

    if len(items) >= 10:
        score += 14
        reasons.append("много позиций")
    elif len(items) >= 5:
        score += 10
    elif len(items) >= 2:
        score += 5

    if not has_my_offer(order):
        score += 12
        reasons.append("наше предложение ещё не отправлено")

    coverage = estimate["coverage"]
    reliable_coverage = estimate["reliable_coverage"]
    if reliable_coverage >= 0.8:
        score += 12
        reasons.append("большинство позиций оценено по достоверным ценам")
    elif coverage >= 0.5:
        score += 6
        reasons.append("стоимость большей части заявки оценена")

    estimated_total = estimate["estimated_total"] or 0
    if estimated_total >= 500000:
        score += 10
        reasons.append("оценочная закупка от 500 тыс. ₽")
    elif estimated_total >= 300000:
        score += 7
        reasons.append("оценочная закупка от 300 тыс. ₽")
    elif estimated_total >= 100000:
        score += 4
        reasons.append("оценочная закупка от 100 тыс. ₽")

    score = min(score, 100)
    verdict = "БРАТЬ В РАБОТУ" if score >= 70 else "ПРОВЕРИТЬ" if score >= 45 else "НИЗКИЙ ПРИОРИТЕТ"
    return {
        "score": score,
        "verdict": verdict,
        "reasons": reasons,
        "estimated_purchase_total": estimate["estimated_total"],
        "catalog_coverage": estimate["coverage"],
        "reliable_price_coverage": estimate["reliable_coverage"],
        "priced_positions": estimate["priced_positions"],
        "positions_count": estimate["positions_count"],
        "matches": estimate["items"],
        "price_estimation_version": 3,
    }
