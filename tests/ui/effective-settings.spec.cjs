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
  const server = { patches: [], packs: [], revision: 1,
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
    if (p.endsWith("/effective-settings")) return route.fulfill({ json: { version: 1, revision: server.revision, plate_id: Number(p.split("/")[5]), fingerprint: "review-fingerprint", evidence: "expected-inputs", machine: { nozzle_diameter: ["0.4"] }, process: { wall_loops: "6", curr_bed_type: "High Temp Plate" }, filament: { filament_type: ["PETG"], nozzle_temperature: ["250"] }, provenance: { process: { wall_loops: [{source:"project",value:"4"},{source:"plate",value:"6"}], curr_bed_type: [{source:"bed",value:"High Temp Plate"}] }, filament: {} }, objects: [], issues: [] } });
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


(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const { page, server } = await open(browser);
    await page.getByRole('button', { name: 'Settings actually used', exact: true }).click({timeout:3000});
    const review = page.locator('#effective-settings-review');
    await review.locator('b').filter({hasText:'PETG'}).waitFor({timeout:3000});
    assert.match(await review.innerText(), /Settings sent to Orca/);
    await review.locator('summary').filter({hasText:'Filament values'}).click();
    await review.locator('summary').filter({hasText:'Process values'}).click();
    assert.match(await review.innerText(), /250/);
    assert.match(await review.innerText(), /High Temp Plate/);
    assert.match(await review.innerText(), /plate/);
    assert.match(await review.innerText(), /not measured output/);
    assert.equal(server.patches.length, 0, 'review is read only');
    // The null-profile fixture has no editor tabs; a repeated review still uses the same Plate response.
    await page.getByRole('button', { name: 'Settings actually used', exact: true }).click();
    await review.locator('b').filter({hasText:'PETG'}).waitFor();
    await page.locator('#plate-tabs [data-plate="2"]').click();
    assert.equal(await review.innerText(), '', 'old Plate review is cleared');
    await page.getByRole('button', { name: 'Settings actually used', exact: true }).click();
    await review.locator('b').filter({hasText:'PETG'}).waitFor();
    await page.close();
    console.log('PASS: selected Plate review shows authoritative expected inputs and clears on selection change');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });

