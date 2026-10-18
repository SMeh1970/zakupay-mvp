"""Private porcelain workflow. All external submission remains manual."""
import html
import io
import json
import logging
import os
import threading
import zipfile
from urllib.parse import urlencode, urlsplit

import requests
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

import porcelain as tiles
from invoice_generator import build_invoice_xlsx, normalize_customer
from price_lists import database, execute, norm, number

SOURCE_FOLDER = 'https://drive.google.com/drive/folders/1Qr9A-rRbzfgFHj6yB29JsgUfCUpuvVUF'
WORKBOOK_URL = 'https://docs.google.com/spreadsheets/d/1ahrs_93E_baD5zCB1inu2x-zc2xF0QlNJF5rsYkzSzA/edit'
CALCULATOR_URL = 'https://keramahall-delivery.mehmans.chatgpt.site/'
URL = '/dashboard/tiles'
esc = lambda value: html.escape('' if value is None else str(value), quote=True)
logger = logging.getLogger('zakupay.tiles')


def page(body, title='Керамогранит'):
    return HTMLResponse("<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{esc(title)}</title><style>body{{font:16px Arial;margin:0;background:#f4f6f8;color:#202124}}main{{max-width:1200px;margin:auto;padding:24px}}"
        "section,article{background:white;border-radius:12px;padding:18px;margin:16px 0}label{display:block;margin:10px 0;font-size:14px}input,select{padding:9px;box-sizing:border-box;width:100%;margin-top:5px;border:1px solid #ccc;border-radius:6px}input[type=checkbox]{width:auto}"
        ".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}.products{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}.product{border:1px solid #ddd;border-radius:8px;padding:12px}img{width:160px;height:140px;object-fit:contain;max-width:100%}"
        "a{color:#174ea6}button,.button{display:inline-block;background:#1a73e8;color:#fff;border:0;padding:11px 15px;border-radius:7px;text-decoration:none;cursor:pointer;margin:5px 0}.error{color:#b3261e}.notice{background:#fff3d4;padding:12px;border-radius:7px}.muted{color:#5f6368}.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:9px;border-bottom:1px solid #ddd;vertical-align:top}"
        "</style><main><a href='/dashboard/automation'>← Заявки</a> · <a href='/dashboard/tiles'>Керамогранит</a> · <a href='/dashboard/tiles/sellers'>Наши фирмы</a>"
        + body + '</main></html>', headers={'Cache-Control': 'no-store', 'Content-Security-Policy': "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"})


def field(name, label, value='', kind='text'):
    step = " step='any' min='0'" if kind == 'number' else ''
    return f"<label>{esc(label)}<input type='{kind}' name='{esc(name)}' value='{esc(value)}'{step}></label>"


def select(name, label, options, value=''):
    return f"<label>{esc(label)}<select name='{esc(name)}'>" + ''.join(f"<option value='{esc(key)}' {'selected' if str(key)==str(value) else ''}>{esc(label)}</option>" for key,label in options) + '</select></label>'


def checkbox(name, label, checked=False):
    return f"<label><input type='checkbox' name='{esc(name)}' value='yes' {'checked' if checked else ''}> {esc(label)}</label>"


def order_for_job(job_id):
    with database() as conn:
        row = execute(conn, 'SELECT order_json,result_json FROM automation_jobs WHERE id=?', (job_id,)).fetchone()
    if not row or not row['order_json']:
        raise HTTPException(404, 'Не найдена сохранённая заявка')
    order = json.loads(row['order_json'])
    result = json.loads(row['result_json'] or '{}')
    if result.get('customer'):
        order['customer'] = result['customer']
    return order


def required_catalog(catalog_id=None):
    catalog = tiles.load_catalog(catalog_id)
    if not catalog: raise HTTPException(409, 'Сначала загрузите каталог керамогранита')
    return catalog


def required_quote(quote_id):
    quote = tiles.load_quote(quote_id)
    if not quote: raise HTTPException(404, 'Черновик не найден')
    return quote


def photo(catalog_id, product):
    return f"<img loading='lazy' alt='{esc(product['name'])}' src='{URL}/catalogs/{catalog_id}/images/{esc(product['id'])}'>" if product.get('image') else "<p class='muted'>Однозначного фото нет</p>"


