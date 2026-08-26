import json
import os
from difflib import SequenceMatcher

import requests
from fastapi import HTTPException, Query
from fastapi.responses import HTMLResponse

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
PRICE_CATALOG_PATH = os.getenv("PRICE_CATALOG_PATH", "price_catalog.json")


def _load_catalog():
    try:
        with open(PRICE_CATALOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _norm(text):
    return " ".join(str(text or "").lower().replace("ё", "е").split())


def _best_match(name, catalog):
    target = _norm(name)
    best = None
    best_score = 0.0
    for row in catalog:
        candidate = _norm(row.get("name"))
        if not candidate:
            continue
        score = SequenceMatcher(None, target, candidate).ratio()
        if score > best_score:
            best_score = score
            best = row
    if not best:
        return None
    return {
        "score": round(best_score, 3),
        "name": best.get("name"),
        "article": best.get("article"),
        "price": best.get("price"),
        "supplier": best.get("supplier"),
    }


def analyze_order(order, has_my_offer, max_competitors):
    items = order.get("orderItems") or []
    delay = order.get("delay")
    competitors = max_competitors(order)
    catalog = _load_catalog()

    score = 0
    reasons = []

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

    matches = []
    estimated_total = 0.0
    matched_qty = 0
    for item in items:
        match = _best_match(item.get("goodName"), catalog) if catalog else None
        row = {
            "item_id": item.get("id"),
            "name": item.get("goodName"),
            "quantity": item.get("count"),
            "match": match,
        }
        if match and match.get("score", 0) >= 0.72 and isinstance(match.get("price"), (int, float)):
            qty = item.get("count") or 0
            try:
                estimated_total += float(qty) * float(match["price"])
                matched_qty += 1
            except (TypeError, ValueError):
                pass
        matches.append(row)

    coverage = matched_qty / len(items) if items else 0
    if coverage >= 0.8:
        score += 12
        reasons.append("большинство позиций найдено в прайсе")
    elif coverage >= 0.5:
        score += 6

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
    if score >= 70:
        verdict = "БРАТЬ В РАБОТУ"
    elif score >= 45:
        verdict = "ПРОВЕРИТЬ"
    else:
        verdict = "НИЗКИЙ ПРИОРИТЕТ"

    return {
        "score": score,
        "verdict": verdict,
        "reasons": reasons,
        "estimated_purchase_total": round(estimated_total, 2) if estimated_total else None,
        "catalog_coverage": round(coverage, 3),
        "catalog_items": len(catalog),
        "matches": matches,
    }


def _call_openai(order_compact, heuristic):
    if not OPENAI_API_KEY:
        return {"enabled": False, "message": "OPENAI_API_KEY не настроен. Пока используется автоматический скоринг без внешнего ИИ."}

    prompt = {
        "task": "Оцени коммерческую привлекательность заявки поставщика. Не выдумывай цены и факты. Используй только данные заявки и автоматический скоринг.",
        "order": order_compact,
        "automatic_analysis": heuristic,
        "output": {
            "summary": "краткий вывод",
            "risks": ["риски"],
            "actions": ["что проверить оператору"],
            "priority": "high|medium|low"
        }
    }
    try:
        r = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": OPENAI_MODEL, "input": json.dumps(prompt, ensure_ascii=False)},
            timeout=45,
        )
        data = r.json()
        if not r.ok:
            return {"enabled": True, "error": data}
        text = data.get("output_text")
        if not text:
            chunks = []
            for item in data.get("output") or []:
                for content in item.get("content") or []:
                    if content.get("type") in ("output_text", "text") and content.get("text"):
                        chunks.append(content["text"])
            text = "\n".join(chunks)
        return {"enabled": True, "model": OPENAI_MODEL, "text": text or "Ответ ИИ пуст"}
    except Exception as exc:
        return {"enabled": True, "error": str(exc)}


