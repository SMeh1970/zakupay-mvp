from copy import deepcopy
from datetime import datetime, timezone

from supplier_comparison import model, mutate, quote_key
from catalog_comparison import records

REQUEST='Гайка М20 DIN 934 кл. пр. 8 Материал Zn Цвет (тип покр) гор. цинк.'


def quote(sku, size='М20', din='934', strength='8', coating='цинк'):
    # Strength, standard, coating and packing are supplier specifications,
    # deliberately absent from the product title.
    return dict(supplier='ВсеИнструменты.ру',sku=sku,name='Гайка тестовая '+size,unit='шт',price=100,
        stock=1000,price_observed_at=datetime.now(timezone.utc).isoformat(),
        technical_specifications=[dict(name='Диаметр резьбы',value=size),dict(name='DIN',value=din),
            dict(name='Класс прочности',value=strength),dict(name='Покрытие',value=coating),
            dict(name='Фасовка',value='1',unit='кг'),dict(name='Размер под ключ',value='30',unit='мм')])


def data():
    return dict(items=[dict(position=1,requested_name=REQUEST,quantity=240,unit='шт',decision='review',
        candidates=[quote('a'),quote('b',strength='10',coating='горячее цинкование'),
                    quote('wrong-size',size='М27',strength='12'),quote('wrong-din',din='985',strength='6')])])


def comparison(result):
    return model(result)['rows'][0]['value_comparison']


def decide(result,field,value,decision):
    feature=next(v for g in comparison(result)['groups'] if g['field']==field for v in g['values'] if v['value']==value)
    mutate(result,dict(action='value',position=1,feature=feature['id'],decision=decision))


def test_values_from_specs_survive_primary_matching_and_are_not_hidden_by_secondary_difference():
    result=data();before=deepcopy(result)
    panel=comparison(result)
    assert {v['key'] for v in panel['offers']}=={quote_key(q) for q in result['items'][0]['candidates'][:2]}
    groups={g['field']:{v['value'] for v in g['values']} for g in panel['groups']}
    assert groups['Класс прочности']=={'8','10'}
    assert groups['Покрытие']=={'Цинк (способ не указан)','Горячее цинкование'}
    assert groups['Размер под ключ']=={'30 мм'}
    assert groups['Фасовка']=={'1 кг'}
    assert result==before


def test_accepting_secondary_value_recalculates_and_rejecting_it_removes_price():
    result=data()
    assert comparison(result)['minimum'] is None
    decide(result,'Класс прочности','10','accepted')
    assert comparison(result)['minimum']['sku']=='b'
    assert result['items'][0]['requested_name']==REQUEST
    assert not result['items'][0].get('selected')
    decide(result,'Класс прочности','10','rejected')
    assert comparison(result)['minimum'] is None
    decide(result,'Покрытие','Цинк (способ не указан)','accepted')
    assert comparison(result)['minimum']['sku']=='a'


def test_saved_pricelist_characteristics_are_also_choices():
    result=data();q=result['items'][0]['candidates'][0]
    q.pop('technical_specifications')
    q['name']='Гайка М20 DIN 934'
    q['characteristics']=[dict(parameter='Класс прочности',value='10',origin='supplier',evidence='Столбец Класс'),
                          dict(parameter='Покрытие',value='Горячее цинкование',origin='supplier',evidence='Столбец Покрытие')]
    result['items'][0]['candidates']=[q]
    groups={g['field']:{v['value'] for v in g['values']} for g in comparison(result)['groups']}
    assert groups['Класс прочности']=={'10'}
    assert groups['Покрытие']=={'Горячее цинкование'}


def test_conflicting_primary_api_and_name_are_not_silently_joined():
    q=quote('contradictory');q['name']='Гайка М27 DIN 934'
    result=data();result['items'][0]['candidates']=[q]
    assert comparison(result)['offers']==[]
    assert any(r['ambiguous'] for r in records(q['name'],q) if r['field']=='Метрическая резьба')