def product_card(catalog_id, product, check=None):
    details = ' · '.join(str(product.get(k) or 'не указано') for k in ('format','color','pattern','surface','thickness_mm'))
    stock = 'неизвестно' if product.get('stock_m2') is None else f"{product['stock_m2']:g} м² по снимку"
    price = 'не заполнена' if product.get('price_rub_m2') is None else f"{product['price_rub_m2']:g} ₽/м²"
    match = '' if check is None else f"<p>{esc('; '.join(check['reasons']) or 'Характеристики требуют уточнения')}</p><p class='muted'>{esc('; '.join(check['unknown']))}</p>"
    return f"<div class='product'>{photo(catalog_id,product)}<h3>{esc(product['name'])}</h3><p>{esc(details)}</p><p>Наличие: {esc(stock)}</p><p>Цена в каталоге: {esc(price)} · НДС источника: {esc(product.get('vat') or 'не указан')}</p>{match}</div>"


def calculator_link(snapshot, catalog):
    payload = {'v': 1, 'address': snapshot['address'], 'palletKg': 30,
        'items': [[l['product']['id'], l['quantity'] or l['required_m2']*(1+l['reserve_pct']/100), l['product'].get('stock_m2'), l['proposed_unit_price'],
                   l['box_m2_used'], l['product'].get('kg_m2_confirmed'), l['kg_m2_used'],
                   l['product'].get('pallet_m2'), l['pallet_m2_used'], l['product'].get('vat')] for l in snapshot['lines']],
        'rates': [[r['vehicle'], r['capacity_kg'],r['low'],r['mid'],r['high'],r['km_low'],r['km_mid'],r['km_high'],r.get('center') or 0] for r in catalog['data'].get('shipping',{}).get('tariffs',[])]}
    # Same hash contract as the existing working Google Sheet; no OAuth in URLs.
    return CALCULATOR_URL + '#' + urlencode({'sheet': json.dumps(payload,ensure_ascii=False,separators=(',',':'))})


