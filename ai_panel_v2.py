from fastapi import HTTPException, Query
from fastapi.responses import HTMLResponse

import ai_panel


def install_ai_panel_v2(app, fetch_all_orders, compact_order, filter_orders, api_filter_dict,
                        has_my_offer, max_competitors, esc, non_purchase_predicate):
    def _exclude_flag(values):
        if not values:
            return True
        return str(values[-1]).strip().lower() not in {"0", "false", "no", "off", ""}

    def _norm(value):
        return str(value or "").strip().lower().replace("ё", "е")

    def _matches_local_search(order, keyword="", title="", order_id=None):
        if order_id is not None and order.get("id") != order_id:
            return False

        title_q = _norm(title)
        if title_q and title_q not in _norm(order.get("name")):
            return False

        keyword_q = _norm(keyword)
        if keyword_q:
            customer = order.get("customer") or {}
            region = order.get("region") or {}
            parts = [
                order.get("name"),
                customer.get("shortName"), customer.get("name"), customer.get("inn"),
                region.get("name"), order.get("deliveryAddress"),
            ]
            for item in order.get("orderItems") or []:
                parts.extend([
                    item.get("goodName"), item.get("comment"),
                    (item.get("category") or {}).get("name"),
                    (item.get("unit") or {}).get("name"),
                ])
            haystack = " | ".join(_norm(x) for x in parts if x not in (None, ""))
            if keyword_q not in haystack:
                return False
        return True

    def _ranked(payment="all", region="", category="", min_positions=0, max_competitors_value=None,
                only_without_my_offer=False, only_not_enough=False, exclude_non_purchase=True,
                min_score=0, min_estimated_total=0, refresh=False, api_filters=None,
                keyword="", title="", order_id=None):
        orders = fetch_all_orders(force=refresh, api_filters=api_filters or {})
        orders = filter_orders(
            orders, payment, region, category, min_positions,
            max_competitors_value, only_without_my_offer,
        )
        orders = [o for o in orders if _matches_local_search(o, keyword, title, order_id)]
        if exclude_non_purchase:
            orders = [o for o in orders if not non_purchase_predicate(o)]

        rows = []
        for order in orders:
            analysis = ai_panel.analyze_order(order, has_my_offer, max_competitors)
            est = analysis.get("estimated_purchase_total") or 0
            if analysis["score"] < min_score or est < min_estimated_total:
                continue
            rows.append({"order": compact_order(order), "analysis": analysis})
        rows.sort(
            key=lambda x: (x["analysis"]["score"], x["analysis"].get("estimated_purchase_total") or 0),
            reverse=True,
        )
        return rows

    @app.get("/analysis/ranked")
    def ranked_orders(
        payment: str = Query("all", pattern="^(all|prepayment|delay)$"),
        region: str = "", category: str = "", keyword: str = "", title: str = "",
        order_id: int | None = None, inn: str = "", min_positions: int = 0,
        max_competitors_value: int | None = Query(None, alias="max_competitors"),
        only_without_my_offer: bool = False, onlyNotEnough: bool = False,
        exclude_non_purchase: list[str] = Query(default=["true"]),
        min_score: int = 0, min_estimated_total: float = 0, refresh: bool = False,
        creationDateFrom: str = "", creationDateTo: str = "",
        finishDateFrom: str = "", finishDateTo: str = "",
        delayFrom: int | None = None, delayTo: int | None = None,
        offersState: str = "", tookInWork: bool | None = None,
    ):
        exclude_flag = _exclude_flag(exclude_non_purchase)
        af = api_filter_dict(
            creationDateFrom=creationDateFrom, creationDateTo=creationDateTo,
            finishDateFrom=finishDateFrom, finishDateTo=finishDateTo,
            delayFrom=delayFrom, delayTo=delayTo, offersState=offersState,
            onlyNotEnough=onlyNotEnough, tookInWork=tookInWork, inn=inn,
        )
        rows = _ranked(
            payment, region, category, min_positions, max_competitors_value,
            only_without_my_offer, onlyNotEnough, exclude_flag,
            min_score, min_estimated_total, refresh, af, keyword, title, order_id,
        )
        return {
            "count": len(rows), "api_filters": af,
            "exclude_non_purchase": exclude_flag, "orders": rows,
        }

    @app.get("/analysis/order/{order_id}")
    def one_order_analysis(order_id: int, refresh: bool = False, ai: bool = False):
        orders = fetch_all_orders(force=refresh)
        order = next((o for o in orders if o.get("id") == order_id), None)
        if not order:
            raise HTTPException(status_code=404, detail="Заявка не найдена")
        heuristic = ai_panel.analyze_order(order, has_my_offer, max_competitors)
        result = {"order": compact_order(order), "analysis": heuristic}
        if ai:
            result["ai"] = ai_panel._call_openai(result["order"], heuristic)
        return result

    @app.get("/dashboard/analysis", response_class=HTMLResponse)
    def analysis_dashboard(
        payment: str = "all", region: str = "", category: str = "", keyword: str = "", title: str = "",
        order_id: int | None = None, inn: str = "", min_positions: int = 0,
        max_competitors_value: int | None = Query(None, alias="max_competitors"),
        only_without_my_offer: bool = False, onlyNotEnough: bool = False,
        exclude_non_purchase: list[str] = Query(default=["true"]),
        min_score: int = 0, min_estimated_total: float = 0, refresh: bool = False,
        creationDateFrom: str = "", creationDateTo: str = "",
        finishDateFrom: str = "", finishDateTo: str = "",
        delayFrom: int | None = None, delayTo: int | None = None,
        offersState: str = "", tookInWork: bool | None = None,
    ):
        exclude_flag = _exclude_flag(exclude_non_purchase)
        af = api_filter_dict(
            creationDateFrom=creationDateFrom, creationDateTo=creationDateTo,
            finishDateFrom=finishDateFrom, finishDateTo=finishDateTo,
            delayFrom=delayFrom, delayTo=delayTo, offersState=offersState,
            onlyNotEnough=onlyNotEnough, tookInWork=tookInWork, inn=inn,
        )
        rows = _ranked(
            payment, region, category, min_positions, max_competitors_value,
            only_without_my_offer, onlyNotEnough, exclude_flag,
            min_score, min_estimated_total, refresh, af, keyword, title, order_id,
        )

        def ck(v): return "checked" if v else ""
        def val(v): return "" if v is None else esc(v)

        payment_options = "".join(
            f"<option value='{k}' {'selected' if payment == k else ''}>{v}</option>"
            for k, v in {"all":"Все условия","prepayment":"Предоплата / без отсрочки","delay":"Есть отсрочка"}.items()
        )
        state_options = "".join(
            f"<option value='{k}' {'selected' if offersState == k else ''}>{v}</option>"
            for k, v in {"":"Любой","ACCEPTED":"ACCEPTED","DECLINED":"DECLINED","WAITING":"WAITING"}.items()
        )

        table_rows = ""
        for row in rows:
            order, a = row["order"], row["analysis"]
            est = a.get("estimated_purchase_total")
            est_text = f"{est:,.0f} ₽".replace(",", " ") if est else "—"
            payment_text = "Предоплата" if order.get("delay_days") == 0 else (
                f"{order.get('delay_days')} дней" if order.get("delay_days") is not None else "—"
            )
            table_rows += (
                f"<tr><td><a href='/dashboard/order/{order['id']}'>{order['id']}</a></td>"
                f"<td>{esc(order.get('name'))}</td><td>{esc(order.get('customer'))}</td>"
                f"<td>{esc(order.get('region'))}</td><td>{esc(payment_text)}</td>"
                f"<td>{order.get('positions_count')}</td><td>{order.get('max_competitors')}</td>"
                f"<td>{est_text}</td><td><b>{a['score']}</b></td><td>{esc(a['verdict'])}</td>"
                f"<td><a href='/analysis/order/{order['id']}?ai=true'>ИИ-анализ JSON</a></td></tr>"
            )

        html_page = f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Анализ заявок</title><style>
