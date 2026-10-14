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

// Public Project browser seam: static files and API answers are routed, so no live server, Spoolman or printer is used.
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
const bundle = (id, filament) => ({ id, label: `Standard ${filament}`, presets: { machine: { name: 'CC1' }, process: { name: 'Standard' }, filament: { name: filament } } });
const ASA = { version: 1, sha256: 'a'.repeat(64), name: 'Workshop ASA', material: ['ASA'] };

const base = { machine: 'CC1', nozzle: '0.4', validated_against: 'saved Project profile' };
const presetPreview = (revision, extra = {}) => ({
  version: 1, source: 'preset', revision, token: `t${revision}`, target: { kind: 'default' }, applies_to: 'project-default', unchanged: false, base,
  current: { name: 'Workshop PETG', material: ['PETG'], sha256: '1' }, proposed: { name: 'Workshop PETG', material: ['PETG'], nozzle: '0.4', sha256: '2' },
  material_changed: false, changes: [{ key: 'nozzle_temperature', before: ['240'], after: ['250'] }],
  preserved: { project_overrides: [], alternates: [{ model_id: 1, preset: 'Workshop ASA', material: ['ASA'], sha256: 'a'.repeat(64) }] },
  affected: { plates: [{ id: 1, name: 'Plate 1' }], outdated_slices: [] }, issues: [], ...extra });
const spoolPreview = (revision, extra = {}) => ({
  version: 1, source: 'spool', revision, token: `s${revision}`, target: { kind: 'default' }, applies_to: 'project-default', unchanged: false, base,
  spool: { id: 7, name: 'Sunlu PETG Clear', vendor: 'Sunlu', material: 'PETG' }, stock: { remaining_weight: 800, initial_weight: 1000 },
  current: { name: 'Workshop PETG', material: ['PETG'], sha256: '1' }, proposed: { name: 'Workshop PETG', material: ['PETG'], nozzle: '0.4', sha256: '1' },
  changes: [{ key: 'nozzle_temperature', before: ['240'], after: ['238'] }, { key: 'filament_density', before: ['1.24'], after: ['1.27'] }],
  skipped: [], cleared: [], preserved: { alternates: [{ model_id: 1, preset: 'Workshop ASA', material: ['ASA'], sha256: 'a'.repeat(64) }] },
  affected: { plates: [{ id: 1, name: 'Plate 1' }], alternate_plates: [{ id: 2, name: 'Plate 2' }], outdated_slices: [] }, issues: [], ...extra });

