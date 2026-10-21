"""Fail-closed commercial rules for unattended KeramaHall offers.

An API product name is never a competitor price. All comparisons use explicit
per-m² prices and a known delivery basis. Retrieval time never dates stock.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
from zoneinfo import ZoneInfo

import porcelain as tiles
from invoice_generator import customer_validation_error
from price_lists import norm, number
from saved_dashboard import normalize_unit

POLICY_VERSION = 'tiles-24h-5pct-920-1440-v1'
FLOORS = {(60.0, 60.0): Decimal('920'), (60.0, 120.0): Decimal('1440')}


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def urgent(order):
    if order.get('urgent') is True or order.get('isUrgent') is True:
        return True
    text = norm(str(order.get('name') or '') + ' ' + str(order.get('comment') or ''))
    text = re.sub(r'\b(?:не\s+срочн(?:о|ая|ый|ые)|без\s+срочности)\b', '', text)
    return bool(re.search(r'\bсрочн(?:о|ая|ый|ые|ое)\b', text))


def due_at(order):
    created = timestamp(order.get('creationDate') or order.get('publicDate'))
    return created + (timedelta(0) if urgent(order) else timedelta(hours=24)) if created else None


def relevant_items(order):
    for position, item in enumerate(order.get('orderItems') or [], 1):
        name = str(item.get('goodName') or item.get('name') or '')
        unit = item.get('unit') or {}
        if (not item.get('deleted') and tiles.is_tile(name)
                and normalize_unit(unit.get('name') if isinstance(unit, dict) else unit) == 'м²'):
            yield position, item


def line_match(item, product):
    """Do not upgrade a vague/fuzzy analogue into unattended approval."""
    name = str(item.get('goodName') or item.get('name') or '')
    requested = name + ' ' + str(item.get('comment') or '')
    check = tiles.assess(requested, product)
    problems = check['conflicts'] + check['unknown']
    wanted = tiles.requirements(requested)
    exact = any(norm(value) and re.search(r'\b' + re.escape(norm(value)) + r'\b', norm(requested))
                for value in (product.get('name'), product.get('model'), product.get('article')))
    if not wanted['format']:
        problems.append('В заявке не указан точный формат')
    # An unrecognised brand/model is not permission to substitute it.
    if not exact and item.get('analogAllow') is not True:
        problems.append('Точная модель не найдена; разрешение аналога не подтверждено')
    if not exact and any(not wanted[key] for key in ('color', 'pattern', 'surface')):
        problems.append('Для аналога нужны цвет, рисунок и поверхность')
    if any(len(wanted[key]) > 1 for key in ('color', 'pattern', 'surface', 'tone')):
        problems.append('В описании несколько значений характеристики; требуется проверка')
    codes = re.findall(r'\b(?:[a-z]{1,6}|[а-я]{1,3})[- ]?\d{2,}[a-zа-я0-9-]*\b', requested.casefold())
    if any(norm(code) not in norm(' '.join(str(product.get(k) or '') for k in ('name', 'article', 'model')))
           for code in codes):
        problems.append('Не подтверждён код модели из заявки')
    if re.search(r'чертеж|см\.?\s*файл|во?\s+вложени|по\s+проекту', requested, re.I):
        problems.append('Нужна проверка характеристик во вложении')
    technical = re.search(r'\b(?:pei\s*\d|r\s*1[0-3]|гост|мороз\w*|водопоглощ\w*|ректиф\w*|обрезн\w*|калибр\w*)\b', norm(requested))
    if technical:
        problems.append('Специальные технические требования требуют проверки: ' + technical.group())
    for label in ('brand', 'article'):
        supplied = str(item.get(label) or '').strip()
        if supplied and norm(supplied) != norm(product.get(label)):
            problems.append('Не подтверждён ' + ('бренд' if label == 'brand' else 'артикул'))
    return list(dict.fromkeys(problems))


def competitor_prices(body, checked_at):
    """Accept only the observed read endpoint's exact, item-ID keyed shape.

    Unknown units, currency, tax or delivery basis remain unknown, not defaults.
    """
    if not isinstance(body, list):
        raise ValueError('Закупай не вернул массив лучших цен')
    result = {}
    for row in body:
        if not isinstance(row, dict) or row.get('orderItemId') is None:
            raise ValueError('В ответе лучших цен нет ID позиции')
        key = str(row['orderItemId'])
        if key in result:
            raise ValueError('Повторный ID в ответе лучших цен')
        best = row.get('bestPrice')
        if not isinstance(best, dict):
            continue
        result[key] = {
            'price': number(best.get('price')), 'unit': best.get('unitName'),
            'currency': best.get('currencyCode'), 'vat_rate': best.get('vatRate'),
            'delivery_included': best.get('deliveryIncluded'), 'checked_at': checked_at,
            'source': 'zakupay:getoffersdeviationpercent',
        }
    return result


def evaluate(order, catalog, best_prices, *, clock=None, seller=None, vat_rate=None, delivery=None):
    clock = clock or datetime.now(timezone.utc)
    blockers, lines, used = [], [], defaultdict(Decimal)
    due = due_at(order)
    report = {'policy': POLICY_VERSION, 'order_id': order.get('id'),
              'due_at': due.isoformat() if due else None, 'urgent': urgent(order),
              'catalog_id': catalog.get('id') if catalog else None, 'lines': lines,
              'blockers': blockers, 'checked_at': clock.isoformat(), 'status': 'blocked'}
    if not due:
        blockers.append('Нет достоверной даты публикации с часовым поясом')
    if due and clock < due:
        report['status'] = 'waiting'
    if order.get('source') == 'manual_entry': blockers.append('Ручная заявка не связана с Закупай')
    if order.get('isLimitedView'): blockers.append('Закупай ограничил доступ к заявке')
    end = str(order.get('finishDate') or '')[:10]
    if end and end < clock.astimezone(ZoneInfo('Europe/Moscow')).date().isoformat():
        blockers.append('Истёк срок заявки')
    error = customer_validation_error(order.get('customer'))
    if error: blockers.append(error)
    if not seller: blockers.append('Не настроены реквизиты фирмы СВ')
    if isinstance(vat_rate, bool) or vat_rate not in (0, 0.1, 0.2, 0.22): blockers.append('Не выбран НДС фирмы СВ')
    if not catalog:
        blockers.append('Нет управляемого каталога КерамаХолл')
        return report
    data = catalog['data']
    ids = [str(item.get('id') or '') for _, item in relevant_items(order)]
    if '' in ids or len(ids) != len(set(ids)):
        blockers.append('Нет уникальных ID позиций Закупай')
    selections = tiles.automatic_selection(data, order)
    products = {p['id']: p for p in data['products']}
    for position, item in relevant_items(order):
        problems = []
        p = products.get(selections.get(position))
        line = {'position': position, 'order_item_id': item.get('id'),
                'requested_name': item.get('goodName') or item.get('name'), 'blockers': problems}
        lines.append(line)
        if not p:
            problems.append('Нет подходящего товара с достаточным известным остатком и ценой')
            continue
        line.update(product=p, product_url=tiles.product_page_url(p))
        problems.extend(line_match(item, p))
        box, count = number(p.get('box_m2')), number(item.get('count'))
        if not box or box <= 0 or not count or count <= 0:
            problems.append('Неизвестна упаковка или количество')
            continue
        quantity = Decimal(str(box)) * math.ceil(Decimal(str(count)) / Decimal(str(box)))
        used[p['id']] += quantity
        if used[p['id']] > Decimal(str(p.get('stock_m2') or 0)):
            problems.append('Недостаточный общий остаток')
        stock_date = timestamp(p.get('stock_as_of') or p.get('stock_updated_at'))
        if not stock_date or not timedelta(0) <= clock - stock_date <= timedelta(hours=48):
            problems.append('Дата остатков неизвестна или старше 48 часов')
        line['quantity'] = float(quantity)
        price = best_prices.get(str(item.get('id'))) or {}
        observed = timestamp(price.get('checked_at'))
        best = number(price.get('price'))
        line['best_price'] = best
        if (price.get('source') != 'zakupay:getoffersdeviationpercent' or not observed
                or not timedelta(0) <= clock - observed <= timedelta(minutes=10) or best is None or best <= 0):
            problems.append('Нет свежей числовой лучшей цены Закупай')
            continue
        if normalize_unit(price.get('unit')) != 'м²' or price.get('currency') != 'RUB':
            problems.append('Не подтверждены м² и рубли для лучшей цены')
        if price.get('vat_rate') != vat_rate: problems.append('Не подтверждена сопоставимость НДС')
        if not isinstance(price.get('delivery_included'), bool):
            problems.append('Неизвестно, включена ли доставка в лучшую цену')
        floor = FLOORS.get(tiles.product_requirements(p)['format'])
        if floor is None:
            problems.append('Для этого формата не установлен минимум цены')
            continue
        target = (Decimal(str(best)) * Decimal('.95')).quantize(Decimal('.01'), rounding=ROUND_FLOOR)
        line.update(target_price=float(target), floor=float(floor), proposed_unit_price=float(target))
        if target < floor: problems.append('Снижение на 5% нарушает минимальную цену')
    if not lines: blockers.append('В заявке нет подходящих позиций керамогранита в м²')
    # Delivery must be tied to the exact destination and have an explicit price.
    # Moscow is never silently converted into free, delivery-included shipping.
    address = tiles.delivery_address(order).strip()
    report['address'] = address
    if not address: blockers.append('Нет точного адреса доставки')
    delivery = delivery or {}
    fee = number(delivery.get('price'))
    valid_until = timestamp(delivery.get('valid_until'))
    if (not address or delivery.get('address') != address or fee is None or fee < 0
            or not valid_until or valid_until < clock or delivery.get('confirmed') is not True):
        blockers.append('Доставка по адресу заявки не рассчитана и не подтверждена')
    else:
        report['delivery_price'] = fee
        total_area = sum(Decimal(str(line.get('quantity') or 0)) for line in lines)
        included = {best_prices.get(str(line.get('order_item_id')), {}).get('delivery_included') for line in lines}
        if len(included) != 1 or not included.issubset({True, False}):
            blockers.append('Разные или неизвестные условия доставки в лучших ценах')
        elif True in included and total_area:
            report['delivery_included'] = True
            # Leave a full, conservatively rounded delivery share outside the floor.
            share = Decimal(str(fee)) / total_area
            for line in lines:
                if 'target_price' in line and Decimal(str(line['target_price'])) - share < Decimal(str(line['floor'])):
                    line['blockers'].append('После вычета доставки цена товара ниже минимума')
        else:
            report['delivery_included'] = False
    for line in lines:
        blockers.extend(f"Позиция {line['position']}: {reason}" for reason in line['blockers'])
    report['blockers'] = list(dict.fromkeys(blockers))
    if not blockers and due and clock >= due: report['status'] = 'ready'
    return report
