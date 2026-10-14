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

// An existing project (id 60) with one model on plate 1. Adding a file on any step goes through the intake table.
const SIZES = { 'small.stl': [40, 30, 20], 'big.stl': [650, 400, 300], 'metres.stl': [0.2, 0.1, 0.05] };
const model = (id, file, state = 'ready') => ({ id, file, state, sha256: `sha${id}`, import: [1, 'z'], size: [10, 10, 10], triangles: 12, volume_cm3: 1, repairs: [] });
const object = (id, modelId, name) => ({ id, model: modelId, plate: 1, name, position: [0, 0, 0], rotation: [0, 0, 0], scale: [1, 1, 1] });

// A model upload and its split (the Model and Split steps exist only for a project made from one)
const split = { id: 1, source_name: 'existing.stl', state: 'ready', settings: { scale: 1, up: 'z', printer: { bed_x: 240, bed_y: 240, height: 250 }, planner: { mode: 'smart', balanced: false }, connectors: { kind: 'none' } },
  cuts: { cuts: [] }, analysis: { raw_size: [40, 30, 20], size: [40, 30, 20], volume_cm3: 1, source_triangles: 12, triangles: 12, repairs: [], mean_wall_mm: 2, fits_whole: true, estimated_parts: 1 } };

async function open(browser, step, { fromSplit = false } = {}) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const server = { calls: [], name: 'Chair', models: [model(1, 'existing.stl')], objects: [object(1, 1, 'existing')], outdated: false };
  const hasSplit = () => fromSplit || server.madeSplit;
  const project = () => ({ id: 60, name: server.name, revision: 1, split_id: hasSplit() ? 1 : null, split_ids: hasSplit() ? [1] : [], models: server.models, slices: {},
    server_time: Date.now() / 1000, data: { plates: [{ id: 1, name: 'Plate 1' }], objects: server.objects, bed: 'textured', profile: { bundle_id: null, process: {}, filament: {} } } });
  await page.route('http://spooler.test/**', route => {
    const file = path.join(root, new URL(route.request().url()).pathname);
    if (!file.startsWith(root) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404 });
    return route.fulfill({ body: fs.readFileSync(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.html') ? 'text/html' : 'application/octet-stream' });
  });
  page.on('pageerror', e => console.error('Page error:', e.message));
  page.on('console', e => { if (process.env.DEBUG_UI && e.type() === 'error') console.error(e.text()); });
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url()), method = request.method(), p = url.pathname, body = request.postData();
    if (method !== 'GET') server.calls.push({ method, path: p, params: Object.fromEntries(url.searchParams), body: p.includes('/models/upload') ? null : body });
    if (p === '/api/projects/60' && method === 'PATCH') { server.name = JSON.parse(body).name; return route.fulfill({ json: project() }); }
    if (p === '/api/projects/60') return route.fulfill({ json: project() });
    if (p === '/api/projects/60/models/upload') {
      const file = url.searchParams.get('filename'), id = server.models.length + 1;
      server.models.push(model(id, file));
      server.objects.push(object(id, id, file.replace(/\.stl$/, '')));
      server.outdated = true;
      return route.fulfill({ json: file === 'orca.3mf' ? { ...project(), import_notes: ['Some User Preset was left out'] } : project() });
    }
    if (p === '/api/projects/import-review') {   // a 3MF is staged and inspected first; this one has nothing to review
      server.staged = url.searchParams.get('filename');
      return route.fulfill({ json: { token: 'b'.repeat(32), kind: 'append', filename: server.staged, required: false, blocked: false, notes: ['Some User Preset was left out'],
        report: { version: 1, losses: [], blockers: [] }, binding: { token: 'b'.repeat(32), content_hash: 'h', report_version: 1, conversion: 'c', target: { project_id: 60, created_at: 1, revision: 1 } } } });
    }
    if (p === `/api/projects/import-review/${'b'.repeat(32)}/confirm`) {
      const id = server.models.length + 1;
      server.models.push(model(id, server.staged));
      server.objects.push(object(id, id, server.staged.replace(/\.3mf$/, '')));
      server.outdated = true;
      return route.fulfill({ json: { ...project(), import_notes: ['Some User Preset was left out'] } });
    }
    if (p === '/api/splits/1') return route.fulfill({ json: split });
    let made;
    if ((made = p.match(/^\/api\/projects\/60\/models\/(\d+)\/make-split$/))) {
      server.models = server.models.filter(x => x.id !== Number(made[1]));
      server.objects = server.objects.filter(x => x.model !== Number(made[1]));
      server.madeSplit = true;
      return route.fulfill({ json: project() });
    }
    if ((made = p.match(/^\/api\/projects\/60\/models\/(\d+)$/)) && method === 'DELETE') {
      server.models = server.models.filter(x => x.id !== Number(made[1]));
      server.objects = server.objects.filter(x => x.model !== Number(made[1]));
      return route.fulfill({ json: project() });
    }
    if (p === '/api/projects/60/intake-files') {
      const rows = server.models.map(m => ({ model: m.id, object: m.id, file: m.file, kind: 'model', state: 'ready', error: null, size: SIZES[m.file] || [10, 10, 10], scale: 1, up: 'z',
        status: m.file === 'big.stl' ? 'split' : m.file === 'metres.stl' ? 'check_units' : 'fits', fits: m.file !== 'big.stl', scale_fix: null, scale_fixes: [], reason: 'This size looks wrong', copies: 1 }));
      const count = status => rows.filter(r => r.status === status).length;
      return route.fulfill({ json: { rows, summary: { models: rows.length, objects: rows.length, split: count('split'), fits: count('fits'), check_units: 0, analysing: 0, failed: 0, can_create: true } } });
    }
    return route.fulfill({ json: p === '/api/splits' ? { splits: [] }
      : p.endsWith('/status') ? { plates: [], progress: { counts: {}, filament_g: {} } }
      : p.endsWith('/jobs') ? { jobs: [] } : p === '/api/printers' ? []
      : p === '/api/defaults' ? { defaults: { options: {} } } : p === '/api/slicer/spools' ? { spools: [] }
      : p === '/api/slicer/presets' ? { machines: [], processes: {}, filaments: {} } : {} });
  });
  await page.goto('http://spooler.test/slicer.html?project=60');
  if (process.env.DEBUG_UI) { await page.waitForTimeout(2500); console.error(await page.evaluate(() => [document.getElementById('dlg-projects').open, [...document.querySelectorAll('.toast')].map(t => t.textContent)])); }
  await page.locator('#project-name').waitFor();
  await page.waitForFunction(() => document.getElementById('project-name').value !== '');
  if (step) await page.locator(`[data-step="${step}"]`).click();
  return { page, server };
}

