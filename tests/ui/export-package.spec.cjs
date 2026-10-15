const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
let playwright;
try { playwright = require(process.env.PLAYWRIGHT_MODULE || 'playwright'); }
catch (error) {
  playwright = require(path.resolve(__dirname, '../../../../spooler-v2/node_modules/playwright'));
}
const { chromium } = playwright;
const root = path.resolve(__dirname, '../../public');

// The per-Plate package choice in the export dialog, against deterministic API responses; no live server, Orca or printer.
const material = (type, preset) => ({ type, preset, colour: '' });
const plates = [{ id: 1, name: 'Front', objects: 1, material: material('PETG', 'Elegoo PETG @ECC'), fingerprint: '1'.repeat(64) },
                { id: 2, name: 'Back', objects: 1, material: material('ASA', 'Workshop ASA'), fingerprint: '2'.repeat(64) }];
const plan = (over = {}) => ({ version: 1, project_id: 80, revision: 3, fingerprint: 'f'.repeat(64), representation: 'single-3mf', faithful: false, plates,
  settings: null, object_overrides: [], blockers: [], limitations: [], filename: null, content_type: null,
  label: 'Settings that will be written to the Orca project file (expected inputs, not measured output)',
  differences: [{ kind: 'filament', key: 'filament_type', plates: [{ id: 1, name: 'Front', value: ['PETG'] }, { id: 2, name: 'Back', value: ['ASA'] }] }],
  package: { available: true, default: true, representation: 'plate-3mf-package', filename: 'Bracket set - Plate files.zip', content_type: 'application/zip',
    note: 'A set of independent Orca project files, one per Plate, in an ordinary ZIP. It is not a Spooler backup and not an Orca-native format.',
    files: plates.map(p => ({ plate_id: p.id, name: p.name, filename: `Plate ${p.id} - ${p.name}.3mf`, material: p.material, fingerprint: p.fingerprint })) }, ...over });

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1300, height: 900 } });
    const state = { plan: plan(), requests: [], stale: false };
    const project = () => ({ id: 80, name: 'Bracket set', revision: state.plan.revision, split_id: null, split_ids: [], models: [], slices: {}, server_time: Date.now() / 1000,
      data: { plates: [{ id: 1, name: 'Front' }, { id: 2, name: 'Back' }], objects: [], bed: 'textured', profile: { bundle_id: null, process: {}, filament: {} } } });
    await page.route('http://spooler.test/**', route => {
      const file = path.join(root, new URL(route.request().url()).pathname);
      if (!file.startsWith(root) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404 });
      return route.fulfill({ body: fs.readFileSync(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.html') ? 'text/html' : 'application/octet-stream' });
    });
    page.on('pageerror', e => console.error('Page error:', e.message));
    await page.route('**/api/**', route => {
      const url = new URL(route.request().url()), p = url.pathname;
      if (p === '/api/projects/80') return route.fulfill({ json: project() });
      if (p === '/api/projects/80/export-plan') { state.requests.push('plan'); return route.fulfill({ json: state.plan }); }
      if (p === '/api/projects/80/export.3mf') { state.requests.push('single'); return route.fulfill({ status: 500, json: { error: 'unexpected single-file download' } }); }
      if (p === '/api/projects/80/export-package.zip') {
        state.requests.push(`package ${url.searchParams.get('revision')} ${url.searchParams.get('fingerprint').slice(0, 2)}`);
        if (state.stale) return route.fulfill({ status: 409, json: { error: 'The Project changed since this export was planned — plan the export again', issues: [{ code: 'export-plan-stale' }] } });
        return route.fulfill({ body: 'PK', contentType: 'application/zip', headers: { 'Content-Disposition': 'attachment; filename="Bracket set - Plate files.zip"' } });
      }
      return route.fulfill({ json: p === '/api/splits' ? { splits: [] } : p.endsWith('/jobs') ? { jobs: [] } : p === '/api/printers' ? []
        : p === '/api/defaults' ? { defaults: { options: {} } } : p === '/api/slicer/spools' ? { spools: [] }
        : p === '/api/slicer/presets' ? { machines: [], processes: {}, filaments: {} } : p.endsWith('/status') ? { plates: [], progress: { counts: {}, filament_g: {} } } : {} });
    });
    await page.goto('http://spooler.test/slicer.html?project=80');
    await page.waitForFunction(() => document.getElementById('project-name').value !== '');
    const open = async () => {
      await page.getByLabel('More project actions').click();
      await page.getByRole('button', { name: 'Export 3MF', exact: true }).click();
      await page.locator('#dlg-export-plan').waitFor({ state: 'visible' });
    };

    // Plates that differ: the package is the offered faithful alternative, named as a ZIP, and no single-file download exists.
    await open();
    const dialog = page.locator('#dlg-export-plan');
    const text = await dialog.innerText();
    for (const expected of ['cannot faithfully keep', 'Export one Orca file per Plate', 'Plate 1 - Front.3mf', 'Plate 2 - Back.3mf', 'ASA', 'Workshop ASA', 'not a Spooler backup', 'Bracket set - Plate files.zip']) assert.match(text, new RegExp(expected));
    assert.equal(await dialog.getByRole('button', { name: 'Download Orca 3MF' }).count(), 0, 'no single file is offered');
    const [zip] = await Promise.all([page.waitForEvent('download'), dialog.getByRole('button', { name: 'Download Plate files (ZIP)' }).click()]);
    assert.equal(zip.suggestedFilename(), 'Bracket set - Plate files.zip');
    assert.deepEqual(state.requests, ['plan', 'package 3 ff']);
    console.log('PASS: Differing Plates offer a revision-bound ZIP of Plate files, never a 3MF-named download');

    // A changed Project asks for a new plan, then downloads what was re-planned.
    state.stale = true;
    state.plan = plan({ revision: 4, fingerprint: 'e'.repeat(64) });
    state.requests.length = 0;
    await dialog.getByRole('button', { name: 'Download Plate files (ZIP)' }).click();
    await dialog.getByText('The Project changed since this plan was made').waitFor();
    assert.deepEqual(state.requests, ['package 3 ff', 'plan']);
    state.stale = false;
    await dialog.getByRole('button', { name: 'Download Plate files (ZIP)' }).click();
    for (let i = 0; i < 30 && state.requests.length < 3; i++) await page.waitForTimeout(100);
    assert.equal(state.requests[2], 'package 4 ee');
    await dialog.getByRole('button', { name: 'Close' }).click();
    console.log('PASS: A stale package plan is refused and must be reviewed again');

    // Blockers keep the package away.
    state.plan = plan({ blockers: [{ code: 'model-not-ready', message: 'Still importing: Bracket' }], package: { ...plan().package, available: false, files: [] } });
    await open();
    assert.equal(await dialog.getByRole('button', { name: /Download/ }).count(), 0);
    assert.match(await dialog.innerText(), /Still importing: Bracket/);
    await dialog.getByRole('button', { name: 'Close' }).click();
    console.log('PASS: A blocked export offers neither file');

    // A faithful Project keeps the single file as the main action and the package as a secondary choice.
    state.plan = plan({ faithful: true, differences: [], filename: 'Bracket set.3mf', content_type: 'model/3mf', settings: { machine: 1, process: 1, filament: 1 },
      package: { ...plan().package, default: false } });
    await open();
    assert.equal(await dialog.getByRole('button', { name: 'Download Orca 3MF' }).count(), 1);
    assert.match(await dialog.innerText(), /Or export one Orca file per Plate/);
    await dialog.getByRole('button', { name: 'Close' }).click();
    console.log('PASS: A faithful Project still leads with the single file');
  } finally {
    await browser.close();
  }
})().catch(err => { console.error(err); process.exit(1); });
