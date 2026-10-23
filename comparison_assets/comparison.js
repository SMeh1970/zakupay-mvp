/* No supplier calls, polling, or mail. Each deliberate decision is saved once. */
(() => {
  'use strict';
  const initial = JSON.parse(document.getElementById('comparison-data').textContent);
  let data = initial, busy = false;
  const hidden = new Set(), open = new Set(), inspected = new Map();
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
  function pair(row, offer, feature = null, field = 'quantity') {
    const wrap = element('span', 'decision-buttons');
    const state = feature ? feature.state : field === 'unit' ? offer.unit_state : offer.quantity_state;
    for (const [symbol, decision, label] of [['✓','accepted','Принять'],['✗','rejected','Отклонить']]) {
      const title = feature ? `${label}: ${feature.field} — ${feature.value}, ${feature.offer_specific ? 'только у этого предложения' : 'у всех поставщиков позиции ' + row.position}` : `${label} ${field === 'unit' ? 'единицу' : 'такое же количество'} у всех поставщиков позиции ${row.position}`;
      const payload = feature ? {action:'feature',position:row.position,feature:feature.id,decision} : {action:field,position:row.position,key:offer.key,decision,scope:'row_value'};
      const b = button(symbol, title, () => save(payload), 'decision ' + decision + (state === decision ? ' active' : ''));
      b.setAttribute('aria-pressed', state === decision ? 'true':'false');
      wrap.append(b);
    }
    if (['accepted','rejected'].includes(state)) {
      wrap.append(button('↺','Сбросить решение',() => save(feature ? {action:'feature',position:row.position,feature:feature.id,decision:'pending'} : {action:field,position:row.position,key:offer.key,decision:'pending',scope:'row_value'}),'decision reset'));
    }
    return wrap;
  }
  function currentRows() {
    return data.rows.map(row => {
      const eligible = row.offers.filter(o => o.eligible);
      const metric = o => o.total;
      eligible.sort((a,b) => metric(a) - metric(b) || a.key.localeCompare(b.key));
      return {...row, best:eligible[0], chosen:row.offers.find(o => o.selected && o.eligible)};
    });
  }
  function renderSummary(rows) {
    const minimum = rows.reduce((s,r) => s + (r.best?.total || 0),0);
    const selected = rows.reduce((s,r) => s + (r.chosen?.total || 0),0);
    const partial = rows.filter(r => r.best && r.best.shortage > 0).length;
    const cards = [
      ['Минимум по проверенным позициям',money(minimum) + ' ₽', partial ? `${partial} поз. с недопоставкой — принято вами` : 'Без непроверенных предложений и доставки'],
      ['Сумма выбранных позиций (вручную)',money(selected) + ' ₽',`${rows.filter(r=>r.chosen).length} из ${rows.length} позиций`],
      ['Позиций учтено в итоге',`${rows.filter(r=>r.best).length} / ${rows.length}`,'Остальные требуют решения'],
      ['Активных поставщиков',`${data.suppliers.filter(s=>s.enabled).length} / ${data.suppliers.length}`,'Скрытые столбцы участвуют в расчёте']
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
  function inspect(row, offer) {
    inspected.get(row.position) === offer.key ? inspected.delete(row.position) : inspected.set(row.position,offer.key);
    render();
  }
  function featureChips(row, offer) {
    const wrap=element('div','name-diff-text'), text=offer.name || '';
    const features=offer.features.filter(f=>f.differs || f.state==='pending' || f.state==='rejected');
    const ranges=[], placed=new Set();
    const normalized=v=>String(v).toLowerCase().replace(/ё/g,'е').replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu,'');
    for(const f of features){
      if(f.evidence && text.includes(f.evidence)){
        const start=text.indexOf(f.evidence);ranges.push({start,end:start+f.evidence.length,features:[f]});placed.add(f.id);
      }else if(f.field==='Слово в наименовании'){
        for(const match of text.matchAll(/\S+/g))if(normalized(match[0])===normalized(f.value)){
          ranges.push({start:match.index,end:match.index+match[0].length,features:[f]});placed.add(f.id);
        }
      }
    }
    ranges.sort((a,b)=>a.start-b.start||b.end-a.end);
    const groups=[];
    for(const range of ranges){const last=groups.at(-1);if(last&&range.start<last.end){last.end=Math.max(last.end,range.end);for(const f of range.features)if(!last.features.some(x=>x.id===f.id))last.features.push(f);}else groups.push({...range,features:[...range.features]});}
    const add=(label,features)=>{
      const group=element('span','word-unit'), state=features.some(f=>f.state==='rejected')?'rejected':features.some(f=>f.state==='pending')?'pending':'accepted';
      group.append(element('span','word-chip word-'+state,label));
      for(const f of features){const controls=element('span','feature-control');controls.title=f.field+': '+f.value+(f.required?.length?' · в заявке: '+f.required.join(', '):'');if(features.length>1)controls.append(element('small','',f.field+' '));controls.append(pair(row,offer,f));group.append(controls);}
      wrap.append(group);
    };
    let cursor=0;for(const range of groups){wrap.append(document.createTextNode(text.slice(cursor,range.start)));add(text.slice(range.start,range.end),range.features);cursor=range.end;}wrap.append(document.createTextNode(text.slice(cursor)));
    for(const f of features.filter(f=>!placed.has(f.id)))add(f.field+': '+f.value,[f]);
    return wrap;
  }
  function isExcluded(offer) {
    return !offer.enabled || offer.features.some(f=>f.state==='rejected') || offer.quantity_state==='rejected' || offer.unit_state==='rejected' || offer.reasons.includes('Поставщик выключен');
  }
  function hasPrice(offer) {return Number.isFinite(offer.price) && offer.price>0;}
  function offerStatus(offer) {
    if(isExcluded(offer))return {kind:'excluded',text:offer.reasons.includes('Поставщик выключен')?'Поставщик выключен':'Исключено'};
    if(!hasPrice(offer))return {kind:'missing',text:'Нет цены'};
    if(!offer.pricing_ready)return {kind:'pending',text:'Проверить цену и НДС'};
    if(!offer.factor)return {kind:'pending',text:'Нужен пересчёт единиц'};
    if(offer.quantity_unconfirmed || offer.quantity_state==='pending'&&offer.quantity_differs)return {kind:'pending',text:'Проверить количество'};
    const pending=offer.features.filter(f=>f.state==='pending').length;
    if(pending)return {kind:'pending',text:'Характеристики: '+pending};
    return offer.eligible ? {kind:'ready',text:offer.selected?'Выбрано для закупки':'Допущено'} : {kind:'pending',text:'Требует проверки'};
  }
  function rankOffers(offers) {
    return [...offers].sort((a,b)=>Number(isExcluded(a))-Number(isExcluded(b)) || Number(b.eligible)-Number(a.eligible) || Number(hasPrice(b))-Number(hasPrice(a)) || a.features.filter(f=>f.state==='pending').length-b.features.filter(f=>f.state==='pending').length || (a.total??Infinity)-(b.total??Infinity) || a.key.localeCompare(b.key));
  }
  function representative(row, offers) {
    return offers.find(o=>o.selected&&o.eligible) || offers.find(o=>o.key===inspected.get(row.position)) || rankOffers(offers)[0];
  }
  function offerAmount(offer) {
    if(!hasPrice(offer))return 'Нет цены';
    if(!offer.pricing_ready)return money(offer.raw_price??offer.price)+' / '+offer.unit;
    if(data.mode==='price')return offer.unit_price!=null?money(offer.unit_price):'Нужен пересчёт';
    return offer.total!=null?money(offer.total):'Не задано количество';
  }
  function compactOffer(row,offer) {
    const status=offerStatus(offer),wrap=element('div','cell-stack '+status.kind+(row.best?.key===offer.key?' is-min':''));
    const line=element('div','cell-inner');
    const cb=element('input');cb.type='checkbox';cb.checked=offer.enabled&&hasPrice(offer);cb.disabled=!!data.read_only||!hasPrice(offer);
    cb.title=hasPrice(offer)?'Разрешить использование после проверки условий':'Без цены предложение не участвует в расчёте';
    cb.setAttribute('aria-label','Участвует: '+offer.supplier+', позиция '+row.position);
    cb.addEventListener('change',()=>save({action:'cell',position:row.position,key:offer.key,enabled:cb.checked}));line.append(cb);
    const price=button(offerAmount(offer),'Показать предложение '+offer.supplier+', позиция '+row.position,()=>inspect(row,offer),'sum-val');
    price.disabled=false;price.setAttribute('aria-expanded',inspected.get(row.position)===offer.key?'true':'false');line.append(price);
    const chosen=button(offer.selected&&offer.eligible?'◉':'○','Выбрать '+offer.supplier+' для позиции '+row.position,()=>save({action:'select',position:row.position,key:offer.key}),'choose'+(offer.selected&&offer.eligible?' active':''));
    chosen.disabled=!offer.eligible||!!data.read_only;line.append(chosen);wrap.append(line);
    const name=element('div','candidate-name',offer.name);name.title=offer.name;wrap.append(name);
    const state=element('button','offer-state '+status.kind,status.text);state.type='button';state.title=offer.reasons.join(' · ')||status.text;
    state.setAttribute('aria-label','Причина: '+status.text+', '+offer.supplier+', позиция '+row.position);state.addEventListener('click',()=>inspect(row,offer));wrap.append(state);
    if(offer.quantity_differs||offer.quantity_unconfirmed||offer.unit!==row.unit)wrap.append(element('div','qty-note',qty(offer.quantity)+' '+offer.unit+(offer.quantity_unconfirmed?' · не подтверждено':'')));
    return wrap;
  }
  function showAlternatives(row, supplier, offers) {
    let dialog=$('alternatives-dialog');if(!dialog){dialog=element('dialog','alternatives-dialog');dialog.id='alternatives-dialog';document.body.append(dialog);dialog.addEventListener('click',e=>{if(e.target===dialog)dialog.close();});}
    dialog.replaceChildren();dialog.setAttribute('aria-labelledby','alternatives-title');
    const header=element('header','alternatives-header');const title=element('h2','',supplier+' · позиция '+row.position);title.id='alternatives-title';
    const close=button('Закрыть','Закрыть варианты',()=>dialog.close());close.disabled=false;header.append(title,close);dialog.append(header,element('p','alternatives-request',row.name));
    const list=element('div','alternatives-list');
    rankOffers(offers).forEach((offer,index)=>{
      const status=offerStatus(offer),entry=element('article','alternative '+status.kind);
      entry.append(element('span','alternative-number',String(index+1)),element('strong','alternative-name',offer.name));
      entry.append(element('div','alternative-price',offerAmount(offer)),element('div','alternative-quantity',qty(offer.quantity)+' '+offer.unit));
      entry.append(element('div','alternative-status',status.text+(offer.source_date?' · '+offer.source_date:'')));
      const inspectButton=button('Сравнить','Сравнить вариант '+(index+1),()=>{dialog.close();inspected.set(row.position,offer.key);render();},'inspect-alternative');inspectButton.disabled=false;entry.append(inspectButton);
      if(offer.sku)entry.append(element('small','alternative-source','Артикул: '+offer.sku));
      if(offer.url){const link=element('a','alternative-source','Страница товара ↗');link.href=offer.url;link.target='_blank';link.rel='noopener noreferrer';entry.append(link);}
      if(offer.source_filename)entry.append(element('small','alternative-source',offer.source_filename+' · строка '+(offer.source_row??'—')));
      list.append(entry);
    });
    dialog.append(list);dialog.showModal();
  }
  function render() {
    const rows=currentRows();
    $('order-title').textContent=`${data.source_label} · ${data.order_name || 'Заявка № '+data.order_id} · Снимите галочку с фирмы или цены, чтобы исключить её из сравнения.`;
    for(const radio of document.querySelectorAll('[name=compareMode]')){radio.checked=radio.value===data.mode;radio.disabled=!!data.read_only;}
    $('load-catalog').disabled=!!data.read_only;
    if(data.read_only)notice('Только просмотр: заявка обрабатывается или предложение уже отправлено.');
    renderSummary(rows);
    const visible=data.suppliers.filter(s=>!hidden.has(s.name)), table=$('matrix');
    const scroll=$('matrix').parentElement; const scrollLeft=scroll.scrollLeft,scrollTop=scroll.scrollTop;
    table.replaceChildren();
    // Explicit colgroup prevents the browser distributing spare space into quantity/unit columns.
    const cols=element('colgroup');
    for(const cls of ['name-width','quantity-width','unit-width',...visible.map(()=> 'supplier-width')])cols.append(element('col',cls));
    table.append(cols);table.style.width=((window.matchMedia('(max-width:700px)').matches?240:320)+90+70+visible.length*190)+'px';
    const head=element('thead'), header=element('tr');
    for(const [text,cls]of[['Позиция','item-col'],['Кол-во','qty-col'],['Ед.','unit-col']])header.append(element('th',cls,text));
    for(const supplier of visible){
      const th=element('th','supplier-head'+(!supplier.enabled?' off':''));
      const hide=element('label','hide-toggle-row'),hc=element('input');hc.type='checkbox';hc.setAttribute('aria-label','Скрыть столбец '+supplier.name);
      hc.addEventListener('change',()=>{hidden.add(supplier.name);render();});hide.append(hc,document.createTextNode('скрыть'));th.append(hide);
      const cb=element('input');cb.type='checkbox';cb.checked=supplier.enabled;cb.disabled=!!data.read_only;cb.setAttribute('aria-label','Поставщик '+supplier.name);
      cb.addEventListener('change',()=>save({action:'supplier',supplier:supplier.name,enabled:cb.checked}));
      th.append(cb,element('span','name',supplier.name));header.append(th);
    }
    head.append(header);table.append(head);const body=element('tbody');
    for(const row of rows){
      const tr=element('tr','request-row'),name=element('td','item-col'), q=element('td','qty-col',qty(row.quantity)),u=element('td','unit-col',row.unit);
      name.append(element('span','row-num',row.position),document.createTextNode(row.name));
      const masters=element('span','row-master-btns');
      for(const[symbol,decision,title]of[['✓','accepted','Принять отличия характеристик у всех поставщиков позиции '],['✗','rejected','Отклонить отличия характеристик у всех поставщиков позиции ']])masters.append(button(symbol,title+row.position,()=>save({action:'row_features',position:row.position,decision}),'diff-btn tiny'));
      name.append(masters);
      const active=row.offers.find(o=>o.key===inspected.get(row.position));
      if(active){
        const raw=element('div','raw-sub');raw.append(element('span','who',active.supplier+':'),featureChips(row,active));
        const tools=element('div','inspect-tools');
        const select=button(active.selected&&active.eligible?'✓ Выбрано':'Выбрать для закупки','Выбрать предложение '+active.supplier,()=>save({action:'select',position:row.position,key:active.key}));select.disabled=!active.eligible||!!data.read_only;
        tools.append(select,button('↺','Сбросить решения позиции '+row.position,()=>save({action:'reset',position:row.position})));
        tools.append(button('Скрыть','Скрыть сравнение позиции '+row.position,()=>{inspected.delete(row.position);render();}));raw.append(tools);name.append(raw);
        const rawQty=element('div','raw-sub raw-quantity',qty(active.quantity));
        if(active.quantity_differs&&!active.quantity_unconfirmed)rawQty.append(pair(row,active));
        if(active.converted_quantity!=null&&active.unit!==row.unit)rawQty.append(element('small','required-note','В ед. заявки: '+qty(active.converted_quantity)));
        if(active.shortage>0)rawQty.append(element('small','required-note','Не хватает '+qty(active.shortage)+' '+row.unit));
        if(active.quantity_unconfirmed)rawQty.append(element('small','required-note','Не подтверждено'));q.append(rawQty);
        const rawUnit=element('div','raw-sub',active.unit);if(active.unit_differs)rawUnit.append(pair(row,active,null,'unit'));u.append(rawUnit);
      }
      tr.append(name,q,u);
      for(const supplier of visible){
        const offers=row.offers.filter(o=>o.supplier===supplier.name&&($('show-excluded').checked||!isExcluded(o)));
        const cell=element('td','price-cell'+(!offers.length?' na':'')+(active?.supplier===supplier.name?' inspected':'')+(offers.some(o=>o.selected&&o.eligible)?' chosen':''));
        if(offers.length){
          cell.append(compactOffer(row,representative(row,offers)));
          if(offers.length>1){const more=button('Все варианты: '+offers.length,'Все варианты '+supplier.name+', позиция '+row.position,()=>showAlternatives(row,supplier.name,offers),'more-offers');more.disabled=false;cell.append(more);}
        }else cell.textContent='Нет предложений';tr.append(cell);
      }
      body.append(tr);
      if(active){
        const detailRow=element('tr','offer-inspection'),td=element('td');td.colSpan=3+visible.length;
        const panel=element('details','inspection-panel');const detailKey='advanced:'+row.position;panel.open=open.has(detailKey);
        panel.addEventListener('toggle',()=>panel.open?open.add(detailKey):open.delete(detailKey));
        panel.append(element('summary','inspection-title','Цена, источник и пересчёт · '+active.supplier+(active.reasons.length?' · '+active.reasons.join(' · '):'')));
        const details=offerCard(row,active);details.className='advanced-body';details.querySelector('details').open=true;panel.append(details);td.append(panel);detailRow.append(td);body.append(detailRow);
      }
    }
    if(!rows.length){const tr=element('tr'),td=element('td','','Для этой заявки ещё нет сохранённых позиций. Вернитесь к проверке заявки.');td.colSpan=3+visible.length;tr.append(td);body.append(tr);}
    table.append(body);
    const foot=element('tfoot');
    const footer=(title,cls,fn)=>{const tr=element('tr',cls);tr.append(element('td','item-col',title),element('td'),element('td'));for(const supplier of visible)tr.append(element('td','foot-cell',fn(supplier)));foot.append(tr);};
    footer('Минимумы по проверенным позициям','total-row',supplier=>money(rows.reduce((sum,row)=>sum+(row.best?.supplier===supplier.name?row.best.total:0),0)));
    footer('Сумма у поставщика (допущенные позиции)','own-total-row',supplier=>money(rows.reduce((sum,row)=>{const offers=row.offers.filter(o=>o.supplier===supplier.name&&o.eligible);return sum+(offers.length?Math.min(...offers.map(o=>o.total)):0);},0)));
    footer('Выбрано для закупки','own-total-row',supplier=>money(rows.reduce((sum,row)=>sum+(row.chosen?.supplier===supplier.name?row.chosen.total:0),0)));
    footer('Покрытие заявки','info-row',supplier=>rows.filter(row=>row.offers.some(o=>o.supplier===supplier.name&&o.eligible)).length+' / '+rows.length);
    footer('Доставка','info-row',()=> 'Не подтверждена; в итоги не включена');
    footer('Дата цены','info-row',supplier=>[...new Set(rows.flatMap(row=>row.offers.filter(o=>o.supplier===supplier.name).map(o=>o.source_date).filter(Boolean)))].join(', ')||'Не указана');
    table.append(foot);scroll.scrollLeft=scrollLeft;scroll.scrollTop=scrollTop;
  }
  $('mode').addEventListener('change',e=>{if(e.target.name==='compareMode')save({action:'mode',value:e.target.value});});
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
  window.matchMedia('(max-width:700px)').addEventListener('change',render);
  render();
})();
