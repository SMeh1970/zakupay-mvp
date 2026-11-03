// Values under "Значения из найденных товаров" come only from products shown in supplier columns.
// npm install jsdom; NODE_PATH must include its installation directory.
const {JSDOM}=require('jsdom');
const fs=require('node:fs');
const assert=require('node:assert/strict');
const src=(key)=>({key,supplier:'А',sku:key,name:'Товар '+key});
const shown={key:'shown',supplier:'А',unit_price:100,price:100,total:100,converted_quantity:1,shortage:0,name:'Отвод показанный',
  features:[],comparable:true,quantity:1,unit:'шт',availability:'В наличии'};
const hidden={...shown,key:'hidden',name:'Коллектор распределительный',comparable:false};
const data={embedded:true,read_only:false,csrf:'c',revision:'r',suppliers:[{name:'А'}],rows:[{position:1,name:'Отвод (седелка) грувлок',unit:'шт',quantity:1,
  invoice:{},supplier_search:{},offers:[shown,hidden],
  value_comparison:{offers:[shown,hidden],minimum:shown,minima:[shown],groups:[
    {field:'Бренд',values:[
      {id:'b1',field:'Бренд',value:'stout',required:[],state:'matched',sources:[src('shown')]},
      {id:'b2',field:'Бренд',value:'wirquin',required:[],state:'matched',sources:[src('hidden')]}]},
    {field:'Вид',values:[
      {id:'v1',field:'Вид',value:'коллектор распределительный',required:[],state:'matched',sources:[src('hidden')]}]}]}}]};
const dom=new JSDOM(`<p id="message"></p><table id="review-table"><colgroup>${'<col>'.repeat(5)}</colgroup><tr><th>Позиция</th></tr><tr><td><div data-value-position="1"></div></td></tr></table><script id="comparison-data" type="application/json">${JSON.stringify(data)}</script>`,{runScripts:'outside-only'});
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true};
dom.window.eval(fs.readFileSync(__dirname+'/comparison_assets/values.js','utf8'));
const text=dom.window.document.querySelector('[data-value-position="1"]').textContent;
assert.match(text,/stout/);
assert.doesNotMatch(text,/wirquin/);
assert.doesNotMatch(text,/коллектор распределительный/);
assert.doesNotMatch(text,/Вид:/);
// No shown products at all: no block of found values.
data.rows[0].value_comparison.offers[0].comparable=false;
const dom2=new JSDOM(`<p id="message"></p><table id="review-table"><colgroup>${'<col>'.repeat(5)}</colgroup><tr><th>Позиция</th></tr><tr><td><div data-value-position="1"></div></td></tr></table><script id="comparison-data" type="application/json">${JSON.stringify(data)}</script>`,{runScripts:'outside-only'});
dom2.window.HTMLDialogElement.prototype.showModal=function(){this.open=true};
dom2.window.eval(fs.readFileSync(__dirname+'/comparison_assets/values.js','utf8'));
assert.equal(dom2.window.document.querySelector('.supplier-found-values'),null);
console.log('ok');
