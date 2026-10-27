const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs=require('fs');
const path=require('path');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.BROWSER_CHANNEL?{channel:process.env.BROWSER_CHANNEL}:{})});
 const page=await browser.newPage();let calls=[];const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const fixture=`<meta charset="utf-8"><button id="check-actuality" data-jobs="[1,2,3]" disabled>Check</button><span id="actuality-progress"></span><div data-actuality-job="1"></div><div data-actuality-job="2"></div>`;
 await page.route('**/*',async route=>{
  const request=route.request();
  if(request.method()==='POST'){
   assert.ok(request.url().endsWith('/actuality'));
   assert.equal(request.headers()['x-requested-with'],'order-actuality');
   const id=Number(request.url().split('/').at(-2));calls.push(id);
   if(id===3) return route.fulfill({status:503,body:'Unavailable'});
   return route.fulfill({contentType:'application/json',body:JSON.stringify({state:id===1?'active':'inactive',label:id===1?'Актуальна':'Неактуальна'})});
  }
  return route.fulfill({contentType:'text/html',body:fixture});
 });
 await page.goto('http://actuality.test/dashboard/automation');
 await page.addScriptTag({content:fs.readFileSync(path.join(__dirname,'order_actuality.py'),'utf8').match(/SCRIPT = """([\s\S]*?)"""/)[1]});
 assert.deepEqual(calls,[]);
 await page.locator('#check-actuality').click();
 await page.waitForFunction(()=>!document.querySelector('#check-actuality').disabled);
 assert.deepEqual(calls,[1,2,3]);
 assert.equal(await page.locator('[data-actuality-job="1"]').innerText(),'Актуальна');
 assert.equal(await page.locator('[data-actuality-job="2"]').innerText(),'Неактуальна');
 assert.ok((await page.locator('#actuality-progress').innerText()).includes('Проверено: 3. Неактуальных: 1. Не удалось подтвердить: 1.'));
 assert.deepEqual(errors,[]);
 console.log('PASS: explicit bulk check, all filtered IDs including off-page, progress, unknown errors, badge updates, no search requests');
 await browser.close();
})().catch(error=>{console.error(error);process.exit(1)});
