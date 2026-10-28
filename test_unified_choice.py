import copy
from datetime import datetime, timezone
import pytest
from supplier_comparison import mutate, model, quote_key, selected_allowed
from automation_pipeline import _included


def data():
    def quote(supplier,name,price):return dict(supplier=supplier,sku=supplier,name=name,price=price,unit='м',price_date=datetime.now(timezone.utc).date().isoformat())
    return dict(items=[dict(position=1,requested_name='Труба 89х4 мм',quantity=10,unit='м',decision='review',
                           candidates=[quote('А','Труба 89х4 мм',100),quote('Б','Труба 89х2 мм',60)])])


def decide(result,value,decision):
    feature=next(v for g in model(result)['rows'][0]['value_comparison']['groups'] if g['field']=='Толщина стенки' for v in g['values'] if v['value']==value)
    mutate(result,dict(action='value',position=1,feature=feature['id'],decision=decision))


def test_choice_filters_clear_choice_without_substitution_and_reload():
    result=data();item=result['items'][0];a,b=item['candidates']
    mutate(result,dict(action='value_select',position=1,key=quote_key(a)))
    assert _included(item) and item['selected']==a and item['proposed_unit_price']==105
    decide(result,'2 мм','accepted')
    assert item['selected']==a
    assert model(result)['rows'][0]['value_comparison']['minimum']['supplier']=='Б'
    decide(result,'4 мм','rejected')
    assert item['selected'] is None and not _included(item) and item['proposed_unit_price']==0
    assert model(copy.deepcopy(result))['rows'][0]['value_comparison']['minimum']['supplier']=='Б'
    mutate(result,dict(action='value_select',position=1,key=quote_key(b)))
    assert _included(item) and item['selected']==b and item['quantity']==10
    decide(result,'2 мм','rejected')
    assert model(result)['rows'][0]['value_comparison']['minimum'] is None
    assert item['selected'] is None


def test_forged_ineligible_choice_cannot_bypass_filter():
    result=data();b=result['items'][0]['candidates'][1]
    with pytest.raises(ValueError,match='характеристикам'):
        mutate(result,dict(action='value_select',position=1,key=quote_key(b)))


def test_batch_filter_changes_all_values_with_one_reconciliation():
    result=data()
    values=[v['id'] for g in model(result)['rows'][0]['value_comparison']['groups']
            if g['field']=='Толщина стенки' for v in g['values']]
    mutate(result,dict(action='value',position=1,features=values,decision='accepted'))
    assert model(result)['rows'][0]['value_comparison']['minimum']['supplier']=='Б'
    mutate(result,dict(action='value',position=1,features=values,decision='rejected'))
    assert model(result)['rows'][0]['value_comparison']['minimum'] is None
    before=copy.deepcopy(result['items'][0]['comparison']['values'])
    with pytest.raises(ValueError):
        mutate(result,dict(action='value',position=1,features=[values[0],'forged'],decision='accepted'))
    assert result['items'][0]['comparison']['values']==before


def test_pack_rounding_keeps_requested_quantity_and_price_basis():
    result=data();item=result['items'][0];item.update(unit='шт',quantity=12)
    offer=item['candidates'][0];offer.update(unit='упак',pack_quantity=10,price=500)
    mutate(result,dict(action='value_select',position=1,key=quote_key(offer)))
    assert item['quantity']==12 and item['purchase_units']==2 and item['pack_size']==10
    assert item['proposed_unit_price']==52.5 and selected_allowed(item)


def test_sent_invoice_filters_preserve_commercial_fields():
    result=data();item=result['items'][0];item.update(selected=item['candidates'][0],decision='approved',proposed_unit_price=105)
    result['live_offer_created']=True
    before=copy.deepcopy(item)
    decide(result,'4 мм','rejected')
    assert {k:v for k,v in item.items() if k not in {'comparison','comparison_source'}}==before
    with pytest.raises(ValueError,match='отправлено'):
        mutate(result,dict(action='value_select',position=1,key=quote_key(item['candidates'][0])))
