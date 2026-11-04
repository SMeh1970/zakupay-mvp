// The supplier dialog shows each product's characteristics coloured like the values in the
// common column, and a short status line explaining why the product can or cannot be chosen.
// npm install jsdom; NODE_PATH must include its installation directory.
const {JSDOM}=require('jsdom');
const fs=require('node:fs');
const assert=require('node:assert/strict');
const size100={id:'s100',field:'Размер 2',value:'100 мм',required:['100 мм'],state:'matched'};
const size90={id:'s90',field:'Размер 2',value:'90 мм',required:['100 мм'],state:'pending'};
const brand={id:'b',field:'Бренд',value:'fischer',required:[],state:'matched'};
const base={supplier:'А',unit:'шт',quantity:2,converted_quantity:100,shortage:0,factor:50,availability:'В наличии'};
const good={...base,key:'good',name:'Анкер fischer FSL M10x100 (50 шт.)',unit_price:118,price:5898,total:11796,comparable:true,comparison_reasons:[],features:[size100,brand]};
const other={...base,key:'other',name:'Анкер-болт Fasty M10x90 (100 шт.)',unit_price:84,price:8417,total:8417,comparable:false,
  comparison_reasons:['Не разрешено: Размер 2'],features:[size90]};
const data={embedded:true,read_only:false,csrf:'c',revision:'r',suppliers:[{name:'А'}],rows:[{position:2,name:'Анкерный болт для бетона М10х100',unit:'шт',quantity:100,
  invoice:{},supplier_search:{А:{state:'found',label:'Есть допустимые варианты',errors:[],rejected:[]}},
  value_comparison:{offers:[good,other],minimum:good,minima:[good],groups:[
    {field:'Размер 2',values:[{...size100,sources:[{key:'good'}]},{...size90,sources:[{key:'other'}]}]},
    {field:'Бренд',values:[{...brand,sources:[{key:'good'}]}]}]}}]};
const dom=new JSDOM(`<p id="message"></p><table id="review-table"><colgroup>${'<col>'.repeat(5)}</colgroup><tr><th>Позиция</th></tr><tr><td><div data-value-position="2"></div></td></tr></table><script id="comparison-data" type="application/json">${JSON.stringify(data)}</script>`,{runScripts:'outside-only'});
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false};
dom.window.eval(fs.readFileSync(__dirname+'/comparison_assets/values.js','utf8'));
const doc=dom.window.document;
doc.querySelector('td[data-supplier-column] button').click();
const options=()=>Array.from(doc.querySelectorAll('dialog .supplier-choice-option'));
const [first,second]=options();
assert.match(first.querySelector('.supplier-choice-status').textContent,/Подходит/);
assert.match(first.querySelector('.supplier-choice-features').textContent,/Размер 2: 100 мм.*Бренд: fischer/);
assert.match(second.querySelector('.supplier-choice-status').textContent,/Нужно решение: отличия — Размер 2: 90 мм \(в заявке 100 мм\)/);
const ninety=second.querySelector('.supplier-choice-features span');
assert.equal(ninety.textContent,'Размер 2: 90 мм');
assert.match(ninety.style.cssText,/font-weight: 600/);
// Marking ✕ on 100 мм in the common column recolours the dialog at once (nothing is sent).
let sent=0;dom.window.fetch=async()=>{sent++;throw new Error('no request expected')};
doc.querySelector('[data-value-position="2"] button[aria-label="Исключить: Размер 2 100 мм"]').click();
const recoloured=options()[0].querySelector('.supplier-choice-features span');
assert.match(recoloured.style.cssText,/line-through/);
assert.equal(sent,0);
console.log('PASS: dialog shows coloured characteristics and a status line; marks recolour them');
