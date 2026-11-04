from datetime import datetime, timezone

from automation_pipeline import _candidate_pack_size
from supplier_comparison import model, mutate, quote_key

NOW = datetime.now(timezone.utc).isoformat()
ANCHOR = 'Втулочный анкер с болтом fischer FSL M10x100/35 D12 ZP(50 шт.) 45655'


def result(**extra):
    candidate = dict(supplier='ВсеИнструменты.ру', sku='43434976', name=ANCHOR, unit='Штука', price=5898,
                     stock=10, price_observed_at=NOW, **extra)
    return dict(items=[dict(position=2, requested_name='Анкерный болт для бетона M10x100', quantity=100,
                            unit='шт', decision='review', candidates=[candidate])])


def offer(r):
    return model(r)['rows'][0]['value_comparison']['offers'][0]


def test_supplier_pack_from_its_data_gives_price_per_piece_and_whole_packs():
    o = offer(result(technical_specifications=[dict(name='Фасовка', value='50', unit='шт'),
                                               dict(name='Фасовка', value='4.6', unit='кг')]))
    assert (o['unit_price'], o['quantity'], o['total']) == (117.96, 2, 11796.0)


def test_pack_in_brackets_of_the_name_is_used_without_supplier_data():
    o = offer(result())
    assert (o['unit_price'], o['quantity']) == (117.96, 2)


def test_price_list_per_piece_stays_per_piece():
    r = result(source_type='price_list')
    r['items'][0]['candidates'][0]['unit'] = 'шт'
    assert model(r)['rows'][0]['value_comparison']['offers'][0]['unit_price'] in (None, 5898.0)


def test_article_at_the_end_of_a_name_is_not_a_pack_size():
    assert _candidate_pack_size({'name': ANCHOR, 'unit': 'шт'}, 'шт') == 50
    assert _candidate_pack_size({'name': 'Анкер fischer FSL 45655', 'unit': 'шт'}, 'шт') == 1


def test_a_chosen_product_can_be_taken_out_of_the_invoice():
    r = result()
    key = quote_key(r['items'][0]['candidates'][0])
    mutate(r, dict(action='value_select', position=2, key=key))
    assert r['items'][0]['selected'] and model(r)['rows'][0]['invoice']['included']
    mutate(r, dict(action='value_unselect', position=2, key=key))
    item = r['items'][0]
    assert item['selected'] is None and item['decision'] == 'excluded'
    assert not model(r)['rows'][0]['invoice']['included']
    assert offer(r)['comparable']


def test_pack_unit_with_count_in_brackets_or_words():
    from supplier_comparison import conversion
    item = dict(position=2, requested_name='Анкерный болт для бетона M10x100', quantity=100, unit='шт')
    for name in ('Анкер-болт Fasty amt plus m10x90 magnisilver1000 (100 шт.) AMT10090AM',
                 'Анкер болт SORREX оцинкованный гильза PFG M6x40 универсальный (100 штук) CPFG-1-6-100'):
        assert conversion(item, dict(name=name, unit='Упаковка 1 шт', price=1, supplier='ВсеИнструменты.ру'))[0] == 100.0
