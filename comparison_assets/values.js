(() => {
  'use strict';
  let data = JSON.parse(document.getElementById('comparison-data').textContent);
  const csrf = data.csrf;
  let busy = false;
  const node = (tag, text, cls) => { const n = document.createElement(tag); n.textContent = text ?? ''; if(cls)n.className=cls; return n; };
  const money = value => Number(value).toLocaleString('ru-RU', {maximumFractionDigits:6});
  function render() {
    const root = document.getElementById('value-rows'); root.replaceChildren();
    for(const row of data.rows) {
      const section = node('section', '', 'value-position');
      section.append(node('h2', `${row.position}. ${row.name}`));
      const panel = node('div', '', 'value-panel');
      panel.setAttribute('aria-label', 'Разрешённые характеристики позиции ' + row.position);
      for(const group of row.value_comparison.groups) {
        const block = node('div', '', 'value-group'); block.append(node('b', group.field));
        for(const value of group.values) {
          const allowed = ['matched','accepted'].includes(value.state);
          const chip = node('span', '', 'value-chip ' + (allowed ? 'matched' : value.state));
          chip.append(node('span', value.value));
          for(const [symbol, decision, label] of [['✓','accepted','Разрешить'],['✕','rejected','Исключить']]) {
            const button = node('button', symbol);
            button.type='button'; button.disabled=busy || data.read_only;
            button.setAttribute('aria-label', `${label}: ${group.field} ${value.value}`);
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
      result.append(details);section.append(result);root.append(section);
    }
  }
  async function save(payload) {
    if(busy || data.read_only)return;
    busy=true;render();const message=document.getElementById('message');message.textContent='Сохраняется и пересчитывается…';
    try {
      const response=await fetch(location.pathname+'/action',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,csrf,revision:data.revision})});
      if(response.redirected || !response.headers.get('content-type')?.includes('application/json'))throw new Error('Обновите страницу и войдите заново.');
      const next=await response.json();if(!response.ok)throw new Error(next.detail || 'Не удалось сохранить решение');
      data=next;message.textContent='Решение сохранено. Минимум пересчитан.';
    } catch(error) { message.textContent=error.message; }
    finally {busy=false;render();}
  }
  render();
})();
