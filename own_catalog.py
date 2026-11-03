"""Our product catalogue, supplier price observations and confirmed sale history."""
from __future__ import annotations

import hashlib
import html
import json
from datetime import date, datetime, timezone
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from price_lists import database, execute, norm, number, page, safe_url, tokens


def initialize_schema(conn, postgres):
    serial = "BIGSERIAL" if postgres else "INTEGER"
    auto = "" if postgres else " AUTOINCREMENT"
    conn.execute(f"""CREATE TABLE IF NOT EXISTS own_catalog_products (
        id {serial} PRIMARY KEY{auto}, product_key TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL, article TEXT, brand TEXT, unit TEXT NOT NULL,
        article_norm TEXT NOT NULL, created_at TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_own_catalog_article ON own_catalog_products(article_norm)")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS own_catalog_observations (
        id {serial} PRIMARY KEY{auto}, product_id BIGINT NOT NULL REFERENCES own_catalog_products(id),
        supplier TEXT NOT NULL, purchase_price DOUBLE PRECISION NOT NULL,
        selling_price DOUBLE PRECISION, price_date TEXT NOT NULL, competitors_count INTEGER,
        found_method TEXT NOT NULL, source_url TEXT, record_type TEXT NOT NULL,
        quantity DOUBLE PRECISION, source_job_id BIGINT, source_position INTEGER,
        source_json TEXT, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
        UNIQUE(source_job_id,source_position)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_own_catalog_latest ON own_catalog_observations(product_id,supplier,price_date,id)")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS own_catalog_tokens (
        product_id BIGINT NOT NULL REFERENCES own_catalog_products(id), token TEXT NOT NULL,
        PRIMARY KEY(token,product_id)
    )""")


def _validate_observation(values):
    name = str(values.get("name") or "").strip()
    supplier = str(values.get("supplier") or "").strip()
    unit = str(values.get("unit") or "").strip()
    method = str(values.get("found_method") or "").strip()
    if not all((name, supplier, unit, method)):
        raise ValueError("Заполните наименование, поставщика, единицу цены и способ поиска")
    if len(name) > 4000 or max(len(supplier), len(unit), len(method)) > 500:
        raise ValueError("Слишком длинное значение поля")
    purchase = number(values.get("purchase_price"))
    selling = number(values.get("selling_price"))
    if purchase is None or purchase <= 0 or (values.get("selling_price") not in (None, "") and (selling is None or selling <= 0)):
        raise ValueError("Закупочная и продажная цены должны быть положительными числами")
    competitors = values.get("competitors_count")
    if competitors not in (None, ""):
        numeric = number(competitors)
        if numeric is None or numeric < 0 or not numeric.is_integer():
            raise ValueError("Число предложений должно быть целым неотрицательным числом")
        competitors = int(numeric)
    else:
        competitors = None
    price_date = str(values.get("price_date") or "")
    if date.fromisoformat(price_date) > date.today():
        raise ValueError("Дата цены не может быть в будущем")
    return {**values, "name": name, "supplier": supplier, "unit": unit, "found_method": method,
        "article": str(values.get("article") or "").strip(), "brand": str(values.get("brand") or "").strip(),
        "purchase_price": purchase, "selling_price": selling, "competitors_count": competitors,
        "price_date": price_date, "source_url": safe_url(values.get("source_url"))}


