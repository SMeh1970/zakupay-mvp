"""Prepared product cards, versioned supplier offers, and indexed matching.

Supplier SKU is scoped to a catalogue; without a unique SKU a row stays separate.
Descriptions are cached by hash/version, corrections are explicit and survive refresh.
"""
import hashlib
import html
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from product_characteristics import VERSION, compare, extract

PROFILES = {
    "saturn": dict(label="Сатурн — известная структура", sheet="Лист_1", header_row=4,
        mapping=dict(name=4, article=2, unit=5, price=7), anchor=(4, "Наименование товаров")),
    "ipk": dict(label="ИПК — остатки с ценами", sheet="TDSheet", header_row=7,
        mapping=dict(name=3, article=0, unit=6, stock=7, price=8), anchor=(3, "Номенклатура"), anchor_row=6),
    "vent": dict(label="Крепёжные системы — вентиляция", sheet="TDSheet", header_row=1,
        mapping=dict(name=1, price=2, unit=3), anchor=(1, "Прайс-лист")),
    "pipe": dict(label="Уралпайп — котельные трубы без цен", sheet="Лист1", header_row=1,
        mapping=dict(name=0, stock=2, warehouse=3), anchor=(0, "Наименование")),
}


def initialize_schema(conn, postgres):
    serial = "BIGSERIAL" if postgres else "INTEGER"
    auto = "" if postgres else " AUTOINCREMENT"
    conn.execute(f"""CREATE TABLE IF NOT EXISTS price_product_cards (
        id {serial} PRIMARY KEY{auto}, product_key TEXT NOT NULL UNIQUE,
        description_hash TEXT NOT NULL, original_name TEXT NOT NULL,
        category TEXT NOT NULL, analysis_json TEXT NOT NULL, overrides_json TEXT NOT NULL,
        parser_version TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_price_cards_category ON price_product_cards(category)")
    conn.execute("""CREATE TABLE IF NOT EXISTS price_product_offers (
        list_id BIGINT NOT NULL REFERENCES supplier_price_lists(id),
        product_id BIGINT NOT NULL REFERENCES price_product_cards(id),
        source_row INTEGER NOT NULL, source_sheet TEXT NOT NULL, payload_json TEXT NOT NULL,
        PRIMARY KEY(list_id,source_sheet,source_row)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_price_offers_product ON price_product_offers(product_id,list_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS price_product_terms (
        product_id BIGINT NOT NULL REFERENCES price_product_cards(id), token TEXT NOT NULL,
        PRIMARY KEY(token,product_id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_price_terms_product ON price_product_terms(product_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS price_product_attributes (
        product_id BIGINT NOT NULL REFERENCES price_product_cards(id), parameter TEXT NOT NULL,
        value_key TEXT NOT NULL, unit TEXT NOT NULL, ambiguous INTEGER NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_price_attrs_lookup ON price_product_attributes(parameter,value_key,unit,product_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_price_attrs_product_parameter ON price_product_attributes(product_id,parameter)")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS price_product_corrections (
        id {serial} PRIMARY KEY{auto}, product_id BIGINT NOT NULL REFERENCES price_product_cards(id),
        before_json TEXT NOT NULL, after_json TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL
    )""")


def key(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def value_key(value):
    # JSON distinguishes 21 from 21.0; physical attributes must not.
    return key(float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value)


def parse_catalog(record, settings):
    from price_lists import table_rows, number, FIELDS
    profile = settings.get("profile", "")
    if profile and profile not in PROFILES:
        raise ValueError("Неизвестный шаблон файла")
    cfg = PROFILES.get(profile, settings)
    names, sheet, rows = table_rows(bytes(record["file_bytes"]), record["filename"], cfg["sheet"])
    if cfg["sheet"] not in names:
        raise ValueError("Лист выбранного шаблона отсутствует")
    start = int(cfg["header_row"])
    if not 1 <= start <= min(100, len(rows)):
        raise ValueError("Проверьте строку заголовков")
    if profile:
        col, expected = cfg["anchor"]
        header = rows[cfg.get("anchor_row", start) - 1]
        if col >= len(header) or str(header[col]).strip() != expected:
            raise ValueError("Структура файла не соответствует выбранному шаблону; выберите столбцы вручную")
    mapping = cfg["mapping"]
    width = max(map(len, rows[:100]))
    used = [v for v in mapping.values() if v is not None]
    if any(type(v) is not int or not 0 <= v < width for v in used) or len(set(used)) != len(used):
        raise ValueError("Проверьте соответствие столбцов")
    if mapping.get("name") is None:
        raise ValueError("Выберите наименование")
    if len(rows) > 100000:
        raise ValueError("В файле больше 100 000 строк")
    items, skipped = [], 0
    group = ""
    for rn, row in enumerate(rows[start:], start + 1):
        def cell(field):
            col = mapping.get(field)
            return row[col] if col is not None and col < len(row) else None
        name = str(cell("name") or "").strip()
        article = str(cell("article") or "").strip()
        if isinstance(cell("article"), float) and cell("article").is_integer():
            article = str(int(cell("article")))
        unit = str(cell("unit") or "").strip() or None
        raw_price = cell("price")
        price = number(raw_price)
        warehouse = str(cell("warehouse") or "").strip() or None
        stock = number(cell("stock"))
        is_product = bool(name and (article or unit or raw_price not in (None, "") or cell("stock") not in (None, "")))
        if profile == "saturn":
            is_product = article.startswith("тов-")
        elif profile == "ipk":
            is_product = bool(article and unit)
        elif profile == "vent":
            is_product = bool(unit and price is not None)
        elif profile == "pipe":
            is_product = name.startswith("Труба")
        if not is_product:
            if name and re.match(r"^\d{2}\s", name):
                group = name
            if any(v not in (None, "") for v in row):
                skipped += 1
            continue
        if len(name) > 4000 or (raw_price not in (None, "") and (price is None or price < 0)):
            raise ValueError(f"Строка {rn}: проверьте наименование и числовую цену")
        items.append(dict(supplier=record["supplier"], name=name, article=article or None,
            sku=article or None, unit=unit, price=None, original_price=price,
            currency=None, vat_mode="unknown", vat_rate=None, pricing_ready=False,
            price_date=record["price_date"] or None, source_list_id=record["id"], source_key=record["catalog_key"],
            source_filename=record["filename"], source_sheet=sheet, source_row=rn,
            source_type="price_list", source_url=record.get("source_url") or None,
            source_group=group or None, raw_cells=row, stock=None,
            price_list_stock=stock if stock is not None and stock >= 0 else None,
            stock_unit=unit if profile != "pipe" else None, stock_as_of=None, warehouse=warehouse,
            availability="on_order" if warehouse and "под заказ" in warehouse.lower() else "unconfirmed",
            source_provenance=settings.get("source_provenance") or {},
            notes=["Цена, валюта, НДС и наличие требуют подтверждения поставщиком"]
                + (["Назначение колонки «6» требует подтверждения"] if profile == "saturn" else [])))
    if not items:
        raise ValueError("Товарные строки не найдены; проверьте шаблон и столбцы")
    return items, skipped


def effective(analysis, overrides):
    result = dict(analysis)
    result["attributes"] = list(analysis["attributes"])
    if "category" in overrides:
        result["category"] = overrides["category"]
    for parameter, attribute in overrides.get("attributes", {}).items():
        result["attributes"] = [a for a in result["attributes"] if a["parameter"] != parameter]
        if attribute is not None:
            result["attributes"].append(attribute)
    return result


def _index(conn, pid, name, article, analysis):
    from price_lists import execute, tokens
    execute(conn, "DELETE FROM price_product_terms WHERE product_id=?", (pid,))
    execute(conn, "DELETE FROM price_product_attributes WHERE product_id=?", (pid,))
    from automation_pipeline import DATABASE_URL
    with conn.cursor() if DATABASE_URL else _sqlite_cursor(conn) as cursor:
        cursor.executemany("INSERT INTO price_product_terms(product_id,token) VALUES (%s,%s)" if DATABASE_URL else
                           "INSERT INTO price_product_terms(product_id,token) VALUES (?,?)",
                           [(pid, term) for term in tokens(name + " " + (article or ""))])
        cursor.executemany("INSERT INTO price_product_attributes(product_id,parameter,value_key,unit,ambiguous) VALUES (%s,%s,%s,%s,%s)" if DATABASE_URL else
                           "INSERT INTO price_product_attributes(product_id,parameter,value_key,unit,ambiguous) VALUES (?,?,?,?,?)",
                           [(pid, a["parameter"], value_key(a["value"]), a.get("unit") or "", int(bool(a.get("ambiguous")))) for a in analysis["attributes"]])


from contextlib import contextmanager


@contextmanager
def _sqlite_cursor(conn):
    cursor = conn.cursor()
    try:
        yield cursor
    finally:
        cursor.close()


def save_products(conn, record, items):
    from price_lists import execute, tokens
    from automation_pipeline import DATABASE_URL
    counts = Counter(str(i.get("article")) for i in items if i.get("article"))
    # One read per catalogue, not one DB round trip for each imported row.
    existing = execute(conn, """SELECT c.* FROM price_product_cards c WHERE c.id IN
        (SELECT product_id FROM price_product_offers WHERE list_id IN
        (SELECT id FROM supplier_price_lists WHERE catalog_key=?))""", (record["catalog_key"],)).fetchall()
    cache = {r["product_key"]: dict(r) for r in existing}
    reused, parsed = 0, 0
    pending, changed, ids = [], [], {}
    for item in items:
        article = item.get("article")
        identity = ("sku:" + article) if article and counts[article] == 1 else f"row:{record['id']}:{item['source_sheet']}:{item['source_row']}"
        product_key = hashlib.sha256((record["catalog_key"] + "|" + identity).encode()).hexdigest()
        digest = hashlib.sha256(item["name"].encode()).hexdigest()
        old = cache.get(product_key)
        unchanged = old and old["description_hash"] == digest and old["parser_version"] == VERSION
        analysis = json.loads(old["analysis_json"]) if unchanged else extract(item["name"])
        overrides = json.loads(old["overrides_json"]) if old else {}
        analysis_effective = effective(analysis, overrides)
        if unchanged:
            reused += 1
            ids[product_key] = old["id"]
        else:
            parsed += 1
            changed.append((product_key, digest, item["name"], analysis_effective["category"], key(analysis), key(overrides), VERSION))
        pending.append((item, product_key, analysis_effective, bool(article and counts[article] == 1)))
    for offset in range(0, len(changed), 100):
        batch = changed[offset:offset + 100]
        inserted = execute(conn, """INSERT INTO price_product_cards
                (product_key,description_hash,original_name,category,analysis_json,overrides_json,parser_version)
                VALUES """ + ",".join("(?,?,?,?,?,?,?)" for _ in batch) + """ ON CONFLICT(product_key) DO UPDATE SET
                description_hash=excluded.description_hash,original_name=excluded.original_name,
                category=excluded.category,analysis_json=excluded.analysis_json,parser_version=excluded.parser_version RETURNING id,product_key""",
                [v for row in batch for v in row]).fetchall()
        ids.update((r["product_key"], r["id"]) for r in inserted)
    changed_keys = {row[0] for row in changed}
    changed_ids = [ids[k] for k in changed_keys]
    for offset in range(0, len(changed_ids), 200):
        batch = changed_ids[offset:offset + 200]
        for table in ("price_product_terms", "price_product_attributes"):
            execute(conn, "DELETE FROM " + table + " WHERE product_id IN (" + ",".join("?" for _ in batch) + ")", batch)
    term_rows, attr_rows, offer_rows = [], [], []
    for item, product_key, analysis_effective, stable in pending:
        pid = ids[product_key]
        if product_key in changed_keys:
            term_rows.extend((pid, t) for t in tokens(item["name"] + " " + (item.get("article") or "")))
            attr_rows.extend((pid, a["parameter"], value_key(a["value"]), a.get("unit") or "", int(bool(a.get("ambiguous")))) for a in analysis_effective["attributes"])
        item["catalog_product_id"] = pid
        item["category"] = analysis_effective["category"]
        item["characteristics"] = analysis_effective["attributes"]
        offer = {"pricing_ready": item.get("price") is not None, "stock_unit": item.get("unit"), **item,
                 "identity_status": "supplier_sku" if stable else "separate_row"}
        offer_rows.append((record["id"], pid, item["source_row"], item["source_sheet"], key(offer)))
    cursor = conn.cursor()
    try:
        for table, columns, rows in [("price_product_terms", "product_id,token", term_rows),
            ("price_product_attributes", "product_id,parameter,value_key,unit,ambiguous", attr_rows),
            ("price_product_offers", "list_id,product_id,source_row,source_sheet,payload_json", offer_rows)]:
            if rows:
                cursor.executemany("INSERT INTO " + table + "(" + columns + ") VALUES ("
                    + ",".join("%s" if DATABASE_URL else "?" for _ in rows[0]) + ")", rows)
    finally:
        cursor.close()
    return dict(parsed_descriptions=parsed, reused_descriptions=reused)


def search(query, limit=12, *, include_conflicts=False, conn=None):
    from price_lists import database, execute, tokens, norm
    started = time.perf_counter()
    request = extract(query)
    terms = tokens(query)
    category = request["category"]
    if not terms:
        return dict(query=query, requirements=request, candidates=[], seconds=0)
    where, params = ["p.status='active'"], []
    if category != "Не определена":
        where.append("c.category=?")
        params.append(category)
    first_word = next((w for w in norm(query).split() if len(w) >= 4 and not w.isdigit()), "")
    retrieval_terms = (tokens(first_word) or terms) if category == "Не определена" else terms
    # Eliminate known attribute conflicts before the candidate LIMIT. Unknown
    # values remain review candidates; the strict pass retrieves confirmed first.
    strict = []
    for a in request["attributes"]:
        known = "EXISTS(SELECT 1 FROM price_product_attributes a WHERE a.product_id=c.id AND a.parameter=? AND a.ambiguous=0)"
        matching = "EXISTS(SELECT 1 FROM price_product_attributes a WHERE a.product_id=c.id AND a.parameter=? AND a.value_key=? AND a.unit=? AND a.ambiguous=0)"
        where.append("(NOT " + known + " OR " + matching + ")")
        params.extend([a["parameter"], a["parameter"], value_key(a["value"]), a.get("unit") or ""])
        strict.append((matching, [a["parameter"], value_key(a["value"]), a.get("unit") or ""]))
        where.append("(NOT EXISTS(SELECT 1 FROM price_product_attributes a WHERE a.product_id=c.id AND a.parameter=?) OR EXISTS(SELECT 1 FROM price_product_attributes a WHERE a.product_id=c.id AND a.parameter=? AND a.value_key=?))")
        params.extend([a["parameter"], a["parameter"], value_key(a["value"])])
    base_prefix = "WITH token_hits AS (SELECT product_id,COUNT(*) AS hits FROM price_product_terms WHERE token IN (" + ",".join("?" for _ in retrieval_terms) + """ ) GROUP BY product_id)
        SELECT c.id,c.analysis_json,c.overrides_json,o.payload_json FROM token_hits t
        JOIN price_product_cards c ON c.id=t.product_id JOIN price_product_offers o ON o.product_id=c.id
        JOIN supplier_price_lists p ON p.id=o.list_id WHERE """
    base = base_prefix + " AND ".join(where)
    hits = []
    from contextlib import nullcontext
    with (nullcontext(conn) if conn is not None else database()) as conn:
        for confirmed in ((True, False) if strict else (False,)):
            sql, args = base, list(retrieval_terms) + list(params)
            if confirmed and strict:
                sql += " AND " + " AND ".join(s[0] for s in strict)
                for _, ps in strict:
                    args.extend(ps)
            # Name overlap ranks within category/attributes, never supplier price.
            sql += " ORDER BY t.hits DESC,c.id,o.list_id DESC LIMIT 150"
            hits.extend(execute(conn, sql, args).fetchall())
        if include_conflicts and category != "Не определена":
            # Alternative dimensions stay visible for an explicit comparison
            # decision, but another product category is never substituted.
            extra = execute(conn, base_prefix + "p.status='active' AND c.category=? ORDER BY t.hits DESC,c.id,o.list_id DESC LIMIT 150",
                list(retrieval_terms) + [category]).fetchall()
            hits.extend(extra)
    candidates, seen = [], set()
    for row in hits:
        offer = json.loads(row["payload_json"])
        identity = (offer["source_list_id"], offer["source_sheet"], offer["source_row"])
        if identity in seen:
            continue
        seen.add(identity)
        analysis = effective(json.loads(row["analysis_json"]), json.loads(row["overrides_json"]))
        match = compare(request, analysis)
        if match["status"] == "conflict" and not include_conflicts:
            continue
        required = request["attributes"]
        evidence_matches = sum(any(a["parameter"] == r["parameter"] and a["value"] == r["value"]
            for a in analysis["attributes"]) for r in required)
        # A broad category (e.g. pipes) also includes unrelated drainage goods.
        # Alternatives need at least one shared requirement when several are
        # specified; lexical overlap alone is insufficient evidence to retrieve.
        if include_conflicts and len(required) >= 2 and not evidence_matches:
            continue
        if required and evidence_matches * 2 < len(required) and not include_conflicts:
            continue
        semantic_score = 0
        if category == "Не определена":
            if first_word and not (set(tokens(first_word)) & set(tokens(offer["name"]))):
                continue
            from supplier_adapters import SupplierQuote
            from vi_order_match import _score_details
            semantic_score, _ = _score_details(query, SupplierQuote(supplier=offer["supplier"], name=offer["name"], article=offer.get("article")))
        # Unsupported categories need a meaningful name match too; shared
        # colour/size words alone are insufficient to suggest another product.
        if category == "Не определена" and semantic_score < .48:
            continue
        overlap = len(set(tokens(query)) & set(tokens(offer["name"])))
        if not overlap:
            continue
        candidates.append({**offer, "product_id": row["id"], "analysis": analysis, "match": match, "overlap": overlap, "semantic_score": semantic_score})
    candidates.sort(key=lambda c: ({"matched": 0, "review": 1, "conflict": 2}[c["match"]["status"]],
        len(c["match"]["conflicts"]), len(c["match"]["missing"]), -c["overlap"]))
    return dict(query=query, requirements=request, candidates=candidates[:max(1, min(limit, 80))],
                seconds=round(time.perf_counter() - started, 4), candidate_window=len(seen))


def correct_product(pid, parameter, value, unit, note, category=""):
    from price_lists import database, execute
    if not note.strip():
        raise ValueError("Укажите основание исправления")
    if len(note) > 2000 or len(parameter) > 100 or len(unit) > 50:
        raise ValueError("Слишком длинное значение")
    with database() as conn:
        # PostgreSQL row lock prevents silent concurrent correction overwrites.
        from automation_pipeline import DATABASE_URL
        row = execute(conn, "SELECT * FROM price_product_cards WHERE id=?" + (" FOR UPDATE" if DATABASE_URL else ""), (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "Товар не найден")
        overrides = json.loads(row["overrides_json"])
        before = key(overrides)
        if category:
            overrides["category"] = category[:200]
        if parameter:
            overrides.setdefault("attributes", {})[parameter] = dict(parameter=parameter, value=value,
                unit=unit or None, ambiguous=False, evidence=note, origin="manual")
        updated = effective(json.loads(row["analysis_json"]), overrides)
        execute(conn, "UPDATE price_product_cards SET overrides_json=?,category=? WHERE id=?", (key(overrides), updated["category"], pid))
        _index(conn, pid, row["original_name"], "", updated)
        execute(conn, "INSERT INTO price_product_corrections(product_id,before_json,after_json,note,created_at) VALUES (?,?,?,?,?)",
                (pid, before, key(overrides), note, datetime.now(timezone.utc).isoformat()))


def candidate_html(candidate):
    esc = lambda x: html.escape(str(x if x is not None else ""), quote=True)
    attrs = "".join("<li>" + esc(a["parameter"]) + ": <b>" + esc(a["value"]) + " " + esc(a.get("unit"))
        + "</b>" + (" — неоднозначно" if a.get("ambiguous") else "")
        + " <small>(" + esc(a["origin"]) + ": «" + esc(a["evidence"]) + "»)</small></li>" for a in candidate["analysis"]["attributes"])
    match = candidate.get("match", {})
    match_text = "Совпали выделенные параметры" if match.get("status") == "matched" else "Нужна проверка: " + ", ".join(match.get("missing", []))
    price = candidate.get("original_price")
    price_text = esc(price) + " / " + esc(candidate.get("unit") or "единица неизвестна") if price is not None else "Цена отсутствует"
    if not candidate.get("pricing_ready", False):
        price_text += " · условия цены не подтверждены; в расчёт не включается"
    stock = candidate.get("price_list_stock")
    stock_text = esc(stock) + " " + esc(candidate.get("stock_unit") or "(единица неизвестна)") if stock is not None else "неизвестен"
    location = f"{candidate['source_filename']} · {candidate['source_sheet']} · строка {candidate['source_row']}"
    return (f"<section><h3><a href='/dashboard/prices/products/{candidate['product_id']}'>{esc(candidate['name'])}</a></h3>"
        f"<p>{esc(candidate['supplier'])} · {esc(candidate['analysis']['category'])} · {esc(match_text)}</p>"
        f"<p>{price_text}<br>Остаток в документе: {stock_text}; текущее наличие не подтверждено. {esc(candidate.get('warehouse'))}</p>"
        f"<ul>{attrs}</ul><p class='note'>{esc(location)} · дата цены: {esc(candidate.get('price_date') or 'неизвестна')}</p></section>")


def match_saved_order(job_id):
    from price_lists import database, execute
    with database() as conn:
        row = execute(conn, "SELECT id,order_id,subject,order_json,result_json FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Заявка не найдена")
    order = json.loads(row["order_json"] or "{}")
    items = order.get("orderItems") or order.get("items") or []
    if not items:
        items = [dict(goodName=i.get("requested_name"), count=i.get("quantity"), unit=i.get("unit")) for i in json.loads(row["result_json"] or "{}").get("items", [])]
    started = time.perf_counter()
    results = [dict(position=n, name=i.get("goodName") or i.get("name") or "", quantity=i.get("count"),
        unit=i.get("unit"), result=search(i.get("goodName") or i.get("name") or "", 5)) for n, i in enumerate(items, 1)]
    return dict(job_id=job_id, order_id=row["order_id"], title=row["subject"], items=results,
                seconds=round(time.perf_counter() - started, 4))


def install(app):
    from price_lists import database, execute, page
    esc = lambda x: html.escape(str(x if x is not None else ""), quote=True)

    @app.get("/dashboard/prices/prepared")
    def prepared_dashboard(q: str = "", list_id: int | None = None):
        content = ""
        with database() as conn:
            stats = execute(conn, """SELECT COUNT(*) AS offers,COUNT(DISTINCT c.category) AS categories
                FROM price_product_offers o JOIN supplier_price_lists p ON p.id=o.list_id
                JOIN price_product_cards c ON c.id=o.product_id WHERE p.status='active'""").fetchone()
        if q.strip():
            result = search(q)
            content = f"<p>Поиск: {result['seconds']} с. Категория: {esc(result['requirements']['category'])}. Совпадение оценивается только по выделенным параметрам.</p>"
            content += "".join(candidate_html(c) for c in result["candidates"]) or "<p>Кандидаты не найдены. Следующий шаг — API поставщиков или поиск через Sniper.</p>"
        else:
            with database() as conn:
                where = " AND p.id=?" if list_id is not None else ""
                args = (list_id,) if list_id is not None else ()
                rows = execute(conn, """SELECT c.id,c.analysis_json,c.overrides_json,o.payload_json FROM price_product_cards c
                    JOIN price_product_offers o ON o.product_id=c.id JOIN supplier_price_lists p ON p.id=o.list_id
                    WHERE """ + ("1=1" if list_id is not None else "p.status='active'") + where + " ORDER BY p.id DESC,o.source_row LIMIT 30", args).fetchall()
            for row in rows:
                c = json.loads(row["payload_json"])
                c.update(product_id=row["id"], analysis=effective(json.loads(row["analysis_json"]), json.loads(row["overrides_json"])))
                content += candidate_html(c)
        return page(f"<h1>Подготовленный каталог прайсов</h1><p>Предложений: {stats['offers']}; категорий, включая не определённую: {stats['categories']}.</p><p>Оригинальные данные и характеристики. Цены и остатки требуют проверки перед закупкой.</p>"
            f"<form><input name='q' value='{esc(q)}' placeholder='Например: Фанера толщина 21 мм'><button>Подобрать</button></form>{content}")

    @app.get("/dashboard/prices/match-order/{job_id}")
    def match_order(job_id: int):
        result = match_saved_order(job_id)
        body = f"<h1>Подбор по прайсам: {esc(result['title'])}</h1><p>Заявка {result['order_id']}; позиций {len(result['items'])}; время {result['seconds']} с.</p>"
        body += "<p>Этот просмотр не изменяет заявку и счёт. Неизвестные параметры требуют проверки. Цены и остатки не подтверждены.</p>"
        body += f"<p><a class='button' href='/dashboard/automation/jobs/{job_id}/comparison'>Открыть сравнительную таблицу поставщиков</a></p>"
        for row in result["items"]:
            body += f"<h2>{row['position']}. {esc(row['name'])}</h2>"
            body += "".join(candidate_html(c) for c in row["result"]["candidates"]) or "<p>Кандидатов нет — уточнить через API или Sniper.</p>"
        return page(body)

    @app.get("/dashboard/prices/products/{pid}")
    def product_detail(pid: int):
        with database() as conn:
            row = execute(conn, "SELECT * FROM price_product_cards WHERE id=?", (pid,)).fetchone()
            offers = execute(conn, "SELECT payload_json FROM price_product_offers WHERE product_id=? ORDER BY list_id DESC LIMIT 20", (pid,)).fetchall()
            corrections = execute(conn, "SELECT note,created_at FROM price_product_corrections WHERE product_id=? ORDER BY id DESC LIMIT 20", (pid,)).fetchall()
        if not row:
            raise HTTPException(404, "Товар не найден")
        body = ""
        for offer in offers:
            c = json.loads(offer["payload_json"])
            snapshot = dict(category=c.get("category", "Не определена"), attributes=c.get("characteristics") or [])
            c.update(product_id=pid, analysis=effective(snapshot, json.loads(row["overrides_json"])))
            body += candidate_html(c) + "<details><summary>Исходные значения строки</summary><pre>" + esc(key(c)) + "</pre></details>"
        body += f"<section><h2>Ручное исправление</h2><form method='post' action='/dashboard/prices/products/{pid}/correct'>"
        body += "<label>Категория (необязательно) <input name='category'></label><label>Параметр <input name='parameter'></label>"
        body += "<label>Значение (число или текст) <input name='value'></label><label>Единица <input name='unit'></label><label>Основание исправления <input name='note' required></label><button>Сохранить исправление</button></form>"
        body += "<p>Исправления сохраняются отдельно и не заменяются новым прайсом при устойчивом артикуле. Без артикула строки разных версий не объединяются.</p>"
        body += "".join(f"<p>{esc(c['created_at'])}: {esc(c['note'])}</p>" for c in corrections) + "</section>"
        return page(body)

    @app.post("/dashboard/prices/products/{pid}/correct")
    async def correction(pid: int, request: Request):
        form = await request.form()
        raw = str(form.get("value") or "").strip()
        try:
            value = json.loads(raw.replace(",", "."))
            if not isinstance(value, (str, int, float, bool)):
                value = raw
        except ValueError:
            value = raw
        try:
            await run_in_threadpool(correct_product, pid, str(form.get("parameter") or "").strip(), value,
                str(form.get("unit") or "").strip(), str(form.get("note") or "").strip(), str(form.get("category") or "").strip())
        except ValueError as exc:
            return page("<p class='error'>" + esc(exc) + "</p>")
        return RedirectResponse(f"/dashboard/prices/products/{pid}", status_code=303)
