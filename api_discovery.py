import re
import requests
from urllib.parse import urljoin
from fastapi import HTTPException
from fastapi.responses import JSONResponse


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

    def safe_body(r, max_text=3000):
        try:
            return r.json()
        except ValueError:
            return r.text[:max_text]

    def interesting_paths(schema):
        if not isinstance(schema, dict):
            return []
        result = []
        for path, methods in (schema.get('paths') or {}).items():
            blob = (path + ' ' + str(methods)).lower()
            if any(k in blob for k in (
                'offer', 'price', 'corridor', 'orderitem', 'order-item',
                'invoice', 'producer', 'proposal', 'bestoffer', 'bestprice'
            )):
                result.append(path)
        return sorted(set(result))

    def extract_schema_urls(text, base_url):
        found = []
        patterns = [
            r"\burl\s*:\s*['\"]([^'\"]+)['\"]",
            r"\burl\s*=\s*['\"]([^'\"]+)['\"]",
            r"['\"]([^'\"]*(?:swagger|openapi)[^'\"]*\.(?:json|yaml|yml)[^'\"]*)['\"]",
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

    @app.get('/analysis/api-discovery/{order_id}')
    def discover_price_api(order_id: int):
        order = get_order(order_id)
        item_ids = [x.get('id') for x in (order.get('orderItems') or []) if x.get('id')]

        swagger_host = 'https://swagger.cynteka.ru'
        swagger_pages = []
        discovered_schema_urls = []
        asset_urls = []

        # 1) Read the actual Swagger UI HTML and all local JS assets that may contain
        # the real OpenAPI URL. The stock Swagger UI keeps it in swagger-initializer.js.
        try:
            r = requests.get(swagger_host + '/', timeout=20)
            root_html = r.text if r.ok else ''
            swagger_pages.append({
                'url': swagger_host + '/', 'status': r.status_code,
                'content_type': r.headers.get('content-type', ''), 'preview': r.text[:2500]
            })
            if root_html:
                discovered_schema_urls.extend(extract_schema_urls(root_html, swagger_host + '/'))
                for m in re.finditer(r"<script[^>]+src=['\"]([^'\"]+)['\"]", root_html, re.I):
                    asset = urljoin(swagger_host + '/', m.group(1))
                    if asset not in asset_urls:
                        asset_urls.append(asset)
        except requests.RequestException as exc:
            swagger_pages.append({'url': swagger_host + '/', 'error': str(exc)})

        swagger_assets = []
        for asset in asset_urls:
            try:
                ar = requests.get(asset, timeout=20)
                entry = {
                    'url': asset, 'status': ar.status_code,
                    'content_type': ar.headers.get('content-type', ''),
                    'preview': ar.text[:3500],
                }
                if ar.ok:
                    urls = extract_schema_urls(ar.text, asset)
                    entry['discovered_schema_urls'] = urls
                    discovered_schema_urls.extend(urls)
                swagger_assets.append(entry)
            except requests.RequestException as exc:
                swagger_assets.append({'url': asset, 'error': str(exc)})

        # Add common locations only as fallback after inspecting the initializer.
        common_schema_urls = [urljoin(swagger_host + '/', x) for x in [
            'swagger/v1/swagger.json', 'swagger.json', 'openapi.json',
            'api/swagger.json', 'api/v1/swagger.json', 'v1/swagger.json',
            'swagger/v1/openapi.json', 'docs/swagger.json', 'api-docs',
        ]]
        schema_urls = []
        for url in discovered_schema_urls + common_schema_urls:
            if url not in schema_urls:
                schema_urls.append(url)

        official_schemas = []
        official_routes = []
        official_schema_paths = {}
        for url in schema_urls:
            try:
                r = requests.get(url, timeout=20)
                entry = {'url': url, 'status': r.status_code, 'content_type': r.headers.get('content-type', '')}
                if r.ok:
                    body = safe_body(r, 8000)
                    if isinstance(body, dict):
                        paths = list((body.get('paths') or {}).keys())
                        entry['paths_count'] = len(paths)
                        entry['interesting_paths'] = interesting_paths(body)[:500]
                        entry['servers'] = body.get('servers')
                        entry['host'] = body.get('host')
                        entry['basePath'] = body.get('basePath')
                        official_routes.extend(entry['interesting_paths'])
                        for p in entry['interesting_paths']:
                            official_schema_paths[p] = (body.get('paths') or {}).get(p)
                    else:
                        entry['preview'] = str(body)[:3000]
                else:
                    entry['preview'] = r.text[:1000]
                official_schemas.append(entry)
            except requests.RequestException as exc:
                official_schemas.append({'url': url, 'error': str(exc)})

        # 2) Inspect any docs exposed by the actual supplier portal host.
        portal_docs_paths = [
            '/swagger/v1/swagger.json', '/swagger.json', '/openapi.json',
            '/api/swagger.json', '/api/v1/swagger.json', '/swagger/index.html',
        ]
        portal_docs = []
        portal_routes = []
        for path in portal_docs_paths:
            url = f"{zakupay_base_url}{path}"
            try:
                r = requests.get(url, headers=zakupay_headers(), timeout=15)
                ct = r.headers.get('content-type', '')
                entry = {'path': path, 'status': r.status_code, 'content_type': ct}
                if r.ok and 'json' in ct:
                    body = safe_body(r)
                    entry['interesting_paths'] = interesting_paths(body)[:500]
                    portal_routes.extend(entry['interesting_paths'])
                else:
                    entry['preview'] = r.text[:800]
                portal_docs.append(entry)
            except requests.RequestException as exc:
                portal_docs.append({'path': path, 'error': str(exc)})

        # 3) Read-only probes. Guessed routes remain only as fallback; schema routes
        # are exposed separately so we can stop guessing once the initializer reveals them.
        candidate_paths = [
            f'/api/v1/orders/{order_id}',
            f'/api/v1/orders/{order_id}/offers',
            f'/api/v1/order/{order_id}/offers',
            f'/api/v1/offers/order/{order_id}',
            f'/api/v1/order-offers/{order_id}',
            f'/api/v1/producer-offers?orderId={order_id}',
            f'/api/v1/producerOffers?orderId={order_id}',
            f'/api/v1/prices?orderId={order_id}',
            f'/api/v1/price-corridor?orderId={order_id}',
        ]
        if item_ids:
            iid = item_ids[0]
            candidate_paths.extend([
                f'/api/v1/order-items/{iid}/offers',
                f'/api/v1/orderItems/{iid}/offers',
                f'/api/v1/prices?orderItemId={iid}',
                f'/api/v1/price-corridor?orderItemId={iid}',
            ])

        route_pool = candidate_paths + official_routes + portal_routes
        probes = []
        seen = set()
        for path in route_pool:
            if not isinstance(path, str) or path in seen:
                continue
            seen.add(path)
            if '{' in path or '}' in path:
                probes.append({'path': path, 'skipped': 'templated route from schema'})
                continue
            url = f"{zakupay_base_url}{path}"
            try:
                r = requests.get(url, headers=zakupay_headers(), timeout=15)
                probes.append({'path': path, 'status': r.status_code, 'body': safe_body(r, 4000)})
            except requests.RequestException as exc:
                probes.append({'path': path, 'error': str(exc)})

        return JSONResponse({
            'order_id': order_id,
            'first_item_id': item_ids[0] if item_ids else None,
            'official_swagger_host': swagger_host,
            'swagger_root': swagger_pages,
            'swagger_assets': swagger_assets,
            'discovered_schema_urls': schema_urls,
            'official_schema_attempts': official_schemas,
            'official_interesting_routes': sorted(set(official_routes)),
            'official_interesting_route_specs': official_schema_paths,
            'portal_schema_attempts': portal_docs,
            'portal_interesting_routes': sorted(set(portal_routes)),
            'endpoint_probes': probes,
        })