def _save_observation(conn, values):
    values = _validate_observation(values)
    if values.get("source_job_id") is not None:
        existing = execute(conn, "SELECT product_id FROM own_catalog_observations WHERE source_job_id=? AND source_position=?",
                           (values["source_job_id"], values["source_position"])).fetchone()
        if existing:
            return {"product_id": existing["product_id"], "created": False}
    now = datetime.now(timezone.utc).isoformat()
    product_key = hashlib.sha256("|".join(norm(values.get(key)) for key in ("name", "article", "brand", "unit")).encode()).hexdigest()
    execute(conn, """INSERT INTO own_catalog_products
        (product_key,name,article,brand,unit,article_norm,created_at) VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(product_key) DO NOTHING""", (product_key, values["name"], values["article"], values["brand"],
        values["unit"], norm(values["article"]).replace(" ", ""), now))
    product_id = execute(conn, "SELECT id FROM own_catalog_products WHERE product_key=?", (product_key,)).fetchone()["id"]
    for token in tokens(" ".join(values.get(key) or "" for key in ("name", "article", "brand"))):
        execute(conn, "INSERT INTO own_catalog_tokens(product_id,token) VALUES (?,?) ON CONFLICT(token,product_id) DO NOTHING", (product_id, token))
    row = execute(conn, """INSERT INTO own_catalog_observations
        (product_id,supplier,purchase_price,selling_price,price_date,competitors_count,found_method,
         source_url,record_type,quantity,source_job_id,source_position,source_json,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_job_id,source_position) DO NOTHING RETURNING id""",
        (product_id, values["supplier"], values["purchase_price"], values["selling_price"], values["price_date"],
         values["competitors_count"], values["found_method"], values["source_url"], values.get("record_type") or "manual",
         values.get("quantity"), values.get("source_job_id"), values.get("source_position"),
         json.dumps(values.get("source") or {}, ensure_ascii=False), now)).fetchone()
    return {"product_id": product_id, "created": bool(row)}


def add_manual_product(values):
    # The form explicitly asks for prices including VAT, where applicable.
    with database() as conn:
        return _save_observation(conn, {**values, "record_type": "manual"})


