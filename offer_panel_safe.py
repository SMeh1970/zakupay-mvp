import asyncio
import copy
import json
import hashlib
import logging
import mimetypes
import os
import threading
import uuid
import xml.etree.ElementTree as ET
from datetime import date
from urllib.parse import urlparse

import requests
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from invoice_generator import customer_validation_error, normalize_customer, validate_seller, invoice_filename
from offer_responses import confirmed_offer_id, UNCONFIRMED_OFFER_ERROR

TOKEN_CHECK_PATH = os.getenv("ZAKUPAY_TOKEN_CHECK_PATH", "/api/v1/util/check/token?format=xml")
FILE_UPLOAD_PATH = os.getenv("ZAKUPAY_FILE_UPLOAD_PATH", "/core/files/upload?format=xml")
OFFER_CREATE_PATH = os.getenv("ZAKUPAY_OFFER_CREATE_PATH", "/core/offers/new/from/1c?format=xml")
MODULE_VERSION = os.getenv("ZAKUPAY_MODULE_VERSION", "zakupay-mvp-0.2")
DEFAULT_CURRENCY_ID = os.getenv("ZAKUPAY_CURRENCY_ID", "643")
logger = logging.getLogger(__name__)

# Wall-clock limits also cover DNS, redirects and a server trickling bytes.
# requests' socket timeout alone does not bound the duration of an operation.
ORDER_CHECK_DEADLINE_SECONDS = 15
UPLOAD_DEADLINE_SECONDS = 35
OFFER_POST_DEADLINE_SECONDS = 35


async def _bounded_call(call, *args, deadline, **kwargs):
    return await asyncio.wait_for(
        run_in_threadpool(call, *args, **kwargs), timeout=deadline,
    )


