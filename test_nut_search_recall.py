from datetime import datetime, timezone
from unittest.mock import patch

from automation_pipeline import _search_variants, _build_match_row
from supplier_adapters import SupplierQuote
from supplier_comparison import model

REQUEST='Гайка М20 DIN 934 кл. пр. 8 Материал Zn Цвет (тип покр)  гор. цинк.'


def test_nut_retrieval_includes_verified_short_query_and_hot_dip_variant():
    queries=_search_variants(REQUEST)
    assert 'Гайка М20 DIN 934 кл. пр. 8' in queries[:8]
    assert 'Гайка М20 DIN 934 горячее цинкование' in queries[:8]
    assert REQUEST in queries


def test_found_zinc_nut_pack_is_visible_but_not_auto_approved_for_hot_dip_pieces():
    quote=SupplierQuote(supplier='ВсеИнструменты.ру',sku='37570572',
        name='Гайка Стройметиз М20 DIN 934, кл.пр. 8, цинк, 1 кг 022093481-1',
        unit='Упаковка 1 шт',price=470,stock=21,price_observed_at=datetime.now(timezone.utc).isoformat())
    with patch('automation_pipeline._search_candidates',return_value=([quote],[],False)):
        row=_build_match_row(1,dict(goodName=REQUEST,count=240,unit=dict(name='шт')),[])
    assert row['requested_name']==REQUEST
    assert row['decision']!='auto_ready'
    comparison=model(dict(items=[row]))['rows'][0]
    assert any(v['sku']=='37570572' for v in comparison['offers'])
    assert not any(v['comparable'] for v in comparison['value_comparison']['offers'])


def test_nut_variant_does_not_merge_strength_8_and_8_8():
    assert 'Гайка М20 DIN 934 кл. пр. 8.8' in _search_variants(REQUEST.replace('пр. 8 ', 'пр. 8.8 '))