async function open(browser, script) {
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  const server = { revision: 1, patches: [], reviews: [], adopts: [], script,
    data: { plates: [{ id: 1, name: 'Plate 1' }, { id: 2, name: 'Plate 2' }], bed: 'textured', profile: { bundle_id: 'b1', process: {}, filament: {}, spool_id: null },
            filaments: { '1': { preset: 'Workshop ASA', colour: 'black', snapshot: ASA } }, objects: [object(1, 1, 50, 50), object(2, 2, 120, 50)] } };
  const project = () => ({ id: 70, name: 'Review', revision: server.revision, split_id: null, split_ids: [], models: [1, 2].map(model), slices: {}, server_time: Date.now() / 1000, data: server.data });
  await page.route('http://spooler.test/**', route => {
    const file = path.join(root, new URL(route.request().url()).pathname);
    if (!file.startsWith(root) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404 });
    return route.fulfill({ body: fs.readFileSync(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.html') ? 'text/html' : 'application/octet-stream' });
  });
  page.on('pageerror', e => console.error('Page error:', e.message));
  await page.route('**/api/**', async route => {
    const request = route.request(), method = request.method(), p = new URL(request.url()).pathname;
    const body = request.postData() ? JSON.parse(request.postData()) : null;
    if (p === '/api/projects/70' && method === 'PATCH') {
      server.data = body.data;
      server.revision += 1;
      server.patches.push(body);
      return route.fulfill({ json: project() });
    }
    if (p === '/api/projects/70/settings/review') {
      server.reviews.push(body);
      return route.fulfill(server.script.review(server, body));
    }
    if (p === '/api/projects/70/settings/adopt') {
      server.adopts.push(body);
      return route.fulfill(server.script.adopt(server, body));
    }
    if (p === '/api/projects/70') return route.fulfill({ json: project() });
    if (/^\/api\/projects\/70\/models\/\d+\/preview$/.test(p)) return route.fulfill({ body: boxPreview(), contentType: 'application/octet-stream' });
    if (p === '/api/projects/70/status') return route.fulfill({ json: { plates: server.data.plates.map(pl => ({ id: pl.id, problems: [] })), progress: { counts: {}, filament_g: {} } } });
    if (p === '/api/slicer') return route.fulfill({ json: { bundles: [bundle('b1', 'Workshop PETG'), bundle('b2', 'Workshop PETG')] } });
    if (/\/api\/slicer\/profiles\/b\d\/settings$/.test(p)) return route.fulfill({ json: { bundle: { id: 'b1' }, machine: { name: 'CC1', nozzle_diameter: ['0.4'] }, process: { wall_loops: '3' }, filament: { filament_type: ['PETG'], nozzle_temperature: ['240'] } } });
    if (p.endsWith('/effective-settings')) return route.fulfill({ json: { version: 1, revision: server.revision, plate_id: 1, fingerprint: 'f', evidence: 'expected-inputs', machine: {}, process: {}, filament: {}, objects: [], provenance: { machine: {}, process: {}, filament: {} }, issues: [] } });
    return route.fulfill({ json: p === '/api/splits' ? { splits: [] } : p.endsWith('/jobs') ? { jobs: [] } : p === '/api/printers' ? []
      : p === '/api/defaults' ? { defaults: { options: {} } }
      : p === '/api/slicer/spools' ? { spools: [{ id: 7, name: 'Sunlu PETG Clear', material: 'PETG', remaining_weight: 800, location: '' }], printers: {} }
      : p === '/api/slicer/presets' ? { machines: ['CC1'], processes: { CC1: ['Standard'] }, filaments: { CC1: ['Workshop PETG', 'Workshop ASA'] }, user: {} } : {} });
  });
  await page.goto('http://spooler.test/slicer.html?project=70');
  await page.locator('#project-name').waitFor();
  await page.waitForFunction(() => document.getElementById('project-name').value !== '');
  await page.locator('[data-step="plates"]').click();
  await page.locator('#object-list [data-object]').first().waitFor();
  await page.locator('#fold-material').evaluate(el => { el.open = true; });
  await page.locator('#fold-material-more').evaluate(el => { el.open = true; });
  return { page, server };
}

const ok = json => ({ json });
const conflict = message => ({ status: 409, json: { error: message } });

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    // Preview, cancel, apply and undo of a preset refresh.
    {
      const { page, server } = await open(browser, {
        review: (s) => ok(presetPreview(s.revision)),
        adopt: (s, body) => {
          assert.equal(body.revision, s.revision);
          assert.equal(body.token, `t${s.revision}`);
          s.data = { ...s.data, profile: { ...s.data.profile, bundle_id: 'b2' } };
          s.revision += 1;
          return ok({ id: 70, name: 'Review', revision: s.revision, split_id: null, split_ids: [], models: [1, 2].map(model), slices: {}, server_time: Date.now() / 1000, data: s.data });
        } });
      await page.locator('#btn-refresh-filament').click();
      const dialog = page.locator('#dlg-review');
      await dialog.waitFor();
      const text = await dialog.innerText();
      assert.match(text, /Checked against CC1, 0\.4 mm nozzle/);
      assert.match(text, /Now: Workshop PETG, PETG\. After: Workshop PETG, PETG, 0\.4 mm nozzle/);
      assert.match(text, /nozzle_temperature\s+240\s+250/);
      assert.match(text, /Project default Filament only/);
      assert.match(text, /Unchanged: Workshop ASA \(Model 1, ASA\)/);
      await page.locator('#btn-review-cancel').click();
      assert.equal(await dialog.evaluate(el => el.open), false);
      assert.equal(server.adopts.length, 0, 'cancelling adopts nothing');
      assert.equal(server.patches.length, 0, 'cancelling saves nothing');
      assert.equal(server.revision, 1);

      await page.locator('#btn-refresh-filament').click();
      await dialog.waitFor();
      await page.locator('#btn-review-apply').click();
      await page.waitForFunction(() => !document.getElementById('dlg-review').open);
      assert.equal(server.adopts.length, 1);
      assert.equal(server.data.profile.bundle_id, 'b2');
      assert.equal(await page.locator('#btn-undo').isDisabled(), false, 'adoption is an ordinary undoable edit');
      await page.locator('#btn-undo').click();
      await page.waitForFunction(() => document.getElementById('save-state').textContent === 'Saved');
      assert.equal(server.patches.at(-1).data.profile.bundle_id, 'b1', 'undo saves the earlier locked profile through the normal edit path');
      await page.close();
      console.log('PASS: preset refresh preview, cancel, apply and undo');
    }

    // A changed Project or preset since the preview shows a new preview instead of applying the old one.
    {
      let attempts = 0;
      const { page, server } = await open(browser, {
        review: (s) => ok(presetPreview(s.revision, attempts ? { changes: [{ key: 'nozzle_temperature', before: ['240'], after: ['255'] }] } : {})),
        adopt: () => { attempts += 1; return conflict('The preset or spool changed since this review — preview the change again'); } });
      await page.locator('#btn-refresh-filament').click();
      await page.locator('#dlg-review').waitFor();
      await page.locator('#btn-review-apply').click();
      await page.waitForFunction(() => /changed since this review/.test(document.getElementById('review-body').innerText));
      assert.equal(await page.locator('#dlg-review').evaluate(el => el.open), true);
      assert.match(await page.locator('#review-body').innerText(), /nozzle_temperature\s+240\s+255/);
      assert.equal(server.reviews.length, 2);
      assert.equal(server.data.profile.bundle_id, 'b1');
      await page.close();
      console.log('PASS: stale preview is replaced by a new preview');
    }

    // Spool: default only, stock shown separately, selector unchanged until applied.
    {
      const { page, server } = await open(browser, {
        review: (s, body) => { assert.deepEqual(body, { source: 'spool', spool_id: 7 }); return ok(spoolPreview(s.revision)); },
        adopt: (s, body) => {
          s.data = { ...s.data, profile: { ...s.data.profile, spool_id: 7, filament: { nozzle_temperature: ['238'] } } };
          s.revision += 1;
          return ok({ id: 70, name: 'Review', revision: s.revision, split_id: null, split_ids: [], models: [1, 2].map(model), slices: {}, server_time: Date.now() / 1000, data: s.data });
        } });
      await page.locator('#spool-select').focus();
      await page.waitForFunction(() => document.querySelector('#spool-select option[value="7"]'));
      await page.locator('#spool-select').selectOption('7');
      await page.locator('#dlg-review').waitFor();
      const text = await page.locator('#dlg-review').innerText();
      assert.match(text, /Apply spool Sunlu PETG Clear/);
      assert.match(text, /Project default Filament only\. Models with their own Filament keep their saved snapshots/);
      assert.match(text, /filament_density\s+1\.24\s+1\.27/);
      assert.match(text, /Remaining stock: 800 g\. Shown for information; it is not a slicing setting/);
      assert.match(text, /Unchanged: Workshop ASA/);
      assert.equal(await page.locator('#spool-select').inputValue(), '', 'the saved spool is unchanged while reviewing');
      await page.locator('#btn-review-cancel').click();
      assert.equal(server.adopts.length, 0);
      assert.equal(server.data.profile.spool_id, null);
      await page.locator('#spool-select').selectOption('7');
      await page.locator('#btn-review-apply').click();
      await page.waitForFunction(() => !document.getElementById('dlg-review').open);
      assert.equal(server.data.profile.spool_id, 7);
      assert.equal(await page.locator('#spool-select').inputValue(), '7');
      assert.deepEqual(server.data.filaments['1'].snapshot, ASA, 'the alternate snapshot is untouched');
      await page.close();
      console.log('PASS: spool review is default-only and separate from stock');
    }

    // Blocking incompatibility disables Apply; a material change needs an explicit acknowledgement.
    {
      const { page, server } = await open(browser, {
        review: (s) => ok(spoolPreview(s.revision, { issues: [{ code: 'spool-material-mismatch', severity: 'error', resolution_required: true, message: 'Sunlu ASA is ASA, but the Project\'s Filament is PETG. Choose a matching Filament preset first.' }] })),
        adopt: () => conflict('should not be called') });
      await page.locator('#spool-select').focus();
      await page.waitForFunction(() => document.querySelector('#spool-select option[value="7"]'));
      await page.locator('#spool-select').selectOption('7');
      await page.locator('#dlg-review').waitFor();
      assert.match(await page.locator('#review-body').innerText(), /Choose a matching Filament preset first/);
      assert.equal(await page.locator('#btn-review-apply').isDisabled(), true);
      assert.equal(server.adopts.length, 0);
      await page.close();

      const second = await open(browser, {
        review: (s) => ok(presetPreview(s.revision, { material_changed: true, proposed: { name: 'Workshop PETG', material: ['ASA'], nozzle: '0.4', sha256: '2' },
          issues: [{ code: 'material-change', severity: 'warning', acknowledgement_required: true, message: 'Material changes from PETG to ASA' }] })),
        adopt: (s, body) => { assert.equal(body.accept_material_change, true); return conflict('stop here'); } });
      await second.page.locator('#btn-refresh-filament').click();
      await second.page.locator('#dlg-review').waitFor();
      assert.equal(await second.page.locator('#btn-review-apply').isDisabled(), true);
      await second.page.locator('[data-accept="material_change"]').check();
      assert.equal(await second.page.locator('#btn-review-apply').isDisabled(), false);
      await second.page.locator('#btn-review-apply').click();
      await second.page.waitForFunction(() => /stop here/.test(document.getElementById('review-body').innerText));
      assert.equal(second.server.adopts.length, 1);
      await second.page.close();
      console.log('PASS: blockers and material-change acknowledgement');
    }

    // A Model's own Filament has its own review, scoped to that Model.
    {
      const { page, server } = await open(browser, {
        review: (s, body) => {
          assert.deepEqual(body, { source: 'preset', target: { kind: 'model', model_id: 1 } });
          return ok({ ...presetPreview(s.revision), applies_to: 'model-filament', target: { kind: 'model', model_id: 1, preset: 'Workshop ASA', colour: 'black' },
            current: { name: 'Workshop ASA', material: ['ASA'], sha256: 'a'.repeat(64) }, proposed: { name: 'Workshop ASA', material: ['ASA'], nozzle: '0.4', sha256: 'c'.repeat(64) },
            changes: [{ key: 'nozzle_temperature', before: ['260'], after: ['300'] }], preserved: { project_default: { bundle_id: 'b1' }, alternates: [] },
            affected: { plates: [{ id: 2, name: 'Plate 2' }], outdated_slices: [] } });
        }, adopt: () => conflict('unused') });
      await page.locator('#object-list [data-object="1"]').click();
      await page.locator('#object-filament-refresh').click();
      await page.locator('#dlg-review').waitFor();
      const text = await page.locator('#dlg-review').innerText();
      assert.match(text, /Update Workshop ASA for Model 1/);
      assert.match(text, /This Model's Filament only, shared by its Copies/);
      assert.match(text, /nozzle_temperature\s+260\s+300/);
      assert.equal(server.adopts.length, 0);
      await page.close();
      console.log('PASS: Model Filament review is scoped to the Model');
    }
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
