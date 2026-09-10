import re
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


def _score(request_name, quote):
    a = _norm(request_name)
    b = _norm(quote.name)
    if not a or not b:
        return 0.0
    seq = SequenceMatcher(None, a, b).ratio()
    ta, tb = _tokens(a), _tokens(b)
    overlap = len(ta & tb) / max(1, len(ta))
    bonus = 0.0
    for needle in (quote.sku, quote.article, quote.brand):
        n = _norm(str(needle or ""))
        if n and n in a:
            bonus += 0.12
    return round(min(1.0, 0.55 * seq + 0.45 * overlap + bonus), 3)


def _label(score):
    if score >= 0.82:
        return "Высокое"
    if score >= 0.62:
        return "Среднее"
    return "Низкое"


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
    items = []
    for pos, item in enumerate(order.get("orderItems") or [], 1):
        name = item.get("goodName") or ""
        quotes = vi.search(name, limit=limit)
        candidates = []
        for q in quotes:
            if q.error:
                candidates.append({"error": q.error})
                continue
            score = _score(name, q)
            d = q.to_dict()
            d["match_score"] = score
            d["match_level"] = _label(score)
            candidates.append(d)
        candidates.sort(key=lambda x: (x.get("error") is not None, -(x.get("match_score") or 0), x.get("price") or 10**18))
        items.append({
            "position": pos,
            "order_item_id": item.get("id"),
            "requested_name": name,
            "quantity": item.get("count"),
            "unit": _unit_name(item),
            "candidates": candidates,
            "best_candidate": next((x for x in candidates if not x.get("error")), None),
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
            product = best.get("name") or "—"
            sku = best.get("sku") or "—"
            article = best.get("article") or "—"
            link = best.get("url")
            product_html = f"<a target='_blank' href='{esc(link)}'>{esc(product)}</a>" if link else esc(product)
            rows += f"<tr><td>{item['position']}</td><td>{esc(item['requested_name'])}</td><td>{esc(item['quantity'])} {esc(item['unit'])}</td><td>{product_html}</td><td>{esc(sku)}</td><td>{esc(article)}</td><td>{level} ({score if score is not None else '—'})</td><td><b>{f'{price:,.2f} ₽'.replace(',', ' ') if isinstance(price,(int,float)) else '—'}</b></td><td>{esc(stock) if stock is not None else '—'}</td><td>{esc(delivery)}</td></tr>"
        html = f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ВИ — заявка {order_id}</title><style>body{{font-family:Arial;margin:24px;background:#f5f5f5;color:#222}}.card{{background:#fff;border-radius:12px;padding:18px;overflow:auto}}table{{width:100%;border-collapse:collapse;font-size:13px}}th{{background:#eee;text-align:left;padding:9px}}td{{padding:9px;border-bottom:1px solid #eee;vertical-align:top}}a{{color:#4c39d4;font-weight:bold;text-decoration:none}}.note{{color:#666;font-size:12px}}</style></head><body><p><a href='/dashboard/analysis/order/{order_id}'>← К заявке</a></p><h1>ВсеИнструменты — заявка {order_id}</h1><p class='note'>Автопоиск по полному наименованию. Совпадение — предварительная оценка; неоднозначные позиции требуют проверки оператором.</p><div class='card'><table><thead><tr><th>№</th><th>Позиция заявки</th><th>Кол-во</th><th>Товар ВИ</th><th>SKU</th><th>Артикул</th><th>Соответствие</th><th>ОПТ цена</th><th>Остаток</th><th>Доставка</th></tr></thead><tbody>{rows}</tbody></table></div></body></html>"""
        return HTMLResponse(html)
