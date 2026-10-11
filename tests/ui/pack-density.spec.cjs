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

// The New project dialog's packing density and plan preview, against a fixture server whose plan needs more plates the roomier
// the density (tight 1, normal 2, roomy 3) and whose oversized model gets a plate of its own, as the real planner gives it.
const SHAPES = {
  'small.stl': { size: [40, 30, 20], status: 'fits' },
  'tiny.stl': { size: [10, 10, 10], status: 'fits' },
  'clip.stl': { size: [20, 20, 10], status: 'fits' },
  'big.stl': { size: [650, 400, 300], status: 'split' },
};
const PLATES = { tight: 1, normal: 2, roomy: 3, custom: 2 };
const shell = (id, extra = {}) => ({ id, name: 'Untitled project', revision: 1, split_id: null, split_ids: [], draft: true, models: [], slices: {}, server_time: Date.now() / 1000,
  data: { plates: [{ id: 1, name: 'Plate 1' }], objects: [], bed: 'textured', profile: { bundle_id: null, process: {}, filament: {} } }, ...extra });

async function open(browser, { remembered = null, refuse = false } = {}) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 860 } });
  const server = { calls: [], models: [], nextModel: 1, pack: remembered };
  await page.route('http://spooler.test/**', route => {
    const file = path.join(root, new URL(route.request().url()).pathname);
    if (!file.startsWith(root) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404 });
    return route.fulfill({ body: fs.readFileSync(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.html') ? 'text/html' : 'application/octet-stream' });
  });
  page.on('pageerror', e => console.error('Page error:', e.message));
  const rows = () => {
    const out = server.models.map(m => {
      const s = SHAPES[m.file];
      return { model: m.id, object: m.id + 100, file: m.file, kind: 'model', state: 'ready', error: null, size_bytes: 1000, size: s.size, scale: 1, up: 'z', status: s.status,
        fits: s.status === 'fits', scale_ok: true, scale_fix: null, scale_fixes: [], reason: null, repairs: [], triangles: 12, volume_cm3: 1, copies: 1, estimate: null };
    });
    const count = status => out.filter(r => r.status === status).length;
    const summary = { models: out.length, objects: out.length, split: count('split'), fits: count('fits'), check_units: 0, analysing: 0, failed: 0, can_create: out.length > 0 };
    summary.steps = (summary.split ? ['split'] : []).concat(['plates', 'slice', 'queue']);
    return { rows: out, summary };
  };
  const plates = preset => {
    const fits = server.models.filter(m => SHAPES[m.file].status === 'fits'), big = server.models.filter(m => SHAPES[m.file].status === 'split');
    const count = Math.max(1, Math.min(PLATES[preset], fits.length));
    const out = Array.from({ length: count }, (_, i) => ({ id: i + 1, name: `Plate ${i + 1}`, new: i > 0, parts: 0, turned: 0, objects: [] }));
    fits.forEach((m, i) => {
      const plate = out[i % count], x = 40 + plate.objects.length * 60;
      plate.objects.push({ id: m.id + 100, name: m.file, model: m.id, position: [x, 60], rotation: [0, 0, 0], pinned: false, outline: [[x - 20, 45], [x + 20, 45], [x + 20, 75], [x - 20, 75]] });
      plate.parts++;
    });
    big.forEach((m, i) => out.push({ id: count + i + 1, name: `Plate ${count + i + 1}`, new: true, parts: 1, turned: 0,
      objects: [{ id: m.id + 100, name: m.file, model: m.id, position: [128, 128], rotation: [0, 0, 0], pinned: false, outline: [[8, 8], [248, 8], [248, 248], [8, 248]] }] }));
    return out;
  };
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url()), method = request.method(), p = url.pathname, body = request.postData();
    if (method !== 'GET') server.calls.push({ method, path: p, body: body && p.includes('/models/upload') ? null : body });
    if (p === '/api/projects' && method === 'GET') return route.fulfill({ json: { projects: [] } });
    if (p === '/api/projects' && method === 'POST') return route.fulfill({ json: shell(50, server.pack ? { data: { ...shell(50).data, pack: server.pack } } : {}) });
    if (p === '/api/projects/50/models/upload') {
      server.models.push({ id: server.nextModel++, file: url.searchParams.get('filename') });
      return route.fulfill({ json: shell(50) });
    }
    if (p === '/api/projects/50/intake-files') return route.fulfill({ json: rows() });
    if (p === '/api/projects/50/pack') {
      server.pack = JSON.parse(body).pack;
      return route.fulfill({ json: shell(50) });
    }
    if (p === '/api/projects/50/plan' && method === 'POST') {
      if (refuse) return route.fulfill({ json: { plates: [], problems: [{}], refused: '1 object would be off the bed (tall.stl)', revision: 1, fingerprint: 'f', bed: [0, 0, 256, 256], kept: [] } });
      return route.fulfill({ json: { plates: plates(server.pack?.preset || 'normal'), problems: [], refused: null, too_big: [], revision: 1, fingerprint: 'f', bed: [0, 0, 256, 256], kept: [] } });
    }
    if (p === '/api/projects/50/plan/densities') {
      const hidden = server.models.filter(m => SHAPES[m.file].status === 'split').length;
      return route.fulfill({ json: Object.fromEntries(['tight', 'normal', 'roomy'].map(k => [k, { plates: Math.min(PLATES[k], Math.max(1, server.models.length - hidden)) + hidden, refused: null }])) });
    }
    if (p === '/api/projects/50/create') return route.fulfill({ json: { step: 'plates', arranged: { summary: '2 plates, 3 objects, 0 turned', plates: 2, too_big: [] },
      project: shell(50, { draft: false, name: JSON.parse(body).name }) } });
    if (p === '/api/projects/50/discard-draft') return route.fulfill({ json: { ok: true } });
    if (p === '/api/projects/50') return route.fulfill({ json: shell(50, { draft: false }) });
    return route.fulfill({ json: p === '/api/splits' ? { splits: [] }
      : p.endsWith('/status') ? { plates: [], progress: { counts: {}, filament_g: {} } }
      : p.endsWith('/jobs') ? { jobs: [] } : p === '/api/printers' ? []
      : p === '/api/defaults' ? { defaults: { options: {} } } : p === '/api/slicer/spools' ? { spools: [] }
      : p === '/api/slicer/presets' ? { machines: [], processes: {}, filaments: {} } : {} });
  });
  await page.goto('http://spooler.test/slicer.html?new=1');
  await page.locator('#dlg-projects').waitFor({ state: 'visible' });
  return { page, server };
}

