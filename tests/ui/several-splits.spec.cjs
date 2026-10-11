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
const whole = { id: 1, name: 'small.stl', file: 'small.stl', state: 'ready', size: [20, 20, 10] };
const project = { id: 1, name: 'Mixed project', revision: 1, split_id: 1, split_ids: [1, 2], models: [whole], slices: {}, server_time: Date.now() / 1000,
  data: { plates: [{ id: 1, name: 'Plate 1' }], objects: [{ id: 1, model: 1, name: 'small', plate: 1, position: [128, 128], rotation: [0, 0, 0], scale: [1, 1, 1], overrides: {} }],
    bed: 'textured', profile: { bundle_id: null, process: {}, filament: {} } } };
const makeSplit = (id, name) => ({ id, source_name: name, state: 'ready', settings: { scale: 1, up: 'z', printer: { bed_x: 240, bed_y: 240, height: 250 }, planner: { mode: 'smart', balanced: false }, connectors: { kind: 'none' } },
  cuts: { cuts: [] }, analysis: { raw_size: [400, 40, 20], size: [400, 40, 20], volume_cm3: 100, source_triangles: 12, triangles: 12, repairs: [], mean_wall_mm: 2, fits_whole: false, estimated_parts: 2 } });
const splitsById = { 1: makeSplit(1, 'long-one.stl'), 2: makeSplit(2, 'long-two.stl') };
const placed = id => ({ id: 10 + id, name: `part${id}`, file: `part${id}.stl`, state: 'ready', split: { id, part: 1, supports: false } });
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    await page.route('http://spooler.test/**', route => {
      const file = path.join(root, new URL(route.request().url()).pathname);
      if (!file.startsWith(root) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404 });
      return route.fulfill({ body: fs.readFileSync(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.html') ? 'text/html' : 'application/octet-stream' });
    });
    page.on('pageerror', e => console.error('Page error:', e.message));
    await page.route('**/api/**', route => {
      const url = new URL(route.request().url());
      assert.equal(route.request().method(), 'GET', 'Review must not mutate fixture data');
      const splitMatch = url.pathname.match(/^\/api\/splits\/(\d+)$/);
      const data = url.pathname === '/api/projects/1' ? project : splitMatch ? splitsById[splitMatch[1]]
        : url.pathname.endsWith('/status') ? { plates: [], progress: { counts: {}, filament_g: {} } }
        : url.pathname.endsWith('/jobs') ? { jobs: [] } : url.pathname === '/api/printers' ? []
        : url.pathname === '/api/defaults' ? { defaults: { options: {} } } : url.pathname === '/api/slicer/spools' ? { spools: [] }
        : url.pathname === '/api/slicer/presets' ? { machines: [], processes: {}, filaments: {} } : {};
      return route.fulfill({ json: data });
    });
    const open = async () => { await page.goto('http://spooler.test/slicer.html?project=1'); await page.waitForSelector('.slicer-step'); };
    const active = () => page.locator('.slicer-step.active').getAttribute('data-step');

    await page.evaluate(() => localStorage.clear()).catch(() => {});
    await open();
    assert.equal(await active(), 'model', 'whole models on plates plus an unplaced split still open on Model');
    await page.getByText('long-one.stl', { exact: true }).first().waitFor({ timeout: 5000 });
    assert.equal(await page.locator('[data-step="split"]').count(), 1);
    console.log('PASS: A mixed project opens on Model for its first unplaced split');

    project.models = [whole, placed(1)];
    await page.evaluate(() => localStorage.clear());
    await open();
    assert.equal(await active(), 'model');
    await page.getByText('long-two.stl', { exact: true }).first().waitFor({ timeout: 5000 });
    console.log('PASS: With one split laid out, the next unplaced split opens');

    project.models = [whole, placed(1), placed(2)];
    await page.evaluate(() => localStorage.clear());
    await open();
    assert.notEqual(await active(), 'model');
    assert.notEqual(await active(), 'split');
    console.log('PASS: With every split laid out the project opens past Split');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
