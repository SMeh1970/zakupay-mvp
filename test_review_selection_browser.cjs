// Run with Playwright installed or PLAYWRIGHT_MODULE pointing to its module.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');
const path = require('path');
const assert = require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.BROWSER_CHANNEL ? {channel:process.env.BROWSER_CHANNEL} : {})}); const page=await browser.newPage();
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.setContent(`<form id="review-form"></form><form id="position-search"><button id="include-all-priced" type="button">Select priced</button><button id="search-selected-sniper" name="engine" value="sniper" disabled>Sniper</button></form><table>${[1,2,3,4].map(n=>`<tr><td><input form="review-form" type="checkbox" name="include_${n}" data-bulk-allowed="${n===4?0:1}" ${n===4?'disabled':''}></td><td><select class="candidate-select" data-position="${n}"><option value="0" data-price="${n===2?'':10}" data-price-missing="${n===2?1:0}" data-details="details" data-supplier="Supplier" data-url="https://example.com">Found</option><option value="none">None</option></select><div class="candidate-details"></div><a class="candidate-source"></a><small class="price-note"></small><input class="price" id="price-${n}" value="${n===2?'':10}"></td></tr>`).join('')}</table>`);
 await page.addScriptTag({content:fs.readFileSync(path.join(__dirname,'automation_pipeline.py'),'utf8').match(/_REVIEW_SELECTION_SCRIPT = """([\s\S]*?)"""/)[1]});
 let requests=0;await page.route('**/*',r=>{requests++;r.abort()});
 await page.locator('#include-all-priced').click();
 assert.deepEqual(await page.locator('input[type=checkbox]:checked').evaluateAll(xs=>xs.map(x=>x.name)),['include_1','include_3']);assert.equal(requests,0);
 await page.locator('[name=include_1]').uncheck();await page.locator('[name=include_2]').check();
 await page.evaluate(()=>{const form=document.querySelector('#position-search');form.addEventListener('submit',e=>e.preventDefault());form.requestSubmit(document.querySelector('#search-selected-sniper'));});
 assert.deepEqual(await page.locator('[name=selected_position]').evaluateAll(xs=>xs.map(x=>x.value)),['2','3']);
 await page.locator('[data-position="3"]').selectOption('none');
 assert.equal(await page.locator('[name=include_3]').isChecked(),false);
 await page.locator('[name=include_2]').uncheck();assert.equal(await page.locator('#search-selected-sniper').isDisabled(),true);
 assert.deepEqual(errors,[]);assert.equal(requests,0);
 console.log('PASS: priced-only selection, no requests, manual selection including missing prices, exact Sniper positions, dropdown updates, empty selection disabled, no JS errors');
 const sellerHtml = `<meta charset="utf-8"><form id="review-form"></form><div id="invoice-default-summary" hidden><b id="invoice-default-name"></b></div><div id="invoice-seller-options"><select name="invoice_seller_id" form="review-form"><option value="">Choose</option><option value="sv">ООО СВ · ИНН 5638073633</option></select><select name="invoice_vat_rate" form="review-form"><option value="0.2">20%</option></select></div><input id="invoice-default" type="checkbox" data-save-url="/dashboard/automation/jobs/1/seller-default"><small id="invoice-default-error"></small>`;
 await page.unroute('**/*');
 let savedBodies=[];let fail=false;
 await page.route('**/*', async route=>{
  if(route.request().method()==='POST'){
   assert.equal(new URL(route.request().url()).pathname,'/dashboard/automation/jobs/1/seller-default');
   savedBodies.push(route.request().postData());
   assert.equal(route.request().headers()['x-requested-with'],'invoice-preferences');
   await route.fulfill({status:fail?500:200,contentType:'application/json',body:'{"saved":true}'});
  }else await route.fulfill({contentType:'text/html',body:sellerHtml});
 });
 await page.goto('http://invoice.test/dashboard/automation/jobs/1/review');
 await page.addScriptTag({content:fs.readFileSync(path.join(__dirname,'automation_pipeline.py'),'utf8').match(/_INVOICE_DEFAULT_SCRIPT = """([\s\S]*?)"""/)[1]});
 await page.locator('#invoice-default').click();
 assert.equal(await page.locator('#invoice-default').isChecked(),false);assert.equal(savedBodies.length,0);
 await page.locator('[name=invoice_seller_id]').selectOption('sv');
 await page.locator('#invoice-default').click();
 await page.waitForFunction(()=>!document.querySelector('#invoice-default').disabled);
 assert.equal(await page.locator('#invoice-seller-options').isVisible(),false);
 assert.equal(await page.locator('#invoice-default-name').innerText(),'ООО СВ');
 assert.equal(new URLSearchParams(savedBodies[0]).get('vat_rate'),'0.2');
 await page.locator('#invoice-default').uncheck();
 await page.waitForFunction(()=>!document.querySelector('#invoice-default').disabled);
 assert.equal(await page.locator('#invoice-seller-options').isVisible(),true);
 assert.equal(new URLSearchParams(savedBodies[1]).get('enabled'),'0');
 fail=true;await page.locator('#invoice-default').click();
 await page.waitForFunction(()=>!document.querySelector('#invoice-default').disabled);
 assert.equal(await page.locator('#invoice-default').isChecked(),false);
 assert.equal(await page.locator('#invoice-seller-options').isVisible(),true);
 assert.ok((await page.locator('#invoice-default-error').innerText()).length>0);
 assert.deepEqual(errors,[]);
 console.log('PASS: default firm validation, compact view, saved VAT, unpin, failed-save rollback');
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});

