import copy
from datetime import datetime, timezone
import pytest
from supplier_comparison import model, reconcile, selected_allowed
from automation_pipeline import _hard_conflicts, _search_variants


def result(request, names, unit='шт'):
    quotes=[dict(supplier='VI',sku=str(i),name=name,price=100+i,unit=unit,
                 price_date=datetime.now(timezone.utc).date().isoformat()) for i,name in enumerate(names)]
    return dict(items=[dict(position=1,requested_name=request,quantity=2,unit=unit,decision='review',candidates=quotes)])


def allowed(data):
    return [v['name'] for v in model(data)['rows'][0]['value_comparison']['offers'] if v['comparable']]


def test_model_word_order_spaces_and_brand_case_do_not_block_exact_product():
    data=result('Пила дисковая Makita HS 7601',[
        'Дисковая пила Makita HS7601', 'Дисковая пила Makita HS7601K',
        'Набор Makita DK0167: лобзик и дисковая пила HS7601',
        'Якорь для дисковой пилы Makita HS7601'])
    assert allowed(data)==['Дисковая пила Makita HS7601']
    assert not _hard_conflicts(data['items'][0]['requested_name'],data['items'][0]['candidates'][0])
    assert any('HS7601' in q for q in _search_variants(data['items'][0]['requested_name']))


@pytest.mark.parametrize('requested,correct,wrong',[
    ('Рулетка измерительная 10 м','Измерительная рулетка GROSS 10м х 25мм','Рулетка GROSS 5м х 25мм'),
    ('Плоскогубцы комбинированные 180 мм','Комбинированные пассатижи STARTUL 180 мм','Пассатижи комбинированные 160 мм'),
    ('Бита PH2х50 мм','Бита PH2, 50 мм, S2 SGS','Бита PZ2, 50 мм, S2 SGS'),
    ('Сверло по металлу 10 мм','Сверло по металлу 10 мм HSS','Сверло по металлу 2 мм HSS'),
    # Order 37210407: parameters that used to impose no requirement at all.
    ('Респиратор РУССИЗ 926 FFP2 с клапаном и угольным слоем','Респиратор РУССИЗ 926 FFP2 с клапаном и угольным слоем','Респиратор РУССИЗ 926 FFP1 с клапаном и угольным слоем'),
    ('Круг лепестковый торцевой тип-1 125 мм P40','Круг лепестковый торцевой тип-1 125 мм P40','Круг лепестковый торцевой тип-1 125 мм P80'),
    ('Самозацепной шлифовальный круг с отверстиями 125 мм RING P120','Самозацепной шлифовальный круг с отверстиями 125 мм RING P120','Самозацепной шлифовальный круг с отверстиями 125 мм RING P240'),
    ('Подшипник 80205 (6205 ZZ) АПЗ-20 Курск','Подшипник 80205 (6205 ZZ) АПЗ-20 Курск','Подшипник 80305 (6305 ZZ) АПЗ-20 Курск'),
    ('Очиститель монтажной пены 500 мл','Очиститель незатвердевшей монтажной пены IRFIX 0,5 л','Очиститель монтажной пены 650 мл'),
    ('Огнетушитель ОП-5 (з) АВСЕ','Огнетушитель Меланти ОП-5 з АВСЕ','Огнетушитель Меланти ОП-4 з АВСЕ'),
])
def test_identity_then_requested_parameters(requested,correct,wrong):
    assert allowed(result(requested,[correct,wrong]))==[correct]


def test_context_product_is_not_the_requested_product():
    assert not allowed(result('наконечник 4 (для кабеля КГ 3х4)',['Кабель КГ 3х4 мм²']))
    requested='наконечник 4 (для кабеля КГ 3х4)'
    queries=_search_variants(requested)
    assert 'наконечник 4' in queries
    assert not any(q.lower().startswith('кабель') for q in queries)
    assert not _hard_conflicts(requested,dict(name='Наконечник кабельный 4 мм²'))


def test_missing_required_parameter_is_not_inferred():
    assert not allowed(result('Бита PH2х50 мм',['Бита PH2 SGS']))


def test_legacy_invalid_price_and_include_are_cleared_together_and_stay_clear():
    data=result('Рулетка 10 м',['Рулетка 5 м'])
    item=data['items'][0]
    item.update(selected=item['candidates'][0],proposed_unit_price=105,decision='approved',operator_included=True)
    assert not selected_allowed(item)
    reconcile(data)
    assert item['selected'] is None and item['proposed_unit_price']==0 and not item['operator_included'] and item['decision']=='excluded'
    reopened=copy.deepcopy(data)
    reconcile(reopened)
    assert reopened==data
    assert model(data)['rows'][0]['invoice']['key'] is None


