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

const TOKEN = 'a'.repeat(32);
const loss = (code, feature, scope, id, name, effect) => ({ code, feature, scope, id, name, detail: null, effect });
const LOSSES = [
  loss('paint-supports', 'Painted supports', 'model', '1', 'Alpha', 'The painted support areas are not kept.'),
  loss('negative-volume', 'Negative volume', 'model', '2', 'Beta', 'The hole is not cut: the Model imports as a solid without it.'),
  loss('mirrored-transform', 'Mirrored placement', 'object', '2', 'Beta', 'The Object is placed unmirrored.'),
  loss('plate-metadata', 'Plate settings', 'plate', '1', 'Front', "The Plate's own bed type, print sequence and similar settings are not kept."),
];
const binding = { token: TOKEN, content_hash: 'h'.repeat(64), report_version: 1, conversion: 'c'.repeat(64), target: null };
const review = (over = {}) => ({ token: TOKEN, kind: 'project', filename: 'Bench set.3mf', expires_at: Date.now() / 1000 + 1800,
  required: true, blocked: false, notes: ['The project used “Some User Preset”'], binding,
  report: { version: 1, losses: LOSSES, blockers: [], objects: 2, plates: 2 }, ...over });
const created = { id: 9, name: 'Bench set', revision: 2, created_at: 1700000000.5, split_id: null, models: [], slices: {}, server_time: Date.now() / 1000,
  data: { plates: [{ id: 1, name: 'Front' }], objects: [], bed: 'textured', profile: { bundle_id: null, process: {}, filament: {} } },
  import_notes: [], import_record: { id: TOKEN, kind: 'project', losses: LOSSES } };