def install_offer_panel(
    app,
    fetch_all_orders,
    zakupay_headers,
    zakupay_base_url,
    esc,
    fetch_order_by_id=None,
    load_offer_context=None,
    enrich_offer_context=None,
    mark_offer_created=None,
    begin_submission=None,
    finish_submission=None,
    latest_submission=None,
    order_hash=None,
    order_changes=None,
    build_invoice=None,
    load_sellers=None,
    save_invoice_settings=None,
):
    def _seller_profiles(result):
        if load_sellers is None:
            from porcelain import sellers
            profiles = sellers()
        else:
            profiles = dict(load_sellers())
        # Preserve the exact requisites already approved for this job.
        if result.get('seller'):
            profiles['saved'] = result['seller']
        return profiles

    def _get_context(order_id: int):
        context = load_offer_context(order_id) if load_offer_context else None
        if not context:
            raise HTTPException(
                status_code=404,
                detail="Сохранённая обработка заявки не найдена. Сначала выберите заявку в рабочей панели и выполните поиск цен.",
            )
        if not context.get("order"):
            raise HTTPException(status_code=409, detail="В обработке не сохранён исходный состав заявки")
        if context["order"].get("source") == "manual_entry":
            raise HTTPException(
                status_code=409,
                detail="Ручная заявка не связана с Закупай. Для неё доступно формирование и скачивание счёта без отправки в Закупай.",
            )
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

    def _find_first_named_value(obj, wanted):
        """Find the first scalar value by any case-insensitive nested key."""
        wanted = {str(key).lower() for key in wanted}
        if isinstance(obj, dict):
            for key, value in obj.items():
                if str(key).lower() in wanted:
                    if not isinstance(value, (dict, list)) and str(value).strip():
                        return str(value).strip()
                    if isinstance(value, dict):
                        for label_key in ("name", "fullName", "shortName", "title"):
                            label = value.get(label_key)
                            if label is not None and str(label).strip():
                                return str(label).strip()
                found = _find_first_named_value(value, wanted)
                if found:
                    return found
        elif isinstance(obj, list):
            for value in obj:
                found = _find_first_named_value(value, wanted)
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
                "bankName": _find_first_named_value(
                    node, {"bankName", "bank_name", "bank", "bankTitle", "bankFullName"}
                ),
                "accountNumber": _find_first_named_value(
                    node, {"accountNumber", "settlementAccount", "checkingAccount", "currentAccount", "rs"}
                ) or node.get('number'),
                "company_inn": str(company_data.get('inn') or node.get('companyInn') or '').strip(),
                "company_name": company_data.get("name"),
                "company_shortName": company_data.get("shortName"),
                "currency_id": currency_data.get("id"),
                "currency_name": currency_data.get("name"),
            }
            company = fields.get("company_shortName") or fields.get("company_name") or "Юрлицо"
            bank = fields.get("bankName") or "банковский счёт Закупай"
            account_name = fields.get("name") or "счёт"
            number = f" № {fields.get('accountNumber')}" if fields.get("accountNumber") else ""
            fields["label"] = f"{company} / {bank}{number} ({account_name})"
            accounts.append(fields)
        if not accounts:
            return [], "Ответ check/token получен, но банковские счета в нём не найдены"
        return accounts, ""

    def _resolve_settings(form, result):
        accounts, error = _portal_accounts()
        if error:
            raise ValueError('Не удалось проверить принадлежность банковского счёта: ' + error)
        account = next((a for a in accounts if str(a['id']) == str(form.get('destination_account_id') or '')), None)
        if not account:
            raise ValueError('Выберите банковский счёт из списка Закупай')
        profiles = _seller_profiles(result)
        seller = profiles.get(str(form.get('seller_id') or ''))
        if form.get('seller_id') and not seller:
            raise ValueError('Выбранная фирма больше не найдена. Обновите форму и выберите её реквизиты.')
        if not seller:
            # Older open forms only submitted a bank account. Resolve it by
            # its actual requisites, never by a global default or display name.
            matches = [p for p in profiles.values() if str(p.get('account')) == str(account.get('accountNumber'))
                       and (not account.get('company_inn') or str(p.get('inn')) == account['company_inn'])]
            unique = {(p.get('inn'), p.get('account')): p for p in matches}
            if len(unique) == 1:
                seller = next(iter(unique.values()))
        seller = validate_seller(seller)
        if str(account.get('accountNumber') or '') != seller['account']:
            raise ValueError('Банковский счёт Закупай не совпадает со счётом выбранной фирмы. Счёт не отправлен.')
        if account.get('company_inn') and account['company_inn'] != seller['inn']:
            raise ValueError('ИНН банковского счёта Закупай не совпадает с выбранной фирмой. Счёт не отправлен.')
        vat = _num(form.get('vat_rate'), 'НДС')
        if vat not in (0, 0.1, 0.2, 0.22):
            raise ValueError('Выберите ставку НДС')
        return seller, str(account['id']), vat

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

    def _submission_block_page(order_id, job_id, submission):
        status = str((submission or {}).get("status") or "unknown")
        if status == "sending":
            title = "Отправка уже выполняется"
            explanation = "Повторный запрос заблокирован, пока не установлен результат текущей отправки."
        elif status == "rejected":
            title = "Этот вариант уже был отклонён Закупай"
            explanation = "Исправьте данные предложения перед новой отправкой. Тот же самый запрос повторно не отправляется."
        else:
            title = "Результат предыдущей отправки не подтверждён"
            explanation = (
                "Закупай мог принять предложение, даже если приложение не получило ответ. "
                "Повторная отправка заблокирована до сверки с Закупай."
            )
        return HTMLResponse(
            "<!doctype html><html lang='ru'><meta charset='utf-8'>"
            f"<title>{esc(title)}</title><body style='font-family:Arial;margin:32px'>"
            f"<h1>{esc(title)}</h1><p>{esc(explanation)}</p>"
            f"<p>Заявка № {order_id}; попытка № {esc((submission or {}).get('id') or '—')}.</p>"
            f"<p><a href='/dashboard/automation/jobs/{job_id}/review'>Вернуться к заявке</a></p>"
            "</body></html>",
            status_code=409,
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    def _not_sent_page(order_id, message, *, status_code=503, job_id=None):
        download = (
            f"<p><a href='/dashboard/automation/jobs/{int(job_id)}/invoice.xlsx'>Скачать сохранённый счёт без отправки</a></p>"
            if job_id is not None else ""
        )
        return HTMLResponse(
            "<!doctype html><html lang='ru'><meta charset='utf-8'>"
            "<title>Счёт не отправлен</title><body style='font-family:Arial;margin:32px'>"
            f"<h1>Счёт не отправлен</h1><p>{esc(message)}</p>"
            "<p>Сохранённая заявка и подбор товаров не изменены.</p>"
            f"<p><a href='/dashboard/order/{order_id}/offer'>Вернуться к предложению</a></p>"
            f"{download}</body></html>", status_code=status_code,
            headers={"Cache-Control": "no-store, max-age=0"},
        )

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
            r = requests.post(
                url, headers=headers, files={"file": (filename, body, ctype)},
                timeout=(5, 20), allow_redirects=False,
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

    def _invoice_draft(result, order, payload, form):
        """The attachment must represent the same rows the operator submits."""
        from supplier_comparison import selected_allowed
        approved = {
            str(item.get("order_item_id")): item for item in result.get("items") or []
            if item.get("decision") in {"approved", "auto_ready"} and selected_allowed(item)
        }
        draft = copy.deepcopy(result)
        draft.update(order_id=order["id"], invoice_number=payload["producerOfferNumber"],
                     invoice_date=payload["producerOfferDate"], items=[])
        for position, submitted in enumerate(payload["offerItems"], 1):
            iid = str(submitted["item"]["orderItem"]["id"])
            if iid not in approved:
                raise HTTPException(409, "В счёт включена неподтверждённая позиция. Вернитесь к проверке заявки.")
            item = copy.deepcopy(approved[iid])
            item.update(position=position, quantity=float(submitted["count"]),
                        unit=submitted["unitName"],
                        proposed_unit_price=_num(form.get(f"price_{iid}"), f"цена позиции {iid}"))
            item["selected"] = {**(item.get("selected") or {}), "name": submitted["providerGoodName"]}
            draft["items"].append(item)
        return draft

    @app.get("/dashboard/order/{order_id}/offer/accounts", response_class=HTMLResponse)
    def offer_accounts(order_id: int):
        _get_context(order_id)
        accounts, error = _portal_accounts()
        if error:
            return HTMLResponse(f"<h1>Юрлица</h1><p>{esc(error)}</p><p><a href='/dashboard/order/{order_id}/offer'>← Назад</a></p>", status_code=502)
        rows = "".join(f"<tr><td>{esc(a.get('id'))}</td><td>{esc(a.get('label'))}</td></tr>" for a in accounts)
        return HTMLResponse(f"<h1>Юрлица и банковские счета</h1><table border='1' cellpadding='8'><tr><th>ID</th><th>Юрлицо / банк</th></tr>{rows}</table><p>Скопируй нужный ID в форму предложения.</p><p><a href='/dashboard/order/{order_id}/offer'>← Назад</a></p>")

    @app.get("/dashboard/order/{order_id}/offer", response_class=HTMLResponse)
    def offer_builder(order_id: int, ids: str = ""):
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
                headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
            )
        submission = latest_submission(job_id) if latest_submission else None
        if submission and submission.get("status") in {"sending", "unknown"}:
            return _submission_block_page(order_id, job_id, submission)
        original_items = {str(item.get("id")): item for item in order.get("orderItems") or [] if item.get("id") is not None}
        rows = ""
        blockers = []
        missing_line_ids = False
        customer_error = customer_validation_error(result.get("customer"))
        if customer_error:
            blockers.append(customer_error)
        from supplier_comparison import selected_allowed
        included = [item for item in result.get("items") or [] if item.get("decision") in {"auto_ready", "approved"} and selected_allowed(item)]
        for idx, item in enumerate(included, 1):
            iid = item.get("order_item_id")
            if iid is None or str(iid) not in original_items:
                missing_line_ids = True
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

        accounts, account_error = _portal_accounts() if not customer_error else ([], "")
        profiles = _seller_profiles(result)
        seller_options = "".join(
            f"<option value='{esc(key)}' {'selected' if key == 'saved' else ''}>{esc(p['name'])} · ИНН {esc(p['inn'])} · р/с {esc(p['account'])}</option>"
            for key, p in profiles.items() if key == 'saved' or p != result.get('seller')
        )
        seller_field = f"<select name='seller_id' required><option value=''>Выберите нашу фирму</option>{seller_options}</select>"
        vat_field = '<select name="vat_rate" required>' + ''.join(
            f"<option value='{value}' {'selected' if value == result.get('vat_rate', 0.22) else ''}>{label}</option>"
            for value, label in ((0.22, '22%'), (0.2, '20%'), (0.1, '10%'), (0, 'Без НДС'))
        ) + '</select>'
        configured_account = str(result.get('destination_account_id') or '')
        if not configured_account and result.get('seller'):
            matches = [a for a in accounts if str(a.get('accountNumber')) == result['seller'].get('account')
                       and (not a.get('company_inn') or a['company_inn'] == result['seller'].get('inn'))]
            if len(matches) == 1:
                configured_account = str(matches[0]['id'])
        if accounts:
            account_options = "".join(
                f"<option value='{esc(a.get('id'))}' {'selected' if str(a.get('id')) == configured_account else ''}>{esc(a.get('label'))}</option>"
                for a in accounts
            )
            account_field = f"<select name='destination_account_id' required><option value=''>Выберите счёт</option>{account_options}</select>"
        else:
            account_field = f"<input name='destination_account_id' value='{esc(configured_account)}' required placeholder='ID юрлица / банковского счёта'>"
        warning = ""
        if ids == "found":
            warning += "<div class='ok'>ID позиций получены и сохранены. Форму можно проверять и отправлять.</div>"
        elif ids == "started":
            warning += (
                "<div class='ok'><b>Запрос за ID позиций запущен в фоне.</b> "
                "Страница больше не ждёт медленный ответ Закупай. "
                f"<a href='/dashboard/order/{order_id}/offer'>Проверить результат</a></div>"
            )
        elif ids == "missing":
            warning += "<div class='warn'><b>Закупай не вернул ID строк.</b> Подбор и цены сохранены, но отправка предложения через API пока невозможна.</div>"
        if account_error:
            warning += f"<div class='warn'>Список банковских счетов не загружен: {esc(account_error)}. Укажите ID вручную.</div>"
        if blockers:
            warning += (
                "<div class='warn'><b>Отправка заблокирована:</b><ul>"
                + "".join(f"<li>{esc(x)}</li>" for x in blockers)
                + "</ul>"
            )
            if customer_error:
                warning += f"<p><a href='/dashboard/automation/jobs/{job_id}/review'>Проверить и сохранить реквизиты плательщика</a></p>"
            if missing_line_ids:
                warning += (
                    "<p>ID должны сохраняться при первоначальном получении заявки. "
                    "Для старой заявки выполните один повторный запрос только за ID строк — подбор товаров и цены не изменятся.</p>"
                    f"<form method='post' action='/dashboard/order/{order_id}/offer/enrich-ids'>"
                    "<button type='submit' style='background:#1a73e8'>Получить ID позиций</button>"
                    "</form>"
                )
            warning += "</div>"
        disabled = "disabled" if blockers else ""
        customer = normalize_customer(result.get("customer"))
        invoice_number = result.get("invoice_number") or context.get("invoice_number") or ""
        html = f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Предложение {order_id}</title><style>body{{font-family:Arial;margin:24px;background:#f4f6f8;color:#202124}}.card{{background:#fff;padding:18px;border-radius:12px;margin-bottom:18px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}}label{{display:block;font-size:12px;margin:6px 0 4px}}input,select,textarea{{width:100%;box-sizing:border-box;padding:8px}}table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{padding:8px;border-bottom:1px solid #ddd;vertical-align:top}}th{{text-align:left;background:#eee}}button{{padding:12px 18px;background:#c62828;color:white;border:0;border-radius:8px;font-weight:bold}}button:disabled{{background:#999}}input[type=checkbox]{{width:auto}}.ok{{background:#edf8ef;padding:10px;border-radius:8px}}.warn{{background:#fff0e8;padding:10px;border-radius:8px;margin:10px 0}}</style></head><body><p><a href='/dashboard/automation/jobs/{job_id}/review'>← К проверке заявки</a></p><h1>Подтверждение предложения в Закупай</h1><div class='card'><div class='ok'>Используется сохранённая заявка и последний проверенный подбор. Перед отправкой выполняется контрольная сверка с Закупай. Счёт сформируется автоматически.</div><p><a href='/dashboard/order/{order_id}/offer/verify'>Проверить доступность заявки без отправки счёта</a></p>{warning}<p><b>Заявка:</b> {esc(order.get('id'))} — {esc(order.get('name'))}<br><b>Позиций к отправке:</b> {len(included)}</p><form method='post' action='/dashboard/order/{order_id}/offer/submit'><div class='grid'><div><label>Наша фирма — продавец в счёте</label>{seller_field}</div><div><label>Банковский счёт выбранной фирмы в Закупай</label>{account_field}</div><div><label>Номер счёта</label><input name='producer_offer_number' value='{esc(invoice_number)}' required></div><div><label>Дата</label><input type='date' name='producer_offer_date' value='{date.today().isoformat()}' required></div><div><label>Валюта ID</label><input name='currency_id' value='{esc(DEFAULT_CURRENCY_ID)}' required></div><div><label>НДС</label>{vat_field}</div><div><label>Предоплата, %</label><input type='number' name='prepayment_percent' min='0' max='100' value='100'></div><div><label>Отсрочка, дней</label><input type='number' name='delay_days' min='0' value='0'></div><div><label>Доставка включена</label><select name='delivery_included'><option value='1' selected>Да</option><option value='0'>Нет</option></select></div></div><p><label>Комментарий покупателю</label><textarea name='comment'>Частичное или полное предложение по подтверждённым позициям. Доставка включена в стоимость.</textarea></p><p><button type='submit' formaction='/dashboard/order/{order_id}/offer/settings' formnovalidate>Сохранить фирму и реквизиты без отправки</button></p><p><a href='/dashboard/automation/jobs/{job_id}/invoice.xlsx'>Скачать счёт с сохранёнными реквизитами</a> · <a href='/dashboard/tiles/sellers'>Реквизиты наших фирм</a></p><h3>Плательщик</h3><p>{esc(customer['name'])} · ИНН {esc(customer['inn'])} · КПП {esc(customer['kpp'] or 'не применяется')}</p><p><a href='/dashboard/automation/jobs/{job_id}/review'>Проверить или исправить реквизиты плательщика</a></p><h3>Позиции</h3><table><thead><tr><th>Включить</th><th>№</th><th>Заявка / предложение</th><th>Кол-во</th><th>Ед.</th><th>Цена за ед.</th><th>Наличие и срок</th></tr></thead><tbody>{rows}</tbody></table><div class='warn'>После нажатия кнопки счёт будет загружен, а предложение реально создано в Закупай. Повторная отправка этой обработки будет заблокирована.</div><p><label><input type='checkbox' name='confirm_send' value='SEND' required> Я проверил реквизиты плательщика, счёт, позиции, количества, цены и подтверждаю отправку.</label></p><button type='submit' {disabled}>Выставить счёт и отправить предложение</button></form></div></body></html>"""
        return HTMLResponse(html)

    @app.post('/dashboard/order/{order_id}/offer/settings')
    async def save_offer_settings(order_id: int, request: Request):
        context = await run_in_threadpool(_get_context, order_id)
        form = await request.form()
        try:
            seller, account_id, vat = await _bounded_call(_resolve_settings, form, context['result'], deadline=ORDER_CHECK_DEADLINE_SECONDS)
            if save_invoice_settings is None:
                raise ValueError('Сохранение реквизитов не подключено')
            await run_in_threadpool(save_invoice_settings, context['job_id'], seller, account_id, vat)
        except (ValueError, LookupError) as exc:
            raise HTTPException(409, str(exc))
        except asyncio.TimeoutError:
            raise HTTPException(503, 'Закупай не ответил при проверке банковского счёта. Реквизиты не изменены.')
        return RedirectResponse(f'/dashboard/order/{order_id}/offer', 303)

    @app.get("/dashboard/order/{order_id}/offer/verify", response_class=HTMLResponse)
    async def verify_offer_order(order_id: int):
        """Read-only preflight; never generate, upload or submit an offer."""
        context = await run_in_threadpool(_get_context, order_id)
        if fetch_order_by_id is None or order_hash is None:
            raise HTTPException(status_code=503, detail="Контроль актуальности заявки не подключён")
        try:
            fresh_order = await _bounded_call(
                fetch_order_by_id, order_id, force=True,
                deadline=ORDER_CHECK_DEADLINE_SECONDS,
            )
        except Exception as exc:
            logger.warning("order verification failed order=%s error=%s", order_id, type(exc).__name__)
            return _not_sent_page(order_id, "Не удалось проверить заявку в Закупай. Отправка не запускалась.", job_id=context["job_id"])
        if not fresh_order:
            return _not_sent_page(
                order_id,
                f"В ответе Закупай нет заявки № {order_id}. Это не подтверждает её удаление или закрытие. "
                "Проверка сохранённых данных остановлена; отправка не запускалась.",
                status_code=409, job_id=context["job_id"],
            )
        if order_hash(fresh_order) != order_hash(context["order"]):
            return _not_sent_page(order_id, "Состав или условия заявки в Закупай изменились. Требуется сверка с сохранённым подбором.", status_code=409, job_id=context["job_id"])
        return HTMLResponse(
            "<!doctype html><html lang='ru'><meta charset='utf-8'><title>Заявка проверена</title>"
            "<body style='font-family:Arial;margin:32px'><h1>Заявка доступна в Закупай</h1>"
            f"<p>№ {order_id}. Позиций: {len(fresh_order.get('orderItems') or [])}.</p>"
            "<p>Состав и условия совпадают с сохранённой заявкой. Эта проверка не отправляет счёт или предложение.</p>"
            f"<p><a href='/dashboard/order/{order_id}/offer'>Вернуться к подтверждению предложения</a></p></body></html>",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.post("/dashboard/order/{order_id}/offer/enrich-ids")
    def enrich_offer_line_ids(order_id: int):
        """Queue a one-shot repair without holding the browser on a slow API."""
        _get_context(order_id)
        if fetch_order_by_id is None or enrich_offer_context is None:
            raise HTTPException(status_code=503, detail="Получение ID позиций не подключено")

        def repair():
            try:
                fresh_order = fetch_order_by_id(order_id, force=True)
                enrich_offer_context(order_id, fresh_order)
            except Exception:
                logger.exception("background line ID repair failed order=%s", order_id)

        threading.Thread(
            target=repair,
            name=f"zakupay-line-ids-{order_id}",
            daemon=True,
        ).start()
        return HTMLResponse(
            "<html><head><meta http-equiv='refresh' content='0;url="
            f"/dashboard/order/{order_id}/offer?ids=started'></head></html>",
            status_code=303,
            headers={"Location": f"/dashboard/order/{order_id}/offer?ids=started"},
        )

    @app.post("/dashboard/order/{order_id}/offer/submit", response_class=HTMLResponse)
    async def submit_offer(order_id: int, request: Request):
        context = await run_in_threadpool(_get_context, order_id)
        order = context["order"]
        result = context["result"]
        if result.get("live_offer_created"):
            offer_id = result.get("live_offer_id") or "—"
            invoice_number = result.get("invoice_number") or context.get("invoice_number") or "—"
            return HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'>"
                "<title>Предложение уже отправлено</title>"
                "<body style='font-family:Arial;margin:32px'>"
                "<h1>Предложение уже отправлено</h1>"
                f"<p>Заявка № {order_id}</p><p>Счёт № {esc(invoice_number)}</p>"
                f"<p>ID предложения Закупай: {esc(offer_id)}</p>"
                "<p>Повторное предложение не создано — защита от дублей сработала.</p>"
                f"<p><a href='/dashboard/automation/jobs/{context['job_id']}/review'>Вернуться к заявке</a></p>"
                "<p><a href='/dashboard/automation'>Вернуться на главную</a></p>"
                "</body></html>",
                status_code=409,
            )
        existing_submission = await run_in_threadpool(latest_submission, context["job_id"]) if latest_submission else None
        if existing_submission and existing_submission.get("status") in {"sending", "unknown"}:
            return _submission_block_page(order_id, context["job_id"], existing_submission)
        form = await request.form()
        if form.get("confirm_send") != "SEND":
            raise HTTPException(status_code=400, detail="Реальная отправка не подтверждена")
        from supplier_comparison import selected_allowed, unit as comparison_unit
        for item in result.get("items") or []:
            iid = str(item.get("order_item_id"))
            if not item.get("comparison") or form.get(f"use_{iid}") != "1":
                continue
            if not selected_allowed(item) or item.get("decision") not in {"approved", "auto_ready"}:
                raise HTTPException(409, "Предложение исключено правилами сравнения. Вернитесь к сравнительной таблице.")
            qty = _num(form.get(f"qty_{iid}"), f"количество позиции {iid}")
            if qty != float(item.get("quantity") or 0) or comparison_unit(form.get(f"unit_{iid}")) != comparison_unit(item.get("unit")):
                raise HTTPException(409, "Подтвердите количество и пересчёт единиц в сравнительной таблице.")
        if build_invoice is None:
            raise HTTPException(status_code=500, detail="Формирование счёта не подключено")
        invoice_number = str(form.get("producer_offer_number") or "").strip()
        if not invoice_number:
            raise HTTPException(status_code=400, detail="Не указан номер счёта")
        result["invoice_number"] = invoice_number
        customer_error = customer_validation_error(result.get("customer"))
        if customer_error:
            raise HTTPException(status_code=409, detail=customer_error)

        # Keep the operator's selected company even if the subsequent order
        # check cannot complete. The saved invoice must not revert to AVIOR.
        payload = _build_payload(order, form, 'pending')
        try:
            seller, account_id, vat = await _bounded_call(_resolve_settings, form, result, deadline=ORDER_CHECK_DEADLINE_SECONDS)
            if save_invoice_settings is None:
                raise ValueError('Сохранение реквизитов не подключено')
            await run_in_threadpool(save_invoice_settings, context['job_id'], seller, account_id, vat)
            result.update(seller=seller, destination_account_id=account_id, vat_rate=vat)
        except (ValueError, LookupError) as exc:
            raise HTTPException(409, str(exc))
        except asyncio.TimeoutError:
            return _not_sent_page(order_id, 'Закупай не ответил при проверке банковского счёта. Счёт не загружен и предложение не отправлено.')
        invoice_draft = _invoice_draft(result, order, payload, form)

        if fetch_order_by_id is None or order_hash is None:
            raise HTTPException(status_code=503, detail="Контроль актуальности заявки не подключён")
        logger.warning("invoice submission order=%s stage=order_check started", order_id)
        try:
            fresh_order = await _bounded_call(
                fetch_order_by_id, order_id, force=True,
                deadline=ORDER_CHECK_DEADLINE_SECONDS,
            )
        except Exception as exc:
            logger.warning("live order check failed order=%s error=%s", order_id, type(exc).__name__)
            return _not_sent_page(
                order_id,
                "Закупай не ответил на контрольный запрос за отведённое время. "
                "Проверка остановлена; загрузка счёта и отправка предложения не запускались.",
            )
        if not fresh_order:
            return _not_sent_page(
                order_id,
                f"Закупай не вернул заявку № {order_id} для контрольной сверки. "
                "Это не подтверждает её удаление или закрытие. Загрузка счёта и создание предложения не запускались.",
                status_code=409, job_id=context["job_id"],
            )
        fresh_customer = normalize_customer(fresh_order.get("customer"))
        invoice_customer = normalize_customer(result.get("customer"))
        if (
            (fresh_customer["inn"] and fresh_customer["inn"] != invoice_customer["inn"])
            or (fresh_customer["kpp"] and invoice_customer["kpp"] and fresh_customer["kpp"] != invoice_customer["kpp"])
        ):
            raise HTTPException(status_code=409, detail="Реквизиты плательщика в счёте отличаются от заявки Закупай. Счёт не отправлен; проверьте ИНН и КПП в обработке заявки.")
        # The immutable locally stored order is the authority for this draft.
        # Older jobs may contain a hash made from only operator-selected rows.
        saved_hash = order_hash(order)
        fresh_hash = order_hash(fresh_order)
        if fresh_hash != saved_hash:
            differences = order_changes(order, fresh_order) if order_changes else ["заявка изменилась"]
            details = "; ".join(differences) or "изменились коммерчески значимые данные"
            raise HTTPException(
                status_code=409,
                detail=f"Счёт устарел и не отправлен: {details}. Запустите новый подбор по обновлённой заявке.",
            )
        try:
            invoice_body = await run_in_threadpool(build_invoice, invoice_draft)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        filename = invoice_filename(invoice_draft)
        if begin_submission is None or finish_submission is None:
            raise HTTPException(status_code=503, detail="Защита отправки не подключена")
        logger.warning("invoice submission order=%s stage=file_upload started", order_id)
        try:
            file_id, upload_response = await _bounded_call(
                _upload_invoice, filename, invoice_body,
                deadline=UPLOAD_DEADLINE_SECONDS,
            )
        except (asyncio.TimeoutError, HTTPException):
            logger.warning("invoice submission order=%s stage=file_upload failed", order_id)
            return _not_sent_page(
                order_id,
                "Не удалось завершить загрузку файла счёта в Закупай. "
                "Создание предложения не запускалось.",
            )
        payload["files"] = [{"id": str(file_id)}]
        commercial_payload = dict(payload)
        commercial_payload.pop("files", None)
        stable_payload = json.dumps(commercial_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(stable_payload.encode("utf-8")).hexdigest()
        external_guid = str((payload.get("additionalDataJson") or {}).get("guid") or "")
        attempt_key = hashlib.sha256(
            f"{context['job_id']}|{saved_hash}|{payload_hash}".encode("utf-8")
        ).hexdigest()
        claim = await run_in_threadpool(
            begin_submission,
            context["job_id"], order_id, attempt_key, external_guid, saved_hash,
            payload_hash, invoice_number, file_id, payload,
        )
        if not claim.get("claimed"):
            return _submission_block_page(order_id, context["job_id"], claim)
        submission_id = int(claim["id"])
        url = zakupay_base_url.rstrip("/") + OFFER_CREATE_PATH
        logger.warning("invoice submission order=%s attempt=%s stage=offer_post started", order_id, submission_id)
        try:
            r = await _bounded_call(
                requests.post, url, headers=_headers("application/json"), json=payload,
                timeout=(5, 20), allow_redirects=False,
                deadline=OFFER_POST_DEADLINE_SECONDS,
            )
        except (asyncio.TimeoutError, requests.RequestException) as exc:
            await run_in_threadpool(finish_submission, submission_id, "unknown", error=f"{type(exc).__name__}: {exc}")
            logger.warning("invoice submission order=%s attempt=%s stage=offer_post unknown", order_id, submission_id)
            return _submission_block_page(
                order_id,
                context["job_id"],
                {"id": submission_id, "status": "unknown"},
            )
        data = _decode_response(r)
        if r.status_code >= 500 or r.status_code in {408, 429}:
            await run_in_threadpool(finish_submission, submission_id, "unknown", response=data, error=f"HTTP {r.status_code}")
            return _submission_block_page(order_id, context["job_id"], {"id": submission_id, "status": "unknown"})
        if not 200 <= r.status_code < 300:
            await run_in_threadpool(
                finish_submission,
                submission_id,
                "rejected",
                response=data,
                error=f"HTTP {r.status_code}",
            )
            return HTMLResponse(f"<h1>Закупай отклонил предложение</h1><p>HTTP {r.status_code}</p><pre>{esc(json.dumps(data, ensure_ascii=False, indent=2))}</pre><h3>JSON</h3><pre>{esc(json.dumps(payload, ensure_ascii=False, indent=2))}</pre><p><a href='/dashboard/order/{order_id}/offer'>← Исправить</a></p>", status_code=r.status_code)
        offer_id = confirmed_offer_id(data)
        if not offer_id:
            await run_in_threadpool(finish_submission, submission_id, "unknown", response=data, error=UNCONFIRMED_OFFER_ERROR)
            return _submission_block_page(order_id, context["job_id"], {"id": submission_id, "status": "unknown"})
        await run_in_threadpool(finish_submission, submission_id, "confirmed", offer_id=offer_id, response=data)
        logger.warning("invoice submission order=%s attempt=%s stage=completed confirmed", order_id, submission_id)
        return HTMLResponse(
            "<!doctype html><html lang='ru'><meta charset='utf-8'>"
            "<title>Предложение создано</title>"
            "<style>body{font-family:Arial;margin:32px}.actions{display:flex;gap:12px;flex-wrap:wrap;margin:24px 0}"
            ".button{display:inline-block;padding:11px 16px;border-radius:7px;background:#1a73e8;color:#fff;text-decoration:none;font-weight:bold}"
            ".secondary{background:#5f6368}</style><body>"
            "<div class='actions'>"
            "<a class='button' href='/dashboard/automation'>← Вернуться на главную</a>"
            f"<a class='button secondary' href='/dashboard/automation/jobs/{context['job_id']}/review'>Вернуться к обработке</a>"
            "</div>"
            f"<h1>Предложение создано</h1><p>Заявка: {order_id}</p><p>Счёт: № {esc(invoice_number)}</p>"
            f"<p>ID файла: {esc(file_id)}</p><p>ID предложения: {esc(offer_id)}</p>"
            f"<pre>{esc(json.dumps(data, ensure_ascii=False, indent=2))}</pre>"
            "<div class='actions'>"
            "<a class='button' href='/dashboard/automation'>← Вернуться на главную</a>"
            f"<a class='button secondary' href='/dashboard/automation/jobs/{context['job_id']}/review'>Вернуться к обработке</a>"
            "</div></body></html>",
            headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
        )
