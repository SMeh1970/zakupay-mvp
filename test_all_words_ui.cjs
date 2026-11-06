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
    words:[{word:'шпилька',count:3,state:'pending'},{word:'zitar',count:1,state:'pending'},{word:'976',count:1,state:'rejected'}]}}]};
const dom=new JSDOM(`<p id="message"></p><table id="review-table"><colgroup>${'<col>'.repeat(5)}</colgroup><tr><th>Позиция</th></tr><tr><td><div data-value-position="1"></div></td></tr></table><script id="comparison-data" type="application/json">${JSON.stringify(data)}</script>`,{runScripts:'outside-only'});
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true};
const sent=[];
dom.window.fetch=async(url,options)=>{sent.push(JSON.parse(options.body));return {ok:true,json:async()=>({ok:true})};};
dom.window.eval(fs.readFileSync(__dirname+'/comparison_assets/values.js','utf8'));
const doc=dom.window.document;
const box=doc.querySelector('.found-words');
assert.ok(box,'block of words is shown');
assert.match(box.textContent,/Все слова найденных товаров \(3\)/);
assert.match(box.textContent,/шпилька \(3\)/);
assert.match(box.textContent,/zitar \(1\)/);
assert.match(box.textContent,/976 \(1\)/);
assert.equal(box.querySelector('[aria-label="Исключить: Слово 976"]').getAttribute('aria-pressed'),'true');
box.querySelector('[aria-label="Исключить: Слово zitar"]').click();
assert.match(doc.getElementById('message').textContent,/Применить/);
const apply=[...doc.querySelectorAll('button')].find(b=>b.textContent.trim()==='Применить');
assert.ok(apply,'apply button');
apply.click();
setTimeout(()=>{
  const body=sent.find(b=>b.action==='values_batch');
  assert.ok(body,'values_batch sent');
  assert.deepEqual(body.decisions,[{position:1,decision:'rejected',features:['word:zitar']}]);
  console.log('ok');
},50);
