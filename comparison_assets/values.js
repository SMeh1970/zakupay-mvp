(() => {
  'use strict';
  let data = JSON.parse(document.getElementById('comparison-data').textContent);
  const csrf = data.csrf;
  const embedded = !!data.embedded;
  const actionUrl = data.action_url || location.pathname+'/action';
  let busy = false;
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
    dialog.append(node('p',`${row.name} · ${money(row.quantity)} ${row.unit}`));
    const choices=row.value_comparison.offers.filter(v=>v.supplier===supplier&&v.comparable).sort((a,b)=>a.unit_price-b.unit_price);
    if(!choices.length)dialog.append(node('p','Нет товаров с разрешёнными характеристиками. Измените ✓/✕ в строке заявки.'));
    const list=node('div','','supplier-choice-list');
    for(const offer of choices) {
      const line=node('article','','supplier-choice-option');line.append(node('strong',offer.name));
      if(offer.sku)line.append(node('p',`Артикул: ${offer.sku}`));
      line.append(node('p',`${money(offer.unit_price)} ₽ / ${row.unit} · исходная цена ${money(offer.price)} ₽ / ${offer.unit}`));
      line.append(node('p',`Закупка: ${money(offer.quantity)} ${offer.unit} = ${money(offer.converted_quantity)} ${row.unit} · ${money(offer.total)} ₽`));
      line.append(node('p',offer.availability));
      const href=offer.product_url||offer.url;
      if(href){const link=node('a','Карточка товара');link.href=href;link.target='_blank';link.rel='noopener noreferrer';line.append(link);}
      if(offer.shortage>0)line.append(node('p',`Не хватает ${money(offer.shortage)} ${row.unit}`));
      const choose=node('button',row.invoice?.key===offer.key&&row.invoice?.included?'Выбрано в счёт':'Выбрать в счёт');choose.type='button';
      choose.disabled=busy||data.selection_read_only||offer.shortage>0||!offer.quantity;
      choose.onclick=async()=>{if(await save({action:'value_select',position:row.position,key:offer.key}))closeChoices();};
      line.append(choose);list.append(line);
    }
    dialog.append(list,node('p',data.selection_read_only?'Отправленный счёт доступен только для просмотра.':'Выбор товара изменяет строку счёта. Отправка выполняется отдельно.'));
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
    const checks=group=>['Слово в наименовании','Нет слова из заявки','Проверка прайса','Проверка категории','Проверка соответствия'].includes(group.field);
    const chip=(group,value,label)=>{
      const allowed=['matched','accepted'].includes(value.state);
      const span=node('span','','value-chip '+(allowed?'matched':value.state));
      span.title=group.field+': '+value.value+(value.offer_label?' · '+value.offer_label:'');
      span.append(node('span',label??value.value));
      for(const [symbol,decision,title] of [['✓','accepted','Разрешить'],['✕','rejected','Исключить']]) {
        const b=node('button',symbol);b.type='button';b.disabled=busy||data.read_only;
        b.setAttribute('aria-label',title+': '+group.field+' '+value.value);
        b.setAttribute('aria-pressed',String(decision==='accepted'?allowed:value.state==='rejected'));
        b.onclick=()=>save({action:'value',position:row.position,feature:value.id,decision});span.append(b);
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
      for(const alternative of p.group.values)if(!checks(p.group) && alternative.id!==p.feature.id && !alternative.ambiguous && alternative.value!=='Не указано' && !alternative.offer_label) {
        line.append(document.createTextNode(' / '),chip(p.group,alternative,alternative.value.replace(/\s+(мм|см|м|кг|г|л|мл|°)$/u,'')));
      }
      cursor=p.end;
    }
    line.append(document.createTextNode(source.slice(cursor)));
    const extra=node('details','','value-extra');
    extra.append(node('summary','Другие характеристики и неизвестные значения'));
    for(const group of row.value_comparison.groups) {
      const values=group.values.filter(v=>!used.has(group)||checks(group)||v.ambiguous||v.value==='Не указано'||v.offer_label);
      if(!values.length)continue;
      const block=node('div','','value-group-details');block.append(node('b',group.field+': '));
      const byValue=new Map();
      for(const value of values) {if(!byValue.has(value.value))byValue.set(value.value,[]);byValue.get(value.value).push(value);}
      for(const [label,items] of byValue) {
        if(items.length===1)block.append(chip(group,items[0],label+(items[0].offer_label?' · '+items[0].offer_label:'')));
        else {
          const offers=node('details');offers.append(node('summary',`${label} · предложений: ${items.length}`));
          for(const value of items)offers.append(chip(group,value,value.offer_label||label));
          block.append(offers);
        }
      }
      extra.append(block);
    }
    if(extra.children.length>1)line.append(extra);
    return line;
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
        } else button.append(node('span','Нет допустимых вариантов'),node('small','Посмотреть ›'));
        button.onclick=()=>showChoices(row.position,supplier.name);td.append(button);
        anchor.closest('tr').append(td);
      }
    }
  }
  function render() {
    supplierColumns();
    const root = document.getElementById('value-rows'); if(root)root.replaceChildren();
    for(const row of data.rows) {
      const target = embedded ? document.querySelector(`[data-value-position="${row.position}"]`) : root;
      if(!target)continue;
      if(embedded)target.replaceChildren();
      const section = node('div', '', 'value-position');
      if(!embedded)section.append(node('h2', `${row.position}. ${row.name}`));
      if(embedded) {
        section.append(inlineName(row));
        const best=row.value_comparison.minimum;
        section.append(node('div',best?`Абсолютный минимум: ${money(best.unit_price)} ₽ / ${row.unit} · ${(row.value_comparison.minima||[best]).map(v=>v.supplier+' · '+v.name).join('; ')}`:'Допустимых предложений с сопоставимой ценой нет','value-result'));
        const recommendation=row.value_comparison.recommendation;
        if(recommendation && best && recommendation.key!==best.key)section.append(node('div',`Рекомендуется: ${recommendation.supplier} · ${recommendation.name} · ${money(recommendation.unit_price)} ₽ / ${row.unit} · охват ${row.value_comparison.coverage[recommendation.supplier]} поз. (в пределах 3%)`,'value-result'));
        const chosen=row.value_comparison.offers.find(v=>row.invoice?.included&&row.invoice.key===v.key);
        section.append(node('div',chosen?`В счёт: ${chosen.supplier} · ${chosen.name}`:'Для счёта товар не выбран','value-result'));
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
          for(const [symbol, decision, label] of [['✓','accepted','Разрешить'],['✕','rejected','Исключить']]) {
            const button = node('button', symbol);
            button.type='button'; button.disabled=busy || data.read_only;
            button.setAttribute('aria-label', `${label}: ${group.field} ${labelValue}`);
            button.setAttribute('aria-pressed', String(decision === 'accepted' ? allowed : value.state === 'rejected'));
            button.onclick=()=>save({action:'value',position:row.position,feature:value.id,decision});
            chip.append(button);
          }
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
      syncInvoice(payload.position,payload.action==='value_select');return true;
    } catch(error) { message.textContent=error.message;choiceError=error.message;return false; }
    finally {busy=false;render();if(activeDialog)renderChoices();}
  }
  render();
})();
