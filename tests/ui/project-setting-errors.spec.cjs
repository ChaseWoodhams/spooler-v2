const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname,'../../public');
(async()=>{
  const browser=await chromium.launch({headless:true});
  try {
    const page=await browser.newPage({viewport:{width:1400,height:1000}});
    page.setDefaultTimeout(6000);
    page.on('pageerror', e=>console.error('Page error:',e.message));
    const project={id:70,name:'Error fixture',revision:1,models:[{id:1,name:'cube',file:'cube.stl',state:'ready',sha256:'fixture',size:[20,20,20],triangles:12,volume_cm3:8,repairs:[]}],split_ids:[],slices:{},server_time:Date.now()/1000,
      data:{bed:'textured',profile:{bundle_id:'fixture',process:{},filament:{}},plates:[{id:1,name:'Plate 1',overrides:{}}],objects:[{id:1,model:1,plate:1,name:'cube',position:[128,128],rotation:[0,0,0],scale:[1,1,1],overrides:{}}]}};
    let rejected=0;
    await page.route('http://spooler.test/**',route=>{
      const file=path.join(root,new URL(route.request().url()).pathname);
      if(!fs.existsSync(file)||!fs.statSync(file).isFile())return route.fulfill({status:404});
      return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.html')?'text/html':'application/json'});
    });
    await page.route('**/api/**',route=>{
      const p=new URL(route.request().url()).pathname;
      if(p==='/api/projects/70'&&route.request().method()==='PATCH'){
        rejected++;
        return route.fulfill({status:400,json:{error:'Scarf start height must be smaller than layer height',issues:[{code:'coupled-setting',kind:'process',key:'seam_slope_start_height',message:'Scarf start height must be smaller than layer height',suggested_correction:{seam_slope_start_height:'0'}}]}});
      }
      return route.fulfill({json:p==='/api/projects/70'?project
        :p==='/api/slicer'?{bundles:[{id:'fixture',label:'PETG fixture',presets:{machine:{name:'CC1'},process:{name:'Process'},filament:{name:'PETG'}}}]}
        :p.endsWith('/fixture/settings')?{bundle:{id:'fixture'},machine:{name:'CC1',nozzle_diameter:['0.4']},process:{wall_loops:'3',layer_height:'0.2',seam_slope_type:'external',seam_slope_start_height:'0'},filament:{filament_type:['PETG']}}
        :p.endsWith('/status')?{plates:[{id:1,problems:[]}],progress:{counts:{},filament_g:{}}}
        :p==='/api/slicer/presets'?{machines:[],processes:{},filaments:{}}
        :p==='/api/printers'?[]:p.endsWith('/spools')?{spools:[]}:p.endsWith('/jobs')?{jobs:[]}:p==='/api/defaults'?{defaults:{options:{}}}:{}});
    });
    await page.goto('http://spooler.test/slicer.html?project=70');
    await page.locator('#project-name').waitFor();
    await page.waitForFunction(()=>document.getElementById('project-name').value==='Error fixture');
    await page.locator('[data-step="plates"]').click();
    await page.locator('#fold-settings').evaluate(el=>el.open=true);
    await page.locator('#settings-mode').selectOption('expert');
    await page.locator('#settings-search').fill('Scarf start height');
    const input=page.locator('[data-key="seam_slope_start_height"]');
    await input.fill('100%'); await input.dispatchEvent('change');
    await page.waitForFunction(()=>document.getElementById('save-state').textContent==='Not saved');
    assert.equal(rejected,1);
    assert.equal(await input.inputValue(),'100%','a rejected save keeps pending Project edits');
    assert.equal(await input.getAttribute('aria-invalid'),'true','server issue identifies its field');
    assert.match(await page.locator('#settings-body').innerText(),/must be smaller than layer height/);
    console.log('Project rejected-setting browser seam passed');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
