from unittest.mock import patch
import pytest
import automation_pipeline as p
from supplier_adapters import SupplierQuote
from vi_order_match import _features, _negative_features, _range_relation, _identifiers


@pytest.mark.parametrize('text', ['без резинового уплотнителя', 'не прорезиненный', 'резиновый уплотнитель отсутствует', 'без EPDM'])
def test_negation_is_not_positive(text):
    assert not _features('Хомут ' + text)
    assert _negative_features('Хомут ' + text)
    assert p._hard_conflicts('Хомут прорезиненный 2"', {'name': 'Хомут 2" ' + text})


@pytest.mark.parametrize('stock', [0, 100, None])
def test_missing_feature_never_auto_selected(stock):
    quote = SupplierQuote(supplier='ВИ', name='Хомут 2" 56-64 мм', price=100, stock=stock, unit='шт')
    with patch.object(p, '_search_candidates', return_value=([quote], [], False)):
        row = p._build_match_row(1, {'goodName': 'Хомут прорезиненный 2" 56-64 мм', 'count': 1, 'unit': {'name': 'шт'}}, [])
    assert row['match_status'] == 'сомнительное'
    assert row['decision'] == 'review'
    assert row['clarification_reasons']


def test_multiple_dimensions_and_different_parameters():
    assert _range_relation('Хомут диаметр 56-64 мм длина 100-120 мм', 'Хомут диаметр 56-64 мм длина 200-220 мм') == 'disjoint'
    conflicts = p._hard_conflicts('Хомут диаметр 56-64 мм', {'name': 'Хомут длина 70-90 мм'})
    assert not conflicts
    assert p._range_note('Хомут диаметр 56-64 мм', {'name': 'Хомут длина 70-90 мм'})


def test_article_is_not_a_dimension():
    for name in ['Болт 123-456 10 мм', 'Хомут артикул 123-456 10 мм']:
        assert '123456' in _identifiers(name)
        assert any('модель/артикул' in c for c in p._hard_conflicts(name, {'name': name.replace('123-456', '123-457')}))


def test_feature_guard_does_not_depend_on_first_word():
    candidate = {'name': 'Хомут 2" 56-64 мм'}
    assert p._match_status('Усиленный прорезиненный хомут 2" 56-64 мм', candidate, .99, []) == 'сомнительное'
