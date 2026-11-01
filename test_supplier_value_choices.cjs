const {JSDOM}=require('jsdom');
const fs=require('node:fs'),assert=require('node:assert/strict');
const values=[
  {id:'class8',value:'8',state:'matched',required:['8']},
  {id:'class10',value:'10',state:'pending',required:['8']}
];
const initial={embedded:true,csrf:'form',revision:'r',suppliers:[{name:'Поставщик'}],rows:[{
  position:1,name:'Гайка М20 DIN 934',quantity:240,unit:'шт',offers:[],
  value_comparison:{groups:[{field:'Класс прочности',values:values.map(v=>({...v,origin:'supplier_api'}))},
    {field:'Фасовка',values:[{id:'pack25',value:'25 кг',state:'matched',required:[],origin:'supplier_api'}]}],offers:[],minimum:null,suitable_count:0}
}]};
const dom=new JSDOM(`<p id="message"></p><table id="review-table"><colgroup><col><col><col></colgroup><tr><th>Позиция</th></tr><tr><td><div data-value-position="1"></div></td></tr></table><script id="comparison-data" type="application/json">${JSON.stringify(initial)}</script>`,{runScripts:'outside-only',url:'https://example.test/review'});
const doc=dom.window.document;let payload;
dom.window.fetch=async(url,request)=>{payload=JSON.parse(request.body);return {ok:true,redirected:false,headers:{get:()=> 'application/json'},json:async()=>initial}};
dom.window.eval(fs.readFileSync(__dirname+'/comparison_assets/values.js','utf8'));
(async()=>{
  const panel=doc.querySelector('[data-value-position="1"]');
  assert.match(panel.textContent,/Класс прочности.*8.*10/);
  assert.match(panel.textContent,/Фасовка.*25 кг/);
  assert.equal(panel.querySelector('details'),null);
  panel.querySelector('button[aria-label="Разрешить: Класс прочности 10"]').click();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(payload.action,'value');assert.equal(payload.decision,'accepted');
  assert.deepEqual(payload.features,['class10']);assert.equal(payload.position,1);
  panel.querySelector('button[aria-label="Исключить: Класс прочности 10"]').click();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(payload.decision,'rejected');assert.deepEqual(payload.features,['class10']);
  assert.equal(initial.rows[0].name,'Гайка М20 DIN 934');
  console.log('PASS: supplier-only values are visible in the selection column; per-value check/cross save exact decisions');
})();
