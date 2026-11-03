const {JSDOM}=require('jsdom');
const fs=require('node:fs'),assert=require('node:assert/strict');
const supplier='ВсеИнструменты.ру',name='Электрическая таль TOR HHBD05-02T 5 т 6 м 380 В';
const source={key:'tor',supplier,sku:'16588376',name,url:'https://example.test/tor',price:198272,origin:'supplier_api',evidence:'Вес нетто: 162'};
const value={id:'weight162',value:'162 кг',required:[],state:'matched',sources:[source]};
const data={embedded:true,csrf:'csrf',revision:'r',suppliers:[{name:supplier}],rows:[{position:1,name:'Таль цепная электрическая 5 тонн',quantity:1,unit:'изд.',
  supplier_search:{[supplier]:{state:'review',label:'Найдены варианты: выберите характеристики',errors:[],rejected:[]}},
  value_comparison:{groups:[{field:'Вес нетто',values:[value]}],minimum:null,suitable_count:0,offers:[{
    ...source,comparable:false,comparison_reasons:['Не разрешено: Модель тали'],unit_price:198272,price:198272,unit:'шт',
    quantity:1,converted_quantity:1,total:198272,factor:1,availability:'Наличие требует подтверждения',features:[]}]} }]};
const dom=new JSDOM(`<p id="message"></p><table id="review-table"><colgroup><col><col><col></colgroup><tr><th>Позиция</th></tr><tr><td><div data-value-position="1"></div></td></tr></table><script id="comparison-data" type="application/json">${JSON.stringify(data)}</script>`,{runScripts:'outside-only',url:'https://example.test/review'});
const doc=dom.window.document;
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new dom.window.Event('close'))};
dom.window.fetch=()=>{throw new Error('Reading sources must not change the invoice')};
dom.window.eval(fs.readFileSync(__dirname+'/comparison_assets/values.js','utf8'));
// The hoist is not shown in the supplier column (not comparable), so its values do not appear
// under the request; the offer and the reason it is not allowed stay visible in the supplier dialog.
assert.equal(doc.querySelector('.supplier-found-values'),null);
assert.equal(doc.querySelector('button[aria-label="Разрешить: Вес нетто 162 кг"]'),null);
doc.querySelector('[data-supplier-column] button')?.click();
// The first supplier-column marker is a col; target its actual price cell.
doc.querySelector('td[data-supplier-column] button').click();
assert.match(doc.querySelector('dialog .supplier-choice-list').textContent,/HHBD05-02T/);
assert.match(doc.querySelector('dialog .supplier-choice-list').textContent,/Не разрешено: Модель тали/);
assert.equal(doc.querySelector('dialog .supplier-choice-option button').disabled,true);
console.log('PASS: values of a product not shown in the column are hidden; pending hoist is visible in the dialog without invoice eligibility');
