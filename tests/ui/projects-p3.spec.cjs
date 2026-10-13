// Ticket 28: Projects P3 (PS-64 landing step, PS-66 one status per Plate row; PS-65 is recorded in the ticket)
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
const now = Date.now() / 1000;

const listed = [
  { id: 7, name: 'Bracket', plates: 2, objects: 3, updated_at: now - 3 * 3600, split_id: null, sliced: 2, slicing: 0, queued: 0, printing: 0, printed_ok: 1, failed: 0, outcome_unknown: 1, busy: false },
  { id: 8, name: 'Empty one', plates: 1, objects: 0, updated_at: now - 40 * 86400, split_id: null, sliced: 0, slicing: 0, queued: 0, printing: 0, printed_ok: 0, failed: 0, outcome_unknown: 0, busy: false },
];
const project = (id = 1) => ({ id, name: 'Fixture project', revision: 1, split_id: null, models: [{ id: 1, name: 'Cube', state: 'failed', error: 'Fixture preview unavailable', size: [20, 20, 20] }], slices: {}, server_time: now,
  data: { plates: [{ id: 1, name: 'Plate 1', printer_id: 'p1' }, { id: 2, name: 'Plate 2', printer_id: 'p2' }], objects: [{ id: 1, name: 'Cube A', model: 1, plate: 1, position: [20, 20, 0], rotation: [0, 0, 0], scale: [1, 1, 1] }, { id: 2, name: 'Cube B', model: 1, plate: 2, position: [30, 30, 0], rotation: [0, 0, 0], scale: [1, 1, 1] }], bed: 'textured', profile: { bundle_id: null, process: {}, filament: {} } } });
const split = { id: 1, source_name: 'fixture-small.stl', state: 'ready', settings: { scale: 1, up: 'z', printer: { bed_x: 240, bed_y: 240, height: 250 }, planner: { mode: 'smart', balanced: false }, connectors: { kind: 'none' } },
  cuts: { cuts: [] }, analysis: { raw_size: [0.1, 0.2, 0.3], size: [0.1, 0.2, 0.3], volume_cm3: 0.001, source_triangles: 12, triangles: 12, repairs: [], mean_wall_mm: 0.1, fits_whole: true, estimated_parts: 1 } };
const status = (over = {}) => ({ plates: [{ id: 1, name: 'Plate 1', status: 'sliced', parts: 1, printer_id: 'p1', estimate_s: 600, estimate_g: 5 },
  { id: 2, name: 'Plate 2', status: 'sliced', parts: 1, printer_id: 'p2', estimate_s: 1200, estimate_g: 10 }], print_order: [1, 2],
  progress: { total: 2, printed_ok: 0, outcome_unknown: 0, counts: { unsliced: 0, sliced: 2, waiting: 0, queued: 0, printing: 0, printed_ok: 0, failed: 0 }, filament_g: { total: 15, used: 0 } }, ...over });
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==', 'base64');
const printers = [{ id: 'p1', name: 'Printer One', printer_type: 'cc1', connected: true }, { id: 'p2', name: 'Printer Two', printer_type: 'cc1', connected: true }];

async function open(browser, { proj, jobs = [], width, height, url, autoStart = { p1: false, p2: true }, statusBody = status() }) {
  const page = await browser.newPage({ viewport: { width, height: height || (width < 700 ? 844 : 800) } });
  const calls = [];
  await page.route('http://spooler.test/**', route => {
    const file = path.join(root, new URL(route.request().url()).pathname);
    if (!file.startsWith(root) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404 });
    return route.fulfill({ body: fs.readFileSync(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.html') ? 'text/html' : 'application/octet-stream' });
  });
  page.on('pageerror', e => console.error('Page error:', e.message));
  await page.route('**/api/**', route => {
    const u = new URL(route.request().url()), method = route.request().method();
    if (method !== 'GET') calls.push({ method, path: u.pathname, body: route.request().postData() });
    if (u.pathname === '/api/projects/7/plates/1/preview.png') return route.fulfill({ body: png, contentType: 'image/png' });
    const bed = u.pathname.match(/^\/api\/printers\/(p\d)\/bed-cleared$/);
    const data = u.pathname === '/api/projects' && method === 'GET' ? { projects: listed }
      : u.pathname === '/api/splits' ? { splits: [] }
      : u.pathname === '/api/projects/7' ? project(7)
      : u.pathname === '/api/projects/1' ? (proj || project(1))
      : u.pathname === '/api/splits/1' ? split
      : u.pathname.endsWith('/status') ? statusBody
      : u.pathname.endsWith('/jobs') ? { jobs }
      : u.pathname === '/api/printers' ? printers
      : bed ? { auto_start: autoStart[bed[1]] }
      : u.pathname === '/api/outcome-prompts' ? { awaiting: [] }
      : u.pathname === '/api/defaults' ? { defaults: { options: {} } }
      : u.pathname === '/api/slicer/spools' ? { spools: [] }
      : u.pathname === '/api/slicer/presets' ? { machines: [], processes: {}, filaments: {} }
      : u.pathname.endsWith('/preview.png') ? null : {};
    if (data === null) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ json: data });
  });
  await page.goto(url);
  return { page, calls };
}

