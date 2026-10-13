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

// A fake draft + intake-files + create. Files are analysed at once unless `holdAnalysis` is set (the plan panel is what is under
// test): small and tiny fit, big is bigger than the bed, metres is a unit mix-up. The summary mirrors the server's.
const SHAPES = {
  'small.stl': { size: [40, 30, 20], status: 'fits' },
  'tiny.stl': { size: [10, 10, 10], status: 'fits' },
  'big.stl': { size: [650, 400, 300], status: 'split' },
  'metres.stl': { size: [0.2, 0.1, 0.05], status: 'check_units' },
};
const makeSplit = id => ({ id, source_name: 'big.stl', state: 'ready', settings: { scale: 1, up: 'z', printer: { bed_x: 240, bed_y: 240, height: 250 }, planner: { mode: 'smart', balanced: false }, connectors: { kind: 'none' } },
  cuts: { cuts: [] }, analysis: { raw_size: [650, 400, 300], size: [650, 400, 300], volume_cm3: 100, source_triangles: 12, triangles: 12, repairs: [], mean_wall_mm: 2, fits_whole: false, estimated_parts: 4 } });
const shell = (id, extra = {}) => ({ id, name: 'Untitled project', revision: 1, split_id: null, split_ids: [], draft: true, models: [], slices: {}, server_time: Date.now() / 1000,
  data: { plates: [{ id: 1, name: 'Plate 1' }], objects: [], bed: 'textured', profile: { bundle_id: null, process: {}, filament: {} } }, ...extra });