body{{font-family:Arial,sans-serif;margin:24px;background:#f5f5f5;color:#222}}.card{{background:#fff;padding:18px;border-radius:12px;margin-bottom:18px;box-shadow:0 2px 8px rgba(0,0,0,.08)}}
.filters{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;align-items:end}}label{{font-size:12px;color:#666;display:block;margin-bottom:5px}}input,select{{width:100%;box-sizing:border-box;padding:9px;border:1px solid #ccc;border-radius:7px}}.check{{display:flex;gap:8px;align-items:center;padding:8px 0}}.check input{{width:auto}}button,a.btn{{padding:10px 14px;background:#222;color:#fff;border:0;border-radius:8px;text-decoration:none;display:inline-block}}details{{margin-top:14px;border-top:1px solid #eee;padding-top:12px}}summary{{cursor:pointer;font-weight:bold}}table{{width:100%;border-collapse:collapse;font-size:13px}}th{{background:#eee;text-align:left;padding:9px}}td{{padding:9px;border-bottom:1px solid #eee;vertical-align:top}}a{{color:#4c39d4;font-weight:bold;text-decoration:none}}.note{{font-size:12px;color:#666;margin-top:8px}}
</style></head><body>
<p><a href='/dashboard'>← Назад к заявкам</a> · <a href='/logout'>Выйти</a></p><h1>Анализ привлекательности заявок</h1>
<div class='card'><form method='get'>
<div class='filters'>
<div><label>Ключевое слово</label><input name='keyword' value='{esc(keyword)}' placeholder='товар, артикул, комментарий...'></div>
<div><label>Название заявки содержит</label><input name='title' value='{esc(title)}' placeholder='ремонт, расходники...'></div>
<div><label>ID заявки</label><input type='number' name='order_id' value='{val(order_id)}' placeholder='37088126'></div>
<div><label>ИНН заказчика</label><input name='inn' value='{esc(inn)}' placeholder='10 или 12 цифр'></div>
<div><label>Категория содержит</label><input name='category' value='{esc(category)}' placeholder='Метизы'></div>
<div><label>Регион</label><input name='region' value='{esc(region)}' placeholder='Москва или Россия'></div>
<div><label>Оплата</label><select name='payment'>{payment_options}</select></div>
<div><label>Мин. позиций</label><input type='number' name='min_positions' value='{min_positions}' min='0'></div>
<div><label>Макс. конкурентов</label><input type='number' name='max_competitors' value='{val(max_competitors_value)}' min='0'></div>
<div><label>Оценочная закупка от, ₽</label><input type='number' name='min_estimated_total' value='{min_estimated_total}' min='0'></div>
<div><label>Мин. балл</label><input type='number' name='min_score' value='{min_score}' min='0' max='100'></div>
<div class='check'><input type='checkbox' name='onlyNotEnough' value='true' {ck(onlyNotEnough)}><span>Только «мало счетов»</span></div>
<div class='check'><input type='checkbox' name='only_without_my_offer' value='true' {ck(only_without_my_offer)}><span>Без моего предложения</span></div>
<div class='check'><input type='hidden' name='exclude_non_purchase' value='false'><input type='checkbox' name='exclude_non_purchase' value='true' {ck(exclude_flag)}><span>Исключать тендеры, расчёты и сбор КП</span></div>
<div><button>Отобрать</button></div><div><a class='btn' href='/dashboard/analysis?refresh=true'>Обновить</a></div>
</div>
<details><summary>Дополнительные фильтры</summary><div class='filters'>
<div><label>Создана от</label><input name='creationDateFrom' value='{esc(creationDateFrom)}' placeholder='2026-08-01'></div>
<div><label>Создана до</label><input name='creationDateTo' value='{esc(creationDateTo)}'></div>
<div><label>Поставка от</label><input name='finishDateFrom' value='{esc(finishDateFrom)}'></div>
<div><label>Поставка до</label><input name='finishDateTo' value='{esc(finishDateTo)}'></div>
<div><label>Отсрочка от, дней</label><input type='number' name='delayFrom' value='{val(delayFrom)}'></div>
<div><label>Отсрочка до, дней</label><input type='number' name='delayTo' value='{val(delayTo)}'></div>
<div><label>Статус моего счёта</label><select name='offersState'>{state_options}</select></div>
<div class='check'><input type='checkbox' name='tookInWork' value='true' {ck(tookInWork)}><span>Взято в работу</span></div>
</div><div class='note'>ИНН передаётся в API Закупай и может требовать платную лицензию. Ключевое слово, название и ID заявки фильтруются локально.</div></details>
</form></div>
<div class='card'><b>Найдено:</b> {len(rows)}</div>
<div class='card' style='overflow:auto'><table><thead><tr><th>ID</th><th>Заявка</th><th>Заказчик</th><th>Регион</th><th>Оплата</th><th>Позиций</th><th>Конкурентов</th><th>Оценочная закупка</th><th>Балл</th><th>Решение</th><th>ИИ</th></tr></thead><tbody>{table_rows}</tbody></table></div>
</body></html>"""
        return HTMLResponse(content=html_page)
