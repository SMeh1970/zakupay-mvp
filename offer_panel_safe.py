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


def install_offer_panel(
    app,
    fetch_all_orders,
    zakupay_headers,
    zakupay_base_url,
    esc,
    fetch_order_by_id=None,
    load_offer_context=None,
    mark_offer_created=None,
    build_invoice=None,
):
    def _get_context(order_id: int):
        context = load_offer_context(order_id) if load_offer_context else None
        if not context:
            raise HTTPException(
                status_code=404,
                detail="Сохранённая обработка заявки не найдена. Сначала выберите заявку в рабочей панели и выполните поиск цен.",
            )
        if not context.get("order"):
            raise HTTPException(status_code=409, detail="В обработке не сохранён исходный состав заявки")
        return context

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
        data = _decode_response(r)
        account_nodes = []

        def walk(value, account_scope=False):
            if isinstance(value, list):
                for child in value:
                    walk(child, account_scope=account_scope)
                return
            if not isinstance(value, dict):
                return
            if account_scope and value.get("id") is not None:
                account_nodes.append(value)
            elif value.get("id") is not None and ("bankName" in value or "company" in value):
                account_nodes.append(value)
            for key, child in value.items():
                walk(child, account_scope=str(key).lower() in {"account", "accounts"})

        walk(data)
        accounts = []
        seen = set()
        for node in account_nodes:
            account_id = str(node.get("id") or "").strip()
            if not account_id or account_id in seen:
                continue
            seen.add(account_id)
            company_data = node.get("company") if isinstance(node.get("company"), dict) else {}
            currency_data = node.get("currency") if isinstance(node.get("currency"), dict) else {}
            fields = {
                "id": account_id,
                "name": node.get("name"),
                "bankName": node.get("bankName"),
                "company_name": company_data.get("name"),
                "company_shortName": company_data.get("shortName"),
                "currency_id": currency_data.get("id"),
                "currency_name": currency_data.get("name"),
            }
            company = fields.get("company_shortName") or fields.get("company_name") or "Юрлицо"
            bank = fields.get("bankName") or "банк не указан"
            account_name = fields.get("name") or "счёт"
            fields["label"] = f"{company} / {bank} ({account_name})"
            accounts.append(fields)
        if not accounts:
            return [], "Ответ check/token получен, но банковские счета в нём не найдены"
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

    def _upload_invoice(filename, body):
        if not body:
            raise HTTPException(status_code=400, detail="Файл счёта пуст")
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
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
        _get_context(order_id)
        accounts, error = _portal_accounts()
        if error:
            return HTMLResponse(f"<h1>Юрлица</h1><p>{esc(error)}</p><p><a href='/dashboard/order/{order_id}/offer'>← Назад</a></p>", status_code=502)
        rows = "".join(f"<tr><td>{esc(a.get('id'))}</td><td>{esc(a.get('label'))}</td></tr>" for a in accounts)
        return HTMLResponse(f"<h1>Юрлица и банковские счета</h1><table border='1' cellpadding='8'><tr><th>ID</th><th>Юрлицо / банк</th></tr>{rows}</table><p>Скопируй нужный ID в форму предложения.</p><p><a href='/dashboard/order/{order_id}/offer'>← Назад</a></p>")

    @app.get("/dashboard/order/{order_id}/offer", response_class=HTMLResponse)
    def offer_builder(order_id: int):
        context = _get_context(order_id)
        order = context["order"]
        result = context["result"]
        job_id = context["job_id"]
        if result.get("live_offer_created"):
            offer_id = result.get("live_offer_id") or "—"
            return HTMLResponse(
                f"<h1>Предложение уже создано</h1><p>Заявка № {order_id}, предложение Закупай: {esc(offer_id)}.</p>"
                f"<p><a href='/dashboard/automation/jobs/{job_id}/review'>Вернуться к обработке</a></p>",
                status_code=409,
            )
        original_items = {str(item.get("id")): item for item in order.get("orderItems") or [] if item.get("id") is not None}
        rows = ""
        blockers = []
        included = [item for item in result.get("items") or [] if item.get("decision") in {"auto_ready", "approved"}]
        for idx, item in enumerate(included, 1):
            iid = item.get("order_item_id")
            if iid is None or str(iid) not in original_items:
                blockers.append(f"позиция {item.get('position')}: отсутствует ID строки Закупай")
                selected = item.get("selected") or {}
                name = selected.get("name") or item.get("requested_name") or ""
                qty = item.get("quantity") or 0
                unit = item.get("unit") or ""
                price = item.get("proposed_unit_price")
                availability = item.get("availability_status") or "наличие не подтверждено"
                delivery = item.get("courier_date") or item.get("pickup_date") or "—"
                rows += f"""<tr><td><input type='checkbox' disabled title='Нет ID строки Закупай'></td><td>{idx}</td><td><b>{esc(item.get('requested_name') or '')}</b><br><small>Предлагается:</small><input value='{esc(name)}' readonly></td><td><input value='{esc(qty)}' readonly></td><td><input value='{esc(unit)}' readonly></td><td><input value='{esc(price)}' readonly></td><td>{esc(availability)}<br><small>Срок: {esc(delivery)}</small></td></tr>"""
                continue
            selected = item.get("selected") or {}
            name = selected.get("name") or item.get("requested_name") or ""
            qty = item.get("quantity") or 0
            unit = item.get("unit") or _unit_name(original_items[str(iid)])
            price = item.get("proposed_unit_price")
            if price is None:
                blockers.append(f"позиция {item.get('position')}: не рассчитана цена")
                continue
            available = "1" if item.get("stock_confirmed") else "0"
            availability = item.get("availability_status") or "наличие не подтверждено"
            delivery = item.get("courier_date") or item.get("pickup_date") or "—"
            comment = f"{item.get('match_status') or 'подбор'}; {availability}; доставка: {delivery}"
            rows += f"""<tr><td><input type='checkbox' name='use_{iid}' value='1' checked></td><td>{idx}</td><td><b>{esc(item.get('requested_name') or '')}</b><br><small>Предлагается:</small><input name='provider_name_{iid}' value='{esc(name)}' required><input type='hidden' name='provider_comment_{iid}' value='{esc(comment)}'></td><td><input name='qty_{iid}' type='number' step='0.001' min='0.001' value='{esc(qty)}' required></td><td><input name='unit_{iid}' value='{esc(unit)}' required></td><td><input name='price_{iid}' type='number' step='0.01' min='0' value='{esc(price)}' required></td><td>{esc(availability)}<br><small>Срок: {esc(delivery)}</small><input type='hidden' name='available_{iid}' value='{available}'></td></tr>"""
        if not included:
            blockers.append("не выбрано ни одной позиции для счёта")

        accounts, account_error = _portal_accounts()
        configured_account = os.getenv("ZAKUPAY_DESTINATION_ACCOUNT_ID", "").strip()
        if accounts:
            account_options = "".join(
                f"<option value='{esc(a.get('id'))}' {'selected' if str(a.get('id')) == configured_account or (not configured_account and len(accounts) == 1) else ''}>{esc(a.get('label'))}</option>"
                for a in accounts
            )
            account_field = f"<select name='destination_account_id' required><option value=''>Выберите счёт</option>{account_options}</select>"
        else:
            account_field = f"<input name='destination_account_id' value='{esc(configured_account)}' required placeholder='ID юрлица / банковского счёта'>"
        warning = ""
        if account_error:
            warning += f"<div class='warn'>Список банковских счетов не загружен: {esc(account_error)}. Укажите ID вручную.</div>"
        if blockers:
            warning += "<div class='warn'><b>Отправка заблокирована:</b><ul>" + "".join(f"<li>{esc(x)}</li>" for x in blockers) + "</ul>ID должны быть сохранены при первоначальном получении заявки. Страница подтверждения не обращается к Закупай повторно.</div>"
        disabled = "disabled" if blockers else ""
        invoice_number = result.get("invoice_number") or context.get("invoice_number") or ""
        html = f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Предложение {order_id}</title><style>body{{font-family:Arial;margin:24px;background:#f4f6f8;color:#202124}}.card{{background:#fff;padding:18px;border-radius:12px;margin-bottom:18px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}}label{{display:block;font-size:12px;margin:6px 0 4px}}input,select,textarea{{width:100%;box-sizing:border-box;padding:8px}}table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{padding:8px;border-bottom:1px solid #ddd;vertical-align:top}}th{{text-align:left;background:#eee}}button{{padding:12px 18px;background:#c62828;color:white;border:0;border-radius:8px;font-weight:bold}}input[type=checkbox]{{width:auto}}.ok{{background:#edf8ef;padding:10px;border-radius:8px}}.warn{{background:#fff7df;padding:10px;border-radius:8px}}</style></head><body><p><a href='/dashboard/analysis/order/{order_id}'>← К заявке</a></p><h1>Создать предложение в Закупай</h1><div class='card'><div class='ok'>Форма загружена без внешних запросов. Юрлицо можно получить отдельной кнопкой, поэтому недоступность check/token больше не блокирует страницу.</div><p><b>Заявка:</b> {esc(order.get('id'))} — {esc(order.get('name'))}</p><form method='post' action='/dashboard/order/{order_id}/offer/submit' enctype='multipart/form-data'><div class='grid'><div><label>Файл счёта / предложения</label><input type='file' name='invoice_file' required></div><div><label>ID юрлица / банковского счёта</label><input name='destination_account_id' required placeholder='Вставь ID'><small><a target='_blank' href='/dashboard/order/{order_id}/offer/accounts'>Получить список юрлиц</a></small></div><div><label>Номер счёта</label><input name='producer_offer_number' required></div><div><label>Дата</label><input type='date' name='producer_offer_date' value='{date.today().isoformat()}' required></div><div><label>Валюта ID</label><input name='currency_id' value='{esc(DEFAULT_CURRENCY_ID)}' required></div><div><label>НДС</label><select name='vat_rate'><option value='0.2'>20%</option><option value='0.22'>22%</option><option value='0.1'>10%</option><option value='0'>Без НДС</option></select></div><div><label>Предоплата, %</label><input type='number' name='prepayment_percent' min='0' max='100' value='100'></div><div><label>Отсрочка, дней</label><input type='number' name='delay_days' min='0' value='0'></div><div><label>Доставка включена</label><select name='delivery_included'><option value='1'>Да</option><option value='0'>Нет</option></select></div><div><label>Рег. номер</label><input name='document_reg_num'></div></div><p><label>Комментарий покупателю</label><textarea name='comment'></textarea></p><h3>Позиции</h3><table><thead><tr><th></th><th>№</th><th>Позиция</th><th>Кол-во</th><th>Ед.</th><th>Цена за ед.</th><th>Наличие</th></tr></thead><tbody>{rows}</tbody></table><div class='warn'>Реальная отправка выполняется только после контрольного подтверждения.</div><p><label><input type='checkbox' name='confirm_send' value='SEND' required> Я проверил юрлицо, файл, позиции, количества и цены.</label></p><button type='submit'>Загрузить файл и создать предложение</button></form></div></body></html>"""
        html = f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Предложение {order_id}</title><style>body{{font-family:Arial;margin:24px;background:#f4f6f8;color:#202124}}.card{{background:#fff;padding:18px;border-radius:12px;margin-bottom:18px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}}label{{display:block;font-size:12px;margin:6px 0 4px}}input,select,textarea{{width:100%;box-sizing:border-box;padding:8px}}table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{padding:8px;border-bottom:1px solid #ddd;vertical-align:top}}th{{text-align:left;background:#eee}}button{{padding:12px 18px;background:#c62828;color:white;border:0;border-radius:8px;font-weight:bold}}button:disabled{{background:#999}}input[type=checkbox]{{width:auto}}.ok{{background:#edf8ef;padding:10px;border-radius:8px}}.warn{{background:#fff0e8;padding:10px;border-radius:8px;margin:10px 0}}</style></head><body><p><a href='/dashboard/automation/jobs/{job_id}/review'>← К проверке заявки</a></p><h1>Подтверждение предложения в Закупай</h1><div class='card'><div class='ok'>Используется сохранённая заявка и последний проверенный подбор. Повторного получения заявки из Закупай нет. Счёт сформируется автоматически.</div>{warning}<p><b>Заявка:</b> {esc(order.get('id'))} — {esc(order.get('name'))}<br><b>Позиций к отправке:</b> {len(included)}</p><form method='post' action='/dashboard/order/{order_id}/offer/submit'><div class='grid'><div><label>Банковский счёт ООО «АВИОР»</label>{account_field}</div><div><label>Номер счёта</label><input name='producer_offer_number' value='{esc(invoice_number)}' required></div><div><label>Дата</label><input type='date' name='producer_offer_date' value='{date.today().isoformat()}' required></div><div><label>Валюта ID</label><input name='currency_id' value='{esc(DEFAULT_CURRENCY_ID)}' required></div><div><label>НДС</label><select name='vat_rate'><option value='0.22' selected>22%</option><option value='0.2'>20%</option><option value='0.1'>10%</option><option value='0'>Без НДС</option></select></div><div><label>Предоплата, %</label><input type='number' name='prepayment_percent' min='0' max='100' value='100'></div><div><label>Отсрочка, дней</label><input type='number' name='delay_days' min='0' value='0'></div><div><label>Доставка включена</label><select name='delivery_included'><option value='1' selected>Да</option><option value='0'>Нет</option></select></div></div><p><label>Комментарий покупателю</label><textarea name='comment'>Частичное или полное предложение по подтверждённым позициям. Доставка включена в стоимость.</textarea></p><h3>Позиции</h3><table><thead><tr><th>Включить</th><th>№</th><th>Заявка / предложение</th><th>Кол-во</th><th>Ед.</th><th>Цена за ед.</th><th>Наличие и срок</th></tr></thead><tbody>{rows}</tbody></table><div class='warn'>После нажатия кнопки счёт будет загружен, а предложение реально создано в Закупай. Повторная отправка этой обработки будет заблокирована.</div><p><label><input type='checkbox' name='confirm_send' value='SEND' required> Я проверил счёт, позиции, количества, цены и подтверждаю отправку.</label></p><button type='submit' {disabled}>Выставить счёт и отправить предложение</button></form></div></body></html>"""
        return HTMLResponse(html)

    @app.post("/dashboard/order/{order_id}/offer/submit", response_class=HTMLResponse)
    async def submit_offer(order_id: int, request: Request):
        context = _get_context(order_id)
        order = context["order"]
        result = context["result"]
        if result.get("live_offer_created"):
            raise HTTPException(status_code=409, detail="Предложение по этой обработке уже создано")
        form = await request.form()
        if form.get("confirm_send") != "SEND":
            raise HTTPException(status_code=400, detail="Реальная отправка не подтверждена")
        if build_invoice is None:
            raise HTTPException(status_code=500, detail="Формирование счёта не подключено")
        invoice_number = str(form.get("producer_offer_number") or "").strip()
        if not invoice_number:
            raise HTTPException(status_code=400, detail="Не указан номер счёта")
        result["invoice_number"] = invoice_number
        try:
            invoice_body = build_invoice(result)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        filename = f"AVIOR_invoice_{invoice_number}_order_{order_id}.xlsx"
        file_id, upload_response = _upload_invoice(filename, invoice_body)
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
        if mark_offer_created:
            mark_offer_created(context["job_id"], offer_id=offer_id, file_id=file_id, response=data)
        return HTMLResponse(f"<h1>Предложение создано</h1><p>Заявка: {order_id}</p><p>Счёт: № {esc(invoice_number)}</p><p>ID файла: {esc(file_id)}</p><p>ID предложения: {esc(offer_id)}</p><pre>{esc(json.dumps(data, ensure_ascii=False, indent=2))}</pre><p><a href='/dashboard/automation/jobs/{context['job_id']}/review'>Вернуться к обработке</a></p>")
