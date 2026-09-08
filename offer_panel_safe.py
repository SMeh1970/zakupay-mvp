import json
import mimetypes
import os
import uuid
import xml.etree.ElementTree as ET
from datetime import date
from urllib.parse import urlparse

import requests
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

TOKEN_CHECK_PATH = os.getenv("ZAKUPAY_TOKEN_CHECK_PATH", "/api/v1/util/check/token?format=xml")
FILE_UPLOAD_PATH = os.getenv("ZAKUPAY_FILE_UPLOAD_PATH", "/core/files/upload?format=xml")
OFFER_CREATE_PATH = os.getenv("ZAKUPAY_OFFER_CREATE_PATH", "/core/offers/new/from/1c?format=xml")
MODULE_VERSION = os.getenv("ZAKUPAY_MODULE_VERSION", "zakupay-mvp-0.2")
DEFAULT_CURRENCY_ID = os.getenv("ZAKUPAY_CURRENCY_ID", "643")


def install_offer_panel(app, fetch_all_orders, zakupay_headers, zakupay_base_url, esc):
    def _get_order(order_id: int):
        orders = fetch_all_orders()
        order = next((o for o in orders if int(o.get("id") or 0) == order_id), None)
        if not order:
            orders = fetch_all_orders(force=True)
            order = next((o for o in orders if int(o.get("id") or 0) == order_id), None)
        if not order:
            raise HTTPException(status_code=404, detail="Заявка не найдена")
        return order

    def _headers(content_type=None):
        headers = dict(zakupay_headers())
        headers["moduleVersion"] = MODULE_VERSION
        host = urlparse(zakupay_base_url).hostname
        if host:
            headers["box"] = host
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _local_name(tag):
        return str(tag).rsplit("}", 1)[-1]

    def _xml_to_dict(elem):
        children = list(elem)
        if not children:
            return (elem.text or "").strip()
        result = {}
        for child in children:
            key = _local_name(child.tag)
            value = _xml_to_dict(child)
            if key in result:
                if not isinstance(result[key], list):
                    result[key] = [result[key]]
                result[key].append(value)
            else:
                result[key] = value
        return result

    def _decode_response(response):
        text = response.text or ""
        ctype = (response.headers.get("content-type") or "").lower()
        if "json" in ctype:
            try:
                return response.json()
            except ValueError:
                pass
        try:
            root = ET.fromstring(text)
            return {_local_name(root.tag): _xml_to_dict(root)}
        except ET.ParseError:
            try:
                return response.json()
            except ValueError:
                return {"raw": text[:10000]}

    def _find_first_key(obj, wanted):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key == wanted and not isinstance(value, (dict, list)) and str(value).strip():
                    return str(value).strip()
                found = _find_first_key(value, wanted)
                if found:
                    return found
        elif isinstance(obj, list):
            for value in obj:
                found = _find_first_key(value, wanted)
                if found:
                    return found
        return None

    def _unit_name(item):
        unit = item.get("unit") or item.get("unitName") or "шт."
        if isinstance(unit, dict):
            return unit.get("name") or unit.get("shortName") or str(unit.get("id") or "шт.")
        return str(unit)

    def _portal_accounts():
        url = zakupay_base_url.rstrip("/") + TOKEN_CHECK_PATH
        try:
            r = requests.get(url, headers=_headers(), timeout=5)
        except Exception as exc:
            return [], f"Не удалось получить список: {exc}"
        if not r.ok:
            return [], f"Закупай вернул HTTP {r.status_code}"
        try:
            root = ET.fromstring(r.text)
        except ET.ParseError:
            return [], "Ответ check/token не удалось разобрать как XML"

        accounts = []
        for node in root.iter():
            if _local_name(node.tag) != "account":
                continue
            fields = {}
            for child in list(node):
                name = _local_name(child.tag)
                if name in {"id", "bankName", "name"}:
                    fields[name] = (child.text or "").strip()
                elif name == "currency":
                    for nested in list(child):
                        n = _local_name(nested.tag)
                        if n in {"id", "name"}:
                            fields[f"currency_{n}"] = (nested.text or "").strip()
                elif name == "company":
                    for nested in list(child):
                        n = _local_name(nested.tag)
                        if n in {"id", "name", "shortName"}:
                            fields[f"company_{n}"] = (nested.text or "").strip()
            if fields.get("id"):
                company = fields.get("company_shortName") or fields.get("company_name") or "Юрлицо"
                bank = fields.get("bankName") or "банк не указан"
                account_name = fields.get("name") or "счёт"
                fields["label"] = f"{company} / {bank} ({account_name})"
                accounts.append(fields)
        return accounts, ""

    def _num(value, field_name):
        try:
            return float(str(value).replace(" ", "").replace(",", "."))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Некорректное значение: {field_name}")

    def _int(value, field_name):
        try:
            return int(float(str(value).replace(" ", "").replace(",", ".")))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Некорректное значение: {field_name}")

    def _stable_guid(*parts):
        raw = "|".join(str(x or "") for x in parts)
        return str(uuid.uuid5(uuid.NAMESPACE_URL, "zakupay-mvp:" + raw))

    def _build_payload(order, form, file_id):
        offer_items = []
        additional_items = []
        vat_rate = str(form.get("vat_rate") or "0.2")

        for item in order.get("orderItems") or []:
            iid = str(item.get("id"))
            if form.get(f"use_{iid}") != "1":
                continue
            qty = _num(form.get(f"qty_{iid}"), f"количество позиции {iid}")
            unit_price = _num(form.get(f"price_{iid}"), f"цена позиции {iid}")
            if qty <= 0 or unit_price < 0:
                raise HTTPException(status_code=400, detail=f"Количество/цена по позиции {iid} должны быть положительными")
            provider_name = str(form.get(f"provider_name_{iid}") or item.get("goodName") or "").strip()
            external_id = _stable_guid(order.get("id"), iid, provider_name)
            is_available = form.get(f"available_{iid}") == "1"
            row = {
                "providerGoodName": provider_name,
                "providerComment": str(form.get(f"provider_comment_{iid}") or "null"),
                "count": str(qty),
                "externalNomenclatureId": external_id,
                "unitName": str(form.get(f"unit_{iid}") or _unit_name(item)),
                "amount": str(round(qty * unit_price, 2)),
                "vatRate": {"rate": vat_rate},
                "isAvailable": "true" if is_available else "false",
                "item": {"orderItem": {"id": iid, "innerComment": external_id}},
            }
            if not is_available:
                raw_count = str(form.get(f"available_count_{iid}") or "").strip()
                raw_days = str(form.get(f"delivery_days_{iid}") or "").strip()
                if raw_count:
                    row["availableCount"] = str(_num(raw_count, f"остаток позиции {iid}"))
                if raw_days:
                    row["deliveryDays"] = str(_int(raw_days, f"срок позиции {iid}"))
            offer_items.append(row)
            additional_items.append({"orderItemId": iid, "guid": external_id, "addedRowId": "", "addedRowIndex": ""})

        if not offer_items:
            raise HTTPException(status_code=400, detail="Не выбрано ни одной позиции")
        account_id = str(form.get("destination_account_id") or "").strip()
        if not account_id:
            raise HTTPException(status_code=400, detail="Не указан ID юридического лица / банковского счёта")
        prepaid = _num(form.get("prepayment_percent") or 0, "предоплата")
        delay = _int(form.get("delay_days") or 0, "отсрочка")
        payload = {
            "moduleVersion": MODULE_VERSION,
            "producerOfferDate": str(form.get("producer_offer_date") or date.today().isoformat()),
            "producerOfferNumber": str(form.get("producer_offer_number") or "").strip(),
            "hasVat": "false" if vat_rate == "0" else "true",
            "currency": {"id": str(form.get("currency_id") or DEFAULT_CURRENCY_ID)},
            "deliveryIncluded": "true" if form.get("delivery_included") == "1" else "false",
            "prepaidPercent": str(prepaid / 100),
            "delay": str(delay),
            "destinationAccount": {"id": account_id},
            "offerItems": offer_items,
            "additionalDataJson": {"type": "ZakupayMVP", "guid": _stable_guid("order", order.get("id"), form.get("producer_offer_number"), form.get("producer_offer_date")), "items": additional_items},
            "files": [{"id": str(file_id)}],
        }
        if str(form.get("comment") or "").strip():
            payload["comment"] = str(form.get("comment")).strip()
        if str(form.get("document_reg_num") or "").strip():
            payload["documentRegNum"] = str(form.get("document_reg_num")).strip()
        return payload

    def _upload_invoice(upload):
        filename = getattr(upload, "filename", None) or "invoice.xlsx"
        upload.file.seek(0)
        body = upload.file.read()
        if not body:
            raise HTTPException(status_code=400, detail="Файл счёта пуст")
        ctype = getattr(upload, "content_type", None) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        url = zakupay_base_url.rstrip("/") + FILE_UPLOAD_PATH
        headers = _headers()
        headers.pop("Content-Type", None)
        try:
            r = requests.post(url, headers=headers, files={"file": (filename, body, ctype)}, timeout=30)
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"Ошибка загрузки файла в Закупай: {exc}")
        data = _decode_response(r)
        if not r.ok:
            raise HTTPException(status_code=r.status_code, detail={"stage": "file_upload", "response": data})
        file_id = _find_first_key(data, "id")
        if not file_id:
            raise HTTPException(status_code=502, detail={"stage": "file_upload", "error": "Закупай не вернул id файла", "response": data})
        return file_id, data

    @app.get("/dashboard/order/{order_id}/offer/accounts", response_class=HTMLResponse)
    def offer_accounts(order_id: int):
        _get_order(order_id)
        accounts, error = _portal_accounts()
        if error:
            return HTMLResponse(f"<h1>Юрлица</h1><p>{esc(error)}</p><p><a href='/dashboard/order/{order_id}/offer'>← Назад</a></p>", status_code=502)
        rows = "".join(f"<tr><td>{esc(a.get('id'))}</td><td>{esc(a.get('label'))}</td></tr>" for a in accounts)
        return HTMLResponse(f"<h1>Юрлица и банковские счета</h1><table border='1' cellpadding='8'><tr><th>ID</th><th>Юрлицо / банк</th></tr>{rows}</table><p>Скопируй нужный ID в форму предложения.</p><p><a href='/dashboard/order/{order_id}/offer'>← Назад</a></p>")

    @app.get("/dashboard/order/{order_id}/offer", response_class=HTMLResponse)
    def offer_builder(order_id: int):
        order = _get_order(order_id)
        rows = ""
        for idx, item in enumerate(order.get("orderItems") or [], 1):
            iid = item.get("id")
            name = item.get("goodName") or ""
            qty = item.get("count") or 0
            unit = _unit_name(item)
            rows += f"""<tr><td><input type='checkbox' name='use_{iid}' value='1' checked></td><td>{idx}</td><td><b>{esc(name)}</b><br><small>Товар поставщика</small><input name='provider_name_{iid}' value='{esc(name)}' required><br><small>Комментарий</small><input name='provider_comment_{iid}'></td><td><input name='qty_{iid}' type='number' step='0.001' min='0.001' value='{esc(qty)}' required></td><td><input name='unit_{iid}' value='{esc(unit)}' required></td><td><input name='price_{iid}' type='number' step='0.01' min='0' placeholder='Цена за единицу' required></td><td><select name='available_{iid}'><option value='1'>В наличии</option><option value='0'>Не полностью</option></select><input name='available_count_{iid}' placeholder='Есть, кол-во'><input name='delivery_days_{iid}' placeholder='Срок, дней'></td></tr>"""
        html = f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Предложение {order_id}</title><style>body{{font-family:Arial;margin:24px;background:#f4f6f8;color:#202124}}.card{{background:#fff;padding:18px;border-radius:12px;margin-bottom:18px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}}label{{display:block;font-size:12px;margin:6px 0 4px}}input,select,textarea{{width:100%;box-sizing:border-box;padding:8px}}table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{padding:8px;border-bottom:1px solid #ddd;vertical-align:top}}th{{text-align:left;background:#eee}}button{{padding:12px 18px;background:#c62828;color:white;border:0;border-radius:8px;font-weight:bold}}input[type=checkbox]{{width:auto}}.ok{{background:#edf8ef;padding:10px;border-radius:8px}}.warn{{background:#fff7df;padding:10px;border-radius:8px}}</style></head><body><p><a href='/dashboard/analysis/order/{order_id}'>← К заявке</a></p><h1>Создать предложение в Закупай</h1><div class='card'><div class='ok'>Форма загружена без внешних запросов. Юрлицо можно получить отдельной кнопкой, поэтому недоступность check/token больше не блокирует страницу.</div><p><b>Заявка:</b> {esc(order.get('id'))} — {esc(order.get('name'))}</p><form method='post' action='/dashboard/order/{order_id}/offer/submit' enctype='multipart/form-data'><div class='grid'><div><label>Файл счёта / предложения</label><input type='file' name='invoice_file' required></div><div><label>ID юрлица / банковского счёта</label><input name='destination_account_id' required placeholder='Вставь ID'><small><a target='_blank' href='/dashboard/order/{order_id}/offer/accounts'>Получить список юрлиц</a></small></div><div><label>Номер счёта</label><input name='producer_offer_number' required></div><div><label>Дата</label><input type='date' name='producer_offer_date' value='{date.today().isoformat()}' required></div><div><label>Валюта ID</label><input name='currency_id' value='{esc(DEFAULT_CURRENCY_ID)}' required></div><div><label>НДС</label><select name='vat_rate'><option value='0.2'>20%</option><option value='0.22'>22%</option><option value='0.1'>10%</option><option value='0'>Без НДС</option></select></div><div><label>Предоплата, %</label><input type='number' name='prepayment_percent' min='0' max='100' value='100'></div><div><label>Отсрочка, дней</label><input type='number' name='delay_days' min='0' value='0'></div><div><label>Доставка включена</label><select name='delivery_included'><option value='1'>Да</option><option value='0'>Нет</option></select></div><div><label>Рег. номер</label><input name='document_reg_num'></div></div><p><label>Комментарий покупателю</label><textarea name='comment'></textarea></p><h3>Позиции</h3><table><thead><tr><th></th><th>№</th><th>Позиция</th><th>Кол-во</th><th>Ед.</th><th>Цена за ед.</th><th>Наличие</th></tr></thead><tbody>{rows}</tbody></table><div class='warn'>Реальная отправка выполняется только после контрольного подтверждения.</div><p><label><input type='checkbox' name='confirm_send' value='SEND' required> Я проверил юрлицо, файл, позиции, количества и цены.</label></p><button type='submit'>Загрузить файл и создать предложение</button></form></div></body></html>"""
        return HTMLResponse(html)

    @app.post("/dashboard/order/{order_id}/offer/submit", response_class=HTMLResponse)
    async def submit_offer(order_id: int, request: Request):
        order = _get_order(order_id)
        form = await request.form()
        if form.get("confirm_send") != "SEND":
            raise HTTPException(status_code=400, detail="Реальная отправка не подтверждена")
        upload = form.get("invoice_file")
        if upload is None or not getattr(upload, "filename", None):
            raise HTTPException(status_code=400, detail="Не приложен файл счёта")
        file_id, upload_response = _upload_invoice(upload)
        payload = _build_payload(order, form, file_id)
        url = zakupay_base_url.rstrip("/") + OFFER_CREATE_PATH
        try:
            r = requests.post(url, headers=_headers("application/json"), json=payload, timeout=30)
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"Ошибка создания предложения: {exc}")
        data = _decode_response(r)
        if not r.ok:
            return HTMLResponse(f"<h1>Закупай отклонил предложение</h1><p>HTTP {r.status_code}</p><pre>{esc(json.dumps(data, ensure_ascii=False, indent=2))}</pre><h3>JSON</h3><pre>{esc(json.dumps(payload, ensure_ascii=False, indent=2))}</pre><p><a href='/dashboard/order/{order_id}/offer'>← Исправить</a></p>", status_code=r.status_code)
        offer_id = _find_first_key(data, "id")
        return HTMLResponse(f"<h1>Предложение создано</h1><p>Заявка: {order_id}</p><p>ID файла: {esc(file_id)}</p><p>ID предложения: {esc(offer_id)}</p><pre>{esc(json.dumps(data, ensure_ascii=False, indent=2))}</pre><p><a href='/dashboard/analysis/order/{order_id}'>Вернуться к заявке</a></p>")