const drop = (page, names) => page.evaluate(names => {
  const dt = new DataTransfer();
  for (const name of names) dt.items.add(new File(['solid x\nendsolid x\n'], name));
  document.getElementById('dlg-projects').dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
}, names);
const callsTo = (server, suffix) => server.calls.filter(c => c.path.endsWith(suffix));
const preview = page => page.locator('#new-intake .pack-preview');
const waitFor = async (check, what) => { for (let i = 0; i < 60; i++) { if (await check()) return; await new Promise(r => setTimeout(r, 100)); } assert.fail(what); };

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    // Dropping several models shows the density choice and a preview of the plan, with the plate count under each preset
    {
      const { page, server } = await open(browser);
      await drop(page, ['small.stl', 'tiny.stl', 'clip.stl']);
      const pack = page.locator('#new-intake .pack');
      await pack.waitFor({ state: 'visible', timeout: 8000 });
      assert.deepEqual(await pack.locator('[data-pack]').evaluateAll(b => b.map(x => x.dataset.pack)), ['tight', 'normal', 'roomy', 'custom']);
      assert.equal(await pack.locator('[data-pack="normal"]').getAttribute('aria-pressed'), 'true', 'Normal is chosen to begin with');
      await preview(page).getByText(/2 plates for 3 models/).waitFor({ timeout: 8000 });
      assert.equal(await preview(page).locator('.plan-map').count(), 2, 'a map of each plate');
      assert.equal(await preview(page).locator('.plan-object').count(), 3, 'the models are drawn by their outline');
      await waitFor(async () => /1 plate/.test(await pack.locator('[data-pack="tight"]').innerText()), 'the plate count appears under Tight');
      assert.match(await pack.locator('[data-pack="roomy"]').innerText(), /3 plates/);
      assert.match(await pack.locator('[data-pack="roomy"]').innerText(), /12 mm apart/);
      assert.match(await pack.locator('.pack-note').innerText(), /22 mm/, 'the supports note says what is kept');
      console.log('PASS: The density choice and a plan preview appear with the plate count at each preset');

      // Roomy: the draft holds it, it is remembered, and the preview is made again
      await pack.locator('[data-pack="roomy"]').click();
      await preview(page).getByText(/3 plates for 3 models/).waitFor({ timeout: 8000 });
      assert.deepEqual(JSON.parse(callsTo(server, '/projects/50/pack').at(-1).body), { pack: { preset: 'roomy', gap: 12 } });
      assert.deepEqual(JSON.parse(server.calls.find(c => c.path === '/api/defaults').body), { pack: { preset: 'roomy', gap: 12 } }, 'remembered for the next project');
      assert.equal(await pack.locator('[data-pack="roomy"]').getAttribute('aria-pressed'), 'true');

      // Custom opens a gap field; a value is sent as it is
      await pack.locator('[data-pack="custom"]').click();
      const gap = pack.locator('[data-pack-gap]');
      await gap.waitFor({ state: 'visible' });
      await gap.fill('9');
      await gap.press('Tab');
      await waitFor(() => callsTo(server, '/projects/50/pack').some(c => JSON.parse(c.body).pack.gap === 9), 'the custom gap is sent');
      assert.deepEqual(JSON.parse(callsTo(server, '/projects/50/pack').at(-1).body), { pack: { preset: 'custom', gap: 9 } });

      // Packing closer than the supports' reach is a separate, explicit choice
      await pack.locator('[data-pack-supports]').check();
      await waitFor(() => JSON.parse(callsTo(server, '/projects/50/pack').at(-1).body).pack.tight_supports === true, 'the supports choice is sent');
      assert.match(await pack.locator('.pack-note').innerText(), /as close as the gap says/);
      console.log('PASS: Choosing Roomy, a custom gap or tighter-than-supports reaches the draft and is remembered');

      // Create arranges at that density; the choice can be turned off
      await page.locator('#new-project-name').fill('Clips');
      await page.locator('#btn-new-create').click();
      await waitFor(() => callsTo(server, '/projects/50/create').length, 'Create is asked for');
      assert.equal(JSON.parse(callsTo(server, '/projects/50/create')[0].body).arrange, true);
      await page.close();
    }

    // Leaving everything on plate 1 is a choice: no plan is promised and Create says so
    {
      const { page, server } = await open(browser);
      await drop(page, ['small.stl', 'tiny.stl']);
      const pack = page.locator('#new-intake .pack');
      await pack.waitFor({ state: 'visible', timeout: 8000 });
      await pack.locator('[data-pack-arrange]').uncheck();
      assert.match(await preview(page).innerText(), /stays on plate 1|Everything stays on plate 1/);
      await page.locator('#new-project-name').fill('By hand');
      await page.locator('#btn-new-create').click();
      await waitFor(() => callsTo(server, '/projects/50/create').length, 'Create is asked for');
      assert.equal(JSON.parse(callsTo(server, '/projects/50/create')[0].body).arrange, false);
      console.log('PASS: Arrange onto plates can be switched off at Create');
      await page.close();
    }

    // A model too big for the bed does not take a plate in the preview, and the preview says it is cut later
    {
      const { page } = await open(browser);
      await drop(page, ['small.stl', 'tiny.stl', 'big.stl']);
      await preview(page).getByText(/oversized model will be cut/).waitFor({ timeout: 8000 });
      assert.match(await preview(page).innerText(), /2 plates for 2 models/, 'the plate given to the oversized model is not counted');
      assert.equal(await preview(page).locator('.plan-map').count(), 2);
      assert.match(await page.locator('#new-intake .pack [data-pack="roomy"]').innerText(), /2 plates/, 'nor in the counts under each preset');
      console.log('PASS: The preview leaves out the plate of a model that will be split');
      await page.close();
    }

    // The density a new project starts with is the one remembered, and a plan that cannot be made is said in words
    {
      const { page } = await open(browser, { remembered: { preset: 'tight', gap: 3 }, refuse: true });
      await drop(page, ['small.stl', 'tiny.stl']);
      const pack = page.locator('#new-intake .pack');
      await pack.waitFor({ state: 'visible', timeout: 8000 });
      assert.equal(await pack.locator('[data-pack="tight"]').getAttribute('aria-pressed'), 'true');
      await preview(page).getByText(/No plan: 1 object would be off the bed/).waitFor({ timeout: 8000 });
      assert.match(await preview(page).innerText(), /plate 1/, 'it says where the models go instead');
      assert.equal(await page.locator('#btn-new-create').isDisabled(), false, 'Create still works');
      console.log('PASS: The remembered density is chosen, and a refused plan is explained without blocking Create');
      await page.close();
    }
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
