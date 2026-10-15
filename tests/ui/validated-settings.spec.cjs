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
      const file = path.join(root, new URL(route.request().url()).pathname);
      if (new URL(route.request().url()).pathname === '/test') return route.fulfill({contentType:'text/html', body:
        '<div id="tabs"></div><div id="pages"></div><div id="body"></div><input id="search"><select id="mode"><option>expert</option></select>'});
      return route.fulfill({body:fs.readFileSync(file), contentType:file.endsWith('.js')?'text/javascript':'application/json'});
    });
    await page.goto('http://spooler.test/test');
    await page.evaluate(async () => {
      const {SettingsPanel} = await import('/slicer/settings.js');
      const schema = await (await fetch('/orca/settings.json')).json();
      const keys = ['outer_wall_line_width','enforce_support_layers','bottom_surface_pattern','scarf_angle_threshold'];
      schema.tabs.process = [{page:'Quality', groups:[{group:'Fixture',rows:keys.map(key=>({options:[key]}))}]}];
      window.edits = [];
      window.panel = new SettingsPanel(Object.fromEntries(['tabs','pages','body','search','mode'].map(k=>[k,document.getElementById(k)])));
      panel.onChange = (_,key,value) => edits.push({key,value});
      panel.onReset = () => panel.render();
      panel.setSchema(schema);
      panel.show({tab:'process',tabs:[{id:'process',label:'Process'}],layout:'process',base:{wall_loops:'3',outer_wall_line_width:'0.4',bottom_shell_layers:'3',bottom_surface_pattern:'monotonic',seam_slope_type:'external',seam_slope_conditional:'1',scarf_angle_threshold:'155'},changes:{},target:{kind:'process'}});
    });
    assert.equal(await page.locator('[data-key="enforce_support_layers"]').count(), 0, 'Develop controls stay out of user modes');
    const width = page.locator('[data-key="outer_wall_line_width"]');
    await width.fill('100%%'); await width.dispatchEvent('change');
    assert.equal(await width.getAttribute('aria-invalid'), 'true');
    await page.evaluate(() => panel.render());
    assert.equal(await width.inputValue(), '100%%', 'invalid pending edits survive rerenders');
    assert.match(await page.locator('#body').innerText(), /Enter a number/, 'field errors survive rerenders too');
    assert.deepEqual(await page.evaluate(() => edits), []);
    await page.locator('[data-reset="outer_wall_line_width"]').click();
    assert.equal(await width.inputValue(),'0.4','Reset explicitly abandons an invalid pending edit');
    await width.fill('150%'); await width.dispatchEvent('change');
    assert.deepEqual(await page.evaluate(() => edits), [{key:'outer_wall_line_width',value:'150%'}]);
    assert.ok(await page.locator('[data-key="bottom_surface_pattern"] option[value="monotonic"]').count(), 'copied enum choices are displayed');
    assert.match(await page.locator('[data-key="scarf_angle_threshold"]').locator('xpath=ancestor::div[contains(@class,"set-row")]').locator('.set-label').getAttribute('title'), /155°/, 'Unicode help is preserved');
    console.log('Validated settings browser seam passed');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
