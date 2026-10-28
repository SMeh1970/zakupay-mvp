from unittest.mock import patch
from datetime import datetime, timezone
import automation_pipeline as p
from supplier_adapters import KrepKompAdapter
from supplier_comparison import model, selected_allowed


def test_cold_catalog_is_prepared_before_search_and_supplier_is_included():
    class Krep:
        enabled=True;name='КРЕП-КОМП'
        def __init__(self):self.ready=False
        def catalog_ready(self):return self.ready
        def prepare_catalog(self):self.ready=True
    krep=Krep();calls=[];progress=[]
    def match(position,item,suppliers,*args):
        calls.append([getattr(s,'name','saved') for s in suppliers])
        return dict(position=position,requested_name='Болт М16',quantity=1,unit='шт',decision='manual',selected=None,candidates=[])
    order=dict(id=1,name='test',orderItems=[dict(id=1,goodName='Болт М16',count=1,unit=dict(name='шт'))])
    with patch.object(p,'KrepKompAdapter',return_value=krep),patch.object(p,'_build_match_row',side_effect=match),patch.object(p,'_excluded_feedback_keys_many',return_value={}):
        result=p.build_vi_draft(order,progress_callback=lambda completed,total,row:progress.append(row['position']))
    assert krep.ready and len(calls)==1 and 'КРЕП-КОМП' in calls[0]
    assert result['deferred_suppliers']==[] and progress==[1]


def test_cable_rank_checks_mark_before_limiting_supplier_candidates():
    wrong=dict(Код='wrong',Наименование='Кабель ВВГ 3х4',Артикул='КГ 3х4')
    right=dict(Код='right',Наименование='Кабель КГ 3х4',Артикул='right')
    with patch.object(KrepKompAdapter,'enabled',True),patch.object(KrepKompAdapter,'_catalog_items',return_value=[wrong,right]),patch.object(KrepKompAdapter,'_enrich',side_effect=lambda items:items):
        found=KrepKompAdapter().search('Кабель КГ 3х4',limit=1)
    assert found[0]['Код']=='right'


def test_legacy_wrong_cable_is_not_included_but_stored_choice_is_retained():
    candidate=dict(supplier='КРЕП-КОМП',sku='old',name='Кабель ВВГ 3х4',unit='1809',price=205.3,price_date=datetime.now(timezone.utc).date().isoformat())
    item=dict(position=1,requested_name='Кабель КГ 3х4',quantity=200,unit='м.п.',selected=candidate,candidates=[candidate],decision='approved')
    assert not selected_allowed(item)
    row=model(dict(items=[item]))['rows'][0]
    assert not row['invoice']['included'] and row['invoice']['requires_review']
    assert item['selected']==candidate
    sent=model(dict(items=[item],live_offer_created=True))['rows'][0]
    assert sent['invoice']['included'] and not sent['invoice']['requires_review']