const blockedReview = review({ blocked: true, required: false, report: { version: 1, losses: [], objects: 1, plates: 1, blockers: [
  { code: 'unmatched-material', scope: 'project', name: 'Generic PETG', message: 'The project’s material “Generic PETG” (PETG) has no matching preset. Spooler would use “Elegoo PLA @ECC” (PLA) instead. Choose a compatible material before importing.',
    requested: { filament: 'Generic PETG', material: 'PETG', nozzle: '0.4' }, proposed: { filament: 'Elegoo PLA @ECC', material: 'PLA', nozzle: '0.4' } }] } });

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    const calls = [];
    let next = review();
    await page.route('http://spooler.test/**', route => {
      const file = path.join(root, new URL(route.request().url()).pathname);
      if (!file.startsWith(root) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404 });
      return route.fulfill({ body: fs.readFileSync(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.html') ? 'text/html' : 'application/octet-stream' });
    });
    page.on('pageerror', e => console.error('Page error:', e.message));
    await page.route('**/api/**', route => {
      const url = new URL(route.request().url()), method = route.request().method();
      if (method !== 'GET') calls.push({ method, path: url.pathname, params: Object.fromEntries(url.searchParams), body: route.request().postData() });
      const data = url.pathname === '/api/projects/import-review' ? next
        : url.pathname === `/api/projects/import-review/${TOKEN}/confirm` ? created
        : url.pathname === `/api/projects/import-review/${TOKEN}` ? { ok: true }
        : url.pathname === '/api/projects' && method === 'GET' ? { projects: [] }
        : url.pathname === '/api/projects' && method === 'POST' ? { ...created, id: 11, data: { ...created.data } }
        : url.pathname === '/api/projects/11/models/upload' ? created
        : url.pathname === '/api/projects/11/intake-files' ? { rows: [], summary: { models: 0, objects: 0, analysing: 0, can_create: false } }
        : url.pathname === '/api/splits' ? { splits: [] }
        : url.pathname === '/api/projects/9' ? created
        : url.pathname === '/api/projects/9/imports' ? { imports: [{ id: TOKEN, filename: 'Bench set.3mf', accepted_at: 1700000000, losses: LOSSES }] }
        : url.pathname.endsWith('/status') ? { plates: [], progress: { counts: {}, filament_g: {} } }
        : url.pathname.endsWith('/jobs') ? { jobs: [] } : url.pathname === '/api/printers' ? []
        : url.pathname === '/api/defaults' ? { defaults: { options: {} } } : url.pathname === '/api/slicer/spools' ? { spools: [] }
        : url.pathname === '/api/slicer/presets' ? { machines: [], processes: {}, filaments: {} } : {};
      return route.fulfill({ json: data });
    });
    const drop = names => page.evaluate(names => {
      const dt = new DataTransfer();
      for (const name of names) dt.items.add(new File([name.endsWith('.3mf') ? 'PK' : 'solid x\nendsolid x\n'], name));
      document.getElementById('dlg-projects').dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
    }, names);
    const review_calls = () => calls.filter(c => c.path.startsWith('/api/projects/import-review'));

    const projects = page.locator('#dlg-projects');
    const openNew = async () => {   // with no project open the Projects page opens by itself; New project then shows the intake
      await page.goto('http://spooler.test/slicer.html?new=1');
      await projects.waitFor({ state: 'visible' });
      await projects.getByRole('button', { name: 'Close', exact: true }).click();
      await page.locator('#btn-pd-new').click();
      await projects.waitFor({ state: 'visible' });
    };
    await openNew();

    // A lone 3MF is reviewed before any project is created; Cancel changes nothing.
    await drop(['Bench set.3mf']);
    const dialog = page.locator('#dlg-import-review');
    await dialog.waitFor({ state: 'visible' });
    assert.equal(await dialog.getByRole('heading', { level: 3 }).innerText(), 'Review import of Bench set.3mf');
    const text = await dialog.innerText();
    for (const expected of ['Model “Alpha”', 'Painted supports', 'Model “Beta”', 'Negative volume', 'Object “Beta”', 'Mirrored placement',
      'Plate “Front”', 'Plate settings', 'Nothing in your Project has changed', '1 note']) assert.match(text, new RegExp(expected));
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.act), 'cancel', 'Cancel has the focus');
    assert.deepEqual(review_calls().map(c => c.method), ['POST'], 'staging only: no confirmation and no project yet');
    assert.equal(review_calls()[0].params.filename, 'Bench set.3mf');
    assert.equal(review_calls()[0].params.target, undefined, 'a lone 3MF would start a new project');
    assert.equal(calls.some(c => c.path === '/api/projects'), false, 'no draft or project was created for the review');
    await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
    await dialog.waitFor({ state: 'detached' });
    await page.getByText('Import cancelled').waitFor();
    assert.deepEqual(review_calls().map(c => c.method), ['POST', 'DELETE'], 'Cancel throws the staging away');
    assert.equal(review_calls().some(c => c.path.endsWith('/confirm')), false);
    console.log('PASS: A 3MF is reviewed by Model, Object and Plate before anything is created; Cancel deletes the staging');

    // Continue with losses commits what was reviewed, bound by the token and fingerprints.
    calls.length = 0;
    await drop(['Bench set.3mf']);
    await dialog.waitFor({ state: 'visible' });
    await dialog.getByRole('button', { name: 'Continue with losses' }).click();
    for (let i = 0; i < 50 && !calls.some(c => c.path.endsWith('/confirm')); i++) await page.waitForTimeout(100);
    const confirm = calls.find(c => c.path.endsWith('/confirm'));
    assert.ok(confirm, 'Continue with losses confirms the review');
    assert.deepEqual(JSON.parse(confirm.body), { ...binding, acknowledged: true });
    for (let i = 0; i < 50 && !(await page.title()).includes('Bench set'); i++) await page.waitForTimeout(100);
    console.log('PASS: Continue with losses sends the reviewed binding with the acknowledgement');

    // The accepted losses and the original archive stay available from the converted Project.
    await page.getByLabel('More project actions').click();
    await page.getByRole('button', { name: 'Imports', exact: true }).click();
    const records = page.locator('#dlg-import-records');
    await records.waitFor({ state: 'visible' });
    const recordText = await records.innerText();
    assert.match(recordText, /Bench set\.3mf/);
    assert.match(recordText, /with these losses accepted/);
    for (const expected of ['Model “Alpha”', 'Painted supports', 'Plate “Front”']) assert.match(recordText, new RegExp(expected));
    assert.equal(await records.getByRole('link', { name: 'Download original' }).getAttribute('href'), `/api/projects/9/imports/${TOKEN}/source`);
    await records.getByRole('button', { name: 'Close' }).click();
    console.log('PASS: Accepted losses and the original archive can be revisited from the Project');

    // An unresolved material substitution has no Continue button.
    calls.length = 0;
    next = blockedReview;
    await openNew();
    await drop(['Generic PETG.3mf']);
    await dialog.waitFor({ state: 'visible' });
    const blockedText = await dialog.innerText();
    assert.match(blockedText, /Resolve before importing/);
    assert.match(blockedText, /The file asks for\s+Generic PETG \(PETG\) 0.4 mm nozzle/);
    assert.match(blockedText, /Spooler would use\s+Elegoo PLA @ECC \(PLA\) 0.4 mm nozzle/);
    assert.equal(await dialog.getByRole('button', { name: 'Continue with losses' }).count(), 0, 'a generic Continue cannot approve a substitution');
    await dialog.getByRole('button', { name: 'Cancel import' }).click();
    await dialog.waitFor({ state: 'detached' });
    assert.deepEqual(review_calls().map(c => c.method), ['POST', 'DELETE']);
    console.log('PASS: A blocked review explains the proposed material and offers only Cancel import');

    // A file with nothing to disclose is confirmed without a dialog.
    calls.length = 0;
    next = review({ required: false, report: { version: 1, losses: [], blockers: [], objects: 1, plates: 1 } });
    await openNew();
    await drop(['Clean.3mf']);
    for (let i = 0; i < 50 && !calls.some(c => c.path.endsWith('/confirm')); i++) await page.waitForTimeout(100);
    assert.ok(calls.some(c => c.path.endsWith('/confirm')));
    assert.equal(await dialog.count(), 0, 'nothing to review, nothing to ask');
    console.log('PASS: A clean 3MF is confirmed without a loss dialog');

    // Several files go through the draft: the 3MF is staged against the draft with its token, and Cancel leaves the draft alone.
    calls.length = 0;
    next = review();
    await openNew();
    await drop(['bracket.stl', 'Bench set.3mf']);
    await dialog.waitFor({ state: 'visible' });
    const staged = review_calls().find(c => c.method === 'POST');
    assert.equal(staged.params.target, '11');
    assert.equal(staged.params.draft_token, '1700000000.5', 'the draft token travels with the review');
    await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
    await dialog.waitFor({ state: 'detached' });
    await page.getByText('Import of Bench set.3mf cancelled').first().waitFor();
    assert.equal(review_calls().some(c => c.path.endsWith('/confirm')), false);
    console.log('PASS: Draft imports stage the 3MF with the draft token and Cancel leaves the draft unchanged');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