def quote_form(quote):
    s = quote['snapshot']; values = s['inputs']; catalog = required_catalog(quote['catalog_id'])
    rows = []
    for line in s['lines']:
        i = line['position']; p = line['product']
        hidden = f"<input type='hidden' name='product_{i}' value='{esc(p['id'])}'><input type='hidden' name='features_{i}' value='{esc(values.get(f'features_{i}'))}'>"
        controls = field(f'area_{i}','Требуется, м²',line['required_m2'],'number') + field(f'reserve_{i}','Запас, %',line['reserve_pct'],'number')
        controls += field(f'box_{i}','м² в коробке',line['box_m2_used'],'number') + field(f'kg_{i}','кг/м² — подтвердить',line['kg_m2_used'],'number')
        controls += field(f'pallet_{i}','м² на паллете — подтвердить',line['pallet_m2_used'],'number') + field(f'price_{i}','Наша продажная цена за м², включая выбранный НДС',line['proposed_unit_price'],'number')
        rows.append(f"<section><h2>Позиция {i}: {esc(line['requested_name'])}</h2>{photo(catalog['id'],p)}<p>Предлагаем: <b>{esc(p['name'])} · {esc(p.get('format'))}</b></p><p>Цена в каталоге: {esc(p.get('price_rub_m2') if p.get('price_rub_m2') is not None else 'не заполнена')} ₽/м². Продажную цену укажите отдельно.</p>{hidden}<div class='grid'>{controls}</div>"
            f"<p>К отгрузке: {esc(line['quantity'])} м² · коробок: {esc(line['boxes'])} · паллет, оценка: {esc(line['pallets'])} · вес: {esc(line['weight_kg'])} кг</p>"
            + checkbox(f'approve_{i}','Я проверил аналог, упаковку, количество и актуальное наличие',line['approved']) + '</section>')
    profiles = tiles.sellers()
    firm = select('seller','От какой нашей фирмы выставить счёт',[('', 'Выберите фирму')]+[(k,p['name']+' · ИНН '+p['inn']) for k,p in profiles.items()],values.get('seller',''))
    controls = firm + select('vat','НДС нашей фирмы',[('', 'Выберите'),('22','22%'),('20','20%'),('10','10%'),('0','Без НДС')],values.get('vat',''))
    controls += field('invoice_number','Номер счёта',s['invoice_number']) + field('address','Адрес доставки из заявки',s['address'])
    customer = normalize_customer(s['customer'])
    buyer = field('customer_name','Покупатель: название',customer['name']) + field('customer_inn','ИНН покупателя',customer['inn'])
    buyer += field('customer_kpp','КПП покупателя (для ИП пусто)',customer['kpp']) + field('customer_address','Юридический адрес покупателя',customer['legalAddress'])
    controls += select('zone','Зона доставки',[('', 'Уточните по адресу'),('mkad','Москва внутри МКАД'),('ttk','Москва внутри ТТК'),('outside','Московская область за МКАД')],values.get('zone',''))
    controls += field('km','Км за МКАД по маршруту, в одну сторону',values.get('km',''),'number')
    controls += field('unloading','Согласованная разгрузка и подъём, ₽; 0 — силами клиента',values.get('unloading',''),'number')
    controls += select('vehicle','Машина',[('', 'Предварительный выбор по весу и цене')]+[(t['vehicle'],t['vehicle']) for t in catalog['data'].get('shipping',{}).get('tariffs',[])],values.get('vehicle',''))
    controls += field('sale_delivery','Доставка для клиента, ₽ (пусто — средняя вверх до 500 ₽)',values.get('sale_delivery',''),'number')
    issues = '<ul class="error">'+''.join(f'<li>{esc(x)}</li>' for x in s['issues'])+'</ul>' if s['issues'] else '<p>Параметры подтверждены. Комплект для клиента доступен ниже.</p>'
    shipping = s['shipping']
    calculation = f"<p>Доставка, оценка: {shipping['low']:,.2f}–{shipping['high']:,.2f} ₽; средняя {shipping['mid']:,.2f} ₽. {esc(shipping['vehicle'])}, рейсов: {shipping['trips']}.</p>" if shipping else '<p>Доставка пока не рассчитана.</p>'
    downloads = f"<a class='button' href='{URL}/quotes/{quote['id']}/bundle'>Скачать комплект для клиента</a>" if not s['issues'] else '<p>Заполните недостающие параметры, чтобы сформировать счёт и комплект для клиента.</p>'
    total = f"{s['total']:,.2f} ₽" if s['total'] is not None else 'Нужны цены и доставка'
    return page(f"<h1>Подготовка предложения № {quote['id']}</h1><a href='/dashboard/automation/jobs/{quote['job_id']}/tiles'>← Изменить подбор</a>"
        f"<p class='notice'>Предлагается аналог: заказчик должен согласовать цвет и дизайн по фото. Снимок остатков: {esc(s['stock_as_of'])}. Вес и число паллет требуют проверки. Отправка в Закупай здесь не выполняется.</p>{issues}"
        f"<form method='post' action='{URL}/quotes/{quote['id']}/revise'>{''.join(rows)}<section><h2>Покупатель</h2><div class='grid'>{buyer}</div></section><section><h2>Доставка и наша фирма</h2><div class='grid'>{controls}</div>"
        + checkbox('seller_confirmed','Я проверил реквизиты выбранной нашей фирмы',values.get('seller_confirmed')=='yes')
        + checkbox('transport_confirmed','Я подтвердил массу, паллетоместа, разгрузку, маршрут и итоговую цену с перевозчиком',values.get('transport_confirmed')=='yes')
        + f"<button>Пересчитать и сохранить версию предложения</button></section></form><section><h2>Итого: {esc(total)}</h2>{calculation}"
        f"<p><a target='_blank' rel='noopener' href='{esc(calculator_link(s,catalog))}'>Открыть позиции и адрес в существующем калькуляторе</a></p>"
        f"{downloads} <a href='{URL}/quotes/{quote['id']}/calculation'>Скачать внутренний расчёт</a><p>Комплект для клиента содержит счёт, описание аналога и имеющиеся фотографии. Закупочные цены и внутренний расчёт в него не попадают.</p></section>")


