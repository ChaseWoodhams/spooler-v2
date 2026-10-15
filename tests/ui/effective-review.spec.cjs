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

// Public Project browser seam with disposable routed responses; no live server or printers.
function boxPreview() {
  const [w, d, h] = [40, 30, 20];
  const corners = [[0, 0, 0], [w, 0, 0], [w, d, 0], [0, d, 0], [0, 0, h], [w, 0, h], [w, d, h], [0, d, h]];
  const tris = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]];
  const buffer = Buffer.alloc(8 + 8 + 8 * 12 + 12 * 12);
  buffer.write('SPV1', 0, 'latin1');
  buffer.writeUInt32LE(1, 4);
  buffer.writeUInt32LE(8, 8);
  buffer.writeUInt32LE(12, 12);
  corners.forEach((c, i) => c.forEach((v, k) => buffer.writeFloatLE(v, 16 + i * 12 + k * 4)));
  tris.forEach((t, i) => t.forEach((v, k) => buffer.writeUInt32LE(v, 16 + 96 + i * 12 + k * 4)));
  return buffer;
}
const model = id => ({ id, file: `box${id}.stl`, state: 'ready', sha256: `sha${id}`, import: [1, 'z'], size: [40, 30, 20], triangles: 12, volume_cm3: 1, repairs: [] });
const object = (id, plate, x, y) => ({ id, model: id, plate, name: `box${id}`, position: [x, y], rotation: [0, 0, 0], scale: [1, 1, 1] });

