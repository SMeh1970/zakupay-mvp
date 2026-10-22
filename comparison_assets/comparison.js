/* No supplier calls, polling, or mail. Each deliberate decision is saved once. */
(() => {
  'use strict';
  const initial = JSON.parse(document.getElementById('comparison-data').textContent);
  let data = initial, busy = false;
  const hidden = new Set(), open = new Set();
  const $ = id => document.getElementById(id);
  const money = n => n == null ? '—' : Number(n).toLocaleString('ru-RU', {minimumFractionDigits:2,maximumFractionDigits:2});
  const qty = n => n == null ? '—' : Number(n).toLocaleString('ru-RU', {maximumFractionDigits:6});
  const element = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };
  function notice(text, error = false) {
    $('message').textContent = text;
    $('message').className = error ? 'notice error' : 'notice';
  }
  async function save(payload) {
    if (busy || data.read_only) return;
    busy = true;
    document.body.classList.add('saving');
    notice('Сохраняется…');
    try {
      const response = await fetch(location.pathname + '/action', {
        method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({...payload, revision:data.revision, csrf:initial.csrf})
      });
      if (response.redirected || !response.headers.get('content-type')?.includes('application/json')) {
        throw new Error('Сессия закончилась. Войдите заново и обновите страницу.');
      }
      const next = await response.json();
      if (!response.ok) throw new Error(next.detail || 'Сохранение не выполнено. Повторите действие.');
      data = {...next, job_id:initial.job_id};
      render();
      notice(payload.action === 'select' ? 'Выбор сохранён и перенесён в проверку заявки.' : payload.action === 'load_catalog' ? 'Подбор из подготовленных прайсов завершён. Проверьте условия и расхождения.' : 'Решение сохранено.');
    } catch (error) {
      render();
      notice(error.message, true);
    } finally {
      busy = false;
      document.body.classList.remove('saving');
    }
  }
  function button(text, title, click, cls = '') {
    const b = element('button', cls, text);
    b.type = 'button'; b.title = title; b.setAttribute('aria-label', title);
    b.disabled = !!data.read_only;
    b.addEventListener('click', click);
    return b;
  }
  function pair(row, offer, feature = null) {
    const wrap = element('span', 'decision-buttons');
    const state = feature ? feature.state : offer.quantity_state;
    for (const [symbol, decision, label] of [['✓','accepted','Принять'],['✗','rejected','Отклонить']]) {
      const title = feature ? `${label}: ${feature.field} — ${feature.value}, ${feature.offer_specific ? 'только у этого предложения' : 'у всех поставщиков позиции ' + row.position}` : `${label} количество только у этого предложения`;
      const payload = feature ? {action:'feature',position:row.position,feature:feature.id,decision} : {action:'quantity',position:row.position,key:offer.key,decision};
      const b = button(symbol, title, () => save(payload), 'decision ' + decision + (state === decision ? ' active' : ''));
      b.setAttribute('aria-pressed', state === decision ? 'true':'false');
      wrap.append(b);
    }
    if (['accepted','rejected'].includes(state)) {
      wrap.append(button('↺','Сбросить решение',() => save(feature ? {action:'feature',position:row.position,feature:feature.id,decision:'pending'} : {action:'quantity',position:row.position,key:offer.key,decision:'pending'}),'decision reset'));
    }
    return wrap;
  }
  function currentRows() {
    return data.rows.map(row => {
      const eligible = row.offers.filter(o => o.eligible);
      const metric = o => data.mode === 'price' ? o.unit_price : o.total;
      eligible.sort((a,b) => metric(a) - metric(b) || a.key.localeCompare(b.key));
      return {...row, best:eligible[0], chosen:row.offers.find(o => o.selected && o.eligible)};
    });
  }
  function renderSummary(rows) {
    const minimum = rows.reduce((s,r) => s + (r.best?.total || 0),0);
    const selected = rows.reduce((s,r) => s + (r.chosen?.total || 0),0);
    const partial = rows.filter(r => r.best && r.best.shortage > 0).length;
    const cards = [
      ['Минимум среди допущенных',money(minimum) + ' ₽', partial ? `${partial} поз. с недопоставкой — принято вами` : 'По сохранённым предложениям'],
      ['Выбрано для закупки',money(selected) + ' ₽',`${rows.filter(r=>r.chosen).length} из ${rows.length} позиций`],
      ['Допущено к сравнению',`${rows.filter(r=>r.best).length} / ${rows.length}`,'Остальные требуют решения'],
      ['Активные поставщики',`${data.suppliers.filter(s=>s.enabled).length} / ${data.suppliers.length}`,'Скрытые столбцы участвуют в расчёте']
    ];
    $('summary').replaceChildren(...cards.map(([label,value,hint]) => {
      const c = element('div','summary-card');
      c.append(element('div','label',label),element('strong','value',value),element('div','hint',hint)); return c;
    }));
  }
  function offerCard(row, offer) {
    const card = element('article','offer' + (!offer.eligible ? ' excluded':'') + (row.best?.key === offer.key ? ' minimum':'') + (row.chosen?.key === offer.key ? ' chosen':''));
    const top = element('div','offer-top');
    const included = element('input'); included.type = 'checkbox'; included.checked = offer.enabled;
    included.disabled = !!data.read_only; included.setAttribute('aria-label','Участвует: ' + offer.supplier + ', позиция ' + row.position);
    included.addEventListener('change',() => save({action:'cell',position:row.position,key:offer.key,enabled:included.checked}));
    const rubles = offer.currency === 'RUB' && offer.pricing_ready;
    top.append(included,element('strong','amount',money(data.mode === 'price' ? offer.unit_price:offer.total) + (rubles ? ' ₽' : '')));
    if (row.best?.key === offer.key) top.append(element('span','best-label','МИНИМУМ'));
    card.append(top);
    card.append(element('div','quote-name',offer.name));
    card.append(element('div','hint',rubles ? `${money(offer.price)} ₽ / ${offer.unit} × ${qty(offer.quantity)} ${offer.unit}` : `Число в документе: ${money(offer.raw_price)} / ${offer.unit}; условия цены требуют подтверждения`));
    const quantity = element('div','quantity ' + offer.quantity_state);
    quantity.append(element('span','',`${offer.quantity_unconfirmed ? 'Расчёт потребности, без подтверждения поставщика' : 'Предложено'}: ${qty(offer.converted_quantity)} ${offer.requested_unit}`));
    if (offer.quantity_differs && !offer.quantity_unconfirmed) {
      quantity.append(pair(row,offer));
      quantity.append(element('small','',offer.shortage > 0 ? `Не хватает ${qty(offer.shortage)} ${offer.requested_unit}` : `Больше заявки на ${qty(offer.converted_quantity - Number(row.quantity))} ${offer.requested_unit}`));
    }
    card.append(quantity);
    if (offer.reasons.length) card.append(element('div','reason',offer.reasons.join(' · ')));
    card.append(element('div','hint',offer.availability + (offer.source_date ? ' · цена от ' + offer.source_date:'')));
    const details = element('details','details');
    const detailKey = row.position + ':' + offer.key;
    details.open = open.has(detailKey);
    details.addEventListener('toggle',() => details.open ? open.add(detailKey) : open.delete(detailKey));
    details.append(element('summary','',`Характеристики и пересчёт (${offer.features.filter(f=>f.state==='pending').length} непроверенных)`));
    if (offer.catalog_prepared) details.append(catalogControls(row,offer));
    const conversion = element('div','conversion');
    conversion.append(element('b','', 'Пересчёт единиц'),element('div','',offer.factor ? `1 ${offer.unit} = ${qty(offer.factor)} ${offer.requested_unit}`:'Коэффициент не задан'),element('small','',offer.factor_source));
    const conversionForm = element('form','factor-form');
    const label = element('label','',`1 ${offer.unit} = `);
    const factor = element('input'); factor.type='number';factor.step='any';factor.min='0.000000001';factor.value=offer.factor || '';factor.required=true;factor.setAttribute('aria-label',`Коэффициент из ${offer.unit} в ${offer.requested_unit}`);
    label.append(factor,document.createTextNode(' ' + offer.requested_unit));
    const apply = element('button','','Применить');apply.type='submit';apply.disabled=!!data.read_only;
    conversionForm.append(label,apply);
    conversionForm.addEventListener('submit',e=>{e.preventDefault();save({action:'conversion',position:row.position,key:offer.key,factor:factor.value});});
    conversion.append(conversionForm); details.append(conversion);
    const features = element('div','features');
    for (const f of offer.features) {
      const line = element('div','feature ' + f.state);
      line.append(element('span','feature-label',f.field),element('span','feature-value',f.value),pair(row,offer,f));
      if (f.ambiguous) line.append(element('small','uncertain','Неизвестное или неоднозначное значение — проверяется отдельно'));
      if (f.evidence) line.append(element('small','evidence',`${f.origin === 'manual' ? 'Ручное исправление' : f.origin === 'parser' ? 'Проверка' : 'Источник'}: «${f.evidence}»`));
      if (f.differs && f.required.length) line.append(element('small','required','В заявке: ' + f.required.join(', ')));
      features.append(line);
    }
    details.append(features);
    if (offer.catalog_prepared) {
      details.append(element('p','hint',`${offer.source_filename || ''} · ${offer.source_sheet || ''} · строка ${offer.source_row ?? '—'}`));
      details.append(element('p','hint',`Остаток в документе: ${qty(offer.document_stock)} ${offer.stock_unit || '(единица неизвестна)'}. ${offer.warehouse || ''} Дата подтверждения остатка неизвестна.`));
      if (offer.email_received_at) details.append(element('p','hint','Письмо получено: ' + offer.email_received_at + ' (не дата подтверждения остатка)'));
      for (const [href,title] of [[offer.product_url,'Открыть карточку / исправить распознавание'],[offer.original_url,'Скачать исходный файл']]) {
        if (href) { const link=element('a','source-link',title);link.href=href;link.target='_blank';link.rel='noopener noreferrer';details.append(link); }
      }
    }
    if (offer.url) {
      const link=element('a','source-link','Страница источника ↗');link.href=offer.url;link.target='_blank';link.rel='noopener noreferrer';details.append(link);
    }
    card.append(details);
    const select = button(offer.selected && offer.eligible ? '✓ Выбрано':'Выбрать для закупки','Перенести это предложение в проверку заявки',()=>save({action:'select',position:row.position,key:offer.key}),'select-offer');
    select.disabled = !offer.eligible || !!data.read_only;
    card.append(select);
    return card;
  }
  function catalogControls(row,offer) {
    const wrap=element('div','catalog-controls');
    wrap.append(element('b','','Подтверждение условий этого предложения'));
    const input=(form,title,name,type,value,required=true)=>{
      const label=element('label','',title);const field=element('input');field.name=name;field.type=type;field.required=required;field.value=value??'';
      if(type==='number'){field.step='any';field.min='0';}
      field.disabled=!!data.read_only;label.append(field);form.append(label);return field;
    };
    const select=(form,title,name,options,value)=>{
      const label=element('label','',title), field=element('select');field.name=name;field.required=true;field.disabled=!!data.read_only;
      for(const [v,t] of options){const option=element('option','',t);option.value=v;field.append(option);}
      field.value=value??'';label.append(field);form.append(label);return field;
    };
    const terms=element('form','terms-form');
    input(terms,'Цена поставщика','price','number',offer.raw_price);
    input(terms,'Единица цены','unit','text',offer.unit === 'не указана' ? '' : offer.unit);
    select(terms,'Валюта расчёта','currency',[['','Подтвердите валюту'],['RUB','RUB — российский рубль']],offer.currency);
    select(terms,'НДС','vat_mode',[['','Выберите'],['included','Включён в указанную цену'],['excluded','Добавить к указанной цене'],['exempt','Поставщик продаёт без НДС']],offer.vat_mode);
    input(terms,'Ставка НДС, %','vat_rate','number',offer.vat_rate);
    input(terms,'Дата подтверждённой цены','price_date','date',offer.source_date);
    input(terms,'Основание подтверждения цены','note','text','');
    const apply=element('button','','Подтвердить цену и условия');apply.type='submit';apply.disabled=!!data.read_only;terms.append(apply);
    terms.addEventListener('submit',e=>{e.preventDefault();save({action:'confirm_terms',position:row.position,key:offer.key,...Object.fromEntries(new FormData(terms))});});
    wrap.append(terms);
    const proposal=element('form','terms-form');
    proposal.append(element('b','','Количество, которое предложил поставщик'));
    input(proposal,`Количество в ${offer.unit}`,'quantity','number',offer.quantity_unconfirmed ? '' : offer.quantity);
    input(proposal,'Основание подтверждения количества','note','text','');
    const confirm=element('button','','Сохранить количество предложения');confirm.type='submit';confirm.disabled=!!data.read_only;proposal.append(confirm);
    proposal.addEventListener('submit',e=>{e.preventDefault();save({action:'confirm_proposal',position:row.position,key:offer.key,...Object.fromEntries(new FormData(proposal))});});
    wrap.append(proposal,element('p','hint','Эти условия относятся к выбранной версии предложения. Принятие недопоставки выполняется отдельно через ✓ у количества.'));
    return wrap;
  }
  function render() {
    const rows = currentRows();
    $('order-title').textContent = `${data.source_label} · ${data.order_name || 'Заявка № ' + data.order_id}`;
    $('mode').value=data.mode;$('mode').disabled=!!data.read_only;
    $('load-catalog').disabled=!!data.read_only;
    if (data.read_only) notice('Сравнение доступно только для просмотра: заявка обрабатывается или предложение уже отправлено.');
    renderSummary(rows);
    const toggles=$('supplier-toggles');toggles.replaceChildren();
    for (const supplier of data.suppliers) {
      const label=element('label','supplier-chip' + (!supplier.enabled ? ' inactive':''));
      const cb=element('input');cb.type='checkbox';cb.checked=supplier.enabled;cb.disabled=!!data.read_only;
      cb.addEventListener('change',()=>save({action:'supplier',supplier:supplier.name,enabled:cb.checked}));label.append(cb,document.createTextNode(supplier.name));toggles.append(label);
    }
    const visible=data.suppliers.filter(s=>!hidden.has(s.name));
    const table=$('matrix');table.replaceChildren();
    const head=element('thead'),header=element('tr');
    for (const [text,cls] of [['Позиция заявки','sticky-name'],['Кол-во','sticky-qty'],['Ед.','sticky-unit']]) header.append(element('th',cls,text));
    for (const supplier of visible) {
      const th=element('th',supplier.enabled ? '':'supplier-off');
      th.append(element('div','supplier-name',supplier.name));
      const hide=button('Скрыть столбец','Скрыть без исключения из расчёта',()=>{hidden.add(supplier.name);render();},'hide-column');hide.disabled=false;
      th.append(hide);header.append(th);
    }
    head.append(header);table.append(head);
    const body=element('tbody');
    for (const row of rows) {
      const tr=element('tr');
      const name=element('td','sticky-name');
      name.append(element('span','row-number',String(row.position).padStart(2,'0')),element('b','',row.name));
      name.append(button('Сбросить решения','Сбросить все решения по этой позиции',()=>save({action:'reset',position:row.position}),'reset-row'));
      name.append(button('Обновить из прайсов','Подобрать подготовленные товары только для этой позиции',()=>save({action:'load_catalog',position:row.position}),'reset-row'));
      if(row.catalog_search) name.append(element('p','hint',`Подбор по прайсам: ${row.catalog_search.count} кандидатов · ${row.catalog_search.seconds} с`));
      tr.append(name,element('td','sticky-qty',qty(row.quantity)),element('td','sticky-unit',row.unit));
      for (const supplier of visible) {
        const cell=element('td','supplier-cell' + (!supplier.enabled ? ' supplier-off':''));
        const quotes=row.offers.filter(o=>o.supplier===supplier.name && ($('show-excluded').checked || o.eligible));
        if (quotes.length) for (const quote of quotes) cell.append(offerCard(row,quote));
        else cell.append(element('span','no-offer','—'));
        tr.append(cell);
      }
      body.append(tr);
    }
    if (!rows.length) { const tr=element('tr');const td=element('td','','Для этой заявки ещё нет результатов подбора. Вернитесь к проверке и выполните поиск.');td.colSpan=3+visible.length;tr.append(td);body.append(tr); }
    table.append(body);
    const foot=element('tfoot');
    for (const [title,selectedOnly] of [['Минимумы поставщика по допущенным позициям',false],['Выбрано у поставщика',true]]) {
      const tr=element('tr');const label=element('th','sticky-name',title);label.colSpan=3;tr.append(label);
      for (const supplier of visible) {
        let total=0, coverage=0;
        for (const row of rows) {
          const offers=row.offers.filter(o=>o.supplier===supplier.name && o.eligible && (!selectedOnly || o.selected));
          if(offers.length){total+=Math.min(...offers.map(o=>o.total));coverage++;}
        }
        const td=element('td');td.append(element('b','',money(total)+' ₽'),element('small','',`${coverage} / ${rows.length} позиций · без отдельной доставки`));tr.append(td);
      }
      foot.append(tr);
    }
    table.append(foot);
  }
  $('mode').addEventListener('change',e=>save({action:'mode',value:e.target.value}));
  $('load-catalog').addEventListener('click',()=>save({action:'load_catalog'}));
  $('show-excluded').addEventListener('change',render);
  $('show-all').addEventListener('click',()=>{hidden.clear();render();});
  $('export').addEventListener('click',()=>{
    const csv=[['№','Заявка','Поставщик','Товар','Количество поставщика','Ед. поставщика','Количество в ед. заявки','Ед. заявки','Цена поставщика','Цена за ед. заявки','Сумма','Допущено','Выбрано','Причины исключения','Число цены в документе','Валюта','НДС','Количество подтверждено','Файл','Лист','Строка','Характеристики и источники']];
    for(const row of currentRows())for(const o of row.offers)csv.push([row.position,row.name,o.supplier,o.name,o.quantity,o.unit,o.converted_quantity,o.requested_unit,o.price,o.unit_price,o.total,o.eligible?'да':'нет',o.selected?'да':'нет',o.reasons.join('; '),o.raw_price,o.currency,o.vat_mode,o.quantity_unconfirmed?'нет':'да',o.source_filename,o.source_sheet,o.source_row,o.features.map(f=>`${f.field}: ${f.value} [${f.state}] ${f.evidence||''}`).join('; ')]);
    const escape = value => '"'+String(value??'').replace(/^[=+@\-]/,"'$&").replaceAll('"','""')+'"';
    const blob=new Blob(['\uFEFF'+csv.map(row=>row.map(escape).join(';')).join('\r\n')],{type:'text/csv;charset=utf-8'});
    const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`comparison-${initial.job_id}.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  render();
})();
