const initial = JSON.parse(document.getElementById('comparison-data').textContent);
const matrix = initial.prototype;
const items = matrix.items, offers = matrix.offers, supplierMeta = matrix.supplierMeta;
const esc = value => String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const realSum = off => off && Number.isFinite(off.price) && off.price>0 && Number.isFinite(off.qty) && off.qty>0 ? off.price*off.qty : null;
let lastSaved = '', saving=false, revision=initial.revision;
function stateSnapshot(){return {supplierOn, cellOn, diffState, choices, mode:compareMode};}
async function persist(){
 if(initial.read_only||saving)return;
 const encoded=JSON.stringify(stateSnapshot());if(encoded===lastSaved)return;
 saving=true;document.body.classList.add('saving');const status=document.getElementById('save-status');status.textContent='Сохраняется…';status.className='';
 try{const response=await fetch(location.pathname+'/action',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'prototype_state',csrf:initial.csrf,revision,signature:matrix.signature,state:JSON.parse(encoded)})});if(!response.ok)throw new Error((await response.json()).detail||'Не удалось сохранить');const next=await response.json();revision=next.revision;lastSaved=encoded;status.textContent='Решения сохранены.';}catch(error){status.textContent=error.message+' Обновите страницу перед продолжением.';status.className='error';document.querySelectorAll('.table-wrap input,.table-wrap button').forEach(e=>e.disabled=true);}finally{saving=false;document.body.classList.remove('saving');}
}
const saved = matrix.state||{}, choices = {...(saved.choices||{})};
for(const [k,key] of Object.entries(choices)){const [id,si]=k.split('_').map(Number);const supplier=matrix.supplierNames[si];const options=matrix.variants[supplier]?.[id]||[];const quote=options.find(q=>q.key===key);if(quote)offers[supplier][id]=quote;}
const suppliers = Object.keys(offers).map(name => ({name, meta: supplierMeta[name]}));

function normUnit(u){
  return (u||'').toLowerCase().replace(/пог\.?\s*/g,'').replace(/[.\s]/g,'');
}

// Возвращает данные предложения + флаги отличий от заявки (item)
function getOffer(item, supplierName){
  const o = offers[supplierName] && offers[supplierName][item.id];
  if(!o) return null;
  const unitDiff = normUnit(o.unit) !== normUnit(item.unit);
  const qtyDiff = !unitDiff && (o.qty !== item.qty);
  const nameDiff = o.name.trim() !== item.name.trim();
  return {
    ...o, name:o.name, qty:o.qty, unit:o.unit, price:o.price, anomaly:!!o.anomaly,
    diffs:{unit:unitDiff, qty:qtyDiff, name:nameDiff},
  };
}


// state
const supplierOn = suppliers.map((s,i)=>saved.supplierOn?.[i]??true);
const hiddenSupplier = suppliers.map(()=>false);
const cellOn = {...(saved.cellOn||{})}; // key `${itemId}_${sIdx}` -> bool (master вкл/выкл ячейки)
const diffState = {...(saved.diffState||{})}; // key `${itemId}_${sIdx}_${field}` -> 'accepted' | 'rejected' | undefined(на рассмотрении)
items.forEach(it=>{
  suppliers.forEach((s,si)=>{
    const key=it.id+'_'+si; if(!(key in cellOn))cellOn[key]=getOffer(it,s.name)?.enabled ?? false;
  });
});

function wordTokens(str){
  return str.trim().split(/\s+/);
}

// LCS по словам (без учёта регистра/пунктуации при сравнении) — возвращает индексы слов offer, совпавших с base
function lcsMatchedIndices(baseWords, offerWords){
  const norm = w => w.toLowerCase().replace(/[.,«»()]/g,'');
  const a = baseWords.map(norm), b = offerWords.map(norm);
  const n=a.length, m=b.length;
  const dp = Array.from({length:n+1}, ()=>new Array(m+1).fill(0));
  for(let i=1;i<=n;i++){
    for(let j=1;j<=m;j++){
      dp[i][j] = (a[i-1]===b[j-1]) ? dp[i-1][j-1]+1 : Math.max(dp[i-1][j], dp[i][j-1]);
    }
  }
  let i=n, j=m; const matched = new Set();
  while(i>0 && j>0){
    if(a[i-1]===b[j-1]){ matched.add(j-1); i--; j--; }
    else if(dp[i-1][j] >= dp[i][j-1]) i--;
    else j--;
  }
  return matched;
}