async function open(browser, { tooClose = false } = {}) {
  const page = await browser.newPage({ viewport: process.env.VIEW ? { width: +process.env.VIEW.split('x')[0], height: +process.env.VIEW.split('x')[1] } : { width: 1400, height: 900 } });
  const server = { requests: [], patches: [], packs: [], resolutions: [], issue: false, revision: 1,
    data: { plates: [{ id: 1, name: 'Plate 1' }, { id: 2, name: 'Plate 2' }], bed: 'textured', profile: { bundle_id: null, process: {}, filament: {} },
            objects: [object(1, 1, 50, 50), object(2, 1, 120, 50), object(3, 1, 200, 150), object(4, 2, 128, 128)] } };
  const project = () => ({ id: 70, name: 'Plates', revision: server.revision, split_id: null, split_ids: [], models: [1, 2, 3, 4].map(model), slices: {},
    server_time: Date.now() / 1000, data: server.data });
  await page.route('http://spooler.test/**', route => {
    const file = path.join(root, new URL(route.request().url()).pathname);
    if (!file.startsWith(root) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404 });
    return route.fulfill({ body: fs.readFileSync(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.html') ? 'text/html' : 'application/octet-stream' });
  });
  page.on('pageerror', e => console.error('Page error:', e.message));
  await page.route('**/api/**', async route => {
    const request = route.request(), method = request.method(), p = new URL(request.url()).pathname, body = request.postData();
    if (p === '/api/projects/70/settings/resolve') {
      const change = JSON.parse(body);
      server.resolutions.push(change);
      if (!change.replace) return route.fulfill({status:409,json:{error:'Review destination value',issues:[{code:'setting-move-collision',destination_value:'300',value:'250'}]}});
      server.issue = false;
      server.revision += 1;
      return route.fulfill({json:project()});
    }
    if (p === '/api/projects/70' && method === 'PATCH') {
      server.data = JSON.parse(body).data;
      server.revision += 1;
      server.patches.push(server.data);
      return route.fulfill({ json: project() });
    }
    if (p === '/api/projects/70/pack') {
      const change = JSON.parse(body);
      server.packs.push(change);
      if ('pack' in change) { if (change.pack) server.data.pack = change.pack; else delete server.data.pack; }
      if (change.plate_id) {
        const plate = server.data.plates.find(x => x.id === change.plate_id);
        if (change.plate_pack) plate.pack = { ...change.plate_pack, gap: { tight: 3, normal: 6, roomy: 12 }[change.plate_pack.preset] }; else delete plate.pack;
      }
      server.revision += 1;
      return route.fulfill({ json: project() });
    }
    if (p === '/api/projects/70') return route.fulfill({ json: project() });
    if (/^\/api\/projects\/70\/models\/\d+\/preview$/.test(p)) return route.fulfill({ body: boxPreview(), contentType: 'application/octet-stream' });
    if (p === '/api/projects/70/status') {
      const problems = tooClose ? [{ key: 2, plate: 1, problem: 'too close', other: 1, name: 'box2', text: 'box2 is too close to box1', soft: true },
                                   { key: 1, plate: 1, problem: 'too close', other: 2, name: 'box1', text: 'box1 is too close to box2', soft: true }] : [];
      return route.fulfill({ json: { plates: server.data.plates.map(pl => ({ id: pl.id, problems: pl.id === 1 ? problems : [], problem_reason: null, warning_reason: null })),
                                     progress: { counts: {}, filament_g: {} } } });
    }
    if (p.endsWith("/effective-settings")) {
      const params = new URL(request.url()).searchParams;
      server.requests.push(params);
      return route.fulfill({ json: reviewFor(params) });
    }
    return route.fulfill({ json: p === '/api/splits' ? { splits: [] } : p.endsWith('/jobs') ? { jobs: [] } : p === '/api/printers' ? []
      : p === '/api/defaults' ? { defaults: { options: {} } } : p === '/api/slicer/spools' ? { spools: [] }
      : p === '/api/slicer/presets' ? { machines: [], processes: {}, filaments: {} } : {} });
  });
  await page.goto('http://spooler.test/slicer.html?project=70');
  await page.locator('#project-name').waitFor();
  await page.waitForFunction(() => document.getElementById('project-name').value !== '');
  await page.locator('[data-step="plates"]').click();
  await page.locator('#object-list [data-object]').first().waitFor();
  await page.waitForTimeout(400);   // geometry loads, then the first measure
  return { page, server };
}

const row = (kind, key, label, section, o = {}) => ({ kind, key, label, section, inherited: null, has_inherited: false, requested: null, effective: '', source: 'preset',
  chain: [], changed: false, enforced: false, enforced_reason: null, inactive: false, inactive_reason: null, reset_to: null, editable: true, read_only: false,
  read_only_reason: null, control: null, note: null, ...o });
const ROWS = [
  row('filament', 'filament_type', 'Filament type', 'material', { effective: ['PETG'], inherited: ['PETG'], has_inherited: true }),
  row('machine', 'nozzle_diameter', 'Nozzle diameter', 'nozzle', { effective: ['0.4'], inherited: ['0.4'], has_inherited: true, editable: false, read_only: true, read_only_reason: 'Machine value from the Printer preset; read-only here.' }),
  row('process', 'curr_bed_type', 'Bed type', 'bed', { effective: 'High Temp Plate', source: 'bed', enforced: true, enforced_reason: 'Project bed selection', editable: false, read_only: true, read_only_reason: 'Project bed selection', changed: true }),
  row('filament', 'nozzle_temperature', 'Nozzle temperature', 'temperature', { inherited: ['240'], has_inherited: true, requested: ['245'], effective: ['245'], source: 'project', changed: true,
    reset_to: { value: ['240'], source: 'preset' }, note: 'From the Project default Filament tuning (Spoolman spool #7 values apply to the default Filament only).', control: { scope: 'project', kind: 'filament', key: 'nozzle_temperature' } }),
  row('filament', 'filament_retraction_speed', 'Retraction speed', 'retraction', { effective: ['30'], source: 'project', requested: ['30'], changed: true, inactive: true, inactive_reason: 'Set a retraction length above zero first.', reset_to: { value: null, source: 'orca-default' } }),
  row('process', 'wall_generator', 'Wall generator', 'other', { requested: 'arachne', effective: 'classic', source: 'bricklayers', enforced: true, enforced_reason: 'Required by BrickLayers post-processing', note: 'Requested arachne but Required by BrickLayers post-processing sets it.', changed: true, editable: false, read_only: true }),
  row('process', 'wall_loops', 'Wall loops', 'other', { inherited: '3', has_inherited: true, requested: '6', effective: '6', source: 'plate', changed: true, reset_to: { value: '4', source: 'project' }, control: { scope: 'plate', kind: 'process', key: 'wall_loops' } }),
  row('process', 'layer_height', 'Layer height', 'other', { inherited: '0.2', has_inherited: true, effective: '0.2' }),
  row('process', 'from', 'from', 'other', { effective: 'system', editable: false, read_only: true, read_only_reason: 'Inherited from the preset; Spooler has no verified control for it, so it is passed through unchanged.' }),
];
function reviewFor(params) {
  const object = params.get('object');
  const needle = (params.get('search') || '').trim().toLowerCase();
  let rows = ROWS.map(r => object && r.key === 'wall_loops' ? { ...r, effective: '8', requested: '8', source: 'object', reset_to: { value: '6', source: 'plate' }, control: { scope: 'object', kind: 'process', key: 'wall_loops', id: 1 } } : r);
  if (needle) rows = rows.filter(r => r.key.includes(needle) || r.label.toLowerCase().includes(needle));
  if (params.get('changed') === '1') rows = rows.filter(r => r.changed);
  return { version: 1, revision: 1, plate_id: 1, fingerprint: 'f', evidence: 'expected-inputs', machine: {}, process: {}, filament: {}, objects: [], provenance: { machine: {}, process: {}, filament: {} },
    filament_choice: null,
    issues: [{ code: 'unsupported-object-setting', key: 'travel_speed', kind: 'process', scope: 'object', affected_id: 1, value: '250', message: 'travel_speed belongs at Project or Plate scope', resolution_required: true,
      control: { scope: 'object', kind: 'process', key: 'travel_speed', id: 1 } },
      { code: 'unresolved-inherited-setting', key: 'mystery', kind: 'filament', scope: 'preset', message: 'mystery: inherited setting has no pinned definition', control: null, read_only: true }],
    review: { version: 1, label: 'Settings that will be sent to Orca (expected inputs, not measured output)', evidence: 'expected-inputs',
      scope: object ? { kind: 'object', id: 1, name: 'box1' } : { kind: 'plate', id: 1 }, changed_basis: object ? "the Plate's effective value" : "the preset's inherited value",
      filament_basis: { kind: 'default', explanation: "Project default Filament preset plus the Project's Filament tuning. Values adopted from Spoolman spool #7 are part of that tuning and apply to the default Filament only." }, rows } };
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const { page, server } = await open(browser);
    await page.getByRole('button', { name: 'Settings actually used', exact: true }).click({ timeout: 3000 });
    const review = page.locator('#effective-settings-review');
    await review.getByRole('heading', { name: 'Settings sent to Orca', exact: true }).waitFor({ timeout: 3000 });
    const text = await review.innerText();
    assert.match(text, /not measured output/);
    assert.match(text, /apply to the default Filament only/);
    // Material, nozzle and bed come before the long tail; machine values are available but read-only.
    const sections = await review.locator('details > summary').allInnerTexts();
    assert.deepEqual(sections.map(s => s.replace(/\s*\(\d+\)$/, '').trim()).slice(0, 5), ['Material', 'Nozzle', 'Bed', 'Temperature', 'Retraction']);
    const walls = review.getByRole('row', { name: /Wall loops/ });
    const cells = await walls.getByRole('cell').allInnerTexts();
    assert.ok(cells.some(c => c.trim() === '3'), 'inherited value ' + JSON.stringify(cells));
    assert.ok(cells.filter(c => c.trim() === '6').length >= 2, 'requested and effective value');
    assert.match(await walls.innerText(), /plate/);
    assert.match(await walls.innerText(), /Reset returns to 4 \(project\)/);
    assert.match(await review.getByRole('row', { name: /Retraction speed/ }).innerText(), /Inactive: Set a retraction length above zero first\. Stored value is retained\./);
    assert.match(await review.getByRole('row', { name: /Wall generator/ }).innerText(), /Enforced: Required by BrickLayers post-processing.*Requested arachne/s);
    assert.match(await review.getByRole('row', { name: /Bed type/ }).innerText(), /Enforced: Project bed selection/);
    assert.match(await review.getByRole('row', { name: /Nozzle diameter/ }).innerText(), /Read-only/);
    assert.match(await review.getByRole('row', { name: /^from/ }).innerText(), /passed through unchanged/);
    assert.doesNotMatch(text, /chamber heater control/i);
    // Search and the changed filter ask the server; the browser does not filter or merge by itself.
    await review.getByRole('searchbox', { name: 'Search reviewed settings' }).fill('wall');
    await page.waitForFunction(() => !document.querySelector('#effective-settings-review')?.innerText.includes('Layer height'));
    await review.getByRole('row', { name: /Wall loops/ }).waitFor();
    assert.equal(server.requests.at(-1).get('search'), 'wall');
    await review.getByRole('searchbox', { name: 'Search reviewed settings' }).fill('');
    await review.getByRole('checkbox', { name: 'Changed values only' }).check();
    await page.waitForFunction(() => { const t = document.querySelector('#effective-settings-review')?.innerText; return t.includes('Nozzle temperature') && !t.includes('Layer height'); });
    await review.getByRole('row', { name: /Wall loops/ }).waitFor();
    assert.equal(server.requests.at(-1).get('changed'), '1');
    await review.getByRole('checkbox', { name: 'Changed values only' }).uncheck();
    // Object drill-down shows the server's Object chain.
    await review.getByRole('combobox', { name: 'Review scope' }).selectOption({ label: 'box1' });
    await review.getByRole('heading', { name: 'Settings sent to Orca — box1' }).waitFor();
    assert.equal(server.requests.at(-1).get('object'), '1');
    assert.match(await review.getByRole('row', { name: /Wall loops/ }).innerText(), /Reset returns to 6 \(plate\)/);
    // Issues link to their owning controls; read-only inherited issues do not offer one.
    await review.getByRole('button', { name: 'Go to travel_speed control' }).click();
    assert.equal(await page.locator('#settings-search').inputValue(), 'travel_speed');
    assert.equal(await page.locator('#fold-settings').evaluate(el => el.open), true);
    assert.equal(await review.getByRole('button', { name: /Go to mystery control/ }).count(), 0);
    assert.match(await review.innerText(), /mystery: inherited setting has no pinned definition/);
    assert.equal(server.patches.length, 0, 'review is read only');
    await page.close();
    console.log('PASS: effective settings review shows chains, explanations, search, changed filter, Object drill-down and issue links');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
