const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
let playwright;
try { playwright = require(process.env.PLAYWRIGHT_MODULE || 'playwright'); }
catch (error) { playwright = require(path.resolve(__dirname, '../../../../spooler-v2/node_modules/playwright')); }
const { chromium } = playwright;
const root = path.resolve(__dirname, '../../public');

// Slice status and saved inputs through routed Project responses; no live server or printers.
const model = { id: 1, file: 'box.stl', state: 'ready', sha256: 'sha1', import: [1, 'z'], size: [40, 30, 20], triangles: 12, volume_cm3: 1, repairs: [] };
const entry = extra => ({ state: 'sliced', fingerprint: 'a', inputs_version: 2, inputs_revision: 3, sliced_at: 1, layers: 1, estimate_s: 600, estimate_g: 12, outdated: false, ...extra });

async function open(browser, slice, inputs) {
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  const project = () => ({ id: 70, name: 'Slices', revision: 4, split_id: null, split_ids: [], models: [model], slices: { 1: slice }, server_time: Date.now() / 1000,
    data: { plates: [{ id: 1, name: 'Plate 1' }], bed: 'textured', profile: { bundle_id: null, process: {}, filament: {} },
            objects: [{ id: 1, model: 1, plate: 1, name: 'box', position: [50, 50], rotation: [0, 0, 0], scale: [1, 1, 1] }] } });
  const seen = [];
  await page.route('http://spooler.test/**', route => {
    const file = path.join(root, new URL(route.request().url()).pathname);
    if (!file.startsWith(root) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404 });
    return route.fulfill({ body: fs.readFileSync(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.html') ? 'text/html' : 'application/octet-stream' });
  });
  page.on('pageerror', e => console.error('Page error:', e.message));
  await page.route('**/api/**', route => {
    const p = new URL(route.request().url()).pathname;
    seen.push(p);
    if (p === '/api/projects/70') return route.fulfill({ json: project() });
    if (p.endsWith('/slice-inputs')) return route.fulfill({ json: inputs });
    if (p.endsWith('/analysis')) return route.fulfill({ json: { layers: [{ offset: 0, length: 0, z: 0.2, height: 0.2 }], features: [], feature_stats: {}, bounds: { model: { min: [0, 0, 0], max: [20, 20, 0.2] } } } });
    if (p.endsWith('/toolpath')) return route.fulfill({ body: Buffer.alloc(0), contentType: 'application/octet-stream' });
    if (/\/models\/\d+\/preview$/.test(p)) return route.fulfill({ body: Buffer.alloc(0), contentType: 'application/octet-stream' });
    if (p.endsWith('/status')) return route.fulfill({ json: { plates: [{ id: 1, problems: [], problem_reason: null, warning_reason: null }], progress: { counts: {}, filament_g: {} } } });
    return route.fulfill({ json: p === '/api/splits' ? { splits: [] } : p.endsWith('/jobs') ? { jobs: [] } : p === '/api/printers' ? []
      : p === '/api/defaults' ? { defaults: { options: {} } } : p === '/api/slicer/spools' ? { spools: [] }
      : p === '/api/slicer/presets' ? { machines: [], processes: {}, filaments: {} } : {} });
  });
  await page.goto('http://spooler.test/slicer.html?project=70');
  await page.waitForFunction(() => document.getElementById('project-name').value !== '');
  await page.locator('[data-step="slice"]').click();
  await page.getByRole('button', { name: 'Layer preview', exact: true }).click();
  await page.getByRole('link', { name: 'Download G-code', exact: true }).waitFor({ timeout: 5000 });
  return { page, seen };
}

const review = extra => ({ version: 1, project_id: 70, plate_id: 1, revision: 4, label: 'Saved inputs this Slice was prepared with (expected inputs, not measured output); the current Project is shown separately',
  slice: { state: 'sliced', outdated: true }, current: { state: 'available', fingerprint: 'b', revision: 4 }, differences: [], ...extra });

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    let { page, seen } = await open(browser, entry({ outdated: true }), review({
      saved: { state: 'saved', reason: null, inputs: { revision: 3, process: { curr_bed_type: 'Textured PEI Plate' }, filament: { filament_type: ['PETG'] } } },
      differences: [{ kind: 'process', key: 'wall_loops', saved: '3', current: '6' }] }));
    assert.match(await page.locator('#preview-panel').innerText(), /changed after it was sliced/);
    assert.equal(seen.filter(p => p.endsWith('/slice-inputs')).length, 0, 'saved inputs load only when asked for');
    await page.locator('[data-slice-inputs] summary').click();
    const panel = page.locator('[data-slice-inputs]');
    await panel.locator('[data-saved-inputs]').waitFor({ timeout: 3000 });
    assert.match(await panel.innerText(), /PETG/);
    const diff = await panel.locator('[data-slice-differences]').innerText();
    assert.match(diff, /wall_loops/);
    assert.match(diff, /Current Project/);
    await page.close();

    ({ page } = await open(browser, entry({ fingerprint: 'old', inputs_version: undefined }), review({
      slice: { state: 'sliced', outdated: false },
      saved: { state: 'unavailable', inputs: null, reason: 'This Slice was made before its inputs were saved, so what it was prepared with cannot be reconstructed.' } })));
    assert.doesNotMatch(await page.locator('#preview-panel').innerText(), /changed after it was sliced/);
    await page.locator('[data-slice-inputs] summary').click();
    await page.locator('[data-saved-unavailable]').waitFor({ timeout: 3000 });
    assert.match(await page.locator('[data-saved-unavailable]').innerText(), /cannot be reconstructed/);
    assert.equal(await page.locator('[data-slice-differences]').count(), 0);
    await page.close();
    console.log('PASS: Slice status shows Outdated, and saved inputs are separate from the current Project or labelled unavailable');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
