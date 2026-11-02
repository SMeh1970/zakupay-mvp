import copy
import hashlib
import io
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
import automation_pipeline as pipeline
import porcelain as tiles
import porcelain_panel as panel
import tile_submission as dispatch
import test_porcelain as sample
from invoice_generator import validate_seller, AVAILABILITY_CONFIRMATION_NOTE


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, 'DB_PATH', str(tmp_path/'tiles.db'))
    monkeypatch.setattr(pipeline, 'DATABASE_URL', '')
    monkeypatch.setattr(pipeline, '_initialized_database_key', None)
    monkeypatch.delenv('KERAMAHALL_CATALOG_API_KEY', raising=False)
    app = FastAPI()
    pipeline.install_automation_pipeline(app, Mock())
    panel.install_porcelain(app)
    catalog_id=tiles.import_catalog(sample.archive())
    catalog=tiles.load_catalog(catalog_id)
    order=copy.deepcopy(sample.ORDER); order['actualityStatus']='ACTUAL'
    form=dict(product_1='KH-GRAY', area_1='40', reserve_1='0', price_1='1000', approve_1='yes',
              address='Москва, Тестовая, 1', zone='mkad', unloading='0', vehicle='Газель 1,5 т',
              transport_confirmed='yes', seller='avior', seller_confirmed='yes', vat='22', invoice_number='К-1',
              technical_1='yes', technical_source_1='Паспорт поставщика, сверено оператором')
    s=tiles.make_quote(order,catalog,form)
    assert not s['issues']
    key=tiles.save_quote(1,catalog,s)
    q=tiles.load_quote(key)
    with patch.object(panel,'required_proposal_catalog',return_value=catalog):
        g=dict(fetch_order=Mock(return_value=order),has_offer=Mock(return_value=False),
               accounts=Mock(return_value=([dict(id='bank',label='Наш банк',accountNumber=s['seller']['account'],company_inn=s['seller']['inn'])],'')),
               resolve=Mock(return_value=(validate_seller(s['seller']),'bank',.22)),
               upload=Mock(return_value=('17',{})),headers=lambda:{},base='https://example.invalid')
        app.state.tile_gateway=g
        with TestClient(app) as client:
            yield q,catalog,g,client


def post_form(q):
    return dict(confirm_send='SEND',revision=dispatch.fingerprint(q['snapshot']),destination_account_id='bank')


def test_download_invoice_contains_delivery_and_exact_total_and_is_stable(prepared):
    q,_,_,client=prepared
    response=client.get(f"/dashboard/tiles/quotes/{q['id']}/invoice.xlsx")
    assert response.status_code==200
    book=load_workbook(io.BytesIO(response.content),data_only=True)
    values=[cell.value for row in book.active for cell in row]
    assert q['snapshot']['total'] in values
    assert 'Доставка по адресу: Москва, Тестовая, 1' in values
    assert AVAILABILITY_CONFIRMATION_NOTE in values
    assert response.content==client.get(f"/dashboard/tiles/quotes/{q['id']}/invoice.xlsx").content


def test_send_requires_confirmation_and_exact_version_before_any_reads_or_upload(prepared):
    q,_,g,client=prepared; url=f"/dashboard/tiles/quotes/{q['id']}/send"
    for form in ({},dict(confirm_send='SEND',revision='different')):
        assert client.post(url,data=form).status_code==400
    g['fetch_order'].assert_not_called(); g['upload'].assert_not_called()


def test_confirmed_send_uploads_downloaded_bytes_and_one_offer_only(prepared):
    q,_,g,client=prepared;url=f"/dashboard/tiles/quotes/{q['id']}/send"
    downloaded=client.get(f"/dashboard/tiles/quotes/{q['id']}/invoice.xlsx").content
    response=Mock(ok=True,status_code=200);response.json.return_value=dict(result=True,offerId=99)
    with patch.object(dispatch.requests,'post',return_value=response) as post:
        result=client.post(url,data=post_form(q))
        assert result.status_code==200
        assert 'confirmed' in result.text
        assert g['upload'].call_args.args[1]==downloaded
        payload=post.call_args.kwargs['json']
        assert sum(row['amount'] for row in payload['offerItems'])==q['snapshot']['total']
        assert payload['offerItems'][-1]['unit']['name']=='услуга'
        assert client.post(url,data=post_form(q)).status_code==409
        assert post.call_count==1
    assert dispatch.dispatch_state(q['snapshot']['order_id'])['invoice_hash']==hashlib.sha256(downloaded).hexdigest()


