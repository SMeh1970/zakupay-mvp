"""Discount only fresh, comparable exact competitor prices."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import math


def discounted_price(evidence, percent, vat, *, clock=None):
    try:
        discount = Decimal(str(percent))
    except InvalidOperation:
        raise ValueError('Укажите процент снижения от 0 до 100 (не включая 100)') from None
    if not discount.is_finite() or not 0 <= discount < 100:
        raise ValueError('Укажите процент снижения от 0 до 100 (не включая 100)')
    evidence = evidence or {}
    try:
        observed = datetime.fromisoformat(str(evidence.get('checked_at')).replace('Z', '+00:00'))
        price = evidence.get('price')
        valid = (evidence.get('source') == 'zakupay:getoffersdeviationpercent'
                 and observed.tzinfo is not None
                 and timedelta(0) <= (clock or datetime.now(timezone.utc)) - observed <= timedelta(minutes=10)
                 and not isinstance(price, bool) and math.isfinite(float(price)) and float(price) > 0
                 and evidence.get('unit') in ('м²', 'м2', 'm2')
                 and evidence.get('currency') == 'RUB'
                 and evidence.get('vat_rate') == float(vat) / 100
                 and evidence.get('delivery_included') is False)
    except (ValueError, TypeError, OverflowError):
        valid = False
    if not valid:
        return None, None
    target = (Decimal(str(price)) * (1 - discount / 100)).quantize(Decimal('.01'), rounding=ROUND_FLOOR)
    return float(target), {**evidence, 'discount_pct': str(discount), 'target_price': str(target)}
