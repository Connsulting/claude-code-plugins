// Read-only browser proof against an isolated preview created with --examples.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
(async()=>{
 const root=process.argv[3];
 if(!root||!process.argv[2])throw new Error('Usage: node tests/async_work_browser.cjs HTTPS_PREVIEW_URL ARTIFACT_DIRECTORY');
 fs.mkdirSync(root+'/screenshots',{recursive:true});
 const browser=await chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
 const context=await browser.newContext({viewport:{width:1440,height:1100}});
 const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const url=process.argv[2].replace(/\/$/,'');
 try {
 await page.goto(url,{waitUntil:'domcontentloaded'});
 assert(await page.locator('.preview-banner').isVisible());
 assert((await page.locator('#qlist .qrow').count())>3);
 await page.locator('.fchip[data-group="workgroup"][data-value="Preview examples"]').click();
 assert.equal(await page.locator('#qlist .qrow:visible').count(),3);
 const row=id=>page.locator('#qlist .qrow').filter({has:page.locator('.task-toggle[data-task-id="'+id+'"]')});
 assert.equal(await page.locator('.task-edit, #work-editor').count(),0);
 assert.equal(await page.locator('.capacity-fold, .work-summary, .filter-more').count(),0);
 for(const chip of await page.locator('.fchip').all())assert(await chip.isVisible());
 const alignment=await page.locator('.fchip[data-value="cooldown"]').evaluate(el=>{const count=el.querySelector(':scope > i');return {label:getComputedStyle(el).fontSize,count:getComputedStyle(count).fontSize}});
 assert.equal(alignment.label,alignment.count);
 const rotation=page.locator('[data-fold="rotation"]');
 const capacity=page.locator('[data-fold="drain"]');
 assert(await rotation.isVisible());assert(await capacity.isVisible());
 assert(await rotation.locator('.mfold-body').isVisible());
 assert(await capacity.locator('.mfold-body').isVisible());
 assert.equal(await capacity.locator('.arow').count(),5);
 assert((await rotation.boundingBox()).y<(await page.locator('#qlist').boundingBox()).y);
 assert((await capacity.boundingBox()).y<(await page.locator('#qlist').boundingBox()).y);
 assert.equal(await row('preview-03-review').getAttribute('data-state'),'waiting');
 assert.equal(await page.locator('.task-run:not([disabled])').count(),0);
 const run=await context.request.post(url+'/api/bonus/task/run',{headers:{Origin:url},data:{id:'preview-02-build',engine:'auto'}});
 assert.equal(run.status(),400);assert((await run.text()).includes('execution is disabled'));
 const rejected=await context.request.post(url+'/api/bonus/task/run',{headers:{Origin:'https://attacker.example'},data:{id:'preview-02-build',engine:'auto'}});
 assert.equal(rejected.status(),403);
 await page.screenshot({path:root+'/screenshots/desktop.png'});
 await page.setViewportSize({width:390,height:844});
 await page.screenshot({path:root+'/screenshots/mobile.png'});
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 for(const chip of await page.locator('.fchip').all())assert(await chip.isVisible());
 assert(await rotation.isVisible());assert(await capacity.isVisible());
 for(const section of [rotation,capacity]){
   if(!await section.locator('.mfold-body').isVisible())await section.locator(':scope > .mfold-sum').click();
   assert(await section.locator('.mfold-body').isVisible());
 }
 await page.screenshot({path:root+'/screenshots/mobile-capacity.png'});
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({https:true,rotation_and_usage_visible:true,no_job_editor:true,dependency_readiness:true,dispatch_disabled:true,cross_origin_rejected:true,mobile_no_overflow:true,page_errors:errors,screenshots:root+'/screenshots'}));
 } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exit(1)});
