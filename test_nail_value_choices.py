from datetime import datetime, timezone
from unittest.mock import patch
import pytest

from automation_pipeline import _hard_conflicts, _build_match_row
from supplier_adapters import SupplierQuote
from supplier_comparison import model, mutate, quote_key, conversion

REAL_NAME='Гвозди Саморезик 4,0х120, неоцинк. ГОСТ 4028-63 (5кг) GS-4х120-5'


def quote(sku,name=REAL_NAME,**changes):
    return dict(supplier='ВИ',sku=sku,name=name,unit='Штука',price=723,stock=100,
        price_observed_at=datetime.now(timezone.utc).isoformat(),**changes)


def result(*quotes,request='Гвозди 120мм'):
    return dict(items=[dict(position=6,requested_name=request,quantity=20,unit='кг',decision='review',candidates=list(quotes))])


def panel(data):
    return model(data)['rows'][0]['value_comparison']


def test_real_4_by_120_nail_is_120mm_and_5kg_pack_price_is_per_kg():
    q=quote('real')
    assert not _hard_conflicts('Гвозди 120мм',q)
    data=result(q);view=panel(data)['offers'][0]
    assert view['comparable'] and view['factor']==5 and view['unit_price']==144.6
    assert view['quantity']==4 and view['converted_quantity']==20 and view['total']==2892
    groups={g['field']:{v['value'] for v in g['values']} for g in panel(data)['groups']}
    assert groups['Длина гвоздя']=={'120 мм'}
    assert groups['Диаметр гвоздя']=={'4 мм'}
    assert groups['Покрытие']=={'Без покрытия'}
    assert groups['Фасовка']=={'5 кг'}


@pytest.mark.parametrize('name',[
    'Дюбель-гвоздь 8х120 мм 300 шт',
    'Дюбель для теплоизоляции с металлическим гвоздём 10х120, 50шт',
    'Клей монтажный Жидкие гвозди 120 мл',
    'Кляймеры N1 с гвоздями (120шт)',
    'Гвозди строительные 4х100 мм (5кг)',
])
def test_unrelated_items_and_wrong_lengths_do_not_pollute_120mm_choices(name):
    data=result(quote('real'),quote('wrong',name))
    assert {v['sku'] for v in panel(data)['offers']}=={'real'}
    assert panel(data)['unrelated_count']==1


def test_diameters_from_matching_products_are_visible_and_recalculate_on_cross():
    data=result(quote('four'),quote('five',REAL_NAME.replace('4,0х120','5,0х120')))
    group=next(g for g in panel(data)['groups'] if g['field']=='Диаметр гвоздя')
    assert {v['value'] for v in group['values']}=={'4 мм','5 мм'}
    four=next(v for v in group['values'] if v['value']=='4 мм')
    mutate(data,dict(action='value',position=6,feature=four['id'],decision='rejected'))
    assert {v['sku'] for v in panel(data)['offers'] if v['comparable']}=={'five'}
    assert data['items'][0]['requested_name']=='Гвозди 120мм'


def test_api_length_and_mass_are_used_but_shipping_weight_is_not_mass_conversion():
    q=quote('api','Гвозди строительные',technical_specifications=[dict(name='Длина',value='120',unit='мм'),
        dict(name='Диаметр стержня',value='4',unit='мм'),dict(name='Фасовка',value='2.5',unit='кг')])
    assert panel(result(q))['offers'][0]['factor']==2.5
    q['technical_specifications']=[dict(name='Длина',value='120',unit='мм')]
    q['weight']=5
    assert conversion(result(q)['items'][0],q)[0] is None
    assert not panel(result(q))['offers'][0]['comparable']


def test_conflicting_pack_masses_require_confirmation():
    q=quote('conflict',technical_specifications=[dict(name='Фасовка',value='1',unit='кг')])
    assert conversion(result(q)['items'][0],q)[0] is None


def test_mass_and_piece_count_of_same_pack_are_compatible():
    q=quote('pack',technical_specifications=[dict(name='Фасовка',value='100',unit='шт')])
    assert conversion(result(q)['items'][0],q)[0]==5


def test_pipeline_keeps_real_nail_for_operator_instead_of_false_dimension_rejection():
    q=SupplierQuote(supplier='ВИ',name=REAL_NAME,sku='real',unit='Штука',price=723,stock=10,
                    price_observed_at=datetime.now(timezone.utc).isoformat())
    with patch('automation_pipeline._search_candidates',return_value=([q],[],False)):
        row=_build_match_row(6,dict(goodName='Гвозди 120мм',count=20,unit=dict(name='кг')),[])
    candidates=row['candidates']+row['rejected_candidates']
    assert any(c.get('sku')=='real' and not c.get('hard_conflicts') for c in candidates)
    assert panel(dict(items=[row]))['minimum']['unit_price']==144.6
