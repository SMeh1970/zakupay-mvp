import re
from urllib.parse import urljoin

import requests
import yaml
from fastapi import HTTPException
from fastapi.responses import JSONResponse


PRICE_WORDS = (
    'offer', 'price', 'corridor', 'orderitem', 'order-item',
    'invoice', 'producer', 'proposal', 'bestoffer', 'bestprice',
    'счет', 'счёт', 'предлож', 'цен', 'коридор',
)


def install_api_discovery(app, fetch_all_orders, zakupay_headers, zakupay_base_url):
    def get_order(order_id: int):
        orders = fetch_all_orders()
        order = next((x for x in orders if x.get('id') == order_id), None)
        if not order:
            orders = fetch_all_orders(force=True)
            order = next((x for x in orders if x.get('id') == order_id), None)
        if not order:
            raise HTTPException(status_code=404, detail='Заявка не найдена среди актуальных')
        return order

    def decode_text(r):
        # Cynteka serves YAML as text/plain and may omit charset.
        raw = r.content
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError:
            return raw.decode(r.apparent_encoding or 'utf-8', errors='replace')

    def safe_body(r, max_text=4000):
        try:
            return r.json()
        except ValueError:
            return decode_text(r)[:max_text]

    def parse_schema_response(r):
        text = decode_text(r)
        try:
            body = r.json()
            if isinstance(body, dict):
                return body, 'json', None
        except ValueError:
            pass
        try:
            body = yaml.safe_load(text)
            if isinstance(body, dict):
                return body, 'yaml', None
        except yaml.YAMLError as exc:
            return None, None, str(exc)
        return None, None, 'response is neither JSON nor YAML OpenAPI object'

    def is_interesting(path, methods):
        blob = (path + ' ' + str(methods)).lower()
        return any(k in blob for k in PRICE_WORDS)

    def route_specs(schema):
        found = {}
        if not isinstance(schema, dict):
            return found
        for path, methods in (schema.get('paths') or {}).items():
            if not isinstance(methods, dict) or not is_interesting(path, methods):
                continue
            found[path] = {}
            for method, spec in methods.items():
                if str(method).lower() not in {'get', 'post', 'put', 'patch', 'delete'}:
                    continue
                spec = spec if isinstance(spec, dict) else {}
                params = []
                for p in spec.get('parameters') or []:
                    if not isinstance(p, dict):
                        continue
                    params.append({
                        'name': p.get('name'),
                        'in': p.get('in'),
                        'required': p.get('required'),
                        'schema': p.get('schema'),
                        'description': p.get('description'),
                    })
                found[path][str(method).lower()] = {
                    'summary': spec.get('summary'),
                    'description': spec.get('description'),
                    'operationId': spec.get('operationId'),
                    'parameters': params,
                    'requestBody': spec.get('requestBody'),
                    'responses': spec.get('responses'),
                }
        return found

    def extract_schema_urls(text, base_url):
        found = []
        patterns = [
            r"\burl\s*:\s*['\"]([^'\"]+)['\"]",
            r"\burl\s*=\s*['\"]([^'\"]+)['\"]",
            r"['\"]([^'\"]*(?:swagger|openapi)[^'\"]*\.(?:json|yaml|yml)[^'\"]*)['\"]",
            r"['\"]([^'\"]*/specs/[^'\"]+\.(?:yaml|yml|json))['\"]",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text or '', re.I):
                value = match.group(1).strip()
                if not value or value.startswith('data:'):
                    continue
                absolute = urljoin(base_url, value)
                if absolute not in found:
                    found.append(absolute)
        return found

    def substitute_path(path, order_id, item_id):
        replacements = {
            'orderid': str(order_id), 'order_id': str(order_id), 'order': str(order_id),
            'requestid': str(order_id), 'request_id': str(order_id),
            'orderitemid': str(item_id) if item_id else None,
            'order_item_id': str(item_id) if item_id else None,
            'itemid': str(item_id) if item_id else None,
            'item_id': str(item_id) if item_id else None,
        }
        unresolved = []
        result = path
        for name in re.findall(r'{([^{}]+)}', path):
            key = name.lower().strip()
            value = replacements.get(key)
            if value is None:
                unresolved.append(name)
            else:
                result = result.replace('{' + name + '}', value)
        return result, unresolved

    @app.get('/analysis/api-discovery/{order_id}')
    def discover_price_api(order_id: int):
        order = get_order(order_id)
        item_ids = [x.get('id') for x in (order.get('orderItems') or []) if x.get('id')]
        item_id = item_ids[0] if item_ids else None

        swagger_host = 'https://swagger.cynteka.ru'
        swagger_root = []
        asset_urls = []
        schema_urls = []

        try:
            r = requests.get(swagger_host + '/', timeout=20)
            root_html = decode_text(r) if r.ok else ''
            swagger_root.append({'url': swagger_host + '/', 'status': r.status_code, 'content_type': r.headers.get('content-type', ''), 'preview': root_html[:2500]})
            schema_urls.extend(extract_schema_urls(root_html, swagger_host + '/'))
            for m in re.finditer(r"<script[^>]+src=['\"]([^'\"]+)['\"]", root_html, re.I):
                asset = urljoin(swagger_host + '/', m.group(1))
                if asset not in asset_urls:
                    asset_urls.append(asset)
        except requests.RequestException as exc:
            swagger_root.append({'url': swagger_host + '/', 'error': str(exc)})

        swagger_assets = []
        for asset in asset_urls:
            try:
                r = requests.get(asset, timeout=20)
                text = decode_text(r)
                urls = extract_schema_urls(text, asset) if r.ok else []
                schema_urls.extend(urls)
                swagger_assets.append({'url': asset, 'status': r.status_code, 'content_type': r.headers.get('content-type', ''), 'discovered_schema_urls': urls, 'preview': text[:3000]})
            except requests.RequestException as exc:
                swagger_assets.append({'url': asset, 'error': str(exc)})

        # Known URLs discovered from Cynteka's own Swagger initializer.
        for url in [
            swagger_host + '/specs/swagger-core.yaml',
            swagger_host + '/specs/swagger-edi.yaml',
        ]:
            if url not in schema_urls:
                schema_urls.append(url)

        official_schemas = []
        all_route_specs = {}
        server_hints = []
        for url in list(dict.fromkeys(schema_urls)):
            try:
                r = requests.get(url, timeout=25)
                entry = {'url': url, 'status': r.status_code, 'content_type': r.headers.get('content-type', '')}
                if r.ok:
                    schema, fmt, error = parse_schema_response(r)
                    entry['format'] = fmt
                    if schema:
                        paths = schema.get('paths') or {}
                        specs = route_specs(schema)
                        entry['paths_count'] = len(paths)
                        entry['interesting_paths'] = list(specs.keys())
                        entry['servers'] = schema.get('servers')
                        entry['title'] = (schema.get('info') or {}).get('title')
                        entry['version'] = (schema.get('info') or {}).get('version')
                        all_route_specs.update(specs)
                        for s in schema.get('servers') or []:
                            if isinstance(s, dict) and s.get('url'):
                                server_hints.append(s.get('url'))
                    else:
                        entry['parse_error'] = error
                        entry['preview'] = decode_text(r)[:5000]
                else:
                    entry['preview'] = decode_text(r)[:1500]
                official_schemas.append(entry)
            except requests.RequestException as exc:
                official_schemas.append({'url': url, 'error': str(exc)})

        # Probe only GET routes explicitly documented in the official schema.
        documented_get_probes = []
        for path, methods in all_route_specs.items():
            if 'get' not in methods:
                continue
            resolved, unresolved = substitute_path(path, order_id, item_id)
            if unresolved:
                documented_get_probes.append({'path': path, 'method': 'get', 'skipped': 'unresolved path parameters', 'unresolved': unresolved})
                continue
            # Swagger paths may already contain /api/v1; supplier base URL is the actual host.
            url = zakupay_base_url.rstrip('/') + '/' + resolved.lstrip('/')
            try:
                r = requests.get(url, headers=zakupay_headers(), timeout=20)
                documented_get_probes.append({'path': path, 'resolved_path': resolved, 'status': r.status_code, 'body': safe_body(r, 8000)})
            except requests.RequestException as exc:
                documented_get_probes.append({'path': path, 'resolved_path': resolved, 'error': str(exc)})

        return JSONResponse({
            'order_id': order_id,
            'first_item_id': item_id,
            'official_swagger_host': swagger_host,
            'swagger_root': swagger_root,
            'swagger_assets': swagger_assets,
            'discovered_schema_urls': list(dict.fromkeys(schema_urls)),
            'official_schema_attempts': official_schemas,
            'official_server_hints': list(dict.fromkeys(server_hints)),
            'official_interesting_routes': list(all_route_specs.keys()),
            'official_interesting_route_specs': all_route_specs,
            'documented_get_probes': documented_get_probes,
        })
