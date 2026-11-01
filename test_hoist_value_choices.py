from copy import deepcopy
from datetime import datetime, timezone
from supplier_comparison import model, mutate, quote_key
from automation_pipeline import _hard_conflicts, _required_clarifications

REQUEST='Таль цепная электрическая 5 тонн (HHBD05-02ST 5т 6м 380В передвижная, 1 скорость, 2ух цепная)'
NAME='Электрическая таль TOR цепная ТЭЦП HHBD05-02T 5,0 т 6 м 380В 12856'


def quote(sku='16588376',name=NAME,capacity='5000',weight='162',**extra):
    return dict(supplier='ВсеИнструменты.ру',sku=sku,name=name,unit='Штука',price=100,stock=5,
        price_observed_at=datetime.now(timezone.utc).isoformat(),
        technical_specifications=[dict(name='Тип',value='цепной'),dict(name='Грузоподъемность',value=capacity,unit='кг'),
          dict(name='Напряжение',value='380'),dict(name='Высота подъема',value='6',unit='м'),
          dict(name='Наличие каретки',value='да'),dict(name='Вес нетто',value=weight,unit='кг')],**extra)


def data(*quotes):
    return dict(items=[dict(position=1,requested_name=REQUEST,quantity=1,unit='изд.',decision='review',candidates=list(quotes))])


def panel(result):
    return model(result)['rows'][0]['value_comparison']


def test_5t_hoist_is_visible_with_secondary_model_difference_and_real_value_sources():
    q=quote(hard_conflicts=['не совпадает модель/артикул']);result=data(q);before=deepcopy(result)
    row=model(result)['rows'][0];comparison=row['value_comparison']
    assert len(comparison['offers'])==1
    assert comparison['offers'][0]['unit_price']==100
    assert comparison['minimum'] is None
    assert row['supplier_search'][q['supplier']]['state']=='review'
    weight=next(v for g in comparison['groups'] if g['field']=='Вес нетто' for v in g['values'])
    assert weight['value']=='162 кг'
    assert weight['sources'][0]['sku']=='16588376'
    assert weight['sources'][0]['name']==NAME
    assert weight['sources'][0]['origin']=='supplier_api'
    assert result==before


def test_primary_gate_excludes_wrong_capacity_drive_type_and_hoist_parts():
    wrong=[quote('two',NAME.replace('5,0 т','2,0 т'),capacity='2000',weight='97'),
           quote('rope',NAME.replace('цепная','канатная'),weight='111'),
           quote('hand',NAME.replace('Электрическая','Ручная'),weight='112'),
           quote('chain','Специальная цепь для талей TOR HHBD05-02',weight='113')]
    comparison=panel(data(quote(),*wrong))
    assert {v['sku'] for v in comparison['offers']}=={'16588376'}
    assert {v['value'] for g in comparison['groups'] if g['field']=='Вес нетто' for v in g['values']}=={'162 кг'}


def test_2500kg_nominal_with_5000kg_through_block_is_not_silently_5t():
    q=quote('block',capacity='2500')
    q['technical_specifications'].append(dict(name='Грузоподъемность через блок',value='5000',unit='кг'))
    assert panel(data(q))['offers']==[]
    assert panel(data(q))['groups']==[]


def test_source_collection_names_each_supplier_quote_not_an_unattributed_union():
    q=quote('other',NAME.replace('TOR','OCALIFT'),weight='250')
    comparison=panel(data(quote(),q))
    weights=next(g for g in comparison['groups'] if g['field']=='Вес нетто')['values']
    assert {v['value']:{s['sku'] for s in v['sources']} for v in weights}=={'162 кг':{'16588376'},'250 кг':{'other'}}


def test_secondary_choices_need_explicit_decisions_and_never_auto_select_invoice():
    q=quote();result=data(q)
    assert panel(result)['minimum'] is None
    view=panel(result)['offers'][0]
    for field in ['Модель тали','Число скоростей','Число ветвей цепи']:
        feature=next(v for g in panel(result)['groups'] if g['field']==field for v in g['values'])
        mutate(result,dict(action='value',position=1,feature=feature['id'],decision='accepted'))
    assert panel(result)['minimum']['key']==quote_key(q)
    assert not result['items'][0].get('selected')
    assert result['items'][0]['requested_name']==REQUEST


def test_pipeline_treats_secondary_model_difference_as_choice_and_keeps_capacity_hard():
    q=quote()
    assert _hard_conflicts(REQUEST,q)==[]
    missing=_required_clarifications(REQUEST,q)
    assert any('Модель тали' in s for s in missing)
    assert any('Число скоростей' in s for s in missing)
    assert any('Число ветвей цепи' in s for s in missing)
    assert any('Грузоподъемность' in s for s in _hard_conflicts(REQUEST,quote(capacity='2000')))
