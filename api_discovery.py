import io
import re
import zipfile
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

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
    def extract_schema_urls(text,base_url):
        found=[]
        for pattern in [r"\burl\s*:\s*['\"]([^'\"]+)['\"]",r"['\"]([^'\"]*/specs/[^'\"]+\.(?:yaml|yml|json))['\"]"]:
            for m in re.finditer(pattern,text or '',re.I):
                value=m.group(1).strip()
                if not value or value.startswith(('data:','mailto:','http://')):continue
                absolute=urljoin(base_url,value)
                if absolute not in found:found.append(absolute)
        return found
    def substitute_path(path,order_id,item_id):
        replacements={'id':str(order_id),'orderid':str(order_id),'order_id':str(order_id),'order':str(order_id),'requestid':str(order_id),'request_id':str(order_id),'orderitemid':str(item_id) if item_id else None,'order_item_id':str(item_id) if item_id else None,'itemid':str(item_id) if item_id else None,'item_id':str(item_id) if item_id else None}
        unresolved=[]; result=path
        for name in re.findall(r'{([^{}]+)}',path):
            value=replacements.get(name.lower().strip())
            if value is None:unresolved.append(name)
            else:result=result.replace('{'+name+'}',value)
        return result,unresolved
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

    @app.get('/analysis/api-discovery/{order_id}')
    def discover_price_api(order_id:int):
        try:
            order=get_order(order_id); item_ids=[x.get('id') for x in (order.get('orderItems') or []) if x.get('id')]; item_id=item_ids[0] if item_ids else None
            swagger_host='https://swagger.cynteka.ru'; schema_urls=[swagger_host+'/specs/swagger-core.yaml']; official_schemas=[]; all_route_specs={}; server_hints=[]
            for url in schema_urls:
                try:
                    r=requests.get(url,timeout=25); entry={'url':url,'status':r.status_code,'content_type':r.headers.get('content-type','')}
                    if r.ok:
                        schema,fmt,error=parse_schema_response(r); entry['format']=fmt
                        if schema:
                            specs=route_specs(schema); all_route_specs.update(specs)
                            entry.update({'paths_count':len(schema.get('paths') or {}),'interesting_paths':list(specs.keys()),'servers':json_safe(schema.get('servers')),'title':(schema.get('info') or {}).get('title'),'version':str((schema.get('info') or {}).get('version',''))})
                            for s in schema.get('servers') or []:
                                if isinstance(s,dict) and s.get('url'):server_hints.append(str(s['url']))
                        else:entry['parse_error']=error
                    official_schemas.append(entry)
                except Exception as e:official_schemas.append({'url':url,'error':f'{type(e).__name__}: {e}'})

            # The official Swagger exposes exactly the report we need: an XLSX comparison of all invoices for an order.
            report_path=f'/api/v1/orders/{order_id}/offer-compare-report'; report_url=zakupay_base_url.rstrip('/')+report_path
            try:
                rr=requests.get(report_url,headers=zakupay_headers(),timeout=30)
                ct=rr.headers.get('content-type','')
                offer_compare_report={'path':report_path,'status':rr.status_code,'content_type':ct,'size':len(rr.content)}
                if rr.ok and (rr.content[:2]==b'PK' or 'spreadsheet' in ct.lower() or 'excel' in ct.lower()):offer_compare_report['xlsx_preview']=xlsx_preview(rr.content)
                else:offer_compare_report['body']=safe_body(rr,8000)
            except requests.RequestException as e:offer_compare_report={'path':report_path,'error':str(e)}

            # Also test the documented offers filter exactly as Swagger specifies.
            offers_url=zakupay_base_url.rstrip('/')+'/api/v1/offers'
            try:
                ro=requests.get(offers_url,headers=zakupay_headers(),params={'orderId':order_id,'page':1,'pageSize':100,'isoDate':'true'},timeout=25)
                offers_probe={'url':ro.url,'status':ro.status_code,'content_type':ro.headers.get('content-type',''),'body':safe_body(ro,12000)}
            except requests.RequestException as e:offers_probe={'error':str(e)}

            return JSONResponse(json_safe({'order_id':order_id,'first_item_id':item_id,'official_schema_attempts':official_schemas,'official_server_hints':server_hints,'official_interesting_routes':list(all_route_specs.keys()),'offer_compare_report':offer_compare_report,'offers_probe':offers_probe}))
        except Exception as e:return JSONResponse({'order_id':order_id,'discovery_error':f'{type(e).__name__}: {e}'},status_code=200)