const sliced = () => ({ ...project(1), slices: { 1: { state: 'sliced', outdated: false, layers: 10, estimate_s: 600, estimate_g: 5 }, 2: { state: 'sliced', outdated: false, layers: 20, estimate_s: 1200, estimate_g: 10 } } });
const landed = async page => {
  await page.locator('#step-bar .slicer-step.active').waitFor({ timeout: 8000 });
  return page.evaluate(() => document.body.dataset.workflowStep);
};

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    // PS-64: a Project opens on the step that reflects where it stands, not always Slice
    {
      const base = { width: 1280, url: 'http://spooler.test/slicer.html?project=1' };
      let fx = await open(browser, { ...base });
      assert.equal(await landed(fx.page), 'plates', 'Plates that are not sliced yet open on Plates');
      await fx.page.close();
      fx = await open(browser, { ...base, proj: sliced() });
      assert.equal(await landed(fx.page), 'print', 'every Plate sliced: open on Print, the next thing is to send them');
      await fx.page.close();
      fx = await open(browser, { ...base, jobs: [{ id: 1, state: 'done', printer_id: 'p1', plate_id: 1 }] });
      assert.equal(await landed(fx.page), 'print', 'a Project that has Jobs opens on Print even when no Job is open');
      await fx.page.close();
      console.log('PASS: A Project opens on the step that reflects its state');
    }

    // PS-66: one status per Plate row, with its time and filament
    {
      const statusBody = status({
        plates: [
          { id: 1, name: 'Plate 1', status: 'sliced', parts: 1, estimate_s: 600, estimate_g: 5 },
          { id: 2, name: 'Plate 2', status: 'unsliced', parts: 1 },
          { id: 3, name: 'Plate 3', status: 'failed', parts: 1, estimate_s: 1200, estimate_g: 10 },
          { id: 4, name: 'Plate 4', status: 'printed_ok', parts: 1, estimate_s: 300, estimate_g: 2.5 },
        ],
      });
      const { page } = await open(browser, { width: 1280, url: 'http://spooler.test/slicer.html?project=1&step=print', statusBody });
      await page.locator('#project-page [data-plate-card]').first().waitFor({ timeout: 8000 });
      const rows = await page.evaluate(() => Object.fromEntries([...document.querySelectorAll('#project-page [data-plate-card]')].map(li => [
        li.querySelector('.pp-plate-btn').textContent.trim(),
        { state: li.querySelector('.pp-state')?.textContent.trim() || null, reason: li.querySelector('.pp-reason')?.textContent.trim() || null, meta: li.querySelector('.pp-meta')?.textContent.trim() || null },
      ])));
      assert.equal(rows['Plate 1'].state, null, 'Ready group already says Ready: the row does not say "sliced" again');
      assert.equal(rows['Plate 1'].meta, '10 min · 5 g', 'a sliced Plate shows its time and grams');
      assert.equal(rows['Plate 2'].state, 'needs slicing', 'a word that adds something stays');
      assert.equal(rows['Plate 2'].meta, null, 'no estimate, no placeholder');
      assert.equal(rows['Plate 3'].state, null, 'a row with a reason does not also say "failed"');
      assert.match(rows['Plate 3'].reason, /failed/);
      assert.equal(rows['Plate 4'].state, null, 'Done group already says Done');
      assert.equal(rows['Plate 4'].meta, '5 min · 2.5 g');
      console.log('PASS: Plate rows say their status once and show time and grams');
      await page.close();
    }
  } finally {
    await browser.close();
  }
})().catch(e => { console.error(e); process.exit(1); });