async function open(browser, viewport = { width: 1280, height: 800 }) {
  const page = await browser.newPage({ viewport });
  const server = { calls: [], models: [], nextModel: 1, holdAnalysis: false, created: false };
  const hasBig = () => server.created && server.models.some(x => SHAPES[x.file].status === 'split');
  // the created project: oversized models are splits, the others are objects stacked on plate 1
  const created = (name) => {
    const small = server.models.filter(x => SHAPES[x.file].status !== 'split');
    return shell(50, { draft: false, name, split_id: hasBig() ? 1 : null, split_ids: hasBig() ? [1] : [],
      models: small.map(x => ({ id: x.id, file: x.file, state: 'ready', import: [1, 'z'] })),
      data: { plates: [{ id: 1, name: 'Plate 1' }], bed: 'textured', profile: { bundle_id: null, process: {}, filament: {} },
        objects: small.map(x => ({ id: x.id + 100, model: x.id, plate: 1, name: x.file, position: [128, 128], rotation: [0, 0, 0], scale: [1, 1, 1] })) } });
  };
  await page.route('http://spooler.test/**', route => {
    const file = path.join(root, new URL(route.request().url()).pathname);
    if (!file.startsWith(root) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404 });
    return route.fulfill({ body: fs.readFileSync(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.html') ? 'text/html' : 'application/octet-stream' });
  });
  page.on('pageerror', e => console.error('Page error:', e.message));
  const rows = () => {
    const out = server.models.map(m => {
      const s = SHAPES[m.file], done = !server.holdAnalysis;
      return { model: m.id, object: m.id + 100, file: m.file, kind: 'model', state: done ? 'ready' : 'analysing', error: null, size_bytes: 1000,
        size: done ? s.size : null, scale: m.scale || 1, up: 'z', status: done ? s.status : null, fits: done ? s.status === 'fits' : null,
        scale_ok: done ? s.status !== 'check_units' : null, scale_fix: null, reason: s.status === 'check_units' ? 'This size looks wrong' : null,
        repairs: [], triangles: 12, volume_cm3: 1, copies: m.copies || 1, estimate: null };
    });
    const count = status => out.filter(r => r.status === status).length;
    const analysing = out.filter(r => r.state === 'analysing').length;
    const summary = { models: out.length, objects: out.reduce((a, r) => a + r.copies, 0), split: count('split'), fits: count('fits'), check_units: count('check_units'), analysing, failed: 0 };
    summary.can_create = out.length > 0 && !summary.check_units && !analysing;
    summary.steps = (summary.split ? ['split'] : []).concat(['plates', 'slice', 'print']);
    return { rows: out, summary };
  };
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url()), method = request.method(), p = url.pathname;
    const body = request.postData();
    if (method !== 'GET') server.calls.push({ method, path: p, body: body && p.includes('/models/upload') ? null : body });
    let m;
    if (p === '/api/projects' && method === 'GET') return route.fulfill({ json: { projects: [] } });
    if (p === '/api/projects' && method === 'POST') return route.fulfill({ json: shell(50) });
    if (p === '/api/projects/50/models/upload') {
      server.models.push({ id: server.nextModel++, file: url.searchParams.get('filename'), copies: 1 });
      return route.fulfill({ json: shell(50) });
    }
    if (p === '/api/projects/50/intake-files') return route.fulfill({ json: rows() });
    if ((m = p.match(/^\/api\/projects\/50\/models\/(\d+)$/)) && method === 'DELETE') {
      server.models = server.models.filter(x => x.id !== Number(m[1]));
      return route.fulfill({ json: shell(50) });
    }
    if (p === '/api/projects/50/copies') {
      const change = JSON.parse(body);
      server.models.find(x => x.id + 100 === change.object).copies = change.copies;
      return route.fulfill({ json: shell(50) });
    }
    if (p === '/api/projects/50/create') {
      server.created = true;
      const big = hasBig();
      return route.fulfill({ json: { step: big ? 'model' : 'plates', project: created(JSON.parse(body).name) } });
    }
    if (p === '/api/projects/50/discard-draft') return route.fulfill({ json: { ok: true } });
    if (p === '/api/projects/50/plan' && method === 'POST') return route.fulfill({ json: { plates: [{ id: 1 }], problems: [], refused: false, too_big: [], revision: 1, fingerprint: 'f' } });
    if (p === '/api/projects/50/plan/accept') return route.fulfill({ json: { ...created('x'), plan: { summary: 'planned' } } });
    if (p === '/api/projects/50') return route.fulfill({ json: created('Untitled project') });
    if (p === '/api/splits/1') return route.fulfill({ json: makeSplit(1) });
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
const stepNames = page => page.locator('#new-intake .plan-steps > li b').allInnerTexts();
const chips = (page, cls, n) => page.waitForFunction(([cls, n]) => document.querySelectorAll(`#new-intake [data-intake-row] .intake-chip.${cls}`).length === n, [cls, n], { timeout: 8000 });
const reasonHas = (page, re) => page.waitForFunction(re => new RegExp(re, 'i').test(document.getElementById('new-create-reason')?.textContent || ''), re, { timeout: 8000 });

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    // Everything fits: no Split step, Plates counts copies, Create opens Plates
    const a = await open(browser);
    await drop(a.page, ['small.stl', 'tiny.stl']);
    await chips(a.page, 'ok', 2);
    assert.deepEqual(await stepNames(a.page), ['Plates', 'Slice', 'Print']);
    const plan = a.page.locator('#new-intake .intake-plan');
    assert.match(await plan.innerText(), /2 objects/);
    assert.match(await plan.innerText(), /default settings/i);
    const copies = a.page.locator('#new-intake [data-intake-row]', { hasText: 'small.stl' }).getByLabel(/Copies/);
    await copies.fill('4');
    await copies.press('Tab');
    await plan.getByText(/5 objects/).waitFor({ timeout: 5000 });
    assert.equal(await a.page.locator('#new-intake .plan-mixed:not([hidden])').count(), 0, 'no mixed sentence when nothing is too big');
    await a.page.waitForFunction(() => !document.getElementById('btn-new-create').disabled);
    await a.page.locator('#btn-new-create').click();
    await a.page.waitForFunction(() => !document.getElementById('dlg-projects').open);
    await a.page.waitForFunction(() => location.search.includes('project=50'));
    await a.page.waitForSelector('.slicer-step.active');
    assert.equal(await a.page.locator('.slicer-step.active').getAttribute('data-step'), 'plates');
    console.log('PASS: All files fit: no Split step, Plates counts copies, Create opens Plates');
    await a.page.close();

    // One big model: Split step with the count; Create opens the step the server named (Model, where the split is laid out)
    const b = await open(browser);
    await drop(b.page, ['big.stl']);
    await chips(b.page, 'split', 1);
    assert.deepEqual(await stepNames(b.page), ['Split', 'Plates', 'Slice', 'Print']);
    assert.match(await b.page.locator('#new-intake .intake-plan').innerText(), /1 model bigger than the bed/);
    assert.equal(await b.page.locator('#new-intake .plan-mixed:not([hidden])').count(), 0, 'a lone big model is not a mixed upload');
    await b.page.waitForFunction(() => !document.getElementById('btn-new-create').disabled);
    await b.page.locator('#btn-new-create').click();
    await b.page.waitForFunction(() => location.search.includes('project=50'));
    await b.page.waitForSelector('.slicer-step.active');
    assert.equal(await b.page.locator('.slicer-step.active').getAttribute('data-step'), 'split', 'the split is analysed and planned, so Create lands on Split');
    console.log('PASS: One big model: Split step with the count, Create opens on Split with the cuts planned');
    await b.page.close();

    // Mixed: says only the oversized model is split; Change opens that file's units choice
    const c = await open(browser);
    await drop(c.page, ['small.stl', 'tiny.stl', 'big.stl']);
    await chips(c.page, 'ok', 2);
    await chips(c.page, 'split', 1);
    const mixed = c.page.locator('#new-intake .plan-mixed:not([hidden])');
    assert.match(await mixed.innerText(), /Only the 1 oversized model is split; the other 2 go to plates/);
    await mixed.getByRole('button', { name: 'Change' }).click();
    await c.page.locator('#new-intake [data-intake-row]', { hasText: 'big.stl' }).locator('.intake-units-panel').waitFor({ timeout: 3000 });
    assert.equal(await c.page.locator('#new-intake .intake-units-panel').count(), 1, 'only the oversized file opens');
    console.log('PASS: A mixed upload says only the oversized model is split; Change opens its units choice');
    // Create: the oversized model opens on Split, and the small models are planned onto plates rather than left stacked on plate 1
    await c.page.waitForFunction(() => !document.getElementById('btn-new-create').disabled);
    await c.page.locator('#btn-new-create').click();
    await c.page.waitForFunction(() => location.search.includes('project=50'));
    for (let i = 0; i < 60 && !c.server.calls.some(x => x.path === '/api/projects/50/create'); i++) await c.page.waitForTimeout(100);
    const created = c.server.calls.find(x => x.path === '/api/projects/50/create');
    assert.equal(JSON.parse(created.body).arrange, true, 'Create asks the server to arrange the models that fit, at the chosen density');
    assert.equal(c.server.calls.some(x => x.path === '/api/projects/50/plan/accept'), false, 'the page no longer plans after Create');
    assert.equal(await c.page.locator('.slicer-step.active').getAttribute('data-step'), 'split', 'arranging the small models does not pull the page off the Split step');
    console.log('PASS: A mixed project opens on Split and plans the small models');
    await c.page.close();

    // Create is disabled with a plain reason
    const d = await open(browser);
    d.server.holdAnalysis = true;
    await drop(d.page, ['small.stl', 'metres.stl']);
    await d.page.locator('#new-intake').getByText('small.stl', { exact: true }).waitFor();
    await reasonHas(d.page, 'checked');
    assert.equal(await d.page.locator('#btn-new-create').isDisabled(), true);
    d.server.holdAnalysis = false;
    await reasonHas(d.page, 'units');
    assert.match(await d.page.locator('#new-create-reason').innerText(), /Check the size of 1 model marked Check units/);
    assert.equal(await d.page.locator('#btn-new-create').isDisabled(), true);
    await d.page.locator('#new-intake [data-intake-row]', { hasText: 'metres.stl' }).getByRole('button', { name: 'Remove metres.stl' }).click();
    await d.page.waitForFunction(() => !document.getElementById('btn-new-create').disabled, null, { timeout: 8000 });
    assert.equal((await d.page.locator('#new-create-reason').innerText()).trim(), '', 'no reason once Create is enabled');
    await d.page.locator('#new-project-name').fill('   ');
    await d.page.waitForFunction(() => document.getElementById('btn-new-create').disabled);
    assert.match(await d.page.locator('#new-create-reason').innerText(), /name/i);
    console.log('PASS: Create is disabled with a plain reason: checking, units, no name');
    await d.page.close();

    // Narrow screen: the panel stacks under the table, no horizontal scroll
    const n = await open(browser, { width: 390, height: 844 });
    await drop(n.page, ['small.stl', 'big.stl']);
    await chips(n.page, 'split', 1);
    assert.equal(await n.page.locator('#new-intake .intake-plan').isVisible(), true);
    assert.deepEqual(await n.page.evaluate(() => ({ doc: document.documentElement.scrollWidth <= innerWidth,
      dialog: document.getElementById('dlg-projects').scrollWidth <= document.getElementById('dlg-projects').clientWidth })), { doc: true, dialog: true });
    console.log('PASS: The plan panel fits at 390 px without horizontal scroll');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
