(() => {
  'use strict';
  let data = JSON.parse(document.getElementById('comparison-data').textContent);
  const csrf = data.csrf;
  const embedded = !!data.embedded;
  const actionUrl = data.action_url || location.pathname+'/action';
  let busy = false;
  const node = (tag, text, cls) => { const n = document.createElement(tag); n.textContent = text ?? ''; if(cls)n.className=cls; return n; };
  const money = value => Number(value).toLocaleString('ru-RU', {maximumFractionDigits:6});
  function inlineName(row) {
    const line=node('div','','value-panel');
    line.setAttribute('aria-label','Наименование с выбором значений позиции '+row.position);
    const source=row.name || '', placements=[], used=new Set();
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
      for(const alternative of p.group.values)if(alternative.id!==p.feature.id) {
        line.append(document.createTextNode(' / '),chip(p.group,alternative,alternative.value.replace(/\s+(мм|см|м|кг|г|л|мл|°)$/u,'')));
      }
      cursor=p.end;
    }
    line.append(document.createTextNode(source.slice(cursor)));
    for(const group of row.value_comparison.groups)if(!used.has(group)) {
      line.append(document.createTextNode(' '));
      for(const value of group.values)line.append(chip(group,value));
    }
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
      table.style.width=(1453+220*data.suppliers.length)+'px';
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
        const groups=new Map();
        for(const offer of offers) {
          const signature=JSON.stringify(offer.features.map(f=>[f.field,f.value]).sort());
          if(!groups.has(signature))groups.set(signature,[]);
          groups.get(signature).push(offer);
        }
        if(!offers.length)td.append(node('span','—'));
        let index=0, more=null;
        for(const variants of groups.values()) {
          if(index++===10) {more=node('details');more.append(node('summary','Остальные варианты'));td.append(more);}
          const offer=variants[0], block=node('div','','supplier-variant');
          const minimum=(comparison.minima||[]).some(v=>v.key===offer.key);
          block.append(node('strong',`${money(offer.unit_price)} ₽ / ${row.unit}`,minimum?'matched':''));
          block.append(node('div',offer.name));
          block.append(node('small',`${offer.availability} · закупка: ${money(offer.quantity)} ${offer.unit}`));
          if(variants.length>1)block.append(node('small',` · предложений: ${variants.length}`));
          if(minimum)block.append(node('div','Абсолютный минимум','matched'));
          if(comparison.recommendation?.key===offer.key)block.append(node('div',`Рекомендуется · охват ${comparison.coverage[supplier.name]} поз.`, 'matched'));
          (more||td).append(block);
        }
        const excluded=comparison.offers.filter(v=>v.supplier===supplier.name && !v.comparable);
        if(excluded.length) {
          const details=node('details');details.append(node('summary',`Вне расчёта: ${excluded.length}`));
          for(const offer of excluded) {
            details.append(node('div',offer.name));
            details.append(node('small',offer.comparison_reasons.join(' · ')));
            if(offer.comparison_reasons.some(r=>/дней|дата|Дата/.test(r)))details.append(node('div','Требует подтверждения наличия и стоимости'));
          }
          td.append(details);
        }
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
  }
  async function save(payload) {
    if(busy || data.read_only)return;
    busy=true;render();const message=document.getElementById('message');message.textContent='Сохраняется и пересчитывается…';
    try {
      const response=await fetch(actionUrl,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,csrf,revision:data.revision})});
      if(response.redirected || !response.headers.get('content-type')?.includes('application/json'))throw new Error('Обновите страницу и войдите заново.');
      const next=await response.json();if(!response.ok)throw new Error(next.detail || 'Не удалось сохранить решение');
      data=next;message.textContent='Решение сохранено. Минимум пересчитан.';
    } catch(error) { message.textContent=error.message; }
    finally {busy=false;render();}
  }
  render();
})();
