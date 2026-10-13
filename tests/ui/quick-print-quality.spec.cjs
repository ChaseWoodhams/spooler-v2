const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
let playwright;
try { playwright = require('playwright'); } catch { playwright = require(path.resolve(root, '../../spooler-v2/node_modules/playwright')); }
const { chromium } = playwright;
(async () => {
 const server = http.createServer((req,res) => { const file = path.join(root,'public',req.url.split('?')[0] === '/' ? 'index.html' : req.url.split('?')[0]); try { res.setHeader('Content-Type',file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');res.end(fs.readFileSync(file)); } catch {res.writeHead(404);res.end();} });
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const browser=await chromium.launch();
 try {
 const page=await browser.newPage({viewport:{width:390,height:844}});
 await page.routeWebSocket(/.*/,ws=>ws.close());
 let kind='model', warnings=[],blocked=false, queueWrites=0, sliceFailure=false;
 const project={id:'fixture',name:'Calibration cube',slices:{'1':{state:'sliced',estimate_s:1800,estimate_g:12}},data:{}};
 await page.route('**/api/**',route=>{ const url=new URL(route.request().url()).pathname; let data={};
 if(url==='/api/printers') data=[];
 if(url.endsWith('/preflight')) data={status:'warn',can_start:true,needs_acknowledgement:true,printer_id:'fake',preview:null,checks:[{id:'filament',level:'warn',title:'Verify material',detail:'Filament type is unknown'}]};
 if(url==='/api/projects/fixture') data=project;
 if(url.endsWith('/quick-options')) data={preset:'PLA',bed:'textured',spool_id:1,options:{layer_height:0.2,infill:15,walls:2,supports:'off',brim:'off'}};
 if(url==='/api/slicer') data={bundles:[{id:'PLA',label:'PLA standard'}]};
 if(url==='/api/slicer/spools') data={spools:[{id:1,name:'Blue PLA'}]};
 if(url.endsWith('/intake')) data={state:'ready',kind,file:kind==='gcode'?'cube.gcode':'cube.stl',size:[20,20,20],volume_cm3:8,fits:true,scale_ok:true,can_print:true,repaired:false,estimate:{seconds:1800,grams:12}};
 if(url.endsWith('/print')){ if(sliceFailure)return route.fulfill({status:503,json:{error:'Slicer is unavailable'}}); const body=route.request().postDataJSON(); if(!body.review)queueWrites++;data={...project,review:{can_start:!blocked,warnings,checks:blocked?[{level:'fail',title:'Printer offline',detail:'Connect the printer'}]:warnings}}; }
 return route.fulfill({json:data});});
 await page.goto(`http://127.0.0.1:${server.address().port}`);
 await page.evaluate(()=>{printers={fake:{id:'fake',name:'Fixture CC1',printer_type:'cc1',connected:true,status:{PrintInfo:{Status:0},CurrentStatus:[0]}}};});
 await page.evaluate(()=>QuickPrint.open({projectId:'fixture',printerId:'fake'}));
 await page.getByRole('button',{name:'Retry preview'}).waitFor();
 await page.evaluate(()=>printers.fake.connected=false);
 await page.getByRole('button',{name:'Slice and review'}).waitFor({state:'visible'});
 await page.waitForFunction(()=>document.querySelector('[data-qp="go"]').disabled);
 assert.ok((await page.locator('.qp-foot').innerText()).includes('offline'),'Disabled action must explain offline in visible text');
 await page.evaluate(()=>printers.fake.connected=true);
 await page.waitForFunction(()=>!document.querySelector('[data-qp="go"]').disabled);
 sliceFailure=true;
 await page.getByRole('button',{name:'Slice and review'}).click();
 await page.getByRole('alert').filter({hasText:'Slicer is unavailable'}).waitFor();
 sliceFailure=false;
 await page.getByRole('button',{name:'Slice and review'}).click();
 await page.getByRole('heading',{name:'Job review',exact:true}).waitFor();
 assert.equal(queueWrites,0,'Clean model must remain in review until user queues');
 for(const text of ['cube.stl','Fixture CC1','Textured PEI','Blue PLA','30 min','Bed leveling'])assert.ok((await page.locator('.qp-body').innerText()).includes(text),text);
 await page.getByRole('button',{name:'Back',exact:true}).click();
 warnings=[{level:'warn',title:'Filament unknown',detail:'Verify loaded filament'}];
 await page.getByRole('button',{name:'Slice and review'}).click();
 await page.getByRole('checkbox',{name:'I have reviewed the warnings'}).check();
 assert.equal(await page.getByRole('button',{name:'Add Job to Queue'}).isEnabled(),true);
 await page.getByRole('button',{name:'Back',exact:true}).click();
 await page.getByLabel('Plate side').selectOption('smooth');
 await page.getByRole('button',{name:'Slice and review'}).click();
 assert.equal(await page.getByRole('checkbox',{name:'I have reviewed the warnings'}).isChecked(),false);
 assert.equal(await page.getByRole('button',{name:'Add Job to Queue'}).isDisabled(),true);
 await page.getByRole('button',{name:'Back',exact:true}).click();blocked=true;
 await page.getByRole('button',{name:'Slice and review'}).click();
 await page.getByRole('heading',{name:'Blockers',exact:true}).waitFor();
 assert.equal(await page.getByRole('button',{name:'Add Job to Queue'}).isDisabled(),true);
 assert.ok((await page.locator('.qp-body').innerText()).includes('Connect the printer'));
 for(const width of [320,360,390,430,768,1024,1440]){await page.setViewportSize({width,height:900});assert.ok(await page.locator('.qp-card').evaluate(el=>el.scrollWidth<=el.clientWidth+1),'modal overflow '+width);}
 await page.keyboard.press('Escape');kind='gcode';blocked=false;warnings=[];
 await page.evaluate(()=>QuickPrint.open({projectId:'fixture',printerId:'fake'}));
 await page.getByRole('button',{name:'Review G-code'}).click();
 await page.getByRole('heading',{name:'Job review',exact:true}).waitFor();
 assert.equal(queueWrites,0);
 await page.keyboard.press('Escape');
 await page.locator('#btn-queue').focus();
 await page.evaluate(()=>openReview({id:77,name:'Fixture Job',file_name:'cube.gcode',printer_id:'fake',print_opts:{},source:'upload'}));
 await page.getByRole('heading',{name:'Warnings',exact:true}).waitFor();
 for(const width of [320,390,430,768,1440]){
  await page.setViewportSize({width,height:844});
  assert.ok(await page.locator('.review-box').evaluate(el=>el.scrollWidth<=el.clientWidth+1),'Job review overflow '+width);
 }
 await page.getByRole('button',{name:'Check again',exact:true}).focus();
 await page.keyboard.press('Shift+Tab');
 assert.ok(await page.locator('#modal-review').evaluate(el=>el.contains(document.activeElement)));
 assert.equal(await page.locator('#modal-review input[type=checkbox]:visible').count(),1,'one acknowledgement checkbox');
 assert.equal(await page.getByRole('button',{name:'Start print',exact:true}).isDisabled(),true,'Start waits for the acknowledgement');
 assert.match(await page.locator('#review-ack-text').innerText(),/reviewed the warnings.*build plate is clear/,'one tick covers the warnings and the bed');
 await page.locator('#review-ack').check();
 assert.equal(await page.getByRole('button',{name:'Start print',exact:true}).isEnabled(),true);
 await page.getByRole('button',{name:'Check again',exact:false}).click();
 assert.equal(await page.locator('#review-ack').isChecked(),false,'Rechecking resets bed confirmation');
 await page.keyboard.press('Escape');assert.equal(await page.locator('#btn-queue').evaluate(el=>el===document.activeElement),true);
 console.log('PASS: model/G-code review, summaries, warning reset, blockers, phone layouts');
 }finally{await browser.close();await new Promise(r=>server.close(r));}
})().catch(err=>{console.error(err);process.exitCode=1;});