def install_ai_panel(app, fetch_all_orders, compact_order, has_my_offer, max_competitors, esc):
    @app.get("/analysis/ranked")
    def ranked_orders(
        payment: str = Query("all", pattern="^(all|prepayment|delay)$"),
        min_score: int = 0,
        min_estimated_total: float = 0,
        refresh: bool = False,
    ):
        orders = fetch_all_orders(force=refresh)
        rows = []
        for order in orders:
            delay = order.get("delay")
            if payment == "prepayment" and delay != 0:
                continue
            if payment == "delay" and (delay is None or delay == 0):
                continue
            analysis = analyze_order(order, has_my_offer, max_competitors)
            est = analysis.get("estimated_purchase_total") or 0
            if analysis["score"] < min_score or est < min_estimated_total:
                continue
            rows.append({"order": compact_order(order), "analysis": analysis})
        rows.sort(key=lambda x: (x["analysis"]["score"], x["analysis"].get("estimated_purchase_total") or 0), reverse=True)
        return {"count": len(rows), "orders": rows}

    @app.get("/analysis/order/{order_id}")
    def one_order_analysis(order_id: int, refresh: bool = False, ai: bool = False):
        orders = fetch_all_orders(force=refresh)
        order = next((o for o in orders if o.get("id") == order_id), None)
        if not order:
            raise HTTPException(status_code=404, detail="Заявка не найдена")
        heuristic = analyze_order(order, has_my_offer, max_competitors)
        result = {"order": compact_order(order), "analysis": heuristic}
        if ai:
            result["ai"] = _call_openai(result["order"], heuristic)
        return result

    @app.get("/dashboard/analysis", response_class=HTMLResponse)
    def analysis_dashboard(payment: str = "all", min_score: int = 0, min_estimated_total: float = 0, refresh: bool = False):
        data = ranked_orders(payment, min_score, min_estimated_total, refresh)
        table_rows = ""
        for row in data["orders"]:
            order = row["order"]
            a = row["analysis"]
            est = a.get("estimated_purchase_total")
            est_text = f"{est:,.0f} ₽".replace(",", " ") if est else "—"
            table_rows += f"""
            <tr>
              <td><a href='/dashboard/order/{order['id']}'>{order['id']}</a></td>
              <td>{esc(order.get('name'))}</td>
              <td>{esc(order.get('customer'))}</td>
              <td>{esc(order.get('payment_type'))}</td>
              <td>{order.get('positions_count')}</td>
              <td>{order.get('max_competitors')}</td>
              <td>{est_text}</td>
              <td><b>{a['score']}</b></td>
              <td>{esc(a['verdict'])}</td>
              <td><a href='/analysis/order/{order['id']}?ai=true'>ИИ-анализ JSON</a></td>
            </tr>"""

        html_page = f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
        <title>Анализ заявок</title><style>
        body{{font-family:Arial,sans-serif;margin:24px;background:#f5f5f5;color:#222}}.card{{background:white;padding:18px;border-radius:12px;margin-bottom:18px;box-shadow:0 2px 8px rgba(0,0,0,.08)}}
        form{{display:flex;gap:12px;flex-wrap:wrap;align-items:end}}label{{font-size:12px;color:#666;display:block}}input,select{{padding:9px;border:1px solid #ccc;border-radius:7px}}button,a.btn{{padding:10px 14px;background:#222;color:#fff;border:0;border-radius:8px;text-decoration:none}}table{{width:100%;border-collapse:collapse;font-size:13px}}th{{background:#eee;text-align:left;padding:9px}}td{{padding:9px;border-bottom:1px solid #eee;vertical-align:top}}a{{color:#4c39d4;font-weight:bold;text-decoration:none}}
        </style></head><body>
        <p><a href='/dashboard'>← Назад к заявкам</a> · <a href='/logout'>Выйти</a></p><h1>Анализ привлекательности заявок</h1>
        <div class='card'><form method='get'><div><label>Оплата</label><select name='payment'><option value='all' {'selected' if payment=='all' else ''}>Все</option><option value='prepayment' {'selected' if payment=='prepayment' else ''}>Предоплата</option><option value='delay' {'selected' if payment=='delay' else ''}>Отсрочка</option></select></div><div><label>Мин. балл</label><input type='number' name='min_score' value='{min_score}' min='0' max='100'></div><div><label>Оценочная закупка от, ₽</label><input type='number' name='min_estimated_total' value='{min_estimated_total}' min='0'></div><button>Отобрать</button><a class='btn' href='/dashboard/analysis?refresh=true'>Обновить</a></form></div>
        <div class='card'><b>Найдено:</b> {data['count']}<p>Стоимость появится после заполнения price_catalog.json. Без прайса рейтинг всё равно учитывает оплату, конкурентов, число позиций и наличие нашего предложения.</p></div>
        <div class='card' style='overflow:auto'><table><thead><tr><th>ID</th><th>Заявка</th><th>Заказчик</th><th>Оплата</th><th>Позиций</th><th>Конкурентов</th><th>Оценочная закупка</th><th>Балл</th><th>Решение</th><th>ИИ</th></tr></thead><tbody>{table_rows}</tbody></table></div>
        </body></html>"""
        return HTMLResponse(content=html_page)
