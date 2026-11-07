// Every word of found product names is shown once with its count and ✓/✕ marks applied with «Применить».
// npm install jsdom; NODE_PATH must include its installation directory.
const {JSDOM}=require('jsdom');
const fs=require('node:fs');
const assert=require('node:assert/strict');
const offer={key:'a',supplier:'А',unit_price:100,price:100,total:100,converted_quantity:1,shortage:0,name:'Шпилька Zitar',
  features:[],comparable:true,quantity:1,unit:'шт',availability:'В наличии'};
const data={embedded:true,read_only:false,csrf:'c',revision:'r',suppliers:[{name:'А'}],rows:[{position:1,name:'Шпилька М8',unit:'шт',quantity:1,
  invoice:{},supplier_search:{},offers:[offer],
  value_comparison:{offers:[offer],minimum:offer,minima:[offer],groups:[],
    words:[{word:'шпилька',count:3,state:'pending'},{word:'zitar',count:1,state:'pending'},{word:'976',count:1,state:'rejected'}],
    word_products:[['шпилька','zitar'],['шпилька'],['шпилька','976']]}}]};
const dom=new JSDOM(`<p id="message"></p><table id="review-table"><colgroup>${'<col>'.repeat(5)}</colgroup><tr><th>Позиция</th></tr><tr><td><div data-value-position="1"></div></td></tr></table><script id="comparison-data" type="application/json">${JSON.stringify(data)}</script>`,{runScripts:'outside-only'});
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true};
const sent=[];
dom.window.fetch=async(url,options)=>{sent.push(JSON.parse(options.body));return {ok:true,json:async()=>({ok:true})};};
dom.window.eval(fs.readFileSync(__dirname+'/comparison_assets/values.js','utf8'));
const doc=dom.window.document;
const box=doc.querySelector('.found-words');
assert.ok(box,'block of words is shown');
assert.match(box.textContent,/шпилька \(2\)/);
assert.match(box.textContent,/zitar \(1\)/);
assert.match(box.textContent,/976 \(0\)/);
assert.equal(box.querySelector('[aria-label="Исключить: Слово 976"]').getAttribute('aria-pressed'),'true');
// 976 is already ✕: two products remain; ✓ zitar leaves one, «шпилька» in it turns green.
assert.match(box.textContent,/осталось товаров 2 из 3/);
doc.querySelector('.found-words [aria-label="Разрешить: Слово zitar"]').click();
const live=doc.querySelector('.found-words');
assert.match(live.textContent,/осталось товаров 1 из 3/);
assert.equal(live.querySelector('[aria-label="Разрешить: Слово шпилька"]').getAttribute('aria-pressed'),'true');
assert.match(doc.getElementById('message').textContent,/Применить/);
const apply=[...doc.querySelectorAll('button')].find(b=>b.textContent.trim()==='Применить');
assert.ok(apply,'apply button');
apply.click();
setTimeout(()=>{
  const body=sent.find(b=>b.action==='values_batch');
  assert.ok(body,'values_batch sent');
  assert.deepEqual(body.decisions,[{position:1,decision:'accepted',features:['word:zitar']}]);
  console.log('ok');
},50);
{
  // ✕ marks only its own word; words left without products are dimmed, not marked ✕.
  const d=JSON.parse(JSON.stringify(data));
  d.rows[0].value_comparison.words=[{word:'фланец',count:2,state:'pending'},{word:'стальной',count:2,state:'pending'},{word:'щетка',count:1,state:'pending'}];
  d.rows[0].value_comparison.word_products=[['фланец','стальной'],['фланец','стальной'],['щетка']];
  const dom2=new JSDOM(`<p id="message"></p><table id="review-table"><colgroup>${'<col>'.repeat(5)}</colgroup><tr><td><div data-value-position="1"></div></td></tr></table><script id="comparison-data" type="application/json">${JSON.stringify(d)}</script>`,{runScripts:'outside-only'});
  dom2.window.HTMLDialogElement.prototype.showModal=function(){};
  dom2.window.eval(fs.readFileSync(__dirname+'/comparison_assets/values.js','utf8'));
  const q=s=>dom2.window.document.querySelector('.found-words [aria-label="'+s+'"]');
  q('Исключить: Слово фланец').click();
  assert.equal(q('Исключить: Слово стальной').getAttribute('aria-pressed'),'false');
  assert.equal(q('Исключить: Слово фланец').getAttribute('aria-pressed'),'true');
  q('Исключить: Слово фланец').click();
  q('Разрешить: Слово фланец').click();
  assert.equal(q('Исключить: Слово щетка').getAttribute('aria-pressed'),'true');
  assert.equal(q('Разрешить: Слово стальной').getAttribute('aria-pressed'),'true');
  console.log('ok cross');
}
{
  // Request words are ✓ by default, framed green, listed under «Выбрано» and can be taken off.
  const d=JSON.parse(JSON.stringify(data));
  d.rows[0].value_comparison.words=[{word:'фланец',count:2,state:'accepted',from_request:true},{word:'стальной',count:2,state:'pending'},{word:'щетка',count:1,state:'pending'}];
  d.rows[0].value_comparison.word_products=[['фланец','стальной'],['фланец','стальной'],['щетка']];
  const dom3=new JSDOM(`<p id="message"></p><table id="review-table"><colgroup>${'<col>'.repeat(5)}</colgroup><tr><td><div data-value-position="1"></div></td></tr></table><script id="comparison-data" type="application/json">${JSON.stringify(d)}</script>`,{runScripts:'outside-only'});
  dom3.window.HTMLDialogElement.prototype.showModal=function(){};
  dom3.window.eval(fs.readFileSync(__dirname+'/comparison_assets/values.js','utf8'));
  const doc3=dom3.window.document;
  const q=s=>doc3.querySelector('[aria-label="'+s+'"]');
  assert.equal(q('Разрешить: Слово фланец').getAttribute('aria-pressed'),'true');
  assert.match(doc3.querySelector('.found-words-chosen').textContent,/✓ фланец/);
  assert.equal(q('Исключить: Слово щетка').getAttribute('aria-pressed'),'true'); // only in non-flange products
  q('Отменить выбор: фланец').click();
  assert.equal(doc3.querySelector('.found-words-chosen'),null);
  assert.equal(q('Разрешить: Слово фланец').getAttribute('aria-pressed'),'false');
  assert.equal(q('Исключить: Слово щетка').getAttribute('aria-pressed'),'false');
  console.log('ok defaults');
}
{
  // Default ✓ is a proposal until taken: «Принять слова заявки» marks them for «Применить».
  const d=JSON.parse(JSON.stringify(data));
  d.rows[0].value_comparison.words=[{word:'фланец',count:2,state:'accepted',from_request:true,explicit:false},{word:'щетка',count:1,state:'pending'}];
  d.rows[0].value_comparison.word_products=[['фланец'],['фланец'],['щетка']];
  const dom4=new JSDOM(`<p id="message"></p><table id="review-table"><colgroup>${'<col>'.repeat(5)}</colgroup><tr><td><div data-value-position="1"></div></td></tr></table><script id="comparison-data" type="application/json">${JSON.stringify(d)}</script>`,{runScripts:'outside-only'});
  dom4.window.HTMLDialogElement.prototype.showModal=function(){};
  dom4.window.eval(fs.readFileSync(__dirname+'/comparison_assets/values.js','utf8'));
  const doc4=dom4.window.document;
  assert.match(doc4.querySelector('.found-words summary').textContent,/как предложение/);
  [...doc4.querySelectorAll('button')].find(b=>b.textContent==='Принять слова заявки').click();
  assert.match(doc4.querySelector('.value-apply-bar').textContent,/Отмечено решений: 1/);
  assert.doesNotMatch(doc4.querySelector('.found-words summary').textContent,/как предложение/);
  console.log('ok proposal');
}
