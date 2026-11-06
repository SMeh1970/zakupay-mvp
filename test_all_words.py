from datetime import datetime, timezone

from characteristic_comparison import name_words
from supplier_comparison import model, mutate

NOW = datetime.now(timezone.utc).isoformat()


def candidate(sku, name):
    return dict(supplier='ВсеИнструменты.ру', sku=sku, name=name, unit='шт', price=100, stock=500, price_observed_at=NOW)


def result():
    names = [('1', 'Шпилька резьбовая DIN 975 М8х1000 оцинкованная Zitar'),
             ('2', 'Шпилька резьбовая DIN 975 M8x1000 оцинкованная РК ГРУП'),
             ('3', 'Шпилька резьбовая DIN 976 М8х1000 нержавеющая')]
    item = lambda position: dict(position=position, requested_name='Шпилька резьбовая М8 1000 мм', quantity=10,
                                 unit='шт', decision='review', candidates=[candidate(s, n) for s, n in names])
    return dict(items=[item(1), item(2)])


def words(r, position=1):
    return {w['word']: w for w in model(r)['rows'][position - 1]['value_comparison']['words']}


def comparable(r, position=1):
    return {o['key'].split('|')[1] if '|' in o['key'] else o['key']: o['comparable']
            for o in model(r)['rows'][position - 1]['value_comparison']['offers']}


def apply(r, decision, *ws, position=1):
    mutate(r, dict(action='values_batch', decisions=[dict(position=position, decision=decision,
                                                          features=['word:' + w for w in ws])]))


def test_every_word_once_with_count_and_latin_lookalikes_merged():
    w = words(result())
    assert w['шпилька']['count'] == 3
    assert w['м8х1000']['count'] == 3  # «M8x1000» in Latin is the same word
    assert w['zitar']['count'] == 1 and w['976']['count'] == 1
    assert all(v['state'] == 'pending' for v in w.values())
    assert name_words('M8x1') == name_words('М8х1')


def test_rejected_word_excludes_products_with_it_only_in_that_position():
    r = result()
    apply(r, 'rejected', 'zitar')
    offers = model(r)['rows'][0]['value_comparison']['offers']
    zitar = next(o for o in offers if 'Zitar' in o['name'])
    assert not zitar['comparable'] and 'Исключено слово: zitar' in zitar['comparison_reasons']
    assert all('Исключено слово: zitar' not in o['comparison_reasons'] for o in offers if 'Zitar' not in o['name'])
    assert words(r)['zitar']['state'] == 'rejected'
    other = model(r)['rows'][1]['value_comparison']
    assert all('Исключено слово: zitar' not in o['comparison_reasons'] for o in other['offers'])
    assert {w['word']: w['state'] for w in other['words']}['zitar'] == 'pending'


def test_accepted_word_keeps_only_products_with_it_and_pending_undoes():
    r = result()
    apply(r, 'accepted', '976')
    offers = model(r)['rows'][0]['value_comparison']['offers']
    assert all(('Нет слова: 976' in o['comparison_reasons']) == ('976' not in o['name']) for o in offers)
    apply(r, 'pending', '976')
    offers = model(r)['rows'][0]['value_comparison']['offers']
    assert all('Нет слова: 976' not in o['comparison_reasons'] for o in offers)
    assert words(r)['976']['state'] == 'pending'


def test_unknown_word_is_refused():
    import pytest
    with pytest.raises(ValueError):
        apply(result(), 'rejected', 'несуществующее')