function nameWordDiffIndices(itemId, si){
  const item = items.find(x=>x.id===itemId);
  const off = getOffer(item, suppliers[si].name);
  if(!off) return [];
  const baseWords = wordTokens(item.name);
  const offerWords = wordTokens(off.name);
  const matched = lcsMatchedIndices(baseWords, offerWords);
  const diffIdx = [];
  offerWords.forEach((w, idx) => { if(!matched.has(idx)) diffIdx.push(idx); });
  return diffIdx;
}

function nameFieldOverallStatus(itemId, si){
  const idxs = nameWordDiffIndices(itemId, si);
  if(idxs.length === 0) return 'accepted'; // формально нет отличающихся слов
  const states = idxs.map(idx => diffState[`${itemId}_${si}_nameword_${idx}`]);
  if(states.some(s => s === 'rejected')) return 'rejected';
  if(states.every(s => s === 'accepted')) return 'accepted';
  return 'pending';
}

function cellHasRejectedDiff(itemId, si){
  if(['unit','qty'].some(f => diffState[`${itemId}_${si}_${f}`] === 'rejected')) return true;
  return nameFieldOverallStatus(itemId, si) === 'rejected';
}
function cellHasUnresolvedRiskyDiff(itemId, si){
  const off = getOffer(items.find(i=>i.id===itemId), suppliers[si].name);
  if(!off) return false;
  const unitQtyPending = ['unit','qty'].some(f => off.diffs[f] && diffState[`${itemId}_${si}_${f}`] !== 'accepted' && diffState[`${itemId}_${si}_${f}`] !== 'rejected');
  const namePending = off.diffs.name && nameFieldOverallStatus(itemId, si) === 'pending';
  return unitQtyPending || namePending;
}

// Ключ «условия»: одинаковое отличие у разных предложений (ед.изм. А→Б, кол-во А→Б, одно и то же слово наименования)
function condKey(itemId, si, field, idx){
  const item = items.find(i=>i.id===itemId);
  const off = getOffer(item, suppliers[si].name);
  if(!off) return null;
  if(field==='unit') return off.diffs.unit ? 'unit|'+normUnit(item.unit)+'>'+normUnit(off.unit) : null;
  if(field==='qty') return off.diffs.qty ? 'qty|'+item.qty+'>'+off.qty : null;
  if(field==='nameword'){
    const w = wordTokens(off.name)[idx];
    return w===undefined ? null : 'w|'+w.toLowerCase().replace(/[.,«»()]/g,'');
  }
  return null;
}
function refreshAll(){
  items.forEach(it=>updateRowInfo(it.id));
  recompute();
}
// Решение по одному отличию распространяется на все такие же отличия у других поставщиков
// (в этой позиции; если отмечен переключатель — и во всех позициях)
function propagateDecision(itemId, si, field, idx, decision, quiet){
  const ownKey = field==='nameword' ? `${itemId}_${si}_nameword_${idx}` : `${itemId}_${si}_${field}`;
  diffState[ownKey] = decision;
  const key = condKey(itemId, si, field, idx);
  const sc = document.getElementById('scopeAll');
  const allItems = !!(sc && sc.checked);
  if(key){
    items.forEach(it=>{
      if(!allItems && it.id!==itemId) return;
      suppliers.forEach((s, sj)=>{
        if(field==='nameword'){
          nameWordDiffIndices(it.id, sj).forEach(j=>{
            if(condKey(it.id, sj, 'nameword', j)===key) diffState[`${it.id}_${sj}_nameword_${j}`] = decision;
          });
        } else if(condKey(it.id, sj, field)===key){
          diffState[`${it.id}_${sj}_${field}`] = decision;
        }
      });
    });
  }
  if(!quiet) refreshAll();
}