def test_sent_invoice_is_frozen():
    data=result('Рулетка 10 м',['Рулетка 5 м'])
    data.update(live_offer_created=True)
    data['items'][0].update(selected=data['items'][0]['candidates'][0],proposed_unit_price=105,decision='approved')
    before=copy.deepcopy(data)
    reconcile(data)
    assert data==before


def test_drill_diameter_and_overall_length_are_independent():
    correct = ['Бур Кратон SDS-plus 14x260 1 02 02 032',
               'Квадробур с двойной спиралью по бетону (14x260 мм; SDS+) Elitech',
               'БИБЕР 78143 Бур 14х200/260мм (1шт) (25/50)',
               'Бур-сверло SDS-plus 14x200x260 мм GEPARD']
    wrong = ['Бур 10х200/260мм', 'Бур 14х150/210мм',
             'Сверло спиральное по дереву 14x260 мм']
    assert set(allowed(result('Бур 14*260', correct + wrong))) == set(correct)


def test_parser_warnings_do_not_override_requested_parameter_match():
    data=result('Бур 14*260', ['Бур БИБЕР 14х200/260мм SDS-plus', 'Бур 10х200/260мм'])
    for q in data['items'][0]['candidates']:
        q['characteristic_missing']=['не подтверждена характеристика: Категория требует ручной проверки']
        q['characteristic_conflicts']=['старый разбор размеров не совпал']
    assert allowed(data)==['Бур БИБЕР 14х200/260мм SDS-plus']
    comparison=model(data)['rows'][0]['value_comparison']
    counts={s['field']:s['remaining'] for s in comparison['filter_stages']}
    assert counts['Диаметр бура']==1
    assert counts['Общая длина бура']==1


def test_respirator_with_valve_is_not_a_valve_and_cardboard_is_not_a_pipe():
    from product_identity import kind
    assert kind('Респиратор РУССИЗ 926 FFP2 с клапаном и угольным слоем') == 'респиратор'
    assert kind('Клапан балансировочный Ду 50') == 'клапан'
    assert kind('Двуслойный гофрокартон «С') != 'гофрированная труба'
    assert kind('Труба гофрированная ПНД d32') == 'гофрированная труба'


# Products outside the closed kind list: same main noun, every size/designation token and every
# other word of the request must be present; anything lacking goes to the operator (not comparable yet).
@pytest.mark.parametrize('requested,correct,wrong', [
    ('Монолитный поликарбонат 8 мм прозрачный 2050×3050', 'Поликарбонат монолитный прозрачный 8 мм 2050х3050', 'Поликарбонат монолитный прозрачный 4 мм 2050х3050'),
    ('Монолитный поликарбонат 8 мм прозрачный 2050×3050', 'Поликарбонат монолитный прозрачный 8 мм 2050х3050', 'Поликарбонат сотовый прозрачный 8 мм 2050х3050'),
    ('Лента конв. 2.2-1000-3-ТК-200-5/2 НБ 11', 'Лента конв. 2.2-1000-3-ТК-200-5/2 НБ 11', 'Лента конв. 2.2-800-3-ТК-200-5/2 НБ 11'),
    ('Секция заборная оцинкованная 2D Спорт 4.8/6.0 50×200 2500×2030', 'Секция заборная оцинкованная 2D Спорт 4.8/6.0 50х200 2500х2030', 'Секция заборная оцинкованная 2D Спорт 4.8/6.0 50х200 2500х1530'),
    ('Ограждение сетчатое оцинкованное 3D 4 мм 2500×1530', 'Ограждение сетчатое оцинкованное 3D 4 мм 2500х1530', 'Ограждение сетчатое оцинкованное 3D 4 мм 2500х1230'),
])
def test_kind_outside_the_closed_list_is_verified_by_noun_words_and_numbers(requested, correct, wrong):
    from product_identity import kind
    assert not kind(requested)
    assert allowed(result(requested, [correct, wrong])) == [correct]


def test_kind_outside_the_list_without_any_number_stays_with_the_operator():
    # Nothing to verify by name: even an identical name is held for confirmation, not auto-passed.
    assert not allowed(result('Двуслойный гофрокартон «С', ['Двуслойный гофрокартон «С']))