def customer_html(s):
    firm = s['seller']; rows=[]
    for line in s['lines']:
        p=line['product']; image=f"<img width='220' src='photos/{esc(p['id'])}.jpg' alt='{esc(p['name'])}'>" if p.get('image') else '<p>Фото не представлено</p>'
        rows.append(f"<h2>{esc(line['requested_name'])}</h2><p>Предлагаемый аналог: <b>{esc(p['name'])} · {esc(p.get('format'))}</b></p>{image}"
            f"<p>{esc(p.get('color'))} · {esc(p.get('pattern'))} · {esc(p.get('surface'))} · толщина {esc(p.get('thickness_mm'))} мм</p>"
            f"<p>Количество к отгрузке: {line['quantity']:g} м²; цена: {line['proposed_unit_price']:.2f} ₽/м²; сумма: {line['amount']:.2f} ₽.</p>")
    return (f"<!doctype html><html lang='ru'><meta charset='utf-8'><title>Предложение</title><body><h1>Предложение по заявке № {esc(s['order_id'])}</h1><p>{esc(firm['name'])} · ИНН {esc(firm['inn'])}</p>"
        + ''.join(rows) + f"<p>Адрес: {esc(s['address'])}</p><p>Доставка: {s['delivery_price']:.2f} ₽. Всего: {s['total']:.2f} ₽.</p>"
        + (f"<p>В том числе НДС {s['vat_rate']*100:g}%.</p>" if s['vat_rate'] else '<p>Без НДС.</p>')
        + '<p>Просим согласовать предлагаемый аналог, цвет и дизайн по фотографиям. Актуальное наличие и срок поставки подтверждаются при согласовании заказа.</p></body></html>')


def bundle(quote):
    s=quote['snapshot']; draft=tiles.invoice_draft(s)
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('invoice.xlsx',build_invoice_xlsx(draft))
        z.writestr('proposal.html',customer_html(s).encode())
        for key in {l['product']['id'] for l in s['lines'] if l['product'].get('image')}:
            image=tiles.catalogue_image(quote['catalog_id'],key)
            if not image: raise ValueError('Не найдено фото выбранного товара')
            z.writestr('photos/'+key+'.jpg',image)
    return output.getvalue()


