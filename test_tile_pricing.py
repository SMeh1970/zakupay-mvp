from datetime import datetime, timezone, timedelta
import pytest
from tile_pricing import discounted_price

NOW = datetime.now(timezone.utc)
BASE = dict(price=1234.56, checked_at=NOW.isoformat(), source='zakupay:getoffersdeviationpercent', unit='м²', currency='RUB', vat_rate=.22, delivery_included=False)

def test_selected_discount_and_saved_evidence():
    price, proof = discounted_price(BASE, '7.5', '22', clock=NOW)
    assert price == 1141.96
    assert proof['discount_pct'] == '7.5'

@pytest.mark.parametrize('changes', [dict(price=True), dict(price=float('nan')), dict(currency='USD'), dict(delivery_included=True), dict(vat_rate=None), dict(checked_at=(NOW-timedelta(minutes=11)).isoformat()), dict(source='corridor'), dict(unit=None)])
def test_unusable_evidence_never_prices_offer(changes):
    assert discounted_price({**BASE, **changes}, 5, 22, clock=NOW) == (None, None)

@pytest.mark.parametrize('discount', ['nan', 'inf', '-1', '100', 'abc'])
def test_invalid_discount(discount):
    with pytest.raises(ValueError):
        discounted_price(BASE, discount, 22, clock=NOW)


def test_live_observed_shape_keeps_delivery_and_leaves_unit_and_vat_unknown():
    from tile_offer_policy import competitor_prices
    rows=competitor_prices([dict(orderItemId=101,bestPrice=dict(currency='₽',price=1350,priceInDefaultCurrency=1350),deliveryIncluded=True)],NOW.isoformat())
    assert rows['101']['currency']=='RUB'
    assert rows['101']['delivery_included'] is True
    assert rows['101']['unit'] is None and rows['101']['vat_rate'] is None
    assert discounted_price(rows['101'],5,22,clock=NOW)==(None,None)