const rowRefs = {};   // itemId -> {tdName, tdQty, tdUnit}
const inspecting = {}; // itemId -> supplier index or null

function renderDiffField(itemId, si, field, baseVal, offerVal){
  const key = `${itemId}_${si}_${field}`;
  const state = diffState[key];
  const row = document.createElement('div');
  row.className = 'diff-row' + (state === 'rejected' ? ' diff-rejected' : state === 'accepted' ? ' diff-accepted' : ' diff-pending');
  const text = document.createElement('span');
  text.textContent = `«${baseVal??"—"}» → «${offerVal??"—"}»`;
  const btns = document.createElement('span');
  btns.className = 'diff-btns';
  const okBtn = document.createElement('button');
  okBtn.className = 'diff-btn ok' + (state==='accepted' ? ' active' : '');
  okBtn.textContent = '✓';
  okBtn.title = 'Принять это отличие';
  okBtn.onclick = (e)=>{ e.stopPropagation(); propagateDecision(itemId, si, field, null, 'accepted'); };
  const noBtn = document.createElement('button');
  noBtn.className = 'diff-btn no' + (state==='rejected' ? ' active' : '');
  noBtn.textContent = '✗';
  noBtn.title = 'Отклонить это отличие (исключить цену из сравнения)';
  noBtn.onclick = (e)=>{ e.stopPropagation(); propagateDecision(itemId, si, field, null, 'rejected'); };
  btns.appendChild(okBtn);
  btns.appendChild(noBtn);
  row.appendChild(text);
  row.appendChild(btns);
  return row;
}

// Наименование целиком, слово за словом: у каждого отличающегося слова — своя пара ✓/✗,
// плюс одна общая пара сверху, которая принимает/отклоняет все слова разом.
function renderNameDiff(itemId, si){
  const item = items.find(x=>x.id===itemId);
  const off = getOffer(item, suppliers[si].name);
  const offerWords = wordTokens(off.name);
  const diffIdxSet = new Set(nameWordDiffIndices(itemId, si));

  const container = document.createElement('div');

  if(diffIdxSet.size > 0){
    const masterRow = document.createElement('div');
    masterRow.className = 'diff-row diff-master';
    const btns = document.createElement('span');
    btns.className = 'diff-btns';
    const okAll = document.createElement('button');
    okAll.className = 'diff-btn ok';
    okAll.textContent = '✓';
    okAll.title = 'Принять все отличия в наименовании';
    okAll.onclick = (e)=>{
      e.stopPropagation();
      diffIdxSet.forEach(idx => propagateDecision(itemId, si, 'nameword', idx, 'accepted', true));
      refreshAll();
    };
    const noAll = document.createElement('button');
    noAll.className = 'diff-btn no';
    noAll.textContent = '✗';
    noAll.title = 'Отклонить все отличия в наименовании (исключить цену из сравнения)';
    noAll.onclick = (e)=>{
      e.stopPropagation();
      diffIdxSet.forEach(idx => propagateDecision(itemId, si, 'nameword', idx, 'rejected', true));
      refreshAll();
    };
    btns.appendChild(okAll);
    btns.appendChild(noAll);
    masterRow.appendChild(btns);
    container.appendChild(masterRow);
  }

  // Текст наименования поставщика, слово за словом; у отличающихся — своя пара ✓/✗
  const textLine = document.createElement('div');
  textLine.className = 'name-diff-text';
  offerWords.forEach((word, idx) => {
    if(diffIdxSet.has(idx)){
      const key = `${itemId}_${si}_nameword_${idx}`;
      const state = diffState[key];
      const chip = document.createElement('span');
      chip.className = 'word-chip' + (state === 'rejected' ? ' word-rejected' : state === 'accepted' ? ' word-accepted' : ' word-pending');
      chip.textContent = word;
      const wbtns = document.createElement('span');
      wbtns.className = 'word-btns';
      const okB = document.createElement('button');
      okB.className = 'diff-btn ok tiny' + (state==='accepted' ? ' active' : '');
      okB.textContent = '✓';
      okB.onclick = (e)=>{ e.stopPropagation(); propagateDecision(itemId, si, 'nameword', idx, 'accepted'); };
      const noB = document.createElement('button');
      noB.className = 'diff-btn no tiny' + (state==='rejected' ? ' active' : '');
      noB.textContent = '✗';
      noB.onclick = (e)=>{ e.stopPropagation(); propagateDecision(itemId, si, 'nameword', idx, 'rejected'); };
      wbtns.appendChild(okB);
      wbtns.appendChild(noB);
      const unit = document.createElement('span');
      unit.className = 'word-unit';
      unit.appendChild(chip);
      unit.appendChild(wbtns);
      textLine.appendChild(unit);
    } else {
      textLine.appendChild(document.createTextNode(word + ' '));
    }
  });
  container.appendChild(textLine);
  return container;
}