def confirmed_sale_rows(job_id):
    from automation_pipeline import _included
    with database() as conn:
        row = execute(conn, "SELECT order_id,result_json FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
    if not row or not row["result_json"]:
        raise HTTPException(status_code=404, detail="Результат подбора не найден")
    result = json.loads(row["result_json"])
    included = [item for item in result.get("items") or [] if _included(item) and item.get("selected")]
    if not included:
        raise ValueError("В заявке нет включённых позиций с выбранным товаром")
    return result, included


def record_confirmed_sale(job_id, sale_date, competitor_counts=None):
    from automation_pipeline import _candidate_pack_size, _lock
    result, included = confirmed_sale_rows(job_id)
    values = []
    for item in included:
        candidate = item["selected"]
        pack_size = _candidate_pack_size(candidate, item.get("unit") or "")
        purchase = number(candidate.get("price"))
        if purchase is None or purchase <= 0:
            raise ValueError(f"Позиция {item['position']}: нет закупочной цены")
        # Store both prices in the request's unit, retaining the original
        # supplier quote and packaging in the immutable source snapshot.
        method = candidate.get("found_method") or (
            f"Прайс: {candidate.get('source_filename')}" if candidate.get("source_type") == "price_list" else
            "Собственный каталог" if candidate.get("source_type") == "own_catalog" else
            f"API: {candidate.get('supplier') or 'поставщик'}")
        name = candidate.get("name") or item["requested_name"]
        if pack_size > 1:
            # A SKU sold in packs remains distinguishable, but lookup prices
            # after a completed sale are now per piece rather than per pack.
            name = name + " (цена за штуку)"
        values.append(_validate_observation({"name": name, "article": candidate.get("article"),
            "brand": candidate.get("brand"), "unit": item.get("unit") or candidate.get("unit") or "шт",
            "supplier": candidate.get("supplier") or "ВсеИнструменты.ру", "purchase_price": purchase / pack_size,
            "selling_price": item.get("proposed_unit_price"), "price_date": sale_date,
            "competitors_count": (competitor_counts or {}).get(str(item["position"])), "found_method": method,
            "source_url": candidate.get("url") or candidate.get("source_url"), "record_type": "sale",
            "source_job_id": job_id, "source_position": int(item["position"]), "quantity": item.get("quantity"),
            "source": {"order_id": result.get("order_id"), "invoice_number": result.get("invoice_number"),
                       "customer": result.get("customer"), "requested_name": item["requested_name"], "selected": candidate}}))
    with _lock, database() as conn:
        results = [_save_observation(conn, value) for value in values]
    return sum(row["created"] for row in results)


def search_catalog(query, limit=8):
    from supplier_adapters import SupplierQuote
    from vi_order_match import _identifiers, _score_details, _requested_brands
    from automation_pipeline import _hard_conflicts, _candidate_matches_brand
    terms = tokens(query)
    identifiers = [value.lower() for value in _identifiers(query)]
    product_ids = set()
    with database() as conn:
        if identifiers:
            rows = execute(conn, "SELECT id FROM own_catalog_products WHERE article_norm IN (" + ",".join("?" for _ in identifiers) + ") LIMIT 300", identifiers).fetchall()
            product_ids.update(row["id"] for row in rows)
        if terms:
            rows = execute(conn, """SELECT product_id,COUNT(*) AS hits FROM own_catalog_tokens
                WHERE token IN (""" + ",".join("?" for _ in terms) + ") GROUP BY product_id ORDER BY hits DESC,product_id LIMIT 400", terms).fetchall()
            product_ids.update(row["product_id"] for row in rows)
        if not product_ids:
            return []
        rows = execute(conn, """SELECT p.*,o.id AS observation_id,o.supplier,o.purchase_price,o.selling_price,
            o.price_date,o.competitors_count,o.found_method,o.source_url,o.record_type
            FROM own_catalog_products p JOIN own_catalog_observations o ON o.product_id=p.id
            WHERE p.id IN (""" + ",".join("?" for _ in product_ids) + """ ) AND o.enabled=1
            AND NOT EXISTS(SELECT 1 FROM own_catalog_observations newer WHERE newer.product_id=o.product_id
                AND newer.supplier=o.supplier AND newer.enabled=1 AND
                (newer.price_date>o.price_date OR (newer.price_date=o.price_date AND newer.id>o.id)))""", list(product_ids)).fetchall()
    quotes = [SupplierQuote(supplier=row["supplier"], name=row["name"], article=row["article"] or None,
        brand=row["brand"] or None, unit=row["unit"], sku=f"catalog-{row['id']}", price=row["purchase_price"],
        stock=None, url=row["source_url"] or None, source_type="own_catalog", source_key=str(row["id"]),
        catalog_product_id=row["id"], price_date=row["price_date"], selling_price=row["selling_price"],
        competitors_count=row["competitors_count"], found_method=row["found_method"], price_type="Наш каталог") for row in rows]
    brands = _requested_brands(query, [item.brand for item in quotes])
    accepted = []
    for item in quotes:
        score, _ = _score_details(query, item)
        if score >= 0.48 and not _hard_conflicts(query, item.to_dict()) and (not brands or _candidate_matches_brand(item.to_dict(), brands)):
            accepted.append((item, score))
    accepted.sort(key=lambda pair: (-pair[1], pair[0].price) if brands else (pair[0].price, -pair[1]))
    return [item for item, _ in accepted[:max(8, min(limit * 8, 80))]]


def install_own_catalog(app):
    esc = lambda value: html.escape(str(value if value is not None else ""), quote=True)

    @app.get("/dashboard/catalog")
    def catalog_dashboard(q: str = "", message: str = ""):
        with database() as conn:
            params = []
            where = ""
            if q.strip():
                search_terms = tokens(q)
                where = "WHERE LOWER(p.name) LIKE ? OR LOWER(p.article) LIKE ? OR LOWER(o.supplier) LIKE ?"
                params = ["%" + q.strip().lower() + "%"] * 3
                if search_terms:
                    where += " OR p.id IN (SELECT product_id FROM own_catalog_tokens WHERE token IN (" + ",".join("?" for _ in search_terms) + "))"
                    params.extend(search_terms)
            rows = execute(conn, """SELECT p.id,p.name,p.article,p.unit,p.brand,o.id AS observation_id,
                o.supplier,o.purchase_price,o.selling_price,o.price_date,o.competitors_count,o.found_method,
                o.record_type,o.enabled FROM own_catalog_products p JOIN own_catalog_observations o ON o.product_id=p.id """
                + where + " ORDER BY o.id DESC LIMIT 100", params).fetchall()
        body_rows = []
        for row in rows:
            sales_label = "Подтверждённая продажа" if row["record_type"] == "sale" else "Введено вручную"
            purchase = f"{row['purchase_price']:,.2f}".replace(",", " ")
            selling = f"{row['selling_price']:,.2f}".replace(",", " ") if row["selling_price"] is not None else "—"
            body_rows.append(f"<tr><td><a href='/dashboard/catalog/{row['id']}'>{esc(row['name'])}</a><br>{esc(row['article'])} · {esc(row['brand'])}</td>"
                f"<td>{esc(row['supplier'])}</td><td>{purchase} ₽ / {esc(row['unit'])}</td><td>{selling} ₽ / {esc(row['unit'])}</td>"
                f"<td>{esc(row['competitors_count']) if row['competitors_count'] is not None else 'Неизвестно'}</td><td>{esc(row['found_method'])}</td>"
                f"<td>{esc(row['price_date'])}<br>{sales_label}</td><td><form method='post' action='/dashboard/catalog/observations/{row['observation_id']}/toggle'><button>{'Отключить цену' if row['enabled'] else 'Включить цену'}</button></form></td></tr>")
        fields = [("name", "Полное наименование *", "text", ""), ("article", "Артикул", "text", ""),
            ("brand", "Бренд", "text", ""), ("unit", "Единица цены *", "text", "шт"),
            ("supplier", "Поставщик *", "text", ""), ("purchase_price", "Закупочная цена, ₽ *", "text", ""),
            ("selling_price", "Продажная цена, ₽", "text", ""), ("price_date", "Дата цены *", "date", date.today().isoformat()),
            ("competitors_count", "Предложений по позиции, включая наше", "number", ""),
            ("found_method", "Как найден *", "text", "Введено вручную"), ("source_url", "Ссылка на первоисточник", "url", "")]
        inputs = "".join(f"<label>{label}<br><input name='{field}' type='{kind}' value='{esc(value)}' {'required' if '*' in label else ''} {'min=0' if kind=='number' else ''}></label>" for field, label, kind, value in fields)
        return page("<p><a href='/dashboard/catalog'>Наш каталог</a></p><h1>Собственный каталог товаров</h1>"
            + (f"<p>{esc(message)}</p>" if message else "")
            + "<p>Закупочные цены из каталога участвуют в подборе вместе с прайсами и API. Историческая цена и наличие требуют проверки перед новой закупкой.</p>"
            f"<section><h2>Добавить товар или новую цену</h2><form method='post' action='/dashboard/catalog/add'><div class='grid'>{inputs}</div>"
            "<p class='note'>Цены указывайте за выбранную единицу, с НДС, если он начисляется поставщиком. Продажная цена — ориентир или факт продажи. Число предложений оставьте пустым, если неизвестно; оно само по себе не доказывает, что наша цена была минимальной.</p><button>Сохранить в каталог</button></form></section>"
            f"<section><form method='get'><input name='q' value='{esc(q)}' placeholder='Товар, артикул или поставщик'><button>Найти</button></form></section>"
            "<section class='scroll'><h2>Последние 100 записей цен и продаж</h2><table><tr><th>Товар</th><th>Поставщик</th><th>Закупочная цена</th><th>Продажная цена</th><th>Предложений</th><th>Как найден</th><th>Дата / источник</th><th>Поиск</th></tr>"
            + ("".join(body_rows) or "<tr><td colspan='8'>Каталог пока пуст.</td></tr>") + "</table></section>", "Собственный каталог")

    @app.post("/dashboard/catalog/add")
    async def catalog_add(request: Request):
        form = dict(await request.form())
        try:
            result = await run_in_threadpool(add_manual_product, form)
        except ValueError as exc:
            return page("<h1>Товар не сохранён</h1><p class='error'>" + esc(exc) + "</p><a href='/dashboard/catalog'>Вернуться к каталогу</a>")
        return RedirectResponse(f"/dashboard/catalog/{result['product_id']}", status_code=303)

    @app.get("/dashboard/catalog/{product_id}")
    def product_history(product_id: int):
        with database() as conn:
            product = execute(conn, "SELECT * FROM own_catalog_products WHERE id=?", (product_id,)).fetchone()
            rows = execute(conn, "SELECT * FROM own_catalog_observations WHERE product_id=? ORDER BY price_date DESC,id DESC", (product_id,)).fetchall()
        if not product:
            raise HTTPException(status_code=404, detail="Товар не найден")
        records = []
        for row in rows:
            source = json.loads(row["source_json"] or "{}")
            source_link = f"<a href='{esc(row['source_url'])}' target='_blank' rel='noopener'>Первоисточник</a>" if row["source_url"] else ""
            job_link = f"<a href='/dashboard/automation/jobs/{row['source_job_id']}/review'>Заявка {esc(source.get('order_id'))}, позиция {row['source_position']}</a>" if row["source_job_id"] else "Ручной ввод"
            records.append(f"<tr><td>{esc(row['price_date'])}</td><td>{esc(row['supplier'])}</td><td>{row['purchase_price']:.2f}</td><td>{esc(row['selling_price']) or '—'}</td>"
                f"<td>{esc(row['competitors_count']) if row['competitors_count'] is not None else 'Неизвестно'}</td><td>{esc(row['found_method'])}<br>{source_link}</td><td>{job_link}<br>{'Продажа подтверждена' if row['record_type']=='sale' else 'Введено вручную'}</td></tr>")
        return page(f"<p><a href='/dashboard/catalog'>← Каталог</a></p><h1>{esc(product['name'])}</h1><p>Артикул: {esc(product['article']) or '—'} · бренд: {esc(product['brand']) or '—'} · цены за {esc(product['unit'])}</p>"
            "<section class='scroll'><h2>История цен и продаж</h2><table><tr><th>Дата</th><th>Поставщик</th><th>Закупка, ₽</th><th>Продажа, ₽</th><th>Предложений</th><th>Как найден</th><th>Основание</th></tr>" + "".join(records) + "</table></section>")

    @app.post("/dashboard/catalog/observations/{observation_id}/toggle")
    def toggle_observation(observation_id: int):
        with database() as conn:
            row = execute(conn, "UPDATE own_catalog_observations SET enabled=1-enabled WHERE id=? RETURNING id", (observation_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Цена не найдена")
        return RedirectResponse("/dashboard/catalog", status_code=303)

    @app.get("/dashboard/automation/jobs/{job_id}/catalog-sale")
    def sale_confirmation(job_id: int, error: str = ""):
        try:
            result, items = confirmed_sale_rows(job_id)
        except ValueError as exc:
            return page("<p class='error'>" + esc(exc) + "</p>")
        rows = "".join(f"<tr><td>{item['position']}</td><td>{esc(item['requested_name'])}</td><td>{esc(item.get('proposed_unit_price'))} ₽</td>"
            f"<td><input name='competitors_{item['position']}' type='number' min='0' placeholder='Если известно'></td></tr>" for item in items)
        return page(f"<h1>Сохранить успешную продажу в каталог</h1><p>Заявка {esc(result.get('order_id'))}. Отправленный счёт или предложение сами по себе не означают продажу.</p>"
            + (f"<p class='error'>{esc(error)}</p>" if error else "")
            + f"<section><form method='post' action='/dashboard/automation/jobs/{job_id}/catalog-sale'><label>Дата успешной продажи <input name='sale_date' type='date' value='{date.today().isoformat()}' required></label>"
            f"<div class='scroll'><table><tr><th>№</th><th>Позиция</th><th>Продажная цена за единицу</th><th>Предложений, включая наше</th></tr>{rows}</table></div>"
            "<label><input type='checkbox' name='confirmed_sale' value='1' required> Подтверждаю, что включённые позиции действительно проданы</label>"
            "<p>Закупочные и продажные цены, поставщик и способ поиска будут взяты из сохранённого подбора. Повторное подтверждение этой заявки не создаст дубликаты.</p><button>Подтвердить и сохранить</button></form></section>")

    @app.post("/dashboard/automation/jobs/{job_id}/catalog-sale")
    async def save_sale(job_id: int, request: Request):
        form = await request.form()
        if form.get("confirmed_sale") != "1":
            return RedirectResponse(f"/dashboard/automation/jobs/{job_id}/catalog-sale?error=" + quote("Подтвердите факт продажи"), status_code=303)
        counts = {key.removeprefix("competitors_"): value for key, value in form.items() if key.startswith("competitors_")}
        try:
            count = await run_in_threadpool(record_confirmed_sale, job_id, str(form.get("sale_date") or ""), counts)
        except ValueError as exc:
            return RedirectResponse(f"/dashboard/automation/jobs/{job_id}/catalog-sale?error=" + quote(str(exc)), status_code=303)
        return RedirectResponse("/dashboard/catalog?message=" + quote(f"Сохранено новых записей о продажах: {count}."), status_code=303)
