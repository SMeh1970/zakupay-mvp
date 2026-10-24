import copy
import gc
import json
from contextlib import closing
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
import automation_pipeline as p
import sniper_integration as sniper
import security


@pytest.fixture
def panel(tmp_path):
    with patch.multiple(p, DB_PATH=str(tmp_path/'jobs.db'), DATABASE_URL='', _initialized_database_key=None), patch.object(security,'_valid_session',return_value=True):
        app=FastAPI()
        app.dependency_overrides[sniper.require_session] = lambda: None
        p.install_automation_pipeline(app, lambda *_: None)
        sniper.install_sniper(app)
        order={'id':123,'name':'Заявка','orderItems':[{'goodName':'Болт','count':2,'unit':{'name':'шт'}},{'goodName':'Гайка','count':4,'unit':{'name':'шт'}}]}
        result={'order_id':123,'live_offer_created':True,'live_offer_id':'sent-1','items':[
            {'position':i,'requested_name':r['goodName'],'quantity':r['count'],'unit':'шт','candidates':[],'decision':'manual'} for i,r in enumerate(order['orderItems'],1)]}
        raw=json.dumps(result)
        with closing(p._connect()) as conn, conn:
            cur=conn.execute("INSERT INTO automation_jobs(dedupe_key,order_id,event_type,status,subject,result_json,order_json,invoice_number,created_at,updated_at) VALUES('sent',123,'manual_order','offer_created','Заявка',?,?,286,'now','now')",(raw,json.dumps(order)))
            job=cur.lastrowid
        with TestClient(app) as client:
            yield client,job,raw
        gc.collect()


def test_sent_revision_preserves_snapshot_and_is_reused(panel):
    _,job,raw=panel
    draft=p.editable_revision(job)
    assert draft!=job and p.editable_revision(job)==draft
    with closing(p._connect()) as conn:
        old=conn.execute('SELECT * FROM automation_jobs WHERE id=?',(job,)).fetchone()
        new=conn.execute('SELECT * FROM automation_jobs WHERE id=?',(draft,)).fetchone()
    assert old['result_json']==raw and old['status']=='offer_created'
    result=json.loads(new['result_json'])
    assert not result['live_offer_created'] and result['previous_offer']['job_id']==job
    assert new['invoice_number']>286 and len(result['items'])==2


def test_sent_page_keeps_search_and_edit_controls(panel):
    client,job,_=panel
    page=client.get(f'/dashboard/automation/jobs/{job}/review')
    assert page.status_code==200
    assert 'Искать другие варианты' in page.text
    assert 'Искать выбранные через Sniper' in page.text
    assert "name='selected_position' value='2'" in page.text
    assert "class='candidate-select'" in page.text
    assert 'По предыдущему варианту выставлен счёт' in page.text


def test_sniper_receives_only_selected_without_launch(panel):
    client,job,_=panel
    response=client.post(f'/dashboard/automation/jobs/{job}/search-selected',data={'engine':'sniper','selected_position':'2'},follow_redirects=False)
    assert response.status_code==303 and 'positions=2' in response.headers['location']
    with patch.object(sniper.SniperClient,'call',side_effect=AssertionError('Must not launch')):
        page=client.get(response.headers['location'])
    assert page.status_code==200
    assert 'Гайка; 4; шт' in page.text and 'Болт; 2; шт' not in page.text


def test_empty_selection_is_rejected_without_fork(panel):
    client,job,_=panel
    response=client.post(f'/dashboard/automation/jobs/{job}/search-selected',data={'engine':'standard'})
    assert response.status_code==400
    with closing(p._connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM automation_jobs').fetchone()[0]==1


def test_revision_link_survives_search():
    old={'previous_offer':{'job_id':7,'offer_id':'sent'}}
    new={}
    p._preserve_invoice_customer(new,old)
    assert new['previous_offer']==old['previous_offer']
    new['previous_offer']['job_id']=8
    assert old['previous_offer']['job_id']==7


def test_sniper_all_positions_are_partitioned_without_loss(panel):
    client,job,_=panel
    order={'id':123,'orderItems':[{'goodName':f'Товар-{i}','count':1,'unit':'шт'} for i in range(1,42)]}
    with closing(p._connect()) as conn,conn:
        conn.execute('UPDATE automation_jobs SET order_json=? WHERE id=?',(json.dumps(order),job))
    for batch,size in [(1,20),(2,20),(3,1)]:
        page=client.get(f'/dashboard/sniper?job_id={job}&batch={batch}')
        assert page.status_code==200
        import re
        goods=re.search(r"name='goods'[^>]*>(.*?)</textarea>",page.text,re.S).group(1)
        assert len(goods.splitlines())==size
        assert f'Товар-{(batch-1)*20+1};' in goods


def test_clamp_range_is_not_an_article_and_original_query_is_first():
    from vi_order_match import _identifiers
    query='Хомут сантехнический прорезиненный 2" (2 дюйма) 56-64'
    assert '5664' not in _identifiers(query)
    assert p._search_variants(query)[0]==query
    assert '378665965' in _identifiers('Хомут ЗУБР 37866-59-65')
    assert '5664' in _identifiers('Артикул 56-64')
    conflicts=p._hard_conflicts(query,{'name':'Сантехнический хомут для труб с гайкой Gigant 2", 59-66, М8 G/1/82'})
    assert 'не совпадает модель/артикул' not in conflicts


def test_selected_standard_search_preserves_other_rows_and_sent_snapshot(panel):
    client,job,raw=panel
    calls=[]
    def build(order, **kwargs):
        calls.append(order)
        rows=[dict(position=r['_source_position'],requested_name=r['goodName'],quantity=r['count'],unit='шт',
            candidates=[],decision='manual',searched=True) for r in order['orderItems']]
        return dict(order_id=123,items=rows,summary={'auto_ready':0},status='needs_review')
    class Immediate:
        def __init__(self,target,**kwargs): self.target=target
        def start(self): self.target()
    with patch.object(p,'build_vi_draft',side_effect=build),patch.object(p.threading,'Thread',Immediate):
        response=client.post(f'/dashboard/automation/jobs/{job}/search-selected',data={'engine':'standard','selected_position':'2'},follow_redirects=False)
    assert response.status_code==303
    assert [r['goodName'] for r in calls[0]['orderItems']]==['Гайка']
    draft=p.editable_revision(job)
    with closing(p._connect()) as conn:
        result=json.loads(conn.execute('SELECT result_json FROM automation_jobs WHERE id=?',(draft,)).fetchone()[0])
        assert conn.execute('SELECT result_json FROM automation_jobs WHERE id=?',(job,)).fetchone()[0]==raw
    assert [r['position'] for r in result['items']]==[1,2]
    assert result['items'][1]['searched'] and not result['items'][0].get('searched')
    assert result['previous_offer']['job_id']==job


def test_single_search_after_send_preserves_original(panel):
    client,job,raw=panel
    with patch.object(p,'_build_match_row',return_value=dict(position=2,requested_name='Гайка',quantity=4,unit='шт',candidates=[],decision='manual')):
        response=client.post(f'/dashboard/automation/jobs/{job}/items/2/refresh',follow_redirects=False)
    assert response.status_code==303
    draft=p.editable_revision(job)
    assert f'/{draft}/review' in response.headers['location']
    with closing(p._connect()) as conn:
        assert conn.execute('SELECT result_json FROM automation_jobs WHERE id=?',(job,)).fetchone()[0]==raw
