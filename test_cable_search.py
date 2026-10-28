from datetime import datetime, timezone
from automation_pipeline import _search_variants, _search_candidates, _hard_conflicts, _build_match_row, _review_candidates
from supplier_adapters import SupplierQuote
from supplier_comparison import model
from cable_identity import identity, reel_length


def quote(name, sku='1', price=100, unit='м'):
    return SupplierQuote(supplier='VI',name=name,sku=sku,price=price,unit=unit,
                         price_observed_at=datetime.now(timezone.utc).isoformat())


def test_cable_mark_and_dimensions_are_required_and_typed():
    assert identity('Кабель КГ 3х4')['section']==4
    for wrong in ['Кабель ВВГнг 3х4','Кабель КГ-ХЛ 3х4','Кабель КГ 4х3','Провод АКБ 3х4','Электроды 4 мм, 3 кг']:
        assert _hard_conflicts('Кабель КГ 3х4',dict(name=wrong))
    assert not _hard_conflicts('Кабель КГ 3х4',dict(name='Кабель СПЕЦ КГ 3x4 мм2, ГОСТ, 15 метров'))
    good=quote('Кабель СПЕЦ КГ 3x4 мм2, ГОСТ, 15 метров').to_dict()
    data=dict(items=[dict(position=1,requested_name='Кабель КГ 3х4',quantity=200,unit='м.п.',candidates=[good])])
    comparison=model(data)['rows'][0]['value_comparison']
    assert comparison['minimum'] is not None
    assert {'Марка кабеля','Число жил','Сечение жилы'} <= {g['field'] for g in comparison['groups']}
    assert not {'Размер 1','Размер 2'} & {g['field'] for g in comparison['groups']}


def test_cable_search_does_not_stop_on_irrelevant_first_page():
    class VI:
        code='vseinstrumenti';name='VI'
        def __init__(self):self.calls=[]
        def search(self,query,limit=8):
            self.calls.append(query)
            if query=='43843283':return [quote('Кабель СПЕЦ КГ 3x4 мм2, ГОСТ, 15 метров','exact')]
            return [quote('Электроды 4 мм 3 кг',f'{query}-{i}',1) for i in range(20)]
    vi=VI()
    row=_build_match_row(1,dict(goodName='Кабель КГ 3х4',count=200,unit=dict(name='м.п.')),[vi])
    assert '43843283' in vi.calls
    assert row['selected']['sku']=='exact'
    assert all(identity(v['name']) for v in _review_candidates(row))
    assert _search_variants('Кабель КГ 3х4')[0]=='кабель КГ 3х4'


def test_reel_price_is_divided_only_when_sold_as_one_reel():
    name='Кабель СПЕЦ КГ 3x4 мм2, ГОСТ, 15 метров'
    assert reel_length(name,'Штука','м.п.')==15
    assert reel_length(name,'м','м.п.') is None
    for supplier_unit,price in [('Штука',7930),('м',7930/15)]:
        candidate=quote(name,price=price,unit=supplier_unit).to_dict()
        data=dict(items=[dict(position=1,requested_name='Кабель КГ 3х4',quantity=200,unit='м.п.',candidates=[candidate])])
        best=model(data)['rows'][0]['value_comparison']['minimum']
        assert round(best['unit_price'],2)==528.67
        if supplier_unit=='Штука':assert best['quantity']==14
