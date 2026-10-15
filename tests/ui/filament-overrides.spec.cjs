const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '../../public');
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(5000);
    await page.route('http://spooler.test/**', route => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === '/test') return route.fulfill({contentType:'text/html',body:
        '<div id="tabs"></div><div id="pages"></div><div id="body"></div><input id="search"><select id="mode"><option>expert</option></select>'});
      const file = path.join(root, pathname);
      return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'text/javascript':'application/json'});
    });
    await page.goto('http://spooler.test/test');
    await page.evaluate(async () => {
      const {SettingsPanel} = await import('/slicer/settings.js');
      window.panel = new SettingsPanel(Object.fromEntries(['tabs','pages','body','search','mode'].map(k=>[k,document.getElementById(k)])));
      panel.setSchema(await (await fetch('/orca/settings.json')).json());
      window.edits=[]; window.resets=[];
      window.context={tab:'filament',tabs:[{id:'filament',label:'Filament'}],layout:'filament',
        base:{nozzle_temperature:['235','245'],filament_retraction_length:['0.8','nil'],filament_wipe:['1'],
          filament_wipe_distance:['2'],enable_overhang_bridge_fan:['0'],overhang_fan_speed:['70']},changes:{},target:{kind:'filament'},
        inheritance:{machine:{retraction_length:['0.6'],enable_long_retraction_when_cut:'0'},process:{ironing_type:'no ironing'}}};
      panel.onChange=(_,key,value)=>{edits.push({key,value});context.changes[key]=value;panel.show(context);};
      panel.onReset=(_,keys)=>{resets.push(keys);keys.forEach(key=>delete context.changes[key]);panel.show(context);};
      panel.show(context);
    });
    await page.locator('#search').fill('nozzle_temperature');
    const temperature = page.locator('[data-key="nozzle_temperature"]');
    await temperature.fill('240'); await temperature.dispatchEvent('change');
    assert.deepEqual(await page.evaluate(()=>edits.at(-1)),{key:'nozzle_temperature',value:['240','245']});
    await page.locator('#search').fill('');
    await page.getByRole('button',{name:'Setting Overrides',exact:true}).click();
    assert.equal(await page.locator('[data-inheritance]').count(),20,'all twenty overrides have explicit inheritance controls');
    await page.locator('[data-inheritance="filament_retraction_length"]').selectOption('inherit');
    assert.deepEqual(await page.evaluate(()=>edits.at(-1)),{key:'filament_retraction_length',value:['nil','nil']});
    assert.equal(await page.locator('[data-key="filament_retraction_length"]').isDisabled(),true);
    await page.locator('[data-reset="filament_retraction_length"]').click();
    assert.equal(await page.locator('[data-key="filament_retraction_length"]').inputValue(),'0.8');
    assert.equal(await page.evaluate(()=>Object.hasOwn(context.changes,'filament_retraction_length')),false);
    await page.locator('[data-inheritance="filament_wipe"]').selectOption('inherit');
    assert.deepEqual(await page.evaluate(()=>edits.at(-1)),{key:'filament_wipe',value:['nil']});
    await page.locator('#search').fill('overhang_fan_speed');
    assert.equal(await page.locator('[data-key="overhang_fan_speed"]').isDisabled(),true);
    assert.match(await page.locator('#body').innerText(),/Enable overhang.*cooling/i);
    assert.deepEqual(await page.evaluate(()=>context.base.overhang_fan_speed),['70'],'inactive values remain stored');
    console.log('Filament override inheritance and vector browser seam passed');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
