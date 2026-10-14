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

// The export plan dialog against deterministic API responses; no live server, Orca or printer.
const plate = (id, name, type, preset) => ({ id, name, objects: 1, material: { type, preset, colour: '' }, fingerprint: String(id).repeat(64) });
const faithful = (over = {}) => ({ version: 1, project_id: 80, revision: 3, fingerprint: 'f'.repeat(64), representation: 'single-3mf', faithful: true,
  plates: [plate(1, 'Front', 'PETG', 'Elegoo PETG @ECC'), plate(2, 'Back', 'PETG', 'Elegoo PETG @ECC')], settings: { machine: 40, process: 300, filament: 60 },
  object_overrides: [{ plate: 1, object_id: 1, name: 'Bracket', key: 'wall_loops', value: '6' }], differences: [], blockers: [],
  limitations: [{ code: 'bricklayers', message: 'BrickLayers post-processing changes the G-code after Orca slices it. An Orca project file cannot reproduce that.', plates: ['Back'] }],
  filename: 'Bracket set.3mf', content_type: 'model/3mf', label: 'Settings that will be written to the Orca project file (expected inputs, not measured output)', ...over });
const different = () => faithful({ faithful: false, filename: null, content_type: null, settings: null, object_overrides: [], limitations: [],
  plates: [plate(1, 'Front', 'PETG', 'Elegoo PETG @ECC'), plate(2, 'Back', 'ASA', 'Workshop ASA')],
  differences: [{ kind: 'filament', key: 'filament_type', plates: [{ id: 1, name: 'Front', value: ['PETG'] }, { id: 2, name: 'Back', value: ['ASA'] }] },
                { kind: 'process', key: 'wall_loops', plates: [{ id: 1, name: 'Front', value: '3' }, { id: 2, name: 'Back', value: '5' }] }] });

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1300, height: 900 } });
    const state = { plan: faithful(), requests: [], stale: false };
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
      if (p === '/api/projects/80/export.3mf') {
        state.requests.push(`download ${url.searchParams.get('revision')} ${url.searchParams.get('fingerprint').slice(0, 2)}`);
        if (state.stale) return route.fulfill({ status: 409, json: { error: 'The Project changed since this export was planned — plan the export again', issues: [{ code: 'export-plan-stale' }] } });
        return route.fulfill({ body: 'PK', contentType: 'model/3mf', headers: { 'Content-Disposition': 'attachment; filename="Bracket set.3mf"' } });
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

    // A compatible Project shows what is kept and offers the download, bound to the plan.
    await open();
    const dialog = page.locator('#dlg-export-plan');
    const text = await dialog.innerText();
    for (const expected of ['One Orca project file keeps', 'Bracket set.3mf', 'Front', 'PETG', 'Elegoo PETG @ECC', 'Bracket: wall_loops = 6', 'BrickLayers', 'not measured output']) assert.match(text, new RegExp(expected));
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.act), 'close', 'the safe choice has the focus');
    const [download] = await Promise.all([page.waitForEvent('download'), dialog.getByRole('button', { name: 'Download Orca 3MF' }).click()]);
    assert.equal(download.suggestedFilename(), 'Bracket set.3mf');
    assert.deepEqual(state.requests, ['plan', 'download 3 ff']);
    console.log('PASS: A compatible Project is exported from a plan bound to its revision and fingerprint');

    // A changed Project asks for a new plan instead of downloading.
    state.stale = true;
    state.plan = faithful({ revision: 4, fingerprint: 'e'.repeat(64) });
    state.requests.length = 0;
    await dialog.getByRole('button', { name: 'Download Orca 3MF' }).click();
    await dialog.getByText('The Project changed since this plan was made').waitFor();
    assert.deepEqual(state.requests, ['download 3 ff', 'plan']);
    state.stale = false;
    await dialog.getByRole('button', { name: 'Download Orca 3MF' }).click();
    for (let i = 0; i < 30 && state.requests.length < 3; i++) await page.waitForTimeout(100);
    assert.equal(state.requests[2], 'download 4 ee', 'the new plan is what downloads');
    await dialog.getByRole('button', { name: 'Close' }).click();
    console.log('PASS: A stale plan is refused and must be reviewed again');

    // Plates that differ in material or process settings have no one-file download and say why.
    state.plan = different();
    state.requests.length = 0;
    await open();
    const notFaithful = await dialog.innerText();
    for (const expected of ['cannot faithfully keep', 'Nothing has been exported', 'filament_type', 'Front: PETG', 'Back: ASA', 'wall_loops', 'will not copy a Plate setting onto Objects']) assert.match(notFaithful, new RegExp(expected));
    assert.equal(await dialog.getByRole('button', { name: /Download/ }).count(), 0, 'no download is offered');
    assert.deepEqual(state.requests, ['plan']);
    await dialog.getByRole('button', { name: 'Close' }).click();
    console.log('PASS: Differing Plates are explained and no file is offered');
  } finally {
    await browser.close();
  }
})().catch(err => { console.error(err); process.exit(1); });
