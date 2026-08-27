import json
import os
from datetime import date

import requests
from fastapi import Form, HTTPException
from fastapi.responses import HTMLResponse

OFFER_API_PATH = os.getenv("ZAKUPAY_OFFER_API_PATH", "/api/v1/offers")


def install_offer_panel(app, fetch_all_orders, zakupay_headers, zakupay_base_url, esc):
    def _get_order(order_id: int):
        orders = fetch_all_orders()
        order = next((o for o in orders if o.get("id") == order_id), None)
        if not order:
            orders = fetch_all_orders(force=True)
            order = next((o for o in orders if o.get("id") == order_id), None)
        if not order:
            raise HTTPException(status_code=404, detail="Заявка не найдена")
        return order

    def _build_payload(order, form):
        items = []
        for item in order.get("orderItems") or []:
            iid = str(item.get("id"))
            price_raw = form.get(f"price_{iid}")
            qty_raw = form.get(f"qty_{iid}")
            enabled = form.get(f"use_{iid}") == "1"
            if not enabled:
                continue
            try:
                price = float(price_raw)
                qty = float(qty_raw)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"Некорректная цена/количество по позиции {iid}")
            items.append({
                "orderItemId": item.get("id"),
                "name": item.get("goodName"),
                "quantity": qty,
                "price": price,
                "vat": float(form.get("vat", 0) or 0),
            })
        if not items:
            raise HTTPException(status_code=400, detail="Не выбрано ни одной позиции")

        total = round(sum(x["quantity"] * x["price"] for x in items), 2)
        payload = {
            "orderId": order.get("id"),
            "producerOfferNumber": form.get("producer_offer_number") or None,
            "producerOfferDate": form.get("producer_offer_date") or None,
            "vat": float(form.get("vat", 0) or 0),
            "prepaymentPercent": float(form.get("prepayment_percent", 0) or 0),
            "delay": int(form.get("delay_days", 0) or 0),
            "deliveryIncluded": form.get("delivery_included") == "1",
            "allInStock": form.get("all_in_stock") == "1",
            "comment": form.get("comment") or None,
            "totalAmount": total,
            "offerItems": items,
        }
        return {k: v for k, v in payload.items() if v is not None}

    @app.get("/dashboard/order/{order_id}/offer", response_class=HTMLResponse)
    def offer_builder(order_id: int):
        order = _get_order(order_id)
        rows = ""
        for item in order.get("orderItems") or []:
            iid = item.get("id")
            rows += (
                f"<tr><td><input type='checkbox' name='use_{iid}' value='1' checked></td>"
                f"<td>{esc(item.get('goodName'))}</td>"
                f"<td><input name='qty_{iid}' type='number' step='0.001' value='{esc(item.get('count') or 0)}'></td>"
                f"<td><input name='price_{iid}' type='number' step='0.01' min='0' required></td></tr>"
            )
        html = f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><title>Предложение {order_id}</title>
<style>body{{font-family:Arial;margin:24px;background:#f5f5f5}}.card{{background:#fff;padding:18px;border-radius:12px;margin-bottom:18px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}}label{{font-size:12px;color:#666;display:block;margin-bottom:4px}}input,select,textarea{{width:100%;box-sizing:border-box;padding:9px}}table{{width:100%;border-collapse:collapse}}th,td{{padding:8px;border-bottom:1px solid #eee}}button{{padding:11px 16px;background:#222;color:white;border:0;border-radius:8px}}</style></head><body>
<p><a href='/dashboard/order/{order_id}'>← К заявке</a></p><h1>Сформировать предложение в Закупай</h1>
<div class='card'><form method='post' action='/dashboard/order/{order_id}/offer/submit'>
<div class='grid'>
<div><label>Номер счёта/предложения</label><input name='producer_offer_number' required></div>
<div><label>Дата</label><input type='date' name='producer_offer_date' value='{date.today().isoformat()}' required></div>
<div><label>НДС, доля</label><select name='vat'><option value='0.2'>20%</option><option value='0'>Без НДС</option></select></div>
<div><label>Предоплата, %</label><input type='number' name='prepayment_percent' min='0' max='100' value='100'></div>
<div><label>Отсрочка, дней</label><input type='number' name='delay_days' min='0' value='0'></div>
<div><label>Доставка включена</label><select name='delivery_included'><option value='1'>Да</option><option value='0'>Нет</option></select></div>
<div><label>Всё в наличии</label><select name='all_in_stock'><option value='1'>Да</option><option value='0'>Нет</option></select></div>
</div><p><label>Комментарий</label><textarea name='comment'></textarea></p>
<h3>Позиции</h3><table><thead><tr><th></th><th>Наименование</th><th>Количество</th><th>Цена за единицу, ₽</th></tr></thead><tbody>{rows}</tbody></table>
<p style='background:#fff7df;padding:10px'>Отправка выполняется только после нажатия кнопки ниже. Передача идёт в текущий endpoint Закупай <code>{esc(OFFER_API_PATH)}</code>.</p>
<button type='submit'>Отправить предложение в Закупай</button></form></div></body></html>"""
        return HTMLResponse(html)

    @app.post("/dashboard/order/{order_id}/offer/submit", response_class=HTMLResponse)
    async def submit_offer(order_id: int, request):
        order = _get_order(order_id)
        form = await request.form()
        payload = _build_payload(order, form)
        url = zakupay_base_url.rstrip("/") + OFFER_API_PATH
        try:
            r = requests.post(url, headers={**zakupay_headers(), "Content-Type": "application/json"}, json=payload, timeout=30)
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"Ошибка соединения с Закупай: {exc}")
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text[:5000]}
        if not r.ok:
            return HTMLResponse(
                f"<h1>Закупай отклонил предложение</h1><p>HTTP {r.status_code}</p>"
                f"<pre>{esc(json.dumps(data, ensure_ascii=False, indent=2))}</pre>"
                f"<h3>Отправленный JSON</h3><pre>{esc(json.dumps(payload, ensure_ascii=False, indent=2))}</pre>"
                f"<p><a href='/dashboard/order/{order_id}/offer'>← Исправить</a></p>",
                status_code=r.status_code,
            )
        return HTMLResponse(
            f"<h1>Предложение отправлено</h1><pre>{esc(json.dumps(data, ensure_ascii=False, indent=2))}</pre>"
            f"<p><a href='/dashboard/order/{order_id}'>Вернуться к заявке</a></p>"
        )
