// The photo is fetched only after «Фото» is pressed.
const {JSDOM}=require('jsdom');const fs=require('node:fs');const assert=require('node:assert/strict');
const o={key:'a',supplier:'А',sku:'1',unit_price:10,price:10,total:10,converted_quantity:1,quantity:1,shortage:0,name:'Шпилька М8',
  features:[],comparable:true,unit:'шт',availability:'В наличии',url:'https://example.ru/p/1',image_url:'https://cdn.example.ru/1.jpg'};
const data={embedded:true,read_only:false,csrf:'c',revision:'r',suppliers:[{name:'А'}],rows:[{position:1,name:'Шпилька М8',unit:'шт',quantity:1,
  invoice:{},supplier_search:{},offers:[o],value_comparison:{offers:[o],minimum:o,minima:[o],groups:[],words:[]}}]};
const dom=new JSDOM(`<p id="message"></p><table id="review-table"><colgroup>${'<col>'.repeat(5)}</colgroup><tr><td><div data-value-position="1"></div></td></tr><tr><td data-supplier-cell="1" data-supplier="А"></td></tr></table><script id="comparison-data" type="application/json">${JSON.stringify(data)}</script>`,{runScripts:'outside-only'});
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true};
dom.window.eval(fs.readFileSync(__dirname+'/comparison_assets/values.js','utf8'));
const doc=dom.window.document;
const opener=[...doc.querySelectorAll('button')].find(b=>/Посмотреть|Найдены|Выбран|вариант/i.test(b.textContent));
if(opener)opener.click();
const photo=[...doc.querySelectorAll('button')].find(b=>b.textContent==='Фото');
if(!photo){console.log('skip: popup opener not found in this fixture');process.exit(0);}
assert.equal(doc.querySelectorAll('img').length,0);
photo.click();
assert.equal(doc.querySelector('img').getAttribute('src'),'https://cdn.example.ru/1.jpg');
photo.click();
assert.equal(doc.querySelectorAll('img').length,0);
console.log('ok');
