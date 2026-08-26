from fastapi import Query
from fastapi.responses import HTMLResponse

from supplier_adapters import compare_suppliers, supplier_statuses


def install_supplier_panel(app, esc):
    @app.get("/suppliers/status")
    def suppliers_status():
        return {"suppliers": supplier_statuses()}

    @app.get("/suppliers/search")
    def suppliers_search(q: str = Query(..., min_length=2), limit: int = 5):
        return compare_suppliers(q, limit_per_supplier=limit)

    @app.get("/dashboard/suppliers", response_class=HTMLResponse)
    def suppliers_dashboard(q: str = "", limit: int = 5):
        statuses = supplier_statuses()
        status_rows = "".join(
            f"<tr><td>{esc(s['name'])}</td><td>{'Подключён' if s.get('enabled') else 'Не подключён'}</td><td>{esc(s.get('note') or '')}</td></tr>"
            for s in statuses
        )

        result_rows = ""
        best_text = ""
        if q.strip():
            data = compare_suppliers(q.strip(), limit_per_supplier=limit)
            best = data.get("best")
            if best:
                best_text = f"Лучшая цена: <b>{best.get('price'):,.2f} ₽</b> — {esc(best.get('supplier'))}".replace(",", " ")
            else:
                best_text = "По подключённым источникам цена не найдена."

            for quote in data.get("quotes") or []:
                price = quote.get("price")
                base_price = quote.get("base_price")
                stock = quote.get("stock")
                result_rows += f"""
                <tr>
                  <td>{esc(quote.get('supplier'))}</td>
                  <td>{esc(quote.get('name'))}</td>
                  <td>{esc(quote.get('sku'))}</td>
                  <td>{esc(quote.get('article'))}</td>
                  <td>{esc(quote.get('brand'))}</td>
                  <td>{f'{price:,.2f} ₽'.replace(',', ' ') if isinstance(price, (int, float)) else '—'}</td>
                  <td>{f'{base_price:,.2f} ₽'.replace(',', ' ') if isinstance(base_price, (int, float)) else '—'}</td>
                  <td>{stock if stock is not None else '—'}</td>
                  <td>{esc(quote.get('courier_date') or quote.get('pickup_date') or '')}</td>
                </tr>"""

        html = f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
        <title>Поставщики</title><style>
        body{{font-family:Arial,sans-serif;margin:24px;background:#f5f5f5;color:#222}}.card{{background:#fff;padding:18px;border-radius:12px;margin-bottom:18px;box-shadow:0 2px 8px rgba(0,0,0,.08)}}
        input{{padding:10px;border:1px solid #ccc;border-radius:8px;min-width:360px}}button,a.btn{{padding:10px 14px;background:#222;color:#fff;border:0;border-radius:8px;text-decoration:none}}table{{width:100%;border-collapse:collapse;font-size:13px}}th{{background:#eee;text-align:left;padding:9px}}td{{padding:9px;border-bottom:1px solid #eee;vertical-align:top}}a{{color:#4c39d4;font-weight:bold;text-decoration:none}}.sub{{color:#666}}.ok{{color:#0a7a24}}.warn{{color:#9a6700}}
        </style></head><body>
        <p><a href='/dashboard/analysis'>← Анализ заявок</a> · <a href='/dashboard'>Все заявки</a> · <a href='/logout'>Выйти</a></p>
        <h1>Сравнение цен поставщиков</h1><p class='sub'>Единый интерфейс для API и прайс-фидов разных поставщиков.</p>
        <div class='card'><form method='get'><input name='q' value='{esc(q)}' placeholder='Введите товар, артикул или модель'><input type='number' name='limit' value='{limit}' min='1' max='20' style='min-width:90px;width:90px'><button>Сравнить</button></form><p>{best_text}</p></div>
        <div class='card'><h2>Источники</h2><table><thead><tr><th>Поставщик</th><th>Статус</th><th>Примечание</th></tr></thead><tbody>{status_rows}</tbody></table></div>
        <div class='card' style='overflow:auto'><h2>Результаты</h2><table><thead><tr><th>Поставщик</th><th>Товар</th><th>SKU</th><th>Артикул</th><th>Бренд</th><th>Наша цена</th><th>Розница</th><th>Остаток</th><th>Доставка</th></tr></thead><tbody>{result_rows}</tbody></table></div>
        </body></html>"""
        return HTMLResponse(content=html)
