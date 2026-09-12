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

# Endpoints confirmed in the official "Закупай: Модуль интеграции" 1C processing.
TOKEN_CHECK_PATH = os.getenv("ZAKUPAY_TOKEN_CHECK_PATH", "/api/v1/util/check/token?format=xml")
FILE_UPLOAD_PATH = os.getenv("ZAKUPAY_FILE_UPLOAD_PATH", "/core/files/upload?format=xml")
OFFER_CREATE_PATH = os.getenv("ZAKUPAY_OFFER_CREATE_PATH", "/core/offers/new/from/1c?format=xml")
MODULE_VERSION = os.getenv("ZAKUPAY_MODULE_VERSION", "zakupay-mvp-0.1")
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

    def _portal_accounts():
        url = zakupay_base_url.rstrip("/") + TOKEN_CHECK_PATH
        try:
            r = requests.get(url, headers=_headers(), timeout=30)
        except requests.RequestException:
            return []
        if not r.ok:
            return []
        try:
            root = ET.fromstring(r.text)
        except ET.ParseError:
            return []

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
            if not fields.get("id"):
                continue
            company = fields.get("company_shortName") or fields.get("company_name") or "Юрлицо"
            bank = fields.get("bankName") or "банк не указан"
            account_name = fields.get("name") or "счёт"
            currency = fields.get("currency_name") or fields.get("currency_id") or ""
            fields["label"] = f"{company} / {bank} ({account_name}{' ' + currency if currency else ''})"
            accounts.append(fields)
        return accounts

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
            provider_comment = str(form.get(f"provider_comment_{iid}") or "").strip()
            unit_name = str(form.get(f"unit_{iid}") or item.get("unit") or item.get("unitName") or "шт.").strip()
            external_id = _stable_guid(order.get("id"), iid, provider_name)
            is_available = form.get(f"available_{iid}") == "1"

            row = {
                "providerGoodName": provider_name,
                "providerComment": provider_comment or "null",
                "count": str(qty),
                "externalNomenclatureId": external_id,
                "unitName": unit_name,
                # Official 1C module sends the whole line amount including VAT, not unit price.
                "amount": str(round(qty * unit_price, 2)),
                "vatRate": {"rate": vat_rate},
                "isAvailable": "true" if is_available else "false",
                "item": {
                    "orderItem": {
                        "id": iid,
                        "innerComment": external_id,
                    }
                },
            }

            if not is_available:
                available_count_raw = str(form.get(f"available_count_{iid}") or "").strip()
                delivery_days_raw = str(form.get(f"delivery_days_{iid}") or "").strip()
                if available_count_raw:
                    row["availableCount"] = str(_num(available_count_raw, f"остаток позиции {iid}"))
                if delivery_days_raw:
                    row["deliveryDays"] = str(_int(delivery_days_raw, f"срок позиции {iid}"))

            offer_items.append(row)
            additional_items.append({
                "orderItemId": iid,
                "guid": external_id,
                "addedRowId": "",
                "addedRowIndex": "",
            })

        if not offer_items:
            raise HTTPException(status_code=400, detail="Не выбрано ни одной позиции")

        destination_account_id = str(form.get("destination_account_id") or "").strip()
        if not destination_account_id:
            raise HTTPException(status_code=400, detail="Не выбрано юридическое лицо / банковский счёт")

        prepayment_percent = _num(form.get("prepayment_percent") or 0, "предоплата")
        if prepayment_percent < 0 or prepayment_percent > 100:
            raise HTTPException(status_code=400, detail="Предоплата должна быть от 0 до 100%")

        delay_days = _int(form.get("delay_days") or 0, "отсрочка")
        if delay_days < 0:
            raise HTTPException(status_code=400, detail="Отсрочка не может быть отрицательной")

        payload = {
            "moduleVersion": MODULE_VERSION,
            "producerOfferDate": str(form.get("producer_offer_date") or date.today().isoformat()),
            "producerOfferNumber": str(form.get("producer_offer_number") or "").strip(),
            "hasVat": "false" if vat_rate == "0" else "true",
            "currency": {"id": str(form.get("currency_id") or DEFAULT_CURRENCY_ID)},
            "deliveryIncluded": "true" if form.get("delivery_included") == "1" else "false",
            # Official module sends a fraction here: 100% => 1.0, 30% => 0.3.
            "prepaidPercent": str(prepayment_percent / 100),
            "delay": str(delay_days),
            "destinationAccount": {"id": destination_account_id},
            "offerItems": offer_items,
            "additionalDataJson": {
                "type": "ZakupayMVP",
                "guid": _stable_guid("order", order.get("id"), payload_date_key(form)),
                "items": additional_items,
            },
            "files": [{"id": str(file_id)}],
        }
        comment = str(form.get("comment") or "").strip()
        if comment:
            payload["comment"] = comment
        document_reg_num = str(form.get("document_reg_num") or "").strip()
        if document_reg_num:
            payload["documentRegNum"] = document_reg_num
        return payload

    def payload_date_key(form):
        return str(form.get("producer_offer_number") or "") + "|" + str(form.get("producer_offer_date") or "")

    def _upload_invoice(upload):
        filename = getattr(upload, "filename", None) or "invoice.xlsx"
        try:
            upload.file.seek(0)
            body = upload.file.read()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Не удалось прочитать файл счёта: {exc}")
        if not body:
            raise HTTPException(status_code=400, detail="Файл счёта пуст")

        content_type = getattr(upload, "content_type", None) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        url = zakupay_base_url.rstrip("/") + FILE_UPLOAD_PATH
        headers = _headers()
        headers.pop("Content-Type", None)
        try:
            r = requests.post(
                url,
                headers=headers,
                files={"file": (filename, body, content_type)},
                timeout=60,
            )
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"Ошибка загрузки файла в Закупай: {exc}")

        data = _decode_response(r)
        if not r.ok:
            raise HTTPException(status_code=r.status_code, detail={"stage": "file_upload", "response": data})
        file_id = _find_first_key(data, "id")
        if not file_id:
            raise HTTPException(status_code=502, detail={"stage": "file_upload", "error": "Закупай не вернул id файла", "response": data})
        return file_id, data

    def _create_offer(payload):
        url = zakupay_base_url.rstrip("/") + OFFER_CREATE_PATH
        try:
            r = requests.post(url, headers=_headers("application/json"), json=payload, timeout=60)
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"Ошибка создания предложения в Закупай: {exc}")
        data = _decode_response(r)
        return r, data

    @app.get("/dashboard/order/{order_id}/offer", response_class=HTMLResponse)
    def offer_builder(order_id: int):
        order = _get_order(order_id)
        accounts = _portal_accounts()

        if accounts:
            account_options = "".join(
                f"<option value='{esc(a.get('id'))}'>{esc(a.get('label'))} — ID {esc(a.get('id'))}</option>"
                for a in accounts
            )
            account_control = f"<select name='destination_account_id' required>{account_options}</select>"
            account_note = "Юрлица/банковские счета получены из check/token официального API."
        else:
            account_control = "<input name='destination_account_id' placeholder='ID банковского счёта юрлица в Закупай' required>"
            account_note = "Список юрлиц автоматически не загрузился; можно указать ID вручную."

        rows = ""
        for idx, item in enumerate(order.get("orderItems") or [], 1):
            iid = item.get("id")
            name = item.get("goodName") or ""
            qty = item.get("count") or 0
            unit = item.get("unit") or item.get("unitName") or "шт."
            rows += f"""
<tr>
  <td><input type='checkbox' name='use_{iid}' value='1' checked></td>
  <td>{idx}</td>
  <td><div class='requested'>{esc(name)}</div><label>Товар поставщика / аналог</label><input name='provider_name_{iid}' value='{esc(name)}' required><label>Комментарий к позиции</label><input name='provider_comment_{iid}'></td>
  <td><input name='qty_{iid}' type='number' step='0.001' min='0.001' value='{esc(qty)}' required></td>
  <td><input name='unit_{iid}' value='{esc(unit)}' required></td>
  <td><input name='price_{iid}' type='number' step='0.01' min='0' placeholder='Цена продажи за единицу' required></td>
  <td><select name='available_{iid}'><option value='1'>В наличии</option><option value='0'>Не полностью</option></select><div class='mini'><input name='available_count_{iid}' placeholder='Есть, кол-во'><input name='delivery_days_{iid}' placeholder='Срок, дней'></div></td>
</tr>"""

        html = f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><title>Предложение {order_id}</title>