function updateRowInfo(itemId){
  const refs = rowRefs[itemId];
  if(!refs) return;
  const item = items.find(i=>i.id===itemId);
  const si = inspecting[itemId];
  refs.tdName.querySelectorAll('.raw-sub').forEach(el=>el.remove());
  refs.tdQty.querySelectorAll('.raw-sub').forEach(el=>el.remove());
  refs.tdUnit.querySelectorAll('.raw-sub').forEach(el=>el.remove());

  if(si !== undefined && si !== null){
    const sname = suppliers[si].name;
    const off = getOffer(item, sname);

    if(off){
      const options=matrix.variants[sname]?.[itemId]||[];
      if(options.length>1){const wrap=document.createElement('div');wrap.className='raw-sub';const select=document.createElement('select');select.className='variant-picker';select.setAttribute('aria-label','Предложение '+sname+', позиция '+itemId);select.disabled=initial.read_only;options.forEach(q=>{const option=document.createElement('option');option.value=q.key;option.textContent=q.name;option.selected=q.key===off.key;select.appendChild(option);});select.onchange=()=>{offers[sname][itemId]=options.find(q=>q.key===select.value);choices[itemId+'_'+si]=select.value;cellOn[itemId+'_'+si]=offers[sname][itemId].enabled;Object.keys(diffState).filter(k=>k.startsWith(itemId+'_'+si+'_')).forEach(k=>delete diffState[k]);buildBody();items.forEach(it=>updateRowInfo(it.id));recompute();};wrap.appendChild(select);refs.tdName.appendChild(wrap);}
      {
        const wrapName = document.createElement('div');
        wrapName.className = 'raw-sub';
        wrapName.appendChild(renderNameDiff(itemId, si));
        if(off.url){const link=document.createElement('a');link.href=off.url;link.target='_blank';link.rel='noopener noreferrer';link.className='source-link';link.textContent='Источник предложения ↗';wrapName.appendChild(link);}if(off.quantity_note){const note=document.createElement('div');note.textContent=off.quantity_note;wrapName.appendChild(note);}refs.tdName.appendChild(wrapName);
      }
      if(off.diffs.qty){
        const wrapQty = document.createElement('div');
        wrapQty.className = 'raw-sub';
        wrapQty.appendChild(renderDiffField(itemId, si, 'qty', item.qty, off.qty));
        refs.tdQty.appendChild(wrapQty);
      }
      if(off.diffs.unit){
        const wrapUnit = document.createElement('div');
        wrapUnit.className = 'raw-sub';
        wrapUnit.appendChild(renderDiffField(itemId, si, 'unit', item.unit, off.unit));
        refs.tdUnit.appendChild(wrapUnit);
      }
    } else {
      const wrapName = document.createElement('div');
      wrapName.className = 'raw-sub';
      wrapName.innerHTML = `<span class="who">${sname}:</span> данных нет`;
      refs.tdName.appendChild(wrapName);
    }
  }

  document.querySelectorAll(`td.price-cell[data-item="${itemId}"]`).forEach(td=>{
    td.classList.toggle('inspected', +td.dataset.supplier === si);
  });
}

