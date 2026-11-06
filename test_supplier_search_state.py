from copy import deepcopy
from unittest.mock import Mock, patch
import pytest
import requests
from supplier_adapters import VseinstrumentiAdapter, SupplierQuote, integration_error
from supplier_comparison import model, mutate, quote_key
from automation_pipeline import _search_variants, _search_candidates, _hard_conflicts, _required_clarifications

REQUEST = 'Клапан балансировочный ручной Ду 100 чугун фланцевый с измерительными ниппелями'


def saved(name=REQUEST):
    quote = dict(name=name, supplier='ВсеИнструменты.ру', sku='1', price=100, unit='шт', price_date='2026-11-01')
    return dict(items=[dict(position=1, requested_name=REQUEST, quantity=1, unit='шт', candidates=[quote])])


def status(result, supplier='ВсеИнструменты.ру'):
    return model(result)['rows'][0]['supplier_search'][supplier]


def test_errors_empty_and_missing_diagnostics_are_distinct_and_read_only():
    result=saved();item=result['items'][0];item['candidates']=[]
    item['search_diagnostics']=[dict(supplier='ВсеИнструменты.ру', queries=1, candidates=0, errors=[])]
    assert status(result)['state']=='empty'
    item['search_diagnostics'][0]['errors']=['HTTP 500']
    assert status(result)['state']=='error'
    item['search_diagnostics']=[];item['rejected_candidates']=[dict(supplier='ВсеИнструменты.ру',error='HTTP 401')]
    assert status(result)['state']=='error'
    item['rejected_candidates']=[]
    result['deferred_suppliers']=['КРЕП-КОМП: ошибка загрузки каталога (HTTPError)']
    before=deepcopy(result)
    assert status(result,'КРЕП-КОМП')['state']=='error'
    assert before==result
    assert any(s['name']=='КРЕП-КОМП' for s in model(result)['suppliers'])


def test_partial_paging_never_claims_empty_success():
    result=saved();result['items'][0]['candidates']=[]
    result['items'][0]['search_diagnostics']=[dict(supplier='ВсеИнструменты.ру',queries=1,paging=dict(complete=False,limits=['page_cap']))]
    assert status(result)['state']=='incomplete'


def test_schema_error_is_not_empty_products():
    adapter=VseinstrumentiAdapter();adapter.token='test'
    for payload in [{'error':'maintenance'}, [], {'products':[None]}, {'products':[{'sku':1}]}]:
        response=Mock(ok=True);response.json.return_value=payload
        with patch.object(adapter,'_request_products',return_value=response):
            assert adapter.search('клапан')[0].error
    response.json.return_value={'products':[]}
    with patch.object(adapter,'_request_products',return_value=response):
        assert adapter.search('клапан')==[]


def test_http_error_status_is_safe_and_not_lost_for_false_response():
    response=requests.Response();response.status_code=500
    error=requests.HTTPError('secret body and credentials',response=response)
    assert integration_error(error)=='HTTP 500 (HTTPError)'


def test_valve_queries_keep_dn_and_original_requirements():
    variants=_search_variants(REQUEST)
    assert 'клапан балансировочный Ду 100' in variants[:8]
    assert 'балансировочный клапан фланцевый DN100' in variants[:8]


@pytest.mark.parametrize('name', [
    REQUEST.replace('100','125'), REQUEST.replace('чугун','латунь'),
    REQUEST.replace('ручной','автоматический'), REQUEST.replace('фланцевый','резьбовой'),
    REQUEST.replace('с измерительными','без измерительных'),
    'Клапан балансировочный Ду100 фланцевый с ниппелями',
])
def test_wrong_or_unconfirmed_valve_is_visible_but_cannot_be_selected(name):
    result=saved(name);row=model(result)['rows'][0]
    assert not row['value_comparison']['offers'][0]['comparable']
    assert status(result)['rejected'][0]['name']==name
    with pytest.raises(ValueError):
        mutate(result,dict(action='value_select',position=1,key=quote_key(result['items'][0]['candidates'][0])))


def test_exact_valve_is_comparable_without_category_override():
    assert model(saved())['rows'][0]['value_comparison']['offers'][0]['comparable']
    assert not _hard_conflicts(REQUEST, dict(name=REQUEST))
    assert not _required_clarifications(REQUEST, dict(name=REQUEST))
    assert _hard_conflicts(REQUEST, dict(name=REQUEST.replace('100', '125')))
    assert _required_clarifications(REQUEST, dict(name='Клапан балансировочный Ду100'))


