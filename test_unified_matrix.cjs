// npm install jsdom; NODE_PATH must include its installation directory.
const {JSDOM}=require('jsdom');
const fs=require('node:fs');
const assert=require('node:assert/strict');
const feature={id:'size',field:'Размер',value:'4 мм',required:['4 мм'],state:'matched'};
const offer=(key,supplier,price)=>({key,supplier,unit_price:price,price,total:price*2,converted_quantity:2,shortage:0,name:'Труба '+key,features:[feature],comparable:true,quantity:2,unit:'шт',availability:'В наличии'});
const a=offer('a','А',100), b=offer('b','Б',100), c=offer('c','В',102);
function data(alternative=false) {
  return {embedded:true,read_only:false,csrf:'csrf',revision:'r1',suppliers:['А','Б','В'].map(name=>({name})),rows:[{position:1,name:'Труба 4 мм',unit:'шт',value_comparison:{groups:[{field:'Размер',values:[feature]}],minimum:a,minima:[a,b],recommendation:alternative?c:a,coverage:{А:1,Б:1,В:2},offers:alternative?[a,b,c]:[a,b,{...c,comparable:false,comparison_reasons:['Не разрешено: Размер']}]} }]};
}
(async()=>{
  const initial=data();
  initial.rows[0].value_comparison.groups[0].values.push(...[1,2,3].map(i=>({id:'unknown'+i,field:'Размер',value:'Не указано',required:['4 мм'],state:'pending',ambiguous:true,offer_label:'Фирма '+i})));
  const dom=new JSDOM(`<p id="message"></p><table id="review-table"><colgroup>${'<col>'.repeat(5)}</colgroup><tr><th>Позиция</th></tr><tr><td><div data-value-position="1"></div></td></tr></table><script id="comparison-data" type="application/json">${JSON.stringify(initial)}</script>`,{runScripts:'outside-only',url:'https://example.test/review'});
  dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true};
  dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new dom.window.Event('close'))};
  let calls=0;
  dom.window.fetch=async()=>{calls++;return {ok:true,redirected:false,headers:{get:()=> 'application/json'},json:async()=>data(true)}};
  dom.window.eval(fs.readFileSync(__dirname+'/comparison_assets/values.js','utf8'));
  const doc=dom.window.document;
  assert.equal(doc.querySelectorAll('th[data-supplier-column]').length,3);
  assert.equal(doc.querySelectorAll('col[data-supplier-column]').length,3);
  assert.equal(doc.querySelector('col[data-supplier-column]').style.width,'220px');
  assert.equal(doc.querySelector('.value-extra').open,false);
  assert.equal(doc.querySelector('.value-extra details summary').textContent,'Не указано · предложений: 3');
  assert.ok(!Array.from(doc.querySelector('.value-panel').childNodes).filter(n=>n.nodeType===1 && n.tagName!=='DETAILS').some(n=>n.textContent.includes('Не указано')));
  assert.equal(doc.querySelectorAll('td[data-supplier-column]').length,3);
  assert.match(doc.querySelector('[data-value-position]').textContent,/А · Труба a; Б · Труба b/);
  assert.match(doc.querySelectorAll('td[data-supplier-column]')[2].textContent,/Нет допустимых вариантов/);
  assert.equal(doc.querySelectorAll('td[data-supplier-column] details').length,0);
  doc.querySelector('button').click();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls,1);
  assert.equal(doc.querySelectorAll('th[data-supplier-column]').length,3);
  assert.match(doc.querySelectorAll('td[data-supplier-column]')[2].textContent,/102/);
  assert.match(doc.querySelector('[data-value-position]').textContent,/Рекомендуется: В/);
  assert.match(doc.querySelector('[data-value-position]').textContent,/Абсолютный минимум: 100/);
  const many=data(true);
  many.rows[0].value_comparison.offers=Array.from({length:12},(_,i)=>({...offer('v'+i,'А',100+i),features:[{...feature,value:i+' мм'}]}));
  doc.getElementById('comparison-data').textContent=JSON.stringify(many);
  dom.window.eval(fs.readFileSync(__dirname+'/comparison_assets/values.js','utf8'));
  assert.equal(doc.querySelectorAll('td[data-supplier-column] .supplier-quote').length,3);
  doc.querySelector('td[data-supplier-column] button').click();
  assert.equal(doc.querySelector('dialog').open,true);
  assert.equal(doc.querySelectorAll('.supplier-choice-option').length,12);
  console.log('PASS: supplier columns, tied minima, filter refresh, newly allowed supplier, coverage recommendation and separate absolute minimum');
})();
