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
const project = { id: 1, name: 'Long project', revision: 1, split_id: 1, split_ids: [1], models: [], slices: {}, server_time: Date.now() / 1000,
  data: { plates: [{ id: 1, name: 'Plate 1' }], objects: [], bed: 'textured', profile: { bundle_id: null, process: {}, filament: {} } } };
const cut = { id: 1, parent: null, normal: [1, 0, 0], offset: 200, scope: null };
const split = { id: 1, source_name: 'long.stl', state: 'ready',
  settings: { scale: 1, up: 'z', printer: { bed_x: 240, bed_y: 240, height: 250 }, planner: { mode: 'smart', balanced: true }, connectors: { kind: 'none' } },
  cuts: { cuts: [cut] },
  // a plan whose only problem is a part too small to print
  check: { cuts: [cut], pieces: [{ fits: true, tiny: false, size: [20, 40, 200], volume_cm3: 100, bbox: [0, 0, 0, 200, 40, 20] },
                                 { fits: true, tiny: true, size: [1, 20, 20], volume_cm3: 0.2, bbox: [200, 0, 0, 201, 40, 20], cut: 1 }] },
  analysis: { raw_size: [201, 40, 20], size: [201, 40, 20], volume_cm3: 100, source_triangles: 12, triangles: 12, repairs: [], mean_wall_mm: 2, fits_whole: false, estimated_parts: 2 } };

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
      assert.equal(route.request().method(), 'GET', 'Viewing the Split step must not change fixture data');
      const data = url.pathname === '/api/projects/1' ? project : url.pathname === '/api/splits/1' ? split
        : url.pathname.endsWith('/status') ? { plates: [], progress: { counts: {}, filament_g: {} } }
        : url.pathname.endsWith('/jobs') ? { jobs: [] } : url.pathname === '/api/printers' ? []
        : url.pathname === '/api/defaults' ? { defaults: { options: {} } } : url.pathname === '/api/slicer/spools' ? { spools: [] }
        : url.pathname === '/api/slicer/presets' ? { machines: [], processes: {}, filaments: {} } : {};
      return route.fulfill({ json: data });
    });
    await page.goto('http://spooler.test/slicer.html?project=1');
    await page.waitForSelector('.slicer-step');
    if (!(await page.locator('#split-balanced').count())) await page.locator('[data-action="go-split"]').click();
    await page.locator('#split-balanced').waitFor({ timeout: 5000 });

    assert.equal(await page.locator('#split-balanced').isDisabled(), true, 'Even grid is greyed out under Smart cuts');
    assert.equal(await page.locator('#split-balanced-why').isVisible(), true);
    assert.match(await page.locator('#split-balanced-why').textContent(), /only applies to Grid/);
    await page.locator('input[name="split-mode"][value="grid"]').check();
    assert.equal(await page.locator('#split-balanced').isDisabled(), false, 'Even grid comes back with Grid cuts');
    assert.equal(await page.locator('#split-balanced-why').isVisible(), false);
    console.log('PASS: Even grid is only offered with Grid cuts');

    assert.equal(await page.locator('[data-action="autofix"]').isDisabled(), false, 'Auto-fix can repair a part too small to print');
    assert.match(await page.locator('#split-check').textContent(), /1 too small to print/);
    console.log('PASS: Auto-fix is offered when the only problem is a part too small to print');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
