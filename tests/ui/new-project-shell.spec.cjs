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
const existing = { id: 7, name: 'Existing project', plates: 1, objects: 2, updated_at: 1700000000, split_id: null };
const created = { id: 9, name: 'bracket', revision: 1, split_id: null, models: [], slices: {}, server_time: Date.now() / 1000,
  data: { plates: [{ id: 1, name: 'Plate 1' }], objects: [], bed: 'textured', profile: { bundle_id: null, process: {}, filament: {} } } };
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    const calls = [];
    await page.route('http://spooler.test/**', route => {
      const file = path.join(root, new URL(route.request().url()).pathname);
      if (!file.startsWith(root) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404 });
      return route.fulfill({ body: fs.readFileSync(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.html') ? 'text/html' : 'application/octet-stream' });
    });
    page.on('pageerror', e => console.error('Page error:', e.message));
    await page.route('**/api/**', route => {
      const url = new URL(route.request().url()), method = route.request().method();
      if (method !== 'GET') calls.push({ method, path: url.pathname, params: Object.fromEntries(url.searchParams), body: route.request().postData() });
      const data = url.pathname === '/api/projects' && method === 'GET' ? { projects: [existing] }
        : url.pathname === '/api/splits' ? { splits: [] }
        : url.pathname === '/api/projects/upload' ? created
        : url.pathname === '/api/projects/9' ? created
        : url.pathname.endsWith('/status') ? { plates: [], progress: { counts: {}, filament_g: {} } }
        : url.pathname.endsWith('/jobs') ? { jobs: [] } : url.pathname === '/api/printers' ? []
        : url.pathname === '/api/defaults' ? { defaults: { options: {} } } : url.pathname === '/api/slicer/spools' ? { spools: [] }
        : url.pathname === '/api/slicer/presets' ? { machines: [], processes: {}, filaments: {} } : {};
      return route.fulfill({ json: data });
    });
    await page.goto('http://spooler.test/slicer.html');
    const dialog = page.locator('#dlg-projects');
    await dialog.waitFor({ state: 'visible' });   // with no project open the Projects page opens by itself
    await dialog.getByRole('button', { name: 'Close', exact: true }).click();
    await page.getByRole('button', { name: 'New project', exact: true }).click();
    await dialog.waitFor({ state: 'visible' });
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'new-project-name', 'Name is the first, focused field');
    assert.equal(await dialog.getByLabel('Name').count(), 1);
    assert.equal(await dialog.getByRole('button', { name: 'Choose files', exact: true }).isVisible(), true);
    assert.equal(await dialog.getByRole('button', { name: 'Choose folder', exact: true }).isVisible(), true);
    assert.equal(await dialog.locator('#new-scale, #new-up').count(), 0, 'Units and Up axis are not asked up front');
    assert.doesNotMatch(await dialog.innerText(), /Quick print on the dashboard/);
    assert.match(await dialog.locator('#project-list').innerText(), /Existing project/);
    assert.match(await dialog.innerText(), /Drop STL, OBJ or 3MF files or a folder here/);
    assert.match(await dialog.locator('#new-model-input').getAttribute('accept'), /\.stl.*\.obj.*\.3mf/);
    assert.equal(await dialog.locator('#new-folder-input').getAttribute('webkitdirectory') !== null, true);
    console.log('PASS: New project page shows Name first, drop zone, pickers and existing projects');

    await page.evaluate(() => {
      const dt = new DataTransfer();
      dt.items.add(new File(['solid x\nendsolid x\n'], 'bracket.stl'));
      document.getElementById('dlg-projects').dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
    });
    await page.waitForFunction(() => !document.getElementById('dlg-projects').open);
    const upload = calls.find(c => c.path === '/api/projects/upload');
    assert.ok(upload, 'a drop anywhere on the page starts a project');
    assert.equal(upload.params.filename, 'bracket.stl');
    assert.equal(upload.params.scale, '1');
    assert.equal(upload.params.up, 'z');
    console.log('PASS: A drop on the page starts a project with millimetre, Z-up defaults');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