function toggleInspect(itemId, si){
  inspecting[itemId] = (inspecting[itemId] === si) ? null : si;
  updateRowInfo(itemId);
}

// Применяет одно решение (accepted/rejected) сразу ко ВСЕМ отличиям (ед./кол-во/каждое слово наименования)
// у ВСЕХ поставщиков по данной позиции — это решение "на всю сессию по этой строке".
function applyDecisionToWholeRow(itemId, decision){
  const item = items.find(i=>i.id===itemId);
  suppliers.forEach((s, si)=>{
    const off = getOffer(item, s.name);
    if(!off) return;
    if(off.diffs.unit) diffState[`${itemId}_${si}_unit`] = decision;
    if(off.diffs.qty) diffState[`${itemId}_${si}_qty`] = decision;
    if(off.diffs.name){
      nameWordDiffIndices(itemId, si).forEach(idx => {
        diffState[`${itemId}_${si}_nameword_${idx}`] = decision;
      });
    }
  });
  updateRowInfo(itemId);
  recompute();
}

function fmt(n){
  if(n===null || !Number.isFinite(n))return '—';
  return n.toLocaleString('ru-RU', {minimumFractionDigits:2, maximumFractionDigits:2});
}

function buildHead(){
  const headRow = document.getElementById('headRow');
  suppliers.forEach((s, si)=>{
    const th = document.createElement('th');
    th.className = 'supplier-head';
    th.dataset.supplier = si;
    th.innerHTML = `
      <label class="hide-toggle-row" title="Скрыть этот столбец из вида">
        <input type="checkbox" data-hide="${si}"> скрыть
      </label>
      <input type="checkbox" data-supplier="${si}" ${supplierOn[si] ? 'checked' : ''}><span class="name">${esc(s.name)}</span>`;
    headRow.appendChild(th);
  });
}

