(() => {
  'use strict';
  let data = JSON.parse(document.getElementById('comparison-data').textContent);
  const csrf = data.csrf;
  const embedded = !!data.embedded;
  const actionUrl = data.action_url || location.pathname+'/action';
  let busy = false;
  // ✓ and ✕ only mark a decision on the page (nothing is sent). «Применить» sends every marked
  // decision of every position in one request. A decision belongs to its own position only.
  const draft = new Map();
  const draftKey = (position, id) => position + '\u0000' + id;
  function draftDecision(row, value) {
    const ids=value.ids||[value.id];
    const marks=ids.map(id=>draft.get(draftKey(row.position,id))?.decision);
    return marks.every(mark=>mark && mark===marks[0]) ? marks[0] : null;
  }
  function toggleDraft(row, value, decision) {
    const ids=value.ids||[value.id];
    const current=draftDecision(row,value);
    // Same button again: drop the mark; on an already saved decision it marks the undo.
    const next=current===decision?null:(!current && value.state===decision)?'pending':decision;
    for(const id of ids) {
      if(next===null)draft.delete(draftKey(row.position,id));
      else draft.set(draftKey(row.position,id),{position:row.position,id,decision:next});
    }
    const message=document.getElementById('message');
    if(message)message.textContent=draft.size?'Решение отмечено. Чтобы применить, нажмите «Применить» внизу экрана.':'';
    render();
  }
  let applyBar=null;
  function renderApplyBar() {
    if(data.read_only && !draft.size){applyBar?.remove();applyBar=null;return;}
    if(!applyBar) {
      applyBar=node('div','','value-apply-bar');
      applyBar.style.cssText='position:fixed;left:50%;transform:translateX(-50%);bottom:12px;z-index:50;box-shadow:0 4px 16px #0003;display:flex;gap:10px;align-items:center;padding:10px 14px;margin:8px 0;background:#fff7df;border:1px solid #e5d3a1;border-radius:8px;font:14px Arial';
      const table=document.getElementById('review-table');
      (table?.parentNode||document.body).insertBefore(applyBar, table?.nextSibling||null);
    }
    applyBar.replaceChildren();
    const count=draft.size;
    applyBar.style.display=count?'flex':'none';
    applyBar.append(node('span',`Отмечено решений: ${count}. Они ещё не применены.`));
    const apply=node('button','Применить');apply.type='button';apply.disabled=busy||!count;
    apply.setAttribute('aria-label','Применить отмеченные решения');
    apply.onclick=applyDrafts;
    const cancel=node('button','Отменить отметки');cancel.type='button';cancel.disabled=busy||!count;
    cancel.onclick=()=>{draft.clear();render();};
    applyBar.append(apply,cancel);
  }
  async function applyDrafts() {
    const groups=new Map();
    for(const {position,id,decision} of draft.values()) {
      const key=position+'|'+decision;
      if(!groups.has(key))groups.set(key,{position,decision,features:[]});
      groups.get(key).features.push(id);
    }
    if(!groups.size)return;
    if(await save({action:'values_batch',decisions:[...groups.values()]})){draft.clear();render();}
  }
  window.addEventListener('beforeunload',event=>{if(draft.size){event.preventDefault();event.returnValue='';}});
  const node = (tag, text, cls) => { const n = document.createElement(tag); n.textContent = text ?? ''; if(cls)n.className=cls; return n; };
  const money = value => Number(value).toLocaleString('ru-RU', {maximumFractionDigits:6});
  let activeDialog=null, dialog=null, choiceError='';
  function closeChoices() { if(dialog?.open)dialog.close();activeDialog=null; }
  function showChoices(position,supplier) {
    activeDialog={position,supplier};
    choiceError='';
    if(!dialog) {
      dialog=node('dialog','','supplier-choice-dialog');
      dialog.setAttribute('aria-label','Выбор товара поставщика');
      dialog.addEventListener('close',()=>{activeDialog=null;});
      document.body.append(dialog);
    }
    renderChoices();
    if(!dialog.open)dialog.showModal();
  }
  function renderChoices() {
    if(!activeDialog||!dialog)return;
    const row=data.rows.find(r=>r.position===activeDialog.position);
    if(!row){closeChoices();return;}
    const supplier=activeDialog.supplier;
    dialog.replaceChildren();
    const header=node('div','','supplier-choice-header');header.append(node('h2',supplier));
    const close=node('button','✕');close.type='button';close.setAttribute('aria-label','Закрыть выбор товара');close.onclick=closeChoices;header.append(close);dialog.append(header);
    dialog.append(node('p',`${row.name} · ${money(row.quantity)} ${row.unit} · совпадение основных параметров`,'supplier-choice-subtitle'));
    const search=row.supplier_search?.[supplier];
    if(search) {
      dialog.append(node('p',search.label,'supplier-choice-detail'));
      for(const error of search.errors)dialog.append(node('p',error,'supplier-choice-error'));
      const other=(search.rejected||[]).filter(q=>!row.value_comparison.offers.some(v=>v.key===q.key));
      if(other.length) {
        const details=node('details');details.append(node('summary','Другие результаты и причины недопуска: '+other.length));
        for(const rejected of other) {
          const article=node('article','','supplier-choice-detail');
          article.append(node('p',rejected.name),node('p',rejected.price==null?'Цена не получена':money(rejected.price)+' ₽'),node('p',rejected.reasons.join('; ')));
          if(rejected.url){const link=node('a','Карточка товара');link.href=rejected.url;link.target='_blank';link.rel='noopener noreferrer';article.append(link);}
          details.append(article);
        }
        dialog.append(details);
      }
    }
    const choices=row.value_comparison.offers.filter(v=>v.supplier===supplier).sort((a,b)=>Number(b.comparable)-Number(a.comparable)||(a.unit_price??Infinity)-(b.unit_price??Infinity));
    if(!choices.length && !search)dialog.append(node('p','Нет товаров с разрешёнными характеристиками. Проверьте причины недопуска.'));
    if(!choices.length) {
      const excluded=row.value_comparison.offers.filter(v=>v.supplier===supplier&&!v.comparable);
      const reasons=[...new Set(excluded.flatMap(v=>v.comparison_reasons))];
      if(reasons.length)dialog.append(node('p','Причины: '+reasons.join('; '),'supplier-choice-detail'));
      if(excluded.some(v=>v.comparison_reasons.some(r=>/дней|дата|Дата/.test(r))))dialog.append(node('p','Сохранённые цены требуют подтверждения наличия и стоимости.','supplier-choice-detail'));
    }
    const list=node('div','','supplier-choice-list');
    for(const offer of choices) {
      const line=node('article','','supplier-choice-option'),description=node('div','','supplier-choice-description');description.append(node('div',offer.name,'supplier-choice-name'));
      const priceBasis=offer.factor>1?` · ${money(offer.factor)} ${row.unit} = ${money(offer.price)} ₽ / ${offer.unit}`:'';
      description.append(node('p',offer.unit_price!=null?`${money(offer.unit_price)} ₽ / ${row.unit}${priceBasis}`:offer.price!=null?`${money(offer.price)} ₽ / ${offer.unit} · пересчёт не подтверждён`:'Цена не получена','supplier-choice-price'));
      if(offer.converted_quantity!=null)description.append(node('p',`Для ${money(row.quantity)} ${row.unit}: ${money(offer.quantity)} ${offer.unit} = ${money(offer.converted_quantity)} ${row.unit} · ${money(offer.total)} ₽`,'supplier-choice-detail'));
      if(!offer.comparable)description.append(node('p','Нужно решение: '+offer.comparison_reasons.join('; '),'supplier-choice-detail'));
      description.append(node('p',offer.availability,'supplier-choice-detail'));
      const href=offer.product_url||offer.url;
      if(href){const link=node('a','Карточка товара'+(offer.sku?' · '+offer.sku:''));link.href=href;link.target='_blank';link.rel='noopener noreferrer';description.append(link);}
      if(offer.shortage>0)description.append(node('p',`Не хватает ${money(offer.shortage)} ${row.unit}`));
      const choose=node('button',row.invoice?.key===offer.key&&row.invoice?.included?'Выбрано в счёт':'Выбрать в счёт');choose.type='button';
      // A product with unresolved differences can still be chosen: the button then accepts its
      // differences (like ✓ on each value) and selects it. Shortage or a missing price still block.
      if(!offer.comparable)choose.textContent='Принять отличия и выбрать';
      choose.disabled=busy||data.selection_read_only||offer.shortage>0||!offer.quantity||offer.unit_price==null;
      const selected=row.invoice?.key===offer.key&&row.invoice?.included;
      if(selected){choose.textContent='✓ Выбрано в счёт';line.style.background='#e7f5ec';choose.disabled=true;}
      choose.onclick=()=>save({action:offer.comparable?'value_select':'value_accept_select',position:row.position,key:offer.key});
      line.append(description,choose);list.append(line);
    }
    dialog.append(list,node('p',data.selection_read_only?'Отправленный счёт доступен только для просмотра.':'Выбор применяется к строке счёта. Разрешённые характеристики сохраняются.','supplier-choice-footer'));
    const error=node('p',choiceError,'supplier-choice-error');error.setAttribute('role','alert');dialog.append(error);
  }
  function syncInvoice(position,force=false) {
    if(data.selection_read_only)return;
    const row=data.rows.find(r=>r.position===position), tr=document.querySelector(`[data-value-position="${position}"]`)?.closest('tr');
    if(!row||!tr)return;
    const select=tr.querySelector('.candidate-select'),invoice=row.invoice;
    if(!invoice)return;
    const currentKey=select?.selectedOptions[0]?.dataset.key||null;
    if(!force&&currentKey===(invoice.key||null))return;
    if(select) {
      let option=Array.from(select.options).find(o=>invoice.key?o.dataset.key===invoice.key:o.value==='none');
      if(!option&&invoice.key){option=node('option','Выбран в сравнительной таблице');option.value='current';option.dataset.key=invoice.key;select.append(option);}
      select.value=option?.value||'none';
    }
    const price=tr.querySelector('.price'),quantity=tr.querySelector('.qty'),include=tr.querySelector('input[name^="include_"]');
    if(price)price.value=invoice.key?invoice.price??'':'';
    if(quantity)quantity.value=invoice.quantity??'';
    if(include){include.checked=!!invoice.included;include.dataset.bulkAllowed=invoice.key?'1':'0';include.dispatchEvent(new Event('change',{bubbles:true}));}
  }
  function inlineName(row) {
    const line=node('div','','value-panel');
    line.setAttribute('aria-label','Наименование с выбором значений позиции '+row.position);
    const source=row.name || '', placements=[], used=new Set();
    // Values are offered only from products shown in the supplier columns (the comparable offers);
    // products that were found but rejected or never shown must not add values here.
    const shownKeys=new Set(row.value_comparison.offers.filter(v=>v.comparable).map(v=>v.key));
    const shown=value=>!Array.isArray(value.sources)||value.sources.some(s=>shownKeys.has(s.key));
    const checks=group=>['Слово в наименовании','Нет слова из заявки','Проверка прайса','Проверка категории','Проверка соответствия'].includes(group.field);
    const chip=(group,value,label)=>{
      const marked=draftDecision(row,value);
      const state=marked==='pending'?(value.required||[]).length?'pending':'matched':marked||value.state;
      const allowed=['matched','accepted'].includes(state);
      const neutral=state==='matched' && !(value.required||[]).length;
      const span=node('span','','value-chip '+(neutral?'available':allowed?'matched':state)+(marked?' draft':''));
      if(marked)span.style.outline='1px dashed #b08a2e';
      span.title=group.field+': '+value.value+(value.offer_label?' · '+value.offer_label:'')+(value.evidence?' · '+value.evidence:'');
      span.append(node('span',label??value.value));
      for(const [symbol,decision,label] of [['✓','accepted','Разрешить'],['✕','rejected','Исключить']]) {
        const action=node('button',symbol);action.type='button';action.disabled=data.read_only;
        action.setAttribute('aria-label',label+': '+group.field+' '+value.value);
        action.title=label+': '+group.field+' '+value.value+' (отметить; применится кнопкой «Применить»)';
        action.setAttribute('aria-pressed',String(state===decision));
        action.onclick=()=>toggleDraft(row,value,decision);
        span.append(action);
      }
      if(value.sources?.length) {
        const sourceButton=node('button','ⓘ');sourceButton.type='button';
        sourceButton.setAttribute('aria-label','Источники: '+group.field+' '+value.value);
        sourceButton.title=value.sources.map(s=>s.supplier+(s.sku?' · '+s.sku:'')+': '+s.name).join('\n');
        sourceButton.onclick=()=>showValueSources(group,value);
        span.append(sourceButton);
      }
      return span;
    };
    for(const group of row.value_comparison.groups) {
      const required=[...new Set(group.values.flatMap(v=>v.required||[]))].sort((a,b)=>b.length-a.length);
      for(const candidate of [...(group.request_spans||[]),...required.map(value=>({text:value,value}))]) {
        const feature=group.values.find(v=>v.value===candidate.value);
        if(!feature)continue;
        const raw=candidate.text.replace(/\s+(мм|см|м|кг|г|л|мл|°)$/u,'');
        const escaped=raw.split(/[.,]/).map(part=>part.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('[.,]');
        const match=[...source.matchAll(new RegExp('(?<![\\d])'+escaped+'(?![\\d])','giu'))].find(m=>!placements.some(p=>m.index<p.end && m.index+m[0].length>p.start));
        if(!match)continue;
        if(feature){placements.push({start:match.index,end:match.index+match[0].length,group,feature,text:match[0]});used.add(group);break;}
      }
    }
    let cursor=0;
    for(const p of placements.sort((a,b)=>a.start-b.start)) {
      line.append(document.createTextNode(source.slice(cursor,p.start)),chip(p.group,p.feature,p.text));
      for(const alternative of p.group.values)if(!checks(p.group) && alternative.id!==p.feature.id && !alternative.ambiguous && alternative.value!=='Не указано' && !alternative.offer_label && shown(alternative)) {
        line.append(document.createTextNode(' / '),chip(p.group,alternative,alternative.value.replace(/\s+(мм|см|м|кг|г|л|мл|°)$/u,'')));
      }
      cursor=p.end;
    }
    line.append(document.createTextNode(source.slice(cursor)));
    const extra=node('div','','supplier-found-values');
    for(const group of row.value_comparison.groups) {
      const realShown=group.values.some(v=>v.value!=='Не указано' && Array.isArray(v.sources) && v.sources.some(s=>shownKeys.has(s.key)));
      if(!realShown && group.values.every(v=>Array.isArray(v.sources)))continue;
      const values=group.values.filter(v=>(!used.has(group)||checks(group)||v.ambiguous||v.value==='Не указано'||v.offer_label) && shown(v));
      if(!values.length)continue;
      const block=node('div','','value-group-details');block.append(node('b',group.field+': '));
      const byValue=new Map();
      for(const value of values) {if(!byValue.has(value.value))byValue.set(value.value,[]);byValue.get(value.value).push(value);}
      for(const [label,items] of byValue) {
        if(items.length===1)block.append(chip(group,items[0],label+(items[0].offer_label?' · '+items[0].offer_label:'')));
        else {
          block.append(chip(group,{...items[0],ids:items.map(v=>v.id),sources:items.flatMap(v=>v.sources||[]),offer_label:'',
            state:items.every(v=>['matched','accepted'].includes(v.state))?'accepted':items.every(v=>v.state==='rejected')?'rejected':'pending'},label));
        }
      }
      extra.append(block);
    }
    // Values of the products shown in the supplier columns, including specifications
    // not written in the request or in a supplier product title.
    if(extra.childNodes.length){extra.prepend(node('div','Значения из найденных товаров','supplier-found-label'));line.append(extra);}
    return line;
  }
  function showValueSources(group,value) {
    const popup=node('dialog','','supplier-choice-dialog');
    const header=node('div','','supplier-choice-header');header.append(node('h2',group.field+': '+value.value));
    const close=node('button','Закрыть');close.type='button';close.onclick=()=>popup.close();header.append(close);popup.append(header);
    const seen=new Set();
    for(const source of value.sources||[]) {
      if(seen.has(source.key))continue;seen.add(source.key);
      const article=node('article','','supplier-choice-option');
      const text=node('div');text.append(node('b',source.supplier+(source.sku?' · '+source.sku:'')),node('p',source.name));
      text.append(node('p',source.origin==='missing'?'Это значение в предложении не указано.':source.evidence||'Значение из наименования товара'));
      if(source.price!=null)text.append(node('p','Сохранённая цена предложения: '+money(source.price)+' ₽'));
      if(source.url){const link=node('a','Открыть товар');link.href=source.url;link.target='_blank';link.rel='noopener noreferrer';text.append(link);}
      article.append(text);popup.append(article);
    }
    popup.addEventListener('close',()=>popup.remove());document.body.append(popup);popup.showModal();
  }
  function supplierColumns() {
    const table=document.getElementById('review-table');
    if(!embedded || !table)return;
    table.querySelectorAll('[data-supplier-column]').forEach(n=>n.remove());
    const columns=table.querySelector('colgroup');
    if(columns) {
      columns.children[1].style.width='260px';
      columns.children[2].style.width='420px';
      table.style.width=(936+220*data.suppliers.length)+'px';
      table.style.minWidth=table.style.width;
      table.style.maxWidth='none';
    }
    for(const supplier of data.suppliers) {
      if(columns) {const col=node('col');col.style.width='220px';col.dataset.supplierColumn='1';columns.append(col);}
      const th=node('th',supplier.name,'supplier-price');th.dataset.supplierColumn='1';
      table.rows[0].append(th);
      for(const row of data.rows) {
        const anchor=table.querySelector(`[data-value-position="${row.position}"]`);
        if(!anchor)continue;
        const td=node('td','','supplier-price');td.dataset.supplierColumn='1';
        const comparison=row.value_comparison;
        const offers=comparison.offers.filter(v=>v.supplier===supplier.name && v.comparable)
          .sort((a,b)=>a.unit_price-b.unit_price);
        const selected=offers.find(v=>row.invoice?.included&&row.invoice.key===v.key), offer=selected||offers[0];
        const button=node('button','','supplier-quote'+(selected?' chosen':''));button.type='button';
        button.setAttribute('aria-label',`Выбрать товар: ${supplier.name}, позиция ${row.position}`);
        if(offer) {
          button.append(node('strong',`${money(offer.unit_price)} ₽ / ${row.unit}`),node('span',offer.name,'supplier-quote-name'),node('small',`${offers.length} вариантов · Выбрать ›`));
          if(selected)button.append(node('small','Выбрано в счёт','matched'));
        } else button.append(node('span',row.supplier_search?.[supplier.name]?.label||'Нет сохранённых результатов поставщика'),node('small','Посмотреть ›'));
        if(offer && ['error','incomplete'].includes(row.supplier_search?.[supplier.name]?.state))button.append(node('small',row.supplier_search[supplier.name].label));
        button.onclick=()=>showChoices(row.position,supplier.name);td.append(button);
        anchor.closest('tr').append(td);
      }
    }
  }
  function render() {
    supplierColumns();
    renderApplyBar();
    const root = document.getElementById('value-rows'); if(root)root.replaceChildren();
    for(const row of data.rows) {
      const target = embedded ? document.querySelector(`[data-value-position="${row.position}"]`) : root;
      if(!target)continue;
      if(embedded)target.replaceChildren();
      const section = node('div', '', 'value-position');
      if(!embedded)section.append(node('h2', `${row.position}. ${row.name}`));
      if(embedded) {
        section.append(inlineName(row));
        target.append(section);continue;
      }
      const panel = node('div', '', 'value-panel');
      panel.setAttribute('aria-label', 'Разрешённые характеристики позиции ' + row.position);
      for(const group of row.value_comparison.groups) {
        const block = node('div', '', 'value-group'); block.append(node('b', group.field));
        for(const value of group.values) {
          const allowed = ['matched','accepted'].includes(value.state);
          const chip = node('span', '', 'value-chip ' + (allowed ? 'matched' : value.state));
          const labelValue=value.value + (value.offer_label ? ' · '+value.offer_label : '');
          chip.append(node('span', labelValue));
          if(value.ambiguous)chip.title='Неизвестное или неоднозначное значение; решение относится к этому предложению';
          block.append(chip);
        }
        panel.append(block);
      }
      section.append(panel);
      const result = node('div', '', 'value-result');
      const minimum = row.value_comparison.minimum;
      result.append(node('strong', minimum ? `Минимум: ${money(minimum.unit_price)} ₽ / ${row.unit} · ${minimum.supplier}` : 'Сопоставимой цены для разрешённых значений нет'));
      if(minimum) {
        result.append(node('p', minimum.name));
        result.append(node('p', minimum.availability + (minimum.shortage > 0 ? ` · По данным предложения не хватает ${money(minimum.shortage)} ${row.unit}` : '')));
      }
      result.append(node('p', `Подходящих предложений: ${row.value_comparison.suitable_count}. Цена за единицу заявки; доставка отдельно. Актуальность цены — 30 дней.`));
      if(row.value_comparison.unrelated_count)result.append(node('p', `Результатов другого или нераспознанного типа товара исключено: ${row.value_comparison.unrelated_count}.`));
      const details = node('details'); details.append(node('summary', `Все найденные предложения (${row.value_comparison.offers.length})`));
      for(const offer of row.value_comparison.offers) {
        const line = node('article', '', 'value-offer');
        line.append(node('b', `${offer.supplier} · ${offer.name}`));
        line.append(node('p', `${offer.unit_price == null ? 'Цена не пересчитана' : money(offer.unit_price)+' ₽ / '+row.unit} · исходная цена ${offer.price == null ? (offer.raw_price == null ? 'не указана' : money(offer.raw_price)+' (условия неизвестны)') : money(offer.price)} / ${offer.unit} · дата ${offer.source_date || 'не указана'}`));
        line.append(node('p', `Пересчёт: ${offer.factor ? '1 '+offer.unit+' = '+money(offer.factor)+' '+row.unit : 'коэффициент неизвестен'}. ${offer.availability}`));
        if(offer.comparison_reasons.length)line.append(node('p', offer.comparison_reasons.join(' · '), 'reason'));
        else line.append(node('p', 'Участвует в минимуме', 'matched'));
        const href=offer.product_url || offer.url;
        if(href) { const link=node('a',offer.product_url ? 'Карточка товара и исходные данные' : 'Источник предложения');link.href=href;link.target='_blank';link.rel='noopener noreferrer';line.append(link); }
        details.append(line);
      }
      result.append(details);section.append(result);target.append(section);
    }
    if(activeDialog)renderChoices();
  }
  async function save(payload) {
    if(busy || data.read_only)return;
    busy=true;render();const message=document.getElementById('message');message.textContent='Сохраняется и пересчитывается…';
    try {
      const response=await fetch(actionUrl,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,csrf,revision:data.revision})});
      if(response.redirected || !response.headers.get('content-type')?.includes('application/json'))throw new Error('Обновите страницу и войдите заново.');
      const next=await response.json();if(!response.ok)throw new Error(next.detail || 'Не удалось сохранить решение');
      data=next;message.textContent='Решение сохранено. Минимум пересчитан.';
      syncInvoice(payload.position,['value_select','value_accept_select'].includes(payload.action));return true;
    } catch(error) { message.textContent=error.message;choiceError=error.message;return false; }
    finally {busy=false;render();if(activeDialog)renderChoices();}
  }
  render();
  for(const row of data.rows)syncInvoice(row.position);
})();
