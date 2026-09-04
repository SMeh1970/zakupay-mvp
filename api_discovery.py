import io
import re
import zipfile
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import requests
import yaml
from fastapi import HTTPException
from fastapi.responses import JSONResponse

PRICE_WORDS = ('offer','price','corridor','orderitem','order-item','invoice','producer','proposal','bestoffer','bestprice','счет','счёт','предлож','цен','коридор')

def install_api_discovery(app, fetch_all_orders, zakupay_headers, zakupay_base_url):
    def get_order(order_id:int):
        orders=fetch_all_orders(); order=next((x for x in orders if x.get('id')==order_id),None)
        if not order:
            orders=fetch_all_orders(force=True); order=next((x for x in orders if x.get('id')==order_id),None)
        if not order: raise HTTPException(status_code=404,detail='Заявка не найдена среди актуальных')
        return order
    def decode_text(r):
        try:return r.content.decode('utf-8')
        except UnicodeDecodeError:return r.content.decode(r.apparent_encoding or 'utf-8',errors='replace')
    def json_safe(v,d=0):
        if d>20:return str(v)
        if v is None or isinstance(v,(str,int,float,bool)):return v
        if isinstance(v,dict):return {str(k):json_safe(x,d+1) for k,x in v.items()}
        if isinstance(v,(list,tuple,set)):return [json_safe(x,d+1) for x in v]
        return str(v)
    def safe_body(r,max_text=4000):
        try:return json_safe(r.json())
        except ValueError:return decode_text(r)[:max_text]
    def parse_schema_response(r):
        text=decode_text(r)
        try:
            b=r.json()
            if isinstance(b,dict):return b,'json',None
        except ValueError:pass
        try:
            b=yaml.safe_load(text)
            if isinstance(b,dict):return b,'yaml',None
        except yaml.YAMLError as e:return None,None,str(e)
        return None,None,'response is neither JSON nor YAML OpenAPI object'
    def is_interesting(path,methods):
        blob=(path+' '+str(methods)).lower(); return any(k in blob for k in PRICE_WORDS)
    def route_specs(schema):
        found={}
        if not isinstance(schema,dict):return found
        for path,methods in (schema.get('paths') or {}).items():
            if not isinstance(methods,dict) or not is_interesting(path,methods):continue
            found[path]={}
            for method,spec in methods.items():
                if str(method).lower() not in {'get','post','put','patch','delete'}:continue
                spec=spec if isinstance(spec,dict) else {}; params=[]
                for p in spec.get('parameters') or []:
                    if isinstance(p,dict):params.append({'name':p.get('name'),'in':p.get('in'),'required':p.get('required'),'schema':json_safe(p.get('schema')),'description':p.get('description')})
                found[path][str(method).lower()]={'summary':spec.get('summary'),'description':spec.get('description'),'operationId':spec.get('operationId'),'parameters':params,'requestBody':json_safe(spec.get('requestBody')),'responses':json_safe(spec.get('responses'))}
        return found
    def xlsx_preview(content,max_rows=80):
        try:
            z=zipfile.ZipFile(io.BytesIO(content)); shared=[]
            if 'xl/sharedStrings.xml' in z.namelist():
                root=ET.fromstring(z.read('xl/sharedStrings.xml'))
                shared=[''.join(t.text or '' for t in si.iter() if t.tag.endswith('}t')) for si in root]
            sheet=next((n for n in z.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml')),None)
            if not sheet:return {'error':'xlsx has no worksheet'}
            root=ET.fromstring(z.read(sheet)); rows=[]
            for row in root.iter():
                if not row.tag.endswith('}row'):continue
                vals=[]
                for c in row:
                    if not c.tag.endswith('}c'):continue
                    typ=c.attrib.get('t'); v=next((x for x in c if x.tag.endswith('}v')),None)
                    value='' if v is None else (v.text or '')
                    if typ=='s' and value.isdigit() and int(value)<len(shared):value=shared[int(value)]
                    vals.append(value)
                if any(str(x).strip() for x in vals):rows.append(vals)
                if len(rows)>=max_rows:break
            return {'rows':rows}
        except Exception as e:return {'error':f'{type(e).__name__}: {e}','size':len(content)}
    def base_variants(server_hints):
        vals=[]
        def add(x):
            if x and x.rstrip('/') not in vals: vals.append(x.rstrip('/'))
        add(zakupay_base_url)
        for hint in server_hints:
            add(hint)
            p=urlparse(hint)
            if p.scheme and p.netloc:add(f'{p.scheme}://{p.netloc}')
        add('https://prodavay.sel-be.ru')
        return vals
    def endpoint_urls(base,path):
        out=[]
        def add(u):
            if u not in out:out.append(u)
        add(base.rstrip('/')+'/'+path.lstrip('/'))
        if base.rstrip('/').endswith('/api/v1') and path.startswith('/api/v1/'):
            add(base.rstrip('/')+'/'+path[len('/api/v1/'):])
        return out
    def probe_get(url,params=None,timeout=30,max_text=12000):
        try:
            r=requests.get(url,headers=zakupay_headers(),params=params or None,timeout=timeout)
            data={'url':r.url,'status':r.status_code,'content_type':r.headers.get('content-type',''),'size':len(r.content)}
            if r.ok and (r.content[:2]==b'PK' or 'spreadsheet' in data['content_type'].lower() or 'excel' in data['content_type'].lower()):data['xlsx_preview']=xlsx_preview(r.content)
            else:data['body']=safe_body(r,max_text)
            return data
        except requests.RequestException as e:return {'url':url,'error':str(e)}
    def probe_best_prices(item_ids):
        url='https://prodavay.sel-be.ru/core/supplier/getoffersdeviationpercent'
        headers=dict(zakupay_headers())
        headers.update({'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest','Accept':'application/json, text/javascript, */*; q=0.01'})
        try:
            r=requests.post(url,headers=headers,json=item_ids,timeout=25)
            data={'url':url,'status':r.status_code,'content_type':r.headers.get('content-type',''),'size':len(r.content),'request_item_ids':item_ids}
            body=safe_body(r,20000)
            data['body']=body
            if r.ok and isinstance(body,list):
                data['price_map']={str(x.get('orderItemId')):((x.get('bestPrice') or {}).get('price')) for x in body if isinstance(x,dict) and x.get('orderItemId') is not None}
            return data
        except requests.RequestException as e:return {'url':url,'request_item_ids':item_ids,'error':str(e)}

    @app.get('/analysis/api-discovery/{order_id}')
    def discover_price_api(order_id:int):
        try:
            order=get_order(order_id); item_ids=[x.get('id') for x in (order.get('orderItems') or []) if x.get('id')]; item_id=item_ids[0] if item_ids else None
            schema_url='https://swagger.cynteka.ru/specs/swagger-core.yaml'; official_schemas=[]; all_route_specs={}; server_hints=[]
            try:
                r=requests.get(schema_url,timeout=25); entry={'url':schema_url,'status':r.status_code,'content_type':r.headers.get('content-type','')}
                if r.ok:
                    schema,fmt,error=parse_schema_response(r); entry['format']=fmt
                    if schema:
                        specs=route_specs(schema); all_route_specs.update(specs)
                        entry.update({'paths_count':len(schema.get('paths') or {}),'interesting_paths':list(specs.keys()),'servers':json_safe(schema.get('servers')),'title':(schema.get('info') or {}).get('title'),'version':str((schema.get('info') or {}).get('version',''))})
                        for s in schema.get('servers') or []:
                            if isinstance(s,dict) and s.get('url'):server_hints.append(str(s['url']))
                    else:entry['parse_error']=error
                official_schemas.append(entry)
            except Exception as e:official_schemas.append({'url':schema_url,'error':f'{type(e).__name__}: {e}'})

            # Exact production endpoint observed in Chrome DevTools. The browser POSTs an array of order item IDs
            # and receives bestPrice/secondPrice/deliveryPositionPrice per orderItemId.
            browser_price_probe=probe_best_prices(item_ids)

            bases=base_variants(server_hints)
            report_path=f'/api/v1/orders/{order_id}/offer-compare-report'; report_probes=[]
            for base in bases:
                for url in endpoint_urls(base,report_path):report_probes.append(probe_get(url,timeout=30,max_text=8000))
            offers_probes=[]; offers_path='/api/v1/offers'; params={'orderId':order_id,'page':1,'pageSize':100,'isoDate':'true'}
            for base in bases:
                for url in endpoint_urls(base,offers_path):offers_probes.append(probe_get(url,params=params,timeout=25,max_text=12000))
            control_order_probes=[]
            for base in bases:
                for url in endpoint_urls(base,'/api/v1/orders'):control_order_probes.append(probe_get(url,params={'page':1,'pageSize':1,'isoDate':'true'},timeout=20,max_text=3000))

            return JSONResponse(json_safe({'order_id':order_id,'first_item_id':item_id,'item_ids':item_ids,'browser_discovered_best_price_endpoint':browser_price_probe,
                'official_schema_attempts':official_schemas,'official_server_hints':server_hints,'official_interesting_routes':list(all_route_specs.keys()),
                'base_variants':bases,'control_order_probes':control_order_probes,'offer_compare_report_probes':report_probes,'offers_probes':offers_probes}))
        except Exception as e:return JSONResponse({'order_id':order_id,'discovery_error':f'{type(e).__name__}: {e}'},status_code=200)
