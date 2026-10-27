const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element {
  constructor(tag,text=''){this.tag=tag;this.textContent=text;this.children=[];this.attributes={};}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  setAttribute(k,v){this.attributes[k]=v;}
  get text(){return this.textContent+this.children.map(n=>n.text||'').join('');}
}
const feature=(id,value)=>({id,value,required:[value],state:'matched'});
const row={position:1,name:'Труба 26,9х2,8 мм',unit:'м',value_comparison:{groups:[
  {field:'Диаметр',values:[feature('d','26.9 мм')]},
  {field:'Стенка',values:[feature('t','2.8 мм'),{id:'alt',value:'3 мм',required:['2.8 мм'],state:'pending'}]}
],minimum:{unit_price:100,supplier:'А'}}};
const initial={embedded:true,csrf:'test',revision:'old',action_url:'/comparison/action',rows:[row]};
const target=new Element('div'),message=new Element('p');let posted;
const document={
  getElementById:id=>id==='comparison-data'?{textContent:JSON.stringify(initial)}:id==='message'?message:null,
  querySelector:()=>target,
  createElement:tag=>new Element(tag),createTextNode:text=>new Element('text',text)
};
const context={document,location:{pathname:'/review'},fetch:async(url,options)=>{
  posted={url,payload:JSON.parse(options.body)};
  return {ok:true,redirected:false,headers:{get:()=> 'application/json'},json:async()=>({...initial,revision:'new',rows:[{...row,value_comparison:{...row.value_comparison,minimum:{unit_price:60,supplier:'Б'}}}]})};
}};
vm.runInNewContext(fs.readFileSync('comparison_assets/values.js','utf8'),context);
assert.match(target.text,/26,9/);assert.match(target.text,/2,8/);assert.match(target.text,/100 ₽/);
const descend=e=>[e,...e.children.flatMap(descend)];
const buttons=descend(target).filter(e=>e.tag==='button');
assert.equal(buttons.length,6);assert.equal(descend(target).filter(e=>['h1','h2','b'].includes(e.tag)).length,0);
(async()=>{
  await buttons.find(b=>b.attributes['aria-label']==='Разрешить: Стенка 3 мм').onclick();
  assert.equal(posted.url,'/comparison/action');assert.equal(posted.payload.feature,'alt');
  assert.equal(posted.payload.revision,'old');assert.match(target.text,/60 ₽/);
  console.log('Inline names: decimal comma/dot, borderless controls and price refresh passed');
})();
