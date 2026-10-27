import copy
from datetime import datetime, timezone

from supplier_comparison import model, mutate, preserve_comparison


def quote(sku, name='Труба 89х4 мм', price=100, **extra):
    return dict(sku=sku, supplier=sku, name=name, price=price, unit='м',
                price_date=datetime.now(timezone.utc).date().isoformat(), **extra)


def result(*offers):
    return dict(order_id=1, status='needs_review', items=[dict(position=1,
        requested_name='Труба 89х4 мм', quantity=10, unit='м', candidates=list(offers), decision='review')])


def row(data):
    return model(data)['rows'][0]['value_comparison']


def decide(data, field, value, decision):
    feature = next(v for g in row(data)['groups'] if g['field'] == field for v in g['values'] if v['value'] == value)
    mutate(data, dict(action='value',position=1,feature=feature['id'],decision=decision))


def test_initial_minimum_all_suppliers_and_variants():
    data=result(quote('a',price=100),quote('b',price=80),quote('c',price=90))
    assert row(data)['minimum']['price']==80
    assert row(data)['suitable_count']==3


def test_allow_alternative_retains_original_and_exclude_original():
    data=result(quote('a'),quote('b','Труба 89х2 мм',60))
    assert row(data)['minimum']['price']==100
    decide(data,'Толщина стенки','2 мм','accepted')
    assert row(data)['suitable_count']==2
    assert row(data)['minimum']['price']==60
    decide(data,'Толщина стенки','4 мм','rejected')
    assert row(data)['suitable_count']==1
    decide(data,'Толщина стенки','2 мм','rejected')
    assert row(data)['minimum'] is None


def test_and_between_parameters():
    data=result(quote('a'),quote('b','Труба 90х2 мм',60))
    decide(data,'Толщина стенки','2 мм','accepted')
    assert row(data)['minimum']['price']==100
    decide(data,'Наружный диаметр','90 мм','accepted')
    assert row(data)['minimum']['price']==60


def test_missing_date_stale_price_and_unit_not_comparable():
    a=quote('a'); b=quote('b',price=1);b['price_date']='2000-01-01'
    c=quote('c',price=2);c.pop('price_date')
    d=quote('d',price=3);d['unit']='т'
    data=result(a,b,c,d)
    assert row(data)['suitable_count']==1
    assert row(data)['minimum']['supplier']=='a'


def test_pack_normalization():
    a=quote('a');a['unit']='шт'
    b=quote('b',price=500,pack_quantity=10);b['unit']='упак'
    data=result(a,b);data['items'][0]['unit']='шт'
    assert row(data)['minimum']['supplier']=='b'
    assert row(data)['minimum']['unit_price']==50


def test_preserve_by_position_and_reject_changed_request():
    old=result(quote('a'),quote('b','Труба 89х2 мм',60))
    decide(old,'Толщина стенки','2 мм','accepted')
    new=result(quote('a'),quote('b','Труба 89х2 мм',50))
    preserve_comparison(new,old)
    assert row(new)['minimum']['price']==50
    changed=result(quote('a'));changed['items'][0]['quantity']=11
    preserve_comparison(changed,old)
    assert not changed['items'][0].get('comparison')


def test_no_invoice_changes_and_unknown_feature_rejected():
    import pytest
    data=result(quote('a'));before=copy.deepcopy(data['items'][0])
    decide(data,'Толщина стенки','4 мм','rejected')
    item=data['items'][0];item.pop('comparison');item.pop('comparison_source')
    assert item==before
    with pytest.raises(ValueError):mutate(data,dict(action='value',position=1,feature='forged',decision='accepted'))


def test_missing_characteristic_needs_explicit_permission():
    data=result(quote('a'),quote('b','Труба 89 мм',20))
    assert row(data)['minimum']['price']==100
    for group in row(data)['groups']:
        for value in group['values']:
            if value['state']=='pending':
                mutate(data,dict(action='value',position=1,feature=value['id'],decision='accepted'))
    assert row(data)['minimum']['price']==20


def test_observed_api_date_and_supplier_exclusion():
    a=quote('a');a['price_observed_at']=a.pop('price_date')
    data=result(a,quote('b',price=80))
    data['comparison']={'suppliers':{'b':False}}
    assert row(data)['minimum']['supplier']=='a'


def test_same_parameter_multiple_allowed_values_use_or():
    from characteristic_comparison import build
    from supplier_comparison import offer_view
    data=result(quote('a'));item=data['items'][0]
    view=offer_view(item,item['candidates'][0])
    feature=next(f for f in view['features'] if f['field']=='Толщина стенки')
    view['features'].append({**feature,'id':'alternate','value':'2 мм','differs':True})
    assert build(item,[view])['suitable_count']==1
