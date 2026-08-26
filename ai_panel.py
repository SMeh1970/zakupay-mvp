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
    best, best_score = None, 0.0
    for row in catalog:
        candidate = _norm(row.get("name"))
        if not candidate:
            continue
        score = SequenceMatcher(None, target, candidate).ratio()
        if score > best_score:
            best_score, best = score, row
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

    matches, estimated_total, matched_qty = [], 0.0, 0
    for item in items:
        match = _best_match(item.get("goodName"), catalog) if catalog else None
        matches.append({
            "item_id": item.get("id"),
            "name": item.get("goodName"),
            "quantity": item.get("count"),
            "match": match,
        })
        if match and match.get("score", 0) >= 0.72 and isinstance(match.get("price"), (int, float)):
            try:
                estimated_total += float(item.get("count") or 0) * float(match["price"])
                matched_qty += 1
            except (TypeError, ValueError):
                pass

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
    verdict = "БРАТЬ В РАБОТУ" if score >= 70 else "ПРОВЕРИТЬ" if score >= 45 else "НИЗКИЙ ПРИОРИТЕТ"
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
        "output": {"summary": "краткий вывод", "risks": ["риски"], "actions": ["что проверить оператору"], "priority": "high|medium|low"},
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


def install_ai_panel(app, fetch_all_orders, compact_order, filter_orders, api_filter_dict, has_my_offer, max_competitors, esc):
    def _ranked(payment="all", region="", category="", min_positions=0, max_competitors_value=None,
                only_without_my_offer=False, min_score=0, min_estimated_total=0, refresh=False, api_filters=None):
        orders = fetch_all_orders(force=refresh, api_filters=api_filters or {})
        orders = filter_orders(orders, payment, region, category, min_positions, max_competitors_value, only_without_my_offer)
        rows = []
        for order in orders:
            analysis = analyze_order(order, has_my_offer, max_competitors)
            est = analysis.get("estimated_purchase_total") or 0
            if analysis["score"] < min_score or est < min_estimated_total:
                continue
            rows.append({"order": compact_order(order), "analysis": analysis})
        rows.sort(key=lambda x: (x["analysis"]["score"], x["analysis"].get("estimated_purchase_total") or 0), reverse=True)
        return rows

    @app.get("/analysis/ranked")
    def ranked_orders(
        payment: str = Query("all", pattern="^(all|prepayment|delay)$"), region: str = "", category: str = "",
        min_positions: int = 0, max_competitors_value: int | None = Query(None, alias="max_competitors"),
        only_without_my_offer: bool = False, min_score: int = 0, min_estimated_total: float = 0, refresh: bool = False,
        creationDateFrom: str = "", creationDateTo: str = "", finishDateFrom: str = "", finishDateTo: str = "",
        publicDateFrom: str = "", publicDateTo: str = "", updateTimeFrom: str = "", updateTimeTo: str = "",
        delayFrom: int | None = None, delayTo: int | None = None, offersState: str = "",
        onlyWithMyOffers: bool | None = None, onlyNotEnough: bool | None = None, tookInWork: bool | None = None,
        inn: str = "", senderId: int | None = None, company: int | None = None, category_id: int | None = None,
        region_id: int | None = None, showNotInteresting: bool | None = None, onlyNotInteresting: bool | None = None,
        ignoreKeywordFilter: bool | None = None,
    ):
        af = api_filter_dict(**locals())
        rows = _ranked(payment, region, category, min_positions, max_competitors_value, only_without_my_offer,
                       min_score, min_estimated_total, refresh, af)
        return {"count": len(rows), "api_filters": af, "orders": rows}

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
    def analysis_dashboard(
        payment: str = "all", region: str = "", category: str = "", min_positions: int = 0,
        max_competitors_value: int | None = Query(None, alias="max_competitors"), only_without_my_offer: bool = False,
        min_score: int = 0, min_estimated_total: float = 0, refresh: bool = False,
        creationDateFrom: str = "", creationDateTo: str = "", finishDateFrom: str = "", finishDateTo: str = "",
        publicDateFrom: str = "", publicDateTo: str = "", updateTimeFrom: str = "", updateTimeTo: str = "",
        delayFrom: int | None = None, delayTo: int | None = None, offersState: str = "",
        onlyWithMyOffers: bool | None = None, onlyNotEnough: bool | None = None, tookInWork: bool | None = None,
        inn: str = "", senderId: int | None = None, company: int | None = None, category_id: int | None = None,
        region_id: int | None = None, showNotInteresting: bool | None = None, onlyNotInteresting: bool | None = None,
        ignoreKeywordFilter: bool | None = None,
    ):
        local_vars = locals().copy()
        af = api_filter_dict(**local_vars)
        rows = _ranked(payment, region, category, min_positions, max_competitors_value, only_without_my_offer,
                       min_score, min_estimated_total, refresh, af)

        table_rows = ""
        for row in rows:
            order, a = row["order"], row["analysis"]
            est = a.get("estimated_purchase_total")
            est_text = f"{est:,.0f} ₽".replace(",", " ") if est else "—"
            payment_text = "Предоплата" if order.get("delay_days") == 0 else (f"{order.get('delay_days')} дней" if order.get("delay_days") is not None else "—")
            table_rows += f"""
            <tr><td><a href='/dashboard/order/{order['id']}'>{order['id']}</a></td><td>{esc(order.get('name'))}</td>
            <td>{esc(order.get('customer'))}</td><td>{esc(order.get('region'))}</td><td>{esc(payment_text)}</td>
            <td>{order.get('positions_count')}</td><td>{order.get('max_competitors')}</td><td>{est_text}</td>
            <td><b>{a['score']}</b></td><td>{esc(a['verdict'])}</td><td><a href='/analysis/order/{order['id']}?ai=true'>ИИ-анализ JSON</a></td></tr>"""

        def ck(v): return "checked" if v else ""
        def val(v): return "" if v is None else esc(v)
        payment_options = "".join(
            f"<option value='{k}' {'selected' if payment == k else ''}>{v}</option>"
            for k, v in {"all":"Все","prepayment":"Предоплата","delay":"Отсрочка"}.items()
        )
        state_options = "".join(
            f"<option value='{k}' {'selected' if offersState == k else ''}>{v}</option>"
            for k, v in {"":"Любой","ACCEPTED":"ACCEPTED","DECLINED":"DECLINED","WAITING":"WAITING"}.items()
        )

        html_page = f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
        <title>Анализ заявок</title><style>
        body{{font-family:Arial,sans-serif;margin:24px;background:#f5f5f5;color:#222}}.card{{background:white;padding:18px;border-radius:12px;margin-bottom:18px;box-shadow:0 2px 8px rgba(0,0,0,.08)}}
        .filters{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;align-items:end}}label{{font-size:12px;color:#666;display:block;margin-bottom:5px}}input,select{{width:100%;box-sizing:border-box;padding:9px;border:1px solid #ccc;border-radius:7px}}.check{{display:flex;gap:8px;align-items:center}}.check input{{width:auto}}button,a.btn{{padding:10px 14px;background:#222;color:#fff;border:0;border-radius:8px;text-decoration:none;display:inline-block}}details{{margin-top:14px;border-top:1px solid #eee;padding-top:12px}}summary{{cursor:pointer;font-weight:bold}}table{{width:100%;border-collapse:collapse;font-size:13px}}th{{background:#eee;text-align:left;padding:9px}}td{{padding:9px;border-bottom:1px solid #eee;vertical-align:top}}a{{color:#4c39d4;font-weight:bold;text-decoration:none}}.note{{font-size:12px;color:#7a5800;background:#fff7df;padding:9px;border-radius:7px;margin:10px 0}}
        </style></head><body>
        <p><a href='/dashboard'>← Назад к заявкам</a> · <a href='/logout'>Выйти</a></p><h1>Анализ привлекательности заявок</h1>
        <div class='card'><form method='get'>
        <div class='filters'>
          <div><label>Оплата</label><select name='payment'>{payment_options}</select></div>
          <div><label>Регион содержит</label><input name='region' value='{esc(region)}' placeholder='Москва'></div>
          <div><label>Категория содержит</label><input name='category' value='{esc(category)}' placeholder='Метизы'></div>
          <div><label>Мин. позиций</label><input type='number' name='min_positions' value='{min_positions}' min='0'></div>
          <div><label>Макс. конкурентов</label><input type='number' name='max_competitors' value='{val(max_competitors_value)}' min='0'></div>
          <div><label>Мин. балл ИИ</label><input type='number' name='min_score' value='{min_score}' min='0' max='100'></div>
          <div><label>Оценочная закупка от, ₽</label><input type='number' name='min_estimated_total' value='{min_estimated_total}' min='0'></div>
          <div class='check'><input type='checkbox' name='only_without_my_offer' value='true' {ck(only_without_my_offer)}><span>Без моего предложения</span></div>
          <div><button>Отобрать</button></div><div><a class='btn' href='/dashboard/analysis?refresh=true'>Обновить</a></div>
        </div>
        <details><summary>Расширенные фильтры Закупай</summary>
        <div class='note'>Параметры передаются непосредственно в API Закупай. Часть фильтров может зависеть от лицензии.</div>
        <div class='filters'>
          <div><label>Создана от</label><input name='creationDateFrom' value='{esc(creationDateFrom)}' placeholder='2026-08-01'></div>
          <div><label>Создана до</label><input name='creationDateTo' value='{esc(creationDateTo)}' placeholder='2026-08-31'></div>
          <div><label>Поставка от</label><input name='finishDateFrom' value='{esc(finishDateFrom)}'></div>
          <div><label>Поставка до</label><input name='finishDateTo' value='{esc(finishDateTo)}'></div>
          <div><label>Размещена от</label><input name='publicDateFrom' value='{esc(publicDateFrom)}'></div>
          <div><label>Размещена до</label><input name='publicDateTo' value='{esc(publicDateTo)}'></div>
          <div><label>Обновлена от</label><input name='updateTimeFrom' value='{esc(updateTimeFrom)}'></div>
          <div><label>Обновлена до</label><input name='updateTimeTo' value='{esc(updateTimeTo)}'></div>
          <div><label>Отсрочка от, дней</label><input type='number' name='delayFrom' value='{val(delayFrom)}'></div>
          <div><label>Отсрочка до, дней</label><input type='number' name='delayTo' value='{val(delayTo)}'></div>
          <div><label>Статус моего счёта</label><select name='offersState'>{state_options}</select></div>
          <div><label>ИНН заказчика</label><input name='inn' value='{esc(inn)}'></div>
          <div><label>ID заявки покупателя</label><input type='number' name='senderId' value='{val(senderId)}'></div>
          <div><label>ID компании</label><input type='number' name='company' value='{val(company)}'></div>
          <div><label>ID категории</label><input type='number' name='category_id' value='{val(category_id)}'></div>
          <div><label>ID региона</label><input type='number' name='region_id' value='{val(region_id)}'></div>
          <div class='check'><input type='checkbox' name='onlyWithMyOffers' value='true' {ck(onlyWithMyOffers)}><span>Только с моими счетами</span></div>
          <div class='check'><input type='checkbox' name='onlyNotEnough' value='true' {ck(onlyNotEnough)}><span>Мало счетов</span></div>
          <div class='check'><input type='checkbox' name='tookInWork' value='true' {ck(tookInWork)}><span>Взято в работу</span></div>
          <div class='check'><input type='checkbox' name='showNotInteresting' value='true' {ck(showNotInteresting)}><span>Показывать неинтересные</span></div>
          <div class='check'><input type='checkbox' name='onlyNotInteresting' value='true' {ck(onlyNotInteresting)}><span>Только неинтересные</span></div>
          <div class='check'><input type='checkbox' name='ignoreKeywordFilter' value='true' {ck(ignoreKeywordFilter)}><span>Игнорировать ключевые слова</span></div>
        </div></details>
        </form></div>
        <div class='card'><b>Найдено:</b> {len(rows)}<p>Стоимость появится после заполнения price_catalog.json. Без прайса рейтинг учитывает оплату, конкурентов, число позиций и наличие нашего предложения.</p></div>
        <div class='card' style='overflow:auto'><table><thead><tr><th>ID</th><th>Заявка</th><th>Заказчик</th><th>Регион</th><th>Оплата</th><th>Позиций</th><th>Конкурентов</th><th>Оценочная закупка</th><th>Балл</th><th>Решение</th><th>ИИ</th></tr></thead><tbody>{table_rows}</tbody></table></div>
        </body></html>"""
        return HTMLResponse(content=html_page)
