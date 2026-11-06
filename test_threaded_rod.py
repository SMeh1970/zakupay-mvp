from datetime import datetime, timezone

from catalog_comparison import records
from product_identity import kind
from supplier_comparison import model

NOW = datetime.now(timezone.utc).isoformat()
REQUEST = 'Шпилька М8 L=1м'


def fields(name):
    return {r['field']: r['value'] for r in records(name)}


def test_rod_length_and_thread_from_every_supplier_notation():
    for name in ('Шпилька М8 L=1м', 'Штанга ОПМ DIN 975 (1 m) М8 8.8 zn, 5 шт. УТ000003764',
                 'Резьбовая шпилька РК ГРУП оц. обл. M8x1 м (230 гр.) РКГ00009253',
                 'Оцинкованная шпилька с резьбой Zitar TR M8x1м 100768'):
        got = fields(name)
        assert (got.get('Тип товара'), got.get('Метрическая резьба'), got.get('Длина изделия')) == ('шпилька', 'М8', '1000 мм'), (name, got)
        assert 'Угол' not in got and 'Размер 1' not in got
    assert fields('Усиленная резьбовая шпилька РК ГРУП 8x200, DIN 976, цинк')['Длина изделия'] == '200 мм'
    assert kind('Шуруп-шпилька STARFIX М8x90 мм, 4 шт.') == 'шуруп-шпилька'
    assert kind('Штанга для душа') is None


def test_only_metre_m8_rods_are_offered_and_packs_are_per_piece():
    def quote(sku, name, price):
        return dict(supplier='ВсеИнструменты.ру', sku=sku, name=name, unit='Штука', price=price, stock=1000, price_observed_at=NOW)
    candidates = [
        quote('opm', 'Штанга ОПМ DIN 975 (1 m) М8 8.8 zn, 5 шт. УТ000003764', 1113),
        quote('rk', 'Резьбовая шпилька РК ГРУП оц. обл. M8x1 м (230 гр.) РКГ00009253', 86),
        quote('zitar', 'Оцинкованная шпилька с резьбой Zitar TR M8x1м 100768', 110),
        quote('screw', 'Шуруп-шпилька STARFIX М8x90 мм, 4 шт. SMM1-15944-4', 80),
        quote('holder', 'Держатель для прутка D6-10 мм вкручиваемый со шпилькой М8 Система КМ L80 мм AURAFORT TD', 83),
        quote('m12', 'Резьбовая шпилька КРЕП-КОМП цинк, DIN 975, м12x1000, пр. 8.8, 20 шт. шрп12', 3106),
        quote('short', 'Усиленная резьбовая шпилька РК ГРУП 8x200, DIN 976, цинк', 63)]
    result = dict(items=[dict(position=4, requested_name=REQUEST, quantity=300, unit='шт', decision='review', candidates=candidates)])
    offers = {o['sku']: o for o in model(result)['rows'][0]['value_comparison']['offers']}
    assert {k for k, o in offers.items() if o['comparable']} == {'opm', 'rk', 'zitar'}, {k: o['comparison_reasons'] for k, o in offers.items()}
    assert offers['opm']['unit_price'] == round(1113 / 5, 2)
    assert 'screw' not in offers and 'holder' not in offers