const dropOn = (page, selector, names) => page.evaluate(([selector, names]) => {
  const dt = new DataTransfer();
  for (const name of names) dt.items.add(new File(['solid x\nendsolid x\n'], name));
  document.querySelector(selector).dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
}, [selector, names]);

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    // The title is edited in place: Enter saves, Escape puts the saved name back, a blank name is not saved
    const { page, server } = await open(browser, 'slice');
    const title = page.locator('#project-name');
    await title.fill('Garden chair');
    await title.press('Enter');
    await page.waitForFunction(() => document.activeElement !== document.getElementById('project-name'));
    for (let i = 0; i < 50 && !server.calls.some(c => c.method === 'PATCH'); i++) await page.waitForTimeout(100);
    assert.deepEqual(JSON.parse(server.calls.find(c => c.method === 'PATCH').body), { name: 'Garden chair' });
    assert.equal(server.name, 'Garden chair', 'the new name is saved');
    await page.waitForFunction(() => document.title.startsWith('Garden chair'));
    await title.fill('Scratch');
    await title.press('Escape');
    assert.equal(await title.inputValue(), 'Garden chair', 'Escape puts the saved name back');
    await title.fill('   ');
    await title.press('Enter');
    assert.equal(await title.inputValue(), 'Garden chair', 'a blank name is not kept');
    assert.equal(server.calls.filter(c => c.method === 'PATCH').length, 1, 'neither Escape nor a blank name is saved');
    await page.reload();
    await page.waitForFunction(() => document.getElementById('project-name').value === 'Garden chair');
    console.log('PASS: Renaming in the header persists; Escape and a blank name leave it alone');

    // There is no separate Add models dialog
    assert.equal(await page.locator('#dlg-import, #import-scale, #import-up').count(), 0, 'the Units and Up axis dialog is gone');

    // Slice step: a drop on the viewport opens the intake table for just the new files, says what it does to Slice, and adds them
    await page.locator('[data-step="slice"]').click();
    await dropOn(page, '#viewport', ['small.stl', 'big.stl']);
    const dialog = page.locator('#dlg-add');
    await dialog.waitFor({ state: 'visible' });
    const table = dialog.locator('.intake');
    await table.locator('[data-intake-row]', { hasText: 'small.stl' }).waitFor({ timeout: 5000 });
    await table.getByText('Too big, will be split').waitFor({ timeout: 5000 });
    assert.equal(await table.locator('[data-intake-row]').count(), 2, 'only the new files are listed, not the ones already in the project');
    assert.match(await dialog.innerText(), /outdated/i, 'the panel says what adding does to the sliced plates');
    const uploads = server.calls.filter(c => c.path === '/api/projects/60/models/upload');
    assert.equal(uploads.length, 2);
    assert.deepEqual([uploads[0].params.scale, uploads[0].params.up, uploads[0].params.plate], ['1', 'z', '1'], 'same defaults as New project, onto the current plate');
    assert.equal(server.calls.some(c => c.path === '/api/projects' && c.method === 'POST'), false, 'no draft is made for an existing project');
    await dialog.getByRole('button', { name: 'Done', exact: true }).click();
    await page.waitForFunction(() => !document.getElementById('dlg-add').open);
    await page.locator('[data-step="plates"]').click();
    await page.locator('#object-list').getByText('small', { exact: false }).first().waitFor({ timeout: 5000 });
    assert.equal(server.calls.some(c => c.method === 'DELETE'), false, 'closing keeps the added files');
    console.log('PASS: A drop on the Slice step goes through the intake table, warns about outdated plates and joins the project');

    // Models too big for the bed become splits on Done; Done waits while a size needs checking
    {
      const s = await open(browser, 'plates');
      await dropOn(s.page, '#viewport', ['big.stl', 'metres.stl']);
      const box = s.page.locator('#dlg-add');
      await box.locator('[data-intake-row]', { hasText: 'metres.stl' }).getByText('Check units').waitFor({ timeout: 5000 });
      await box.locator('[data-intake-row]', { hasText: 'big.stl' }).getByText('Too big, will be split').waitFor({ timeout: 5000 });
      const done = box.getByRole('button', { name: 'Done', exact: true });
      assert.equal(await done.isDisabled(), true, 'Done waits while a model is marked Check units');
      assert.match(await box.locator('#add-reason').innerText(), /Check the size of 1 model marked Check units/);
      await s.page.keyboard.press('Escape');
      assert.equal(await box.evaluate(d => d.open), true, 'Escape cannot skip the check either');
      await box.locator('[data-intake-row]', { hasText: 'metres.stl' }).getByRole('button', { name: 'Remove metres.stl' }).click();
      await s.page.waitForFunction(() => !document.getElementById('btn-add-done').disabled, null, { timeout: 5000 });
      await done.click();
      await s.page.waitForFunction(() => !document.getElementById('dlg-add').open);
      for (let i = 0; i < 50 && !s.server.calls.some(c => /make-split$/.test(c.path)); i++) await s.page.waitForTimeout(100);
      assert.equal(s.server.calls.filter(c => /\/models\/\d+\/make-split$/.test(c.path)).length, 1, 'the model too big for the bed becomes a split');
      assert.equal(s.server.calls.some(c => /make-split$/.test(c.path) && /\/models\/3\//.test(c.path)), false, 'the model with a doubtful size is left alone');
      await s.page.locator('.toast', { hasText: 'too big for the bed became a split' }).waitFor({ timeout: 5000 });
      await s.page.waitForFunction(() => document.querySelector('.slicer-step.active')?.dataset.step === 'model', null, { timeout: 5000 });
      await s.page.close();
      console.log('PASS: Adding a model too big for the bed makes it a split on Done, and Done waits for unit checks');
    }

    // A drop on each step opens the table for those files, adds them and says what that step does to the work
    const drops = [['model', '#split-stage', /whole models on the Plates step/], ['split', '#split-stage', /whole models on the Plates step/],
      ['plates', '#viewport', /current plate.*outdated/], ['slice', '#viewport', /Slice outdated/]];
    for (const [step, stage, note] of drops) {
      const s = await open(browser, step, { fromSplit: step === 'model' || step === 'split' });
      await dropOn(s.page, stage, ['small.stl']);
      const box = s.page.locator('#dlg-add');
      await box.waitFor({ state: 'visible' });
      await box.locator('[data-intake-row]', { hasText: 'small.stl' }).waitFor({ timeout: 5000 });
      assert.match(await box.locator('#add-note').innerText(), note, `the note for the ${step} step`);
      assert.equal(s.server.calls.filter(c => c.path === '/api/projects/60/models/upload').length, 1);
      await box.getByRole('button', { name: 'Done', exact: true }).click();
      await s.page.waitForFunction(() => !document.getElementById('dlg-add').open);
      assert.ok(s.server.models.some(m => m.file === 'small.stl'), `the file joined the project from ${step}`);
      assert.equal(s.server.calls.some(c => c.method === 'DELETE'), false);
      await s.page.close();
    }
    console.log('PASS: A drop on Model, Split, Plates and Slice adds the files and shows that step\'s note');

    // An Orca project 3MF added to an open project says what the import left out, in the dialog and again once it is closed
    const orca = await open(browser, 'plates');
    await dropOn(orca.page, '#viewport', ['orca.3mf']);
    const orcaBox = orca.page.locator('#dlg-add');
    await orcaBox.locator('.intake-notes').getByText(/Opened with a note: Some User Preset was left out/).waitFor({ timeout: 5000 });
    await orcaBox.getByRole('button', { name: 'Done', exact: true }).click();
    await orca.page.locator('.toast', { hasText: 'Opened with a note: Some User Preset was left out' }).waitFor({ timeout: 5000 });
    await orca.page.close();
    console.log('PASS: Adding an Orca project 3MF shows its import notes');

    // Add files is in the header on every step
    for (const step of ['plates', 'slice']) {
      const s = await open(browser, step);
      assert.equal(await s.page.locator('#btn-add-files').isVisible(), true, `an Add files button is in the header on ${step}`);
      await s.page.close();
    }
    console.log('PASS: Add files is always in the header');
    await page.close();
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
