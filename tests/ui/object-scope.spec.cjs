const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '../../public');
(async () => {
 const browser = await chromium.launch({headless:true});
 try {
  const page = await browser.newPage();
  page.setDefaultTimeout(5000);
  await page.route('http://spooler.test/**', route => {
   const pathname = new URL(route.request().url()).pathname;
   if(pathname === '/test') return route.fulfill({contentType:'text/html',body:'<div id="tabs"></div><div id="pages"></div><div id="body"></div><input id="search"><select id="mode"><option>expert</option></select>'});
   const file=path.join(root,pathname);
   return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'text/javascript':'application/json'});
  });
  await page.goto('http://spooler.test/test');
  await page.evaluate(async()=>{
   const {SettingsPanel}=await import('/slicer/settings.js');
   const schema=await(await fetch('/orca/settings.json')).json();
   schema.tabs.process=[{page:'Quality',groups:[{group:'Fixture',rows:['travel_speed','wall_loops'].map(key=>({options:[key]}))}]}];
   window.resolutions=[]; window.resets=[];
   window.panel=new SettingsPanel(Object.fromEntries(['tabs','pages','body','search','mode'].map(k=>[k,document.getElementById(k)])));
   panel.onResolve=(target,key,action,destination)=>resolutions.push({target,key,action,destination});
   panel.onReset=(_,keys)=>{resets.push(keys); delete panel.context.changes.wall_loops;panel.render();};
   panel.setSchema(schema);
   panel.show({tab:'object',tabs:[{id:'object',label:'Bracket'}],layout:'process',base:{wall_loops:'4',travel_speed:'300'},changes:{wall_loops:'6',travel_speed:'250',unknown_old_setting:'kept'},target:{kind:'object',id:7}});
  });
  assert.equal(await page.locator('[data-key="travel_speed"]').count(),0,'travel cannot be authored as Object setting');
  assert.equal(await page.locator('[data-key="wall_loops"]').inputValue(),'6');
  assert.match(await page.locator('#body').innerText(),/travel_speed.*250/s,'legacy value remains visible');
  await page.locator('[data-resolve="move"][data-setting="travel_speed"][data-destination="plate"]').click();
  assert.deepEqual(await page.evaluate(()=>resolutions[0]),{target:{kind:'object',id:7},key:'travel_speed',action:'move',destination:'plate'});
  await page.locator('[data-resolve="remove"][data-setting="unknown_old_setting"]').click();
  assert.equal((await page.evaluate(()=>resolutions[1])).action,'remove');
  await page.locator('[data-reset="wall_loops"]').click();
  assert.equal(await page.locator('[data-key="wall_loops"]').inputValue(),'4','reset inherits parent Plate');
  console.log('Object scope and explicit legacy resolution browser seam passed');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
