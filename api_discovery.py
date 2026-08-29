import re
import requests
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

    @app.get('/analysis/api-discovery/{order_id}')
    def discover_price_api(order_id: int):
        order = get_order(order_id)
        item_ids = [x.get('id') for x in (order.get('orderItems') or []) if x.get('id')]

        # 1) Inspect the official Swagger host referenced by Cynteka's own wiki.
        swagger_host = 'https://swagger.cynteka.ru'
        swagger_pages = []
        discovered_schema_urls = []
        root_html = ''
        try:
            r = requests.get(swagger_host + '/', timeout=20)
            root_html = r.text if r.ok else ''
            swagger_pages.append({'url': swagger_host + '/', 'status': r.status_code, 'content_type': r.headers.get('content-type', ''), 'preview': r.text[:2500]})
            if root_html:
                # Swagger UI commonly embeds url: '/swagger/v1/swagger.json' or urls: [...].
                for m in re.finditer(r"(?:url|URL)\s*[:=]\s*['\"]([^'\"]+\.json[^'\"]*)['\"]", root_html):
                    discovered_schema_urls.append(m.group(1))
                for m in re.finditer(r"['\"]([^'\"]*(?:swagger|openapi)[^'\"]*\.json[^'\"]*)['\"]", root_html, re.I):
                    discovered_schema_urls.append(m.group(1))
        except requests.RequestException as exc:
            swagger_pages.append({'url': swagger_host + '/', 'error': str(exc)})

        common_schema_paths = [
            '/swagger/v1/swagger.json', '/swagger.json', '/openapi.json',
            '/api/swagger.json', '/api/v1/swagger.json', '/v1/swagger.json',
            '/swagger/v1/openapi.json', '/docs/swagger.json', '/api-docs',
        ]
        schema_urls = []
        for value in discovered_schema_urls + common_schema_paths:
            if value.startswith('http://') or value.startswith('https://'):
                url = value
            else:
                url = swagger_host + ('/' + value.lstrip('/'))
            if url not in schema_urls:
                schema_urls.append(url)

        official_schemas = []
        official_routes = []
        for url in schema_urls:
            try:
                r = requests.get(url, timeout=20)
                entry = {'url': url, 'status': r.status_code, 'content_type': r.headers.get('content-type', '')}
                if r.ok:
                    body = safe_body(r)
                    if isinstance(body, dict):
                        paths = list((body.get('paths') or {}).keys())
                        entry['paths_count'] = len(paths)
                        entry['interesting_paths'] = interesting_paths(body)[:500]
                        official_routes.extend(entry['interesting_paths'])
                        # Keep API server/base hints; this tells us which host the docs expect.
                        entry['servers'] = body.get('servers')
                        entry['host'] = body.get('host')
                        entry['basePath'] = body.get('basePath')
                    else:
                        entry['preview'] = str(body)[:2500]
                else:
                    entry['preview'] = r.text[:1000]
                official_schemas.append(entry)
            except requests.RequestException as exc:
                official_schemas.append({'url': url, 'error': str(exc)})

        # 2) Also inspect docs potentially exposed by the actual supplier portal host.
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

        # 3) Probe only plausible routes. We keep this diagnostic and read-only.
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

        # Official schema routes are more trustworthy than guessed paths.
        route_pool = candidate_paths + official_routes + portal_routes
        probes = []
        seen = set()
        for path in route_pool:
            if not isinstance(path, str) or path in seen:
                continue
            seen.add(path)
            # Do not blindly call templated swagger routes that still contain unresolved params.
            if '{' in path or '}' in path:
                probes.append({'path': path, 'skipped': 'templated route from schema'})
                continue
            url = f"{zakupay_base_url}{path}"
            try:
                r = requests.get(url, headers=zakupay_headers(), timeout=15)
                probes.append({'path': path, 'status': r.status_code, 'body': safe_body(r, 4000)})
            except requests.RequestException as exc:
                probes.append({'path': path, 'error': str(exc)})

        # Known control values from the user's screenshots are NOT hardcoded into logic;
        # this endpoint exposes enough raw data for us to identify the route that carries them.
        return JSONResponse({
            'order_id': order_id,
            'first_item_id': item_ids[0] if item_ids else None,
            'official_swagger_host': swagger_host,
            'swagger_root': swagger_pages,
            'official_schema_attempts': official_schemas,
            'official_interesting_routes': sorted(set(official_routes)),
            'portal_schema_attempts': portal_docs,
            'portal_interesting_routes': sorted(set(portal_routes)),
            'endpoint_probes': probes,
        })
