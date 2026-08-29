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

    @app.get('/analysis/api-discovery/{order_id}')
    def discover_price_api(order_id: int):
        order = get_order(order_id)
        item_ids = [x.get('id') for x in (order.get('orderItems') or []) if x.get('id')]
        docs_paths = [
            '/swagger/v1/swagger.json', '/swagger.json', '/openapi.json',
            '/api/swagger.json', '/api/v1/swagger.json', '/swagger/index.html',
        ]
        docs = []
        discovered_routes = []
        for path in docs_paths:
            url = f"{zakupay_base_url}{path}"
            try:
                r = requests.get(url, headers=zakupay_headers(), timeout=15)
                ct = r.headers.get('content-type', '')
                entry = {'path': path, 'status': r.status_code, 'content_type': ct}
                if r.ok and 'json' in ct:
                    try:
                        body = r.json()
                        paths = list((body.get('paths') or {}).keys()) if isinstance(body, dict) else []
                        interesting = [p for p in paths if any(k in p.lower() for k in ('offer', 'price', 'corridor', 'orderitem', 'order-item'))]
                        entry['interesting_paths'] = interesting[:300]
                        discovered_routes.extend(interesting)
                    except ValueError:
                        entry['preview'] = r.text[:1000]
                else:
                    entry['preview'] = r.text[:500]
                docs.append(entry)
            except requests.RequestException as exc:
                docs.append({'path': path, 'error': str(exc)})

        candidate_paths = [
            f'/api/v1/orders/{order_id}/offers',
            f'/api/v1/order/{order_id}/offers',
            f'/api/v1/offers/order/{order_id}',
            f'/api/v1/order-offers/{order_id}',
            f'/api/v1/producer-offers?orderId={order_id}',
            f'/api/v1/producerOffers?orderId={order_id}',
            f'/api/v1/prices?orderId={order_id}',
            f'/api/v1/price-corridor?orderId={order_id}',
            f'/api/v1/orders/{order_id}',
        ]
        if item_ids:
            iid = item_ids[0]
            candidate_paths.extend([
                f'/api/v1/order-items/{iid}/offers',
                f'/api/v1/orderItems/{iid}/offers',
                f'/api/v1/prices?orderItemId={iid}',
                f'/api/v1/price-corridor?orderItemId={iid}',
            ])

        probes = []
        seen = set()
        for path in candidate_paths + discovered_routes:
            if path in seen:
                continue
            seen.add(path)
            url = f"{zakupay_base_url}{path}"
            try:
                r = requests.get(url, headers=zakupay_headers(), timeout=15)
                try:
                    body = r.json()
                except ValueError:
                    body = r.text[:1500]
                probes.append({'path': path, 'status': r.status_code, 'body': body})
            except requests.RequestException as exc:
                probes.append({'path': path, 'error': str(exc)})

        return JSONResponse({
            'order_id': order_id,
            'first_item_id': item_ids[0] if item_ids else None,
            'swagger_and_openapi': docs,
            'discovered_routes': discovered_routes,
            'endpoint_probes': probes,
        })