<style>
body{{font-family:Arial,sans-serif;margin:24px;background:#f4f6f8;color:#202124}}.card{{background:#fff;padding:18px;border-radius:12px;margin-bottom:18px;box-shadow:0 1px 3px #0001}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}}label{{font-size:12px;color:#666;display:block;margin:6px 0 4px}}input,select,textarea{{width:100%;box-sizing:border-box;padding:9px;border:1px solid #ccd1d7;border-radius:6px}}table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{padding:9px;border-bottom:1px solid #e7eaee;vertical-align:top}}th{{text-align:left;background:#f7f8fa}}button{{padding:12px 18px;background:#1677ff;color:#fff;border:0;border-radius:8px;font-weight:700;cursor:pointer}}.danger{{background:#c62828}}.note{{background:#fff7df;padding:10px;border-radius:8px}}.ok{{background:#edf8ef;padding:10px;border-radius:8px}}.requested{{font-weight:700;margin-bottom:8px}}.mini{{display:grid;grid-template-columns:1fr 1fr;gap:4px;margin-top:4px}}code{{font-size:12px}}input[type=checkbox]{{width:auto}}
</style></head><body>
<p><a href='/dashboard/analysis/order/{order_id}'>← К заявке</a></p>
<h1>Тестовая отправка предложения в Закупай</h1>
<div class='card'>
<div class='ok'><b>MVP:</b> позиции и цены заполняются здесь вручную. После подтверждения система сама загрузит файл счёта и создаст предложение через официальный API-поток 1С.</div>
<p><b>Заявка:</b> {esc(order.get('id'))} — {esc(order.get('name'))}</p>
<form method='post' action='/dashboard/order/{order_id}/offer/submit' enctype='multipart/form-data'>
<div class='grid'>
<div><label>Файл счёта / предложения</label><input type='file' name='invoice_file' required><small>Для первого теста можно использовать любой допустимый файл счёта.</small></div>
<div><label>Юридическое лицо / банковский счёт</label>{account_control}<small>{esc(account_note)}</small></div>
<div><label>Номер счёта/предложения</label><input name='producer_offer_number' required></div>
<div><label>Дата</label><input type='date' name='producer_offer_date' value='{date.today().isoformat()}' required></div>
<div><label>Валюта, ID</label><input name='currency_id' value='{esc(DEFAULT_CURRENCY_ID)}' required></div>
<div><label>НДС</label><select name='vat_rate'><option value='0.2'>20%</option><option value='0.22'>22%</option><option value='0.1'>10%</option><option value='0'>Без НДС</option></select></div>
<div><label>Предоплата, %</label><input type='number' name='prepayment_percent' min='0' max='100' step='0.01' value='100'></div>
<div><label>Отсрочка, дней</label><input type='number' name='delay_days' min='0' value='0'></div>
<div><label>Доставка включена</label><select name='delivery_included'><option value='1'>Да</option><option value='0'>Нет</option></select></div>
<div><label>Рег. номер договора, если нужен</label><input name='document_reg_num'></div>
</div>
<p><label>Комментарий покупателю</label><textarea name='comment'></textarea></p>
<h3>Позиции предложения</h3>
<table><thead><tr><th></th><th>№</th><th>Позиция</th><th>Кол-во</th><th>Ед.</th><th>Цена за ед., ₽</th><th>Наличие</th></tr></thead><tbody>{rows}</tbody></table>
<div class='note'><b>Фактические endpoint'ы официального модуля:</b><br><code>{esc(FILE_UPLOAD_PATH)}</code><br><code>{esc(OFFER_CREATE_PATH)}</code><br><br>Отправка произойдёт только после установки контрольного флажка и нажатия красной кнопки.</div>
<p><label><input type='checkbox' name='confirm_send' value='SEND' required> Я проверил выбранное юрлицо, файл, позиции, количества и цены. Создать реальное предложение в Закупай.</label></p>
<button class='danger' type='submit'>Загрузить файл и создать предложение</button>
</form></div></body></html>"""
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

        # Stage 1: official file upload flow.
        file_id, upload_response = _upload_invoice(upload)

        # Stage 2: build official 1C-compatible offer payload and create offer.
        payload = _build_payload(order, form, file_id)
        r, data = _create_offer(payload)

        if not r.ok:
            return HTMLResponse(
                f"<h1>Закупай отклонил предложение</h1>"
                f"<p>Файл загружен успешно: ID <code>{esc(file_id)}</code>, но создание предложения вернуло HTTP {r.status_code}.</p>"
                f"<h3>Ответ Закупай</h3><pre>{esc(json.dumps(data, ensure_ascii=False, indent=2))}</pre>"
                f"<h3>Отправленный JSON</h3><pre>{esc(json.dumps(payload, ensure_ascii=False, indent=2))}</pre>"
                f"<p><a href='/dashboard/order/{order_id}/offer'>← Исправить и повторить</a></p>",
                status_code=r.status_code,
            )

        offer_id = _find_first_key(data, "id")
        offer_url = _find_first_key(data, "url")
        result_lines = [
            "<h1>Предложение создано в Закупай</h1>",
            f"<p><b>Заявка:</b> {esc(order_id)}</p>",
            f"<p><b>ID загруженного файла:</b> {esc(file_id)}</p>",
        ]
        if offer_id:
            result_lines.append(f"<p><b>ID предложения:</b> {esc(offer_id)}</p>")
        if offer_url:
            result_lines.append(f"<p><b>URL:</b> {esc(offer_url)}</p>")
        result_lines.extend([
            f"<h3>Ответ создания предложения</h3><pre>{esc(json.dumps(data, ensure_ascii=False, indent=2))}</pre>",
            f"<details><summary>Ответ загрузки файла</summary><pre>{esc(json.dumps(upload_response, ensure_ascii=False, indent=2))}</pre></details>",
            f"<details><summary>Отправленный JSON</summary><pre>{esc(json.dumps(payload, ensure_ascii=False, indent=2))}</pre></details>",
            f"<p><a href='/dashboard/analysis/order/{order_id}'>Вернуться к заявке</a></p>",
        ])
        return HTMLResponse("".join(result_lines))