def test_unknown_response_blocks_other_version_and_never_retries(prepared):
    q,catalog,g,client=prepared
    with patch.object(dispatch.requests,'post',side_effect=dispatch.requests.Timeout):
        assert client.post(f"/dashboard/tiles/quotes/{q['id']}/send",data=post_form(q)).status_code==200
    assert dispatch.dispatch_state(q['snapshot']['order_id'])['state']=='unknown'
    other=copy.deepcopy(q);other['id']=tiles.save_quote(1,catalog,q['snapshot'])
    assert client.post(f"/dashboard/tiles/quotes/{other['id']}/send",data=post_form(other)).status_code==409
    assert g['upload'].call_count==1


@pytest.mark.parametrize('change',[dict(actualityStatus='CLOSED'),dict(deliveryAddress='Другой адрес'),dict(customer=dict(name='Другая фирма',inn='5017134957',kpp='501701001'))])
def test_changed_or_closed_order_never_uploads(prepared,change):
    q,_,g,client=prepared
    g['fetch_order'].return_value={**q['snapshot']['order_snapshot'],**change}
    response=client.post(f"/dashboard/tiles/quotes/{q['id']}/send",data=post_form(q))
    assert response.status_code==409
    g['upload'].assert_not_called()


def test_catalog_or_missing_technical_evidence_blocks(prepared):
    q,catalog,_,_=prepared
    changed=copy.deepcopy(catalog);changed['data']['products'][0]['stock_m2']=0
    assert dispatch.validate_quote(q,q['snapshot']['order_snapshot'],changed)
    s=q['snapshot'];s['inputs'].pop('technical_source_1');s['order_snapshot']['orderItems'][0]['comment']='R10 ГОСТ'
    assert any('технические' in e for e in dispatch.validate_quote(q,s['order_snapshot'],catalog))


def test_plinth_is_not_ordinary_tile_even_when_measured_in_square_metres():
    order=copy.deepcopy(sample.ORDER)
    order['orderItems'][0]['goodName']='Плинтус керамогранитный 600х95х9 мм'
    assert tiles.automatic_selection(dict(products=[sample.PRODUCT]),order)=={}
    assert tiles.tile_kind('Ступени керамогранитные')=='accessory'


def test_bank_from_other_company_and_cross_origin_never_upload(prepared):
    q,_,g,client=prepared;url=f"/dashboard/tiles/quotes/{q['id']}/send"
    assert client.post(url,data=post_form(q),headers={'Origin':'https://foreign.invalid'}).status_code==403
    g['accounts'].return_value=([dict(id='bank',accountNumber=q['snapshot']['seller']['account'],company_inn='foreign')],'')
    assert client.post(url,data=post_form(q)).status_code==409
    g['upload'].assert_not_called()


def test_price_check_does_not_turn_corridor_into_exact_price(prepared):
    q,_,g,client=prepared
    with patch.object(panel,'order_for_job',return_value=q['snapshot']['order_snapshot']), patch('browser_prices.fetch_best_prices',return_value={'ok':True,'prices':{'101':{'orderItemId':101,'offerDeviation':5}},'errors':[]}):
        response=client.get('/dashboard/automation/jobs/1/tiles/prices')
    assert response.status_code==200
    assert 'Точная цена недоступна' in response.text
    g['upload'].assert_not_called()


def test_plinth_page_explains_failure_without_empty_prepare_button(prepared):
    q,_,_,client=prepared
    order=copy.deepcopy(q['snapshot']['order_snapshot'])
    order['orderItems'][0].update(goodName='Плинтус керамогранитный 600х95х9 мм',unit={'name':'м.п.'})
    with patch.object(panel,'order_for_job',return_value=order):
        response=client.get('/dashboard/automation/jobs/1/tiles')
    assert response.status_code==200
    assert 'фасонное изделие' in response.text
    assert 'Рассчитать предложение по выбранным товарам</button>' not in response.text
    assert "name='area_1'" not in response.text