function buildBody(){
  const tbody = document.getElementById('bodyRows');
  tbody.innerHTML = '';
  items.forEach(item=>{
    const tr = document.createElement('tr');
    const tdName = document.createElement('td');
    tdName.className = 'item-col';

    const nameWrap = document.createElement('span');
    nameWrap.textContent = item.name;
    tdName.appendChild(nameWrap);

    const rowBtns = document.createElement('span');
    rowBtns.className = 'row-master-btns';
    rowBtns.title = 'Применить решение сразу ко всем поставщикам этой позиции';
    const rowOk = document.createElement('button');
    rowOk.className = 'diff-btn ok tiny';
    rowOk.textContent = '✓';
    rowOk.title = 'Принять все отличия по этой позиции у всех поставщиков';
    rowOk.onclick = (e)=>{ e.stopPropagation(); applyDecisionToWholeRow(item.id, 'accepted'); };
    const rowNo = document.createElement('button');
    rowNo.className = 'diff-btn no tiny';
    rowNo.textContent = '✗';
    rowNo.title = 'Отклонить все отличия по этой позиции у всех поставщиков';
    rowNo.onclick = (e)=>{ e.stopPropagation(); applyDecisionToWholeRow(item.id, 'rejected'); };
    rowBtns.appendChild(rowOk);
    rowBtns.appendChild(rowNo);
    tdName.appendChild(rowBtns);

    tr.appendChild(tdName);

    const tdQty = document.createElement('td');
    tdQty.className = 'qty-col';
    tdQty.textContent = item.qty === null ? 'разное' : item.qty;
    tr.appendChild(tdQty);

    const tdUnit = document.createElement('td');
    tdUnit.className = 'unit-col';
    tdUnit.textContent = item.unit;
    tr.appendChild(tdUnit);

    rowRefs[item.id] = {tdName, tdQty, tdUnit};

    suppliers.forEach((s, si)=>{
      const td = document.createElement('td');
      td.className = 'price-cell';
      td.dataset.item = item.id;
      td.dataset.supplier = si;
      const off = getOffer(item, s.name);
      if(off === null){
        td.classList.add('na');
        td.textContent = '—';
      } else {
        const sum = realSum(off); // ВСЕГДА реальная сумма из счёта поставщика
        const key = item.id+'_'+si;
        const wrap = document.createElement('div');
        wrap.className = 'cell-inner';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = !!cellOn[key] && realSum(off)!==null; cb.disabled=initial.read_only||realSum(off)===null; cb.setAttribute('aria-label','Участвует: '+s.name+', позиция '+item.id);
        cb.dataset.item = item.id;
        cb.dataset.supplier = si;
        cb.addEventListener('change', onCellToggle);
        const span = document.createElement('span');
        span.className = 'sum-val';
        span.dataset.price = off.price;
        span.dataset.sum = sum===null?'':sum;
        span.textContent = fmt(compareMode === 'price' ? off.price : sum);
        wrap.appendChild(cb);
        if(off.diffs.unit || off.diffs.qty || off.diffs.name){
          const badge = document.createElement('span');
          badge.className = 'diff-badge';
          badge.textContent = '!';
          badge.title = 'Есть отличие от заявки (наименование/кол-во/ед.) — кликните для деталей';
          wrap.appendChild(badge);
        }
        wrap.appendChild(span);
        td.appendChild(wrap);
        if(off.anomaly) td.classList.add('anomaly');
        td.title = 'Клик — показать формулировку поставщика (наименование/кол-во/ед.)';
        td.addEventListener('click', (e)=>{
          if(e.target.tagName === 'INPUT' || e.target.tagName === 'BUTTON') return;
          toggleInspect(item.id, si);
        });
      }
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
}

function fmtDelivery(d){
  if(d.included) return '<span class="vus-yes">вкл. в цену товара</span>' + (d.note ? '<br><span class="delivery-note">'+d.note+'</span>' : '');
  if(d.amount !== null) return '<b>'+fmt(d.amount)+' ₽</b>' + (d.note ? '<br><span class="delivery-note">'+d.note+'</span>' : '');
  return '<span style="color:var(--excluded)">не указана</span>';
}

function buildFootStatic(){
  const covRow = document.getElementById('coverageRow');
  const delRow = document.getElementById('deliveryRow');
  const vusRow = document.getElementById('vusRow');
  const noteRow = document.getElementById('noteRow');
  suppliers.forEach((s, si)=>{
    const covered = items.filter(it => getOffer(it, s.name) !== null).length;
    const tdC = document.createElement('td'); tdC.className='foot-cell'; tdC.dataset.supplier=si; tdC.textContent = covered+'/'+items.length; covRow.appendChild(tdC);
    const tdD = document.createElement('td'); tdD.className='foot-cell'; tdD.dataset.supplier=si; tdD.innerHTML = fmtDelivery(s.meta.delivery); delRow.appendChild(tdD);
    const tdV = document.createElement('td'); tdV.className='foot-cell'; tdV.dataset.supplier=si;
    tdV.textContent=s.meta.date||'Не указана';
    vusRow.appendChild(tdV);
    const tdN = document.createElement('td'); tdN.className='foot-cell'; tdN.dataset.supplier=si; tdN.textContent = s.meta.note; noteRow.appendChild(tdN);
  });
}

function onSupplierToggle(e){
  const si = +e.target.dataset.supplier;
  supplierOn[si] = e.target.checked;

  // sync every price checkbox in this supplier's column
  document.querySelectorAll(`input[type=checkbox][data-supplier="${si}"]`).forEach(cb=>{
    if(cb === e.target) return; // skip the header checkbox itself
    if(!supplierOn[si]){
      cb.checked = false;
      cb.disabled = true;
    } else {
      const item=items.find(it=>String(it.id)===cb.dataset.item);const off=item&&getOffer(item,suppliers[si].name);cb.checked=realSum(off)!==null;
      cb.disabled = initial.read_only||realSum(off)===null;
    }
  });
  // reset underlying state to match (re-enabling a supplier restores all its cells to "on")
  items.forEach(item=>{
    const key = item.id+'_'+si;
    if(key in cellOn) cellOn[key] = supplierOn[si];
  });

  recompute();
}

function onHideToggle(e){
  const si = +e.target.dataset.hide;
  hiddenSupplier[si] = e.target.checked;
  applyHiddenColumns();
}

function applyHiddenColumns(){
  document.querySelectorAll(`[data-supplier]`).forEach(el=>{
    const si = +el.dataset.supplier;
    if(si === undefined || Number.isNaN(si)) return;
    // skip the checkbox inputs themselves (they carry data-supplier too but aren't columns)
    if(el.tagName === 'INPUT') return;
    el.classList.toggle('col-hidden', hiddenSupplier[si]);
  });
}

document.getElementById('showAllBtn').addEventListener('click', ()=>{
  hiddenSupplier.forEach((_, i)=>hiddenSupplier[i] = false);
  document.querySelectorAll('input[data-hide]').forEach(cb=>cb.checked = false);
  applyHiddenColumns();
});

function onCellToggle(e){
  const itemId = +e.target.dataset.item;
  const si = +e.target.dataset.supplier;
  cellOn[itemId+'_'+si] = e.target.checked;
  recompute();
}

let compareMode = saved.mode==='price'?'price':'sum'; // 'sum' | 'price' — влияет только на отображение в ячейках, не на выбор минимума

function updateCellDisplay(){
  document.querySelectorAll('td.price-cell .sum-val').forEach(span=>{
    const raw=compareMode==='price'?span.dataset.price:span.dataset.sum; const val=raw===''?null:Number(raw);
    span.textContent = fmt(val);
  });
}

function recompute(){
  const heads = document.querySelectorAll('#headRow th.supplier-head');
  heads.forEach((th, si)=>{
    th.classList.toggle('off', !supplierOn[si]);
  });

  const minimumTotals=suppliers.map(()=>0);
  let grandTotal = 0;
  let rowsCounted = 0;

  items.forEach(item=>{
    const cells = document.querySelectorAll(`td.price-cell[data-item="${item.id}"]`);
    let minSum = Infinity;
    let minCell = null;
    let anyIncluded = false;

    cells.forEach(td=>{
      const si = +td.dataset.supplier;
      const off = getOffer(item, suppliers[si].name);
      td.classList.remove('is-min');
      const badge = td.querySelector('.diff-badge');
      if(off === null) return;
      const key = item.id+'_'+si;
      const rejected = cellHasRejectedDiff(item.id, si);
      const included = supplierOn[si] && cellOn[key] && !rejected && realSum(off)!==null;
      td.classList.toggle('excluded', !included);
      td.classList.toggle('rejected-diff', rejected);
      if(badge){
        badge.classList.toggle('pending', cellHasUnresolvedRiskyDiff(item.id, si));
        badge.classList.toggle('resolved', !cellHasUnresolvedRiskyDiff(item.id, si) && !rejected);
      }
      if(included){
        anyIncluded = true;
        const sum = realSum(off);
        if(sum < minSum){ minSum = sum; minCell = td; }
      }
    });

    if(minCell){
      minCell.classList.add('is-min');minimumTotals[+minCell.dataset.supplier]+=minSum;
    }
    if(anyIncluded && isFinite(minSum)){
      grandTotal += minSum;
      rowsCounted++;
    }
  });

  const totalRow=document.getElementById('totalRow');while(totalRow.children.length>3)totalRow.removeChild(totalRow.lastChild);minimumTotals.forEach((value,si)=>{const td=document.createElement('td');td.className='foot-cell';td.dataset.supplier=si;td.textContent=supplierOn[si]?fmt(value):'—';totalRow.appendChild(td);});
  document.getElementById('grandTotal').textContent = fmt(grandTotal)+' ₽';
  document.getElementById('rowsCounted').textContent = rowsCounted+' / '+items.length;
  document.getElementById('activeSuppliers').textContent = supplierOn.filter(Boolean).length + ' / ' + suppliers.length;

  // "Сумма выбранных позиций" — минимальная РЕАЛЬНАЯ сумма среди отмеченных/непринятых-отклонённых ячеек по каждой строке
  let selectedTotal = 0;
  items.forEach(item=>{
    let rowMinSum = Infinity;
    suppliers.forEach((s, si)=>{
      const off = getOffer(item, s.name);
      if(off === null) return;
      const key = item.id+'_'+si;
      const rejected = cellHasRejectedDiff(item.id, si);
      if(supplierOn[si] && cellOn[key] && !rejected && realSum(off)!==null){
        const sum = realSum(off);
        if(sum < rowMinSum) rowMinSum = sum;
      }
    });
    if(isFinite(rowMinSum)) selectedTotal += rowMinSum;
  });
  document.getElementById('selectedTotal').textContent = fmt(selectedTotal)+' ₽';

  updateCellDisplay();

  // own totals per supplier
  const ownRow = document.getElementById('ownTotalRow');
  while(ownRow.children.length > 3) ownRow.removeChild(ownRow.lastChild);
  const ownTotals = [];
  suppliers.forEach((s, si)=>{
    const td = document.createElement('td');
    td.className = 'foot-cell';
    td.dataset.supplier = si;
    let total = 0;
    if(!supplierOn[si]){
      td.textContent = '—';
      td.style.opacity = .4;
      total = null;
    } else {
      items.forEach(item=>{
        const off = getOffer(item, s.name);
        if(off === null) return;
        const key = item.id+'_'+si;
        if(cellOn[key] && !cellHasRejectedDiff(item.id, si) && realSum(off)!==null) total += realSum(off);
      });
      td.textContent = fmt(total);
    }
    ownTotals.push(total);
    ownRow.appendChild(td);
  });

  // grand total with delivery
  const gwdRow = document.getElementById('grandWithDeliveryRow');
  while(gwdRow.children.length > 3) gwdRow.removeChild(gwdRow.lastChild);
  suppliers.forEach((s, si)=>{
    const td = document.createElement('td');
    td.className = 'foot-cell';
    td.dataset.supplier = si;
    if(!supplierOn[si] || ownTotals[si] === null){
      td.textContent = '—';
      td.style.opacity = .4;
    } else {
      let withDelivery = ownTotals[si];
      let marker = '';
      if(s.meta.delivery.included){
        // already inside item prices, nothing to add
      } else if(s.meta.delivery.amount !== null){
        withDelivery += s.meta.delivery.amount;
      } else {
        marker = ' *';
      }
      td.innerHTML = fmt(withDelivery) + marker;
      if(marker) td.title = 'Стоимость доставки не указана поставщиком — сумма без неё';
    }
    gwdRow.appendChild(td);
  });

  // re-apply hide state after footer rebuild
  applyHiddenColumns();
  document.querySelectorAll('.table-wrap .diff-btn,.table-wrap input[data-supplier]').forEach(e=>{if(initial.read_only)e.disabled=true;});
  if(ready)persist();
}

let ready=false;
buildHead();
buildBody();
buildFootStatic();
document.querySelectorAll('input[data-supplier]').forEach(cb=>{
  if(cb.closest('th')) cb.addEventListener('change', onSupplierToggle);
});
document.querySelectorAll('input[data-hide]').forEach(cb=>{
  cb.addEventListener('change', onHideToggle);
});
document.querySelectorAll('input[name="compareMode"]').forEach(radio=>{
  radio.addEventListener('change', (e)=>{
    compareMode = e.target.value;
    recompute();
  });
});
recompute();

lastSaved=JSON.stringify(stateSnapshot());ready=true;
document.getElementById('comparison-title').textContent='Сравнение поставщиков — '+(initial.order_name||initial.order_id);
document.querySelectorAll('input[name="compareMode"]').forEach(r=>r.checked=r.value===compareMode);
if(initial.read_only){document.body.classList.add('readonly');document.getElementById('save-status').textContent='Предложение уже отправлено: решения доступны только для просмотра.';}
document.getElementById('cmpTable').style.width=(460+suppliers.length*140)+'px';