def test_error_on_later_page_remains_in_pipeline_diagnostics():
    class Adapter:
        code='vseinstrumenti';name='ВсеИнструменты.ру';enabled=True
        def search_pages(self,*args,**kwargs):
            return [], dict(pages=1,fetched=1,stopped='error',error='HTTP 503')
    with patch('automation_pipeline._search_variants',return_value=['клапан']):
        _,diagnostics,_=_search_candidates([Adapter()],REQUEST)
    assert diagnostics[0]['errors']==['HTTP 503']


def test_saved_row_retains_supplier_of_failed_request():
    from automation_pipeline import _build_match_row
    error=SupplierQuote(supplier='ВсеИнструменты.ру',name=REQUEST,error='HTTP 503')
    with patch('automation_pipeline._search_candidates',return_value=([error],[],False)):
        row=_build_match_row(1,dict(goodName=REQUEST,count=1,unit=dict(name='шт')),[])
    assert row['rejected_candidates'][0]['supplier']=='ВсеИнструменты.ру'
    assert status(dict(items=[row]))['state']=='error'


def test_catalog_preparation_error_is_persisted_and_marks_search_metrics():
    import automation_pipeline as pipeline
    response=requests.Response();response.status_code=500
    krep=Mock(enabled=True)
    krep.prepare_catalog.side_effect=requests.HTTPError('private upstream body',response=response)
    row=dict(position=1,requested_name=REQUEST,quantity=1,unit='шт',decision='manual',selected=None,candidates=[])
    order=dict(id=37515277,name='test',orderItems=[dict(id=1,goodName=REQUEST,count=1,unit=dict(name='шт'))])
    with patch.object(pipeline,'KrepKompAdapter',return_value=krep), patch.object(pipeline,'_build_match_row',return_value=row), patch.object(pipeline,'_excluded_feedback_keys_many',return_value={}):
        result=pipeline.build_vi_draft(order)
    assert result['supplier_search_errors'][0]['errors']==['HTTP 500 (HTTPError)']
    assert result['search_metrics']['preparation_failures']==1
    assert result['search_metrics']['search_has_errors'] is True
    assert status(result,'КРЕП-КОМП')['state']=='error'
    assert 'private upstream body' not in str(result)


def test_real_saved_benarmo_dn100_requires_evidence_but_wrong_dn_conflicts():
    name='Клапан балансировочный Benarmo чугун, Ду100, Ру16, фланцевый, Kvs153.5, с ниппелями 022-6969'
    assert _hard_conflicts(REQUEST,dict(name=name))==[]
    missing=_required_clarifications(REQUEST,dict(name=name))
    assert 'не подтверждено: Управление клапаном' in missing
    assert 'не подтверждено: Измерительные ниппели' in missing
    for dn in ['65','125','150','200']:
        assert any('Условный проход' in s for s in _hard_conflicts(REQUEST,dict(name=name.replace('Ду100','Ду'+dn))))
    result=saved(name)
    assert not model(result)['rows'][0]['value_comparison']['offers'][0]['comparable']


def test_material_of_inner_part_does_not_prove_body_material():
    name='Клапан обратный пружинный 1/2" с латунным сердечником В/В'
    assert 'не подтверждено: Материал корпуса клапана' in _required_clarifications(REQUEST,dict(name=name))


def test_label_says_found_only_when_the_dialog_has_something_to_choose():
    from supplier_search_state import search_states
    def state(offer):
        view = dict(supplier='ВсеИнструменты.ру', key='k', name=offer['name'], price=100)
        comparison = dict(offers=[dict(view, **offer)])
        return search_states(dict(search_diagnostics=[]), {}, [view], comparison)['ВсеИнструменты.ру']['label']
    base = dict(comparable=False, unit_price=100.0, quantity=1, shortage=0)
    assert state(dict(base, name='Фланец 200', comparison_reasons=['Не разрешено: Условный проход'])) == 'Найдены варианты: выберите характеристики'
    assert state(dict(base, name='Песочница', comparison_reasons=['Не разрешено: Проверка категории'])) == 'Найденные товары не подходят'
    assert state(dict(base, name='Фланец 300', unit_price=None, comparison_reasons=['Нет коэффициента пересчёта единиц'])) == 'Найденные товары не подходят'