def install_porcelain(app, fetch_all_orders=None):
    @app.get(URL)
    def tile_index(q: str=''):
        catalog=tiles.load_catalog()
        with database() as conn:
            rows=execute(conn,'SELECT id,order_id,order_json FROM automation_jobs WHERE order_json IS NOT NULL ORDER BY id DESC LIMIT 500').fetchall()
        jobs=[]
        for row in rows:
            order=json.loads(row['order_json'] or '{}')
            if any(tiles.is_tile(item.get('goodName') or item.get('name')) for item in order.get('orderItems',[])):
                jobs.append(f"<li><a href='/dashboard/automation/jobs/{row['id']}/tiles'>№ {row['order_id']} — {esc(order.get('name'))}</a></li>")
        found=tiles.find_analogues(catalog['data'],q,200) if catalog else []
        cards=''.join(product_card(catalog['id'],r['product'],r if q else None) for r in found)
        warnings=''.join(f"<li>{esc(message)}</li>" for message in catalog['data'].get('workbook_import_warnings',[])) if catalog else ''
        return page("<h1>Керамогранит: заявки и наши аналоги</h1><section>"
            f"<form method='post' action='{URL}/sync'><button>Получить заявки с керамогранитом из Закупай</button></form>"
            "<p>Подбор запускается после открытия заявки. Ни счёт, ни сообщение автоматически не отправляются.</p>"
            f"<h2>Заявки с керамогранитом среди последних 500 сохранённых</h2><ul>{''.join(jobs) or '<li>Пока нет подходящих заявок</li>'}</ul></section>"
            f"<section><h2>Каталог: {len(catalog['data']['products']) if catalog else 0} позиций</h2>{'<ul class=notice>'+warnings+'</ul>' if warnings else ''}<form method='get'>{field('q','Поиск по характеристикам: цвет, рисунок, формат, поверхность',q)}<button>Подобрать аналоги</button></form>"
            f"<p class='notice'>Цены, НДС, наличие, упаковка и соответствие аналога проверяются перед счётом. Доставка по исходным тарифам — только Москва и МО.</p><div class='products'>{cards}</div></section>"
            f"<section><h2>Обновление исходных данных</h2><p><a href='{SOURCE_FOLDER}' target='_blank' rel='noopener'>Папка проекта</a> · <a href='{WORKBOOK_URL}' target='_blank' rel='noopener'>Рабочая Google Таблица</a></p>"
            f"<form method='post' enctype='multipart/form-data' action='{URL}/import'><label>ZIP с catalog.json и фото или XLSX рабочей таблицы<input type='file' name='file' accept='.zip,.xlsx' required></label><button>Импортировать новую версию</button></form>"
            "<p>Google Таблица используется как источник. После её изменения загрузите обновлённый XLSX; фоновой синхронизации пока нет. Сохранённые предложения сохраняют свои версии цен, характеристик и фото.</p></section>")

    @app.post(URL+'/import')
    async def tile_import(request: Request):
        form=await request.form(); upload=form.get('file')
        if not upload or not hasattr(upload,'read'): raise HTTPException(400,'Выберите файл')
        content=await upload.read(tiles.MAX_IMPORT+1)
        try:
            await run_in_threadpool(tiles.import_workbook if str(upload.filename).lower().endswith('.xlsx') else tiles.import_catalog,content)
        except ValueError as exc: raise HTTPException(400,str(exc))
        return RedirectResponse(URL,303)

    @app.post(URL+'/sync')
    def tile_sync():
        if fetch_all_orders is None: raise HTTPException(409,'Не подключено получение заявок')
        from automation_pipeline import save_api_order_for_manual_start
        orders=fetch_all_orders(force=True)
        for order in orders:
            if any(tiles.is_tile(item.get('goodName') or item.get('name')) for item in order.get('orderItems',[])):
                save_api_order_for_manual_start(order)
        return RedirectResponse(URL,303)

    @app.get('/dashboard/automation/jobs/{job_id}/tiles')
    def tile_job(job_id: int, request: Request):
        catalog=required_catalog(); order=order_for_job(job_id); rows=[]
        with database() as conn:
            previous=execute(conn,'SELECT id,created_at FROM tile_quotes WHERE job_id=? ORDER BY id DESC LIMIT 30',(job_id,)).fetchall()
        history=''.join(f"<li><a href='{URL}/quotes/{r['id']}'>Предложение № {r['id']} — {esc(r['created_at'])}</a></li>" for r in previous)
        for i,item in enumerate(order.get('orderItems',[]),1):
            name=item.get('goodName') or item.get('name') or ''
            if not tiles.is_tile(name): continue
            features=request.query_params.get(f'features_{i}','')
            found=tiles.find_analogues(catalog['data'],name+' '+features)
            options=[('', 'Не включать позицию')]+[(r['product']['id'],r['product']['name']+' · '+str(r['product'].get('format') or '')) for r in found]
            unit=item.get('unit') or {}; unit=unit.get('name') if isinstance(unit,dict) else unit
            area=item.get('count') if norm(str(unit).replace('²','2')) in {'м2','кв м','м кв','квадратный метр'} else ''
            rows.append(f"<section><h2>{i}. {esc(name)}</h2><p>Количество в заявке: {esc(item.get('count'))} {esc(unit)}</p>"
                + field(f'features_{i}','Уточнить цвет / рисунок / характеристики',features)
                + f"<button type='submit' formmethod='get' formaction='/dashboard/automation/jobs/{job_id}/tiles'>Обновить варианты</button>"
                + select(f'product_{i}','Наш предлагаемый аналог',options,request.query_params.get(f'product_{i}',''))
                + field(f'area_{i}','Требуемая площадь, м² (если в заявке штуки/коробки — укажите пересчёт)',request.query_params.get(f'area_{i}',area),'number')
                + "<div class='products'>"+''.join(product_card(catalog['id'],r['product'],r) for r in found)+"</div></section>")
        return page(f"<h1>Подбор аналога к заявке № {esc(order.get('id'))}</h1><p>Адрес доставки: {esc(tiles.delivery_address(order) or 'в заявке не указан')}</p>"
            + (f"<section><h2>Сохранённые предложения по заявке</h2><ul>{history}</ul></section>" if history else '')
            + f"<form method='post' action='/dashboard/automation/jobs/{job_id}/tiles/prepare'><input type='hidden' name='catalog_id' value='{catalog['id']}'>"
            + ''.join(rows) + ("<button>Выбрать аналоги и подготовить расчёт</button>" if rows else '<p>В сохранённых позициях керамогранит не найден.</p>')+'</form>')

    @app.post('/dashboard/automation/jobs/{job_id}/tiles/prepare')
    async def tile_prepare(job_id: int,request: Request):
        form=dict(await request.form()); order=await run_in_threadpool(order_for_job,job_id)
        form.setdefault('address',tiles.delivery_address(order))
        try:
            catalog=await run_in_threadpool(required_catalog,int(form.get('catalog_id') or 0))
            snapshot=await run_in_threadpool(tiles.make_quote,order,catalog,form)
            key=await run_in_threadpool(tiles.save_quote,job_id,catalog,snapshot)
        except ValueError as exc: raise HTTPException(400,str(exc))
        return RedirectResponse(f'{URL}/quotes/{key}',303)

    @app.get(URL+'/quotes/{quote_id}')
    def tile_quote(quote_id: int): return quote_form(required_quote(quote_id))

    @app.post(URL+'/quotes/{quote_id}/revise')
    async def tile_revise(quote_id: int, request: Request):
        quote=await run_in_threadpool(required_quote,quote_id); form=dict(await request.form())
        try:
            catalog=await run_in_threadpool(required_catalog,quote['catalog_id'])
            snapshot=await run_in_threadpool(tiles.make_quote,quote['snapshot']['order_snapshot'],catalog,form)
            key=await run_in_threadpool(tiles.save_quote,quote['job_id'],catalog,snapshot)
        except ValueError as exc: raise HTTPException(400,str(exc))
        return RedirectResponse(f'{URL}/quotes/{key}',303)

    @app.get(URL+'/quotes/{quote_id}/calculation')
    def tile_calculation(quote_id: int):
        quote=required_quote(quote_id)
        return Response(json.dumps(quote['snapshot'],ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="internal-calculation-{quote_id}.json"','Cache-Control':'no-store'})

    @app.get(URL+'/quotes/{quote_id}/bundle')
    def tile_bundle(quote_id: int):
        try: content=bundle(required_quote(quote_id))
        except ValueError as exc: raise HTTPException(409,str(exc))
        return Response(content,media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="proposal-{quote_id}.zip"','Cache-Control':'no-store'})

    @app.get(URL+'/catalogs/{catalog_id}/images/{product_id}')
    def tile_image(catalog_id: int,product_id: str):
        image=tiles.catalogue_image(catalog_id,product_id)
        if not image: raise HTTPException(404,'Фото не найдено')
        return Response(image,media_type='image/png' if image.startswith(b'\x89PNG') else 'image/jpeg',headers={'Cache-Control':'private, max-age=86400'})

    @app.get(URL+'/sellers')
    def tile_sellers():
        names=''.join(f"<li>{esc(p['name'])} · ИНН {esc(p['inn'])} · р/с {esc(p['account'])}</li>" for p in tiles.sellers().values())
        labels={'name':'Название нашей фирмы / ИП','inn':'ИНН','kpp':'КПП (для ИП пусто)','ogrn':'ОГРН / ОГРНИП','address':'Юридический адрес','bank':'Банк','bik':'БИК','account':'Расчётный счёт','corr_account':'Корреспондентский счёт','director':'Руководитель / ИП'}
        return page(f"<h1>Наши фирмы для счёта</h1><ul>{names}</ul><section><h2>Добавить реквизиты своей фирмы</h2><form method='post'><div class='grid'>"+''.join(field(k,v) for k,v in labels.items())+"</div><button>Сохранить фирму для выбора</button></form></section>")

    @app.post(URL+'/sellers')
    async def tile_save_seller(request: Request):
        try: await run_in_threadpool(tiles.save_seller,dict(await request.form()))
        except ValueError as exc: raise HTTPException(400,str(exc))
        return RedirectResponse(URL+'/sellers',303)

    def bootstrap():
        try:
            if tiles.load_catalog(): return
            url=os.getenv('KERAMAHALL_BOOTSTRAP_URL','')
            parsed=urlsplit(url)
            if parsed.scheme!='https' or not (parsed.hostname or '').endswith('.oaiusercontent.com'): return
            with requests.get(url,stream=True,allow_redirects=False,timeout=25) as response:
                if response.status_code!=200: raise ValueError('source unavailable')
                data=bytearray()
                for chunk in response.iter_content(65536):
                    data.extend(chunk)
                    if len(data)>tiles.MAX_IMPORT: raise ValueError('source too large')
            tiles.import_catalog(bytes(data))
            logger.warning('Porcelain catalogue imported from configured source')
        except Exception:
            logger.warning('Porcelain catalogue import unavailable; use private upload form')

    if os.getenv('KERAMAHALL_BOOTSTRAP_URL'):
        app.router.on_startup.append(lambda: threading.Thread(target=bootstrap,name='porcelain-import',daemon=True).start())
