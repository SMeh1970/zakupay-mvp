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
 console.log('PASS: priced-only selection, no requests, manual selection including missing prices, exact Sniper positions, dropdown updates, empty selection disabled, no JS errors');await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});

