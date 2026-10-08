const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
let playwright;
try { playwright = require(process.env.PLAYWRIGHT_MODULE || 'playwright'); }
catch (error) {
  // Managed sibling worktrees share the integration checkout's installed dependencies.
  playwright = require(path.resolve(__dirname, '../../../../spooler-v2/node_modules/playwright'));
}
const { chromium } = playwright;
const root = path.resolve(__dirname, '../../public');
const project = { id: 1, name: 'Fixture project', revision: 1, split_id: 1, models: [], slices: {}, server_time: Date.now()/1000,
  data: { plates: [{id:1,name:'Plate 1'}], objects: [], bed: 'textured', profile: {bundle_id:null, process:{},filament:{}} } };
const split = {id:1, source_name:'fixture-small.stl', state:'ready', settings:{scale:1, up:'z',printer:{bed_x:240,bed_y:240,height:250},planner:{mode:'smart',balanced:false},connectors:{kind:'none'}},
  cuts:{cuts:[]}, analysis:{raw_size:[0.1,0.2,0.3],size:[0.1,0.2,0.3], volume_cm3:0.001,source_triangles:12,triangles:12,repairs:['Filled one small hole'],mean_wall_mm:0.1,fits_whole:true,estimated_parts:1} };
(async()=>{
  const browser=await chromium.launch({headless:true});
  try {
    const page=await browser.newPage({viewport:{width:390,height:844}});
    await page.route('http://spooler.test/**', route => {
      const file=path.join(root,new URL(route.request().url()).pathname);
      if(!file.startsWith(root)||!fs.existsSync(file)||!fs.statSync(file).isFile()) return route.fulfill({status:404});
      return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.html')?'text/html':'application/octet-stream'});
    });
    page.on('pageerror', e=>console.error('Page error:',e.message));
    page.on('console', e=>{if(e.type()==='error') console.error(e.text());});
    await page.route('**/api/**',route=>{
      const url=new URL(route.request().url());
      assert.equal(route.request().method(),'GET','Review must not mutate fixture data');
      const data = url.pathname==='/api/projects/1'?project : url.pathname==='/api/splits/1'?split
        : url.pathname.endsWith('/status')?{plates:[],progress:{counts:{},filament_g:{}}}
        : url.pathname.endsWith('/jobs')?{jobs:[]}:url.pathname==='/api/printers'?[]
        :url.pathname==='/api/defaults'?{defaults:{options:{}}}:url.pathname==='/api/slicer/spools'?{spools:[]}
        :url.pathname==='/api/slicer/presets'?{machines:[],processes:{},filaments:{}}:{};
      return route.fulfill({json:data});
    });
    await page.goto('http://spooler.test/slicer.html?project=1');
    await page.getByText('fixture-small.stl',{exact:true}).waitFor({timeout:5000}).catch(async e=>{console.error(await page.locator('body').innerText());throw e;});
    assert.match(await page.locator('#split-side').innerText(),/Original source is retained/);
    assert.match(await page.locator('#split-side').innerText(),/Target bed/);
    assert.match(await page.locator('#split-side').innerText(),/Ready to continue/);
    console.log('PASS: Model review explains scale, repair readiness and retained original');
    await page.route('**/api/splits/1/settings',route=>{
      const change=route.request().postDataJSON();
      split.settings={...split.settings,...change};
      split.analysis.size=split.analysis.raw_size.map(value=>value*change.scale);
      split.analysis.fits_whole=false;
      return route.fulfill({json:split});
    });
    await page.getByRole('button',{name:'m ×1000',exact:true}).click();
    await page.getByRole('button',{name:'Apply and analyse again',exact:true}).click();
    await page.getByText('100 × 200 × 300 mm',{exact:true}).waitFor({timeout:5000});
    assert.equal(await page.getByRole('spinbutton',{name:'Scale',exact:true}).inputValue(),'1000');
    console.log('PASS: Unit mistake has an explicit scale correction that updates Model review');
    split.cuts={cuts:[{id:1,normal:[0,0,1],offset:10}]};
    split.analysis.size=[500,240,200]; split.analysis.fits_whole=false;
    split.check={pieces:[{fits:true},{fits:false,bbox:[[0,0,0],[300,200,200]]}]};
    await page.reload();
    await page.getByRole('button',{name:'Plan the cuts →'}).click();
    const cut=page.getByRole('button',{name:'Select Cut 1'});
    await cut.focus();await page.keyboard.press('Enter');
    assert.equal(await cut.getAttribute('aria-pressed'),'true');
    assert.match(await page.locator('#split-side').innerText(),/2 parts.*1 too big/s);
    assert.equal(await page.getByRole('button',{name:'Split the model',exact:true}).isEnabled(),false);
    console.log('PASS: Oversize cut plan can be selected by keyboard and blocks invalid Split');
    split.state='failed';split.analysis=null;split.error='Invalid mesh: no solid volume';
    await page.reload();await page.getByText('Invalid mesh: no solid volume',{exact:true}).waitFor({timeout:5000});
    assert.equal(await page.getByRole('button',{name:'Try again',exact:true}).isEnabled(),true);
    assert.equal(await page.getByRole('button',{name:'Put it on a plate →'}).count(),0);
    split.state='analyzing';split.error=null;
    await page.reload();await page.getByText(/Checking the model: reading it/).waitFor({timeout:5000});
    console.log('PASS: Invalid mesh offers retry and processing is visibly distinct from successful review');
    split.state='split';split.result={summary:{parts:2,cuts:1,rods:{},rod_lengths_mm:{},holes_not_shared_by_two:0,joints:1,joints_without_rods:1,parts_needing_supports:0},
      parts:[{number:1,print_size:[200,120,40],holes:0},{number:2,print_size:[200,120,40],holes:0}],assembly:[{part:1},{part:2,joins:[1],rods:0}]};
    split.analysis={raw_size:[400,120,40],size:[400,120,40],fits_whole:false,volume_cm3:100,source_triangles:12,triangles:12,repairs:[],mean_wall_mm:2};
    await page.reload();
    await page.locator('[data-step="split"]').click();
    await page.getByRole('button',{name:'Highlight Part 1',exact:true}).focus();
    assert.match(await page.locator('#split-side').innerText(),/2 from 1 cut/);
    assert.equal(await page.getByRole('button',{name:'Lay out on plates →',exact:true}).isVisible(),true);
    console.log('PASS: Finished Split keeps numbered Parts, dimensions and assembly review before layout');
    split.state='ready';
    project.split_id=null;
    project.models=[{id:1,name:'Cube',state:'failed',error:'Fixture preview unavailable',size:[20,20,20]}];
    project.data.plates.push({id:2,name:'Plate 2'});
    project.data.objects=[{id:1,name:'Cube A',model:1,plate:1,position:[20,20,0],rotation:[0,0,0],scale:[1,1,1]},
      {id:2,name:'Cube B',model:1,plate:2,position:[30,30,0],rotation:[0,0,0],scale:[1,1,1]}];
    await page.reload();
    assert.equal(await page.locator('body').getAttribute('data-viewport-editable'),'false');
    const object=page.getByRole('button',{name:'Select Cube A'});
    await object.focus();await page.keyboard.press('Enter');
    assert.equal(await object.getAttribute('aria-pressed'),'true');
    assert.deepEqual(project.data.objects[0].position,[20,20,0]);
    assert.equal(await page.locator('body').getAttribute('data-viewport-editable'),'false');
    const controls=page.getByRole('button',{name:'Show editing controls'});
    await controls.click();
    assert.equal(await page.locator('body').getAttribute('data-viewport-editable'),'true');
    assert.equal(await page.getByRole('spinbutton',{name:'Position X (mm)'}).count(),1);
    assert.equal(await page.getByRole('spinbutton',{name:'Rotation Z (degrees)'}).count(),1);
    assert.equal(await page.evaluate(()=>document.body.scrollWidth<=innerWidth),true);
    console.log('PASS: Phone defer path and keyboard object selection expose numeric transforms');
    project.models[0].state='ready';
    project.slices={'1':{state:'sliced',outdated:true,layers:10,estimate_s:600,estimate_g:5}};
    await page.reload();
    await page.locator('#slice-state').getByText(/Outdated Slice/).waitFor({timeout:5000});
    assert.match(await page.locator('#slice-state').innerText(),/objects|placement|settings/i);
    project.slices['1']={state:'failed',error:'Fixture slicer stopped'};
    await page.reload();
    await page.locator('#slice-state').getByText(/Failed Slice/).waitFor({timeout:5000});
    project.slices['1']={state:'slicing',stage:'preparing',started_at:Date.now()/1000};
    await page.emulateMedia({reducedMotion:'reduce'});
    await page.reload();await page.locator('#slice-state').getByText(/Slice in progress/).waitFor({timeout:5000});
    assert.equal(await page.locator('.slicer-plate-tab .dot.busy').first().evaluate(element=>getComputedStyle(element).animationName),'none');
    console.log('PASS: Changed Plate invalidates Slice with explanation and failure remains explicit');
    const printers=[{id:'p1',name:'Printer One',printer_type:'cc1',connected:true},{id:'p2',name:'Printer Two',printer_type:'cc1',connected:true}];
    await page.route('**/api/printers',route=>route.fulfill({json:printers}));
    project.data.plates[0].printer_id='p1';project.data.plates[1].printer_id='p2';
    project.slices={'1':{state:'sliced',outdated:false,layers:10,estimate_s:600,estimate_g:5},
      '2':{state:'sliced',outdated:false,layers:20,estimate_s:1200,estimate_g:10}};
    await page.reload();
    await page.locator('[data-step="queue"]').click();
    assert.match(await page.locator('#batch-summary').innerText(),/Printer One/);
    assert.match(await page.locator('#batch-summary').innerText(),/Printer Two/);
    assert.match(await page.locator('#batch-summary').innerText(),/15 g/);
    assert.match(await page.locator('#batch-summary').innerText(),/Textured/);
    await page.locator('[data-send-plate="2"]').uncheck();
    assert.match(await page.locator('#batch-summary').innerText(),/1 Plate/);
    console.log('PASS: Batch summary tracks selected Plates, assignments, bed and filament');
    const progress={plates:[{id:1,name:'Plate 1',status:'queued',parts:1,job_id:1,printer_id:'p1'},
      {id:2,name:'Plate 2',status:'sliced',parts:1,outcome_unknown:true,printer_id:'p2'}],
      print_order:[1,2], progress:{total:2,printed_ok:0,outcome_unknown:1,counts:{unsliced:0,sliced:1,waiting:0,queued:1,printing:0,printed_ok:0,failed:0},filament_g:{total:15,used:0}}};
    await page.route('**/api/projects/1/status',route=>route.fulfill({json:progress}));
    await page.route('**/api/jobs',route=>route.fulfill({json:{jobs:[]}}));
    await page.route('**/api/outcome-prompts',route=>route.fulfill({json:{awaiting:[]}}));
    await page.locator('[data-step="print"]').click();
    await page.getByText(/1 latest Outcome unknown/).waitFor({timeout:5000});
    const plateChoice=page.getByRole('button',{name:'Plate 2: Sliced, latest Outcome unknown',exact:true});
    await plateChoice.focus();await page.keyboard.press('Enter');
    assert.equal(await plateChoice.getAttribute('aria-pressed'),'true');
    console.log('PASS: Project progress exposes unknown Outcomes and readable keyboard Plate selection');
    project.data.profile.bundle_id='fixture-profile';project.data.profile.process={wall_loops:'4'};
    await page.route('**/api/slicer',route=>route.fulfill({json:{bundles:[{id:'fixture-profile',presets:{machine:{name:'Fixture machine'},filament:{name:'Fixture PLA'},process:{name:'Fixture process'}}}]}}));
    await page.route('**/api/slicer/profiles/fixture-profile/settings',route=>route.fulfill({json:{process:{wall_loops:'3'},filament:{},machine:{}}}));
    await page.route('**/api/projects/1',route=>{
      if(route.request().method()==='PATCH') {project.data=route.request().postDataJSON().data;project.revision++;}
      return route.fulfill({json:project});
    });
    await page.route('**/orca/settings.json',route=>route.fulfill({json:{options:{wall_loops:{type:'int',mode:'simple',label:'Wall loops',min:0,max:100},advanced_fixture:{type:'int',mode:'advanced',label:'Advanced fixture'},expert_fixture:{type:'int',mode:'expert',label:'Expert fixture'}},tabs:{process:[{page:'Strength',groups:[{group:'Walls',rows:[{options:['wall_loops']},{options:['advanced_fixture']},{options:['expert_fixture']}]}]}],filament:[]}}}));
    await page.reload();await page.locator('[data-step="plates"]').click();
    assert.equal(await page.getByRole('textbox',{name:'Wall loops',exact:true}).inputValue(),'4');
    await page.getByRole('button',{name:'Reset Wall loops to inherited value'}).click();
    await page.waitForTimeout(800);
    assert.equal(await page.getByRole('textbox',{name:'Wall loops',exact:true}).inputValue(),'3');
    await page.getByRole('combobox',{name:'Setting detail',exact:true}).selectOption('advanced');
    assert.equal(await page.getByRole('textbox',{name:'Advanced fixture',exact:true}).isVisible(),true);
    assert.equal(await page.getByRole('textbox',{name:'Expert fixture',exact:true}).count(),0);
    await page.getByRole('combobox',{name:'Setting detail',exact:true}).selectOption('expert');
    assert.equal(await page.getByRole('textbox',{name:'Expert fixture',exact:true}).isVisible(),true);
    await page.getByRole('combobox',{name:'Setting detail',exact:true}).selectOption('simple');
    const processTab=page.getByRole('tab',{name:'Process',exact:true});
    await processTab.focus();await page.keyboard.press('ArrowRight');
    assert.equal(await page.getByRole('tab',{name:'Filament',exact:true}).getAttribute('aria-selected'),'true');
    console.log('PASS: Scoped settings reset to inherited preset and tabs expose keyboard selection');
    for(const [width,height] of [[320,640],[360,800],[390,844],[430,932],[768,1024],[1024,768],[1440,1000]]) {
      await page.setViewportSize({width,height});
      assert.equal(await page.evaluate(()=>document.body.scrollWidth<=innerWidth),true,`${width}x${height}: Project page overflow`);
    }
    console.log('PASS: Project fixtures fit every required phone, tablet and desktop viewport');
    await page.getByRole('button',{name:'Hide sidebar',exact:true}).click();
    assert.equal(await page.locator('#project-sidebar').isVisible(),false);
    await page.getByRole('button',{name:'Show sidebar',exact:true}).click();
    assert.equal(await page.locator('#project-sidebar').isVisible(),true);
    await page.route('**/api/projects/1/plates/*/analysis',route=>route.fulfill({json:{layers:[{offset:0,length:0,z:0.2,height:0.2}],features:[],feature_stats:{},bounds:{model:{min:[0,0,0],max:[20,20,0.2]}}}}));
    await page.route('**/api/projects/1/plates/*/toolpath',route=>route.fulfill({body:Buffer.alloc(0),contentType:'application/octet-stream'}));
    await page.setViewportSize({width:390,height:844});
    await page.locator('[data-step="slice"]').click();
    await page.getByRole('button',{name:'Layer preview',exact:true}).click();
    await page.getByRole('link',{name:'Download G-code',exact:true}).waitFor({timeout:5000});
    const modelRect=await page.locator('#viewport').boundingBox(),previewRect=await page.locator('#preview-panel').boundingBox();
    assert.ok(previewRect.y>=modelRect.y+modelRect.height-1,'Preview details should be below the phone model view');
    await page.getByRole('slider',{name:'Layer',exact:true}).focus();await page.keyboard.press('Home');
    console.log('PASS: Desktop sidebar collapses; phone preview details preserve model view and keyboard layer access');
    await page.locator('[data-step="queue"]').click();
    await page.locator('[data-send-plate="1"]').check();await page.locator('[data-send-plate="2"]').check();
    await page.getByRole('button',{name:'Balance selected Plates by estimated time'}).click();
    assert.equal(await page.getByRole('combobox',{name:'Destination printer for Plate 2',exact:true}).inputValue(),'p1');
    assert.equal(await page.getByRole('combobox',{name:'Destination printer for Plate 1',exact:true}).inputValue(),'p2');
    await page.getByRole('combobox',{name:'Destination printer for Plate 1',exact:true}).selectOption('p1');
    assert.match(await page.locator('#batch-summary').innerText(),/Plate 1 → Printer One/);
    console.log('PASS: Balancing suggests destinations before Queueing and the operator can override them');
    progress.plates[0].status='printing';progress.progress.counts.queued=0;progress.progress.counts.printing=1;
    await page.locator('[data-step="print"]').click();
    await page.locator('.pp-progress').getByText(/1 printing/).waitFor({timeout:5000});
    progress.plates[0].status='failed';progress.progress.counts.printing=0;progress.progress.counts.failed=1;
    await page.locator('[data-step="queue"]').click();await page.locator('[data-step="print"]').click();
    await page.locator('.pp-progress').getByText(/1 failed/).waitFor({timeout:5000});
    console.log('PASS: Queued, printing and failed Project states transition without physical printer commands');
    await page.addInitScript(()=>localStorage.setItem('theme','light'));
    await page.reload();
    assert.equal(await page.locator('body').evaluate(element=>element.classList.contains('light-mode')),true);
    console.log('PASS: Project workspace retains the user’s chosen light theme');
    await page.locator('.pp-chip').first().waitFor({timeout:5000});
    for(const theme of ['light','dark']) {
      await page.evaluate(theme=>window.dispatchEvent(new StorageEvent('storage',{key:'theme',newValue:theme})),theme);
      const measurements=await page.evaluate(()=>{
        const rgb=color=>color.match(/[\d.]+/g).map(Number);
        const lum=color=>{const c=color.slice(0,3).map(x=>{x/=255;return x<=0.04045?x/12.92:((x+0.055)/1.055)**2.4;});return c[0]*0.2126+c[1]*0.7152+c[2]*0.0722;};
        const background=element=>{const layers=[];for(let el=element;el;el=el.parentElement)layers.unshift(rgb(getComputedStyle(el).backgroundColor));return layers.reduce((base,top)=>{const a=top[3]??1;return [0,1,2].map(i=>top[i]*a+base[i]*(1-a));},[255,255,255]);};
        return [...document.querySelectorAll('.pp-chip,.slicer-step.active .num')].filter(el=>el.getClientRects().length).map(el=>{
          const fg=lum(rgb(getComputedStyle(el).color)),bg=lum(background(el));return {label:el.textContent,ratio:(Math.max(fg,bg)+0.05)/(Math.min(fg,bg)+0.05)};
        });
      });
      assert.ok(measurements.length>0);
      for(const sample of measurements) assert.ok(sample.ratio>=4.5,`${theme} ${sample.label}: ${sample.ratio.toFixed(2)} contrast`);
    }
    console.log('PASS: Project status labels and current-step numbers measure at least 4.5:1 in both themes');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});

