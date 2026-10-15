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

// The explicit lossy export choice against deterministic API responses; no live server, Orca or printer.
const material = (type, preset) => ({ type, preset, colour: '' });
const plate = (id, name, m) => ({ id, name, objects: 1, material: m, fingerprint: String(id).repeat(64) });
const PETG = material('PETG', 'Elegoo PETG @ECC'), ASA = material('ASA', 'Workshop ASA');
const lossy = (over = {}) => ({
  available: true, reason: null, representation: 'lossy-3mf', report_version: 1, report_fingerprint: 'a'.repeat(64),
  filename: 'Bracket set (lossy).3mf', content_type: 'model/3mf', basis: { plate_id: 1, plate_name: 'Front', material: PETG },
  plates: [{ id: 1, name: 'Front', objects: ['Bracket'], material: PETG, written_as: PETG, material_changed: false },
           { id: 2, name: 'Back', objects: ['Clip'], material: ASA, written_as: PETG, material_changed: true }],
  differences: [
    { kind: 'filament', category: 'material', key: 'filament_type', written: ['PETG'], plates: [{ id: 1, name: 'Front', value: ['PETG'], kept: true }, { id: 2, name: 'Back', value: ['ASA'], kept: false }] },
    { kind: 'process', category: 'plate-settings', key: 'wall_loops', written: '3', plates: [{ id: 1, name: 'Front', value: '3', kept: true }, { id: 2, name: 'Back', value: '5', kept: false }] },
    { kind: 'process', category: 'bed', key: 'curr_bed_type', written: 'Textured PEI Plate', plates: [{ id: 1, name: 'Front', value: 'Textured PEI Plate', kept: true }, { id: 2, name: 'Back', value: 'Smooth PEI Plate', kept: false }] }],
  object_overrides: [{ plate: 1, object_id: 1, name: 'Bracket', key: 'sparse_infill_density', value: '40%' }],
  inherited: [{ kind: 'filament', key: 'workshop_only_setting', value: ['7'], message: 'no pinned definition', plates: ['Back'] }],
  features: [{ record_id: 'r1', filename: 'Clip.3mf', losses: [{ code: 'paint-supports', feature: 'Painted supports', scope: 'object', name: 'Clip', effect: 'Support painting is not kept' }] }],
  bricklayers: { plates: ['Back'], message: 'BrickLayers changes the G-code after Orca slices it, and Orca configuration and geometry cannot reproduce that transformation. The Plate\'s actual Slice G-code, with BrickLayers applied, stays a separate download.' },
  faithful_alternatives: [{ representation: 'single-3mf', available: false, why: 'Plates differ in material or settings that one Orca file cannot hold' }], ...over });
const plan = (over = {}) => ({ version: 1, project_id: 80, revision: 3, fingerprint: 'f'.repeat(64), representation: 'single-3mf', faithful: false,
  plates: [plate(1, 'Front', PETG), plate(2, 'Back', ASA)], settings: null, object_overrides: [], blockers: [], limitations: [],
  differences: [{ kind: 'filament', key: 'filament_type', plates: [{ id: 1, name: 'Front', value: ['PETG'] }, { id: 2, name: 'Back', value: ['ASA'] }] }],
  filename: null, content_type: null, label: 'Settings that will be written to the Orca project file (expected inputs, not measured output)', lossy: lossy(), ...over });

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1300, height: 1000 } });
    const state = { plan: plan(), requests: [], posted: [], stale: false };
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
      if (p === '/api/projects/80/export.3mf') { state.requests.push('faithful-download'); return route.fulfill({ status: 409, json: { error: 'review', issues: [{ code: 'export-review-required' }] } }); }
      if (p === '/api/projects/80/export-lossy') {
        const body = JSON.parse(route.request().postData());
        state.posted.push(body);
        if (state.stale) return route.fulfill({ status: 409, json: { error: 'The Project changed since you reviewed these losses', issues: [{ code: 'export-acknowledgement-stale' }] } });
        return route.fulfill({ body: 'PK', contentType: 'model/3mf', headers: { 'Content-Disposition': 'attachment; filename="Bracket set (lossy).3mf"' } });
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
    await open();
    const dialog = page.locator('#dlg-export-plan');

    // The alternative sits beside the plan; the plan's own reasons and the absence of a faithful download stay visible.
    let text = await dialog.innerText();
    for (const expected of ['cannot faithfully keep', 'filament_type', 'Convenient alternative: one lossy file']) assert.match(text, new RegExp(expected));
    assert.equal(await dialog.getByRole('button', { name: /Download/ }).count(), 0, 'nothing downloads from the plan itself');
    console.log('PASS: The lossy alternative is offered beside the plan without a download');

    // The review lists the actual differences per scope and explains BrickLayers; it exports nothing yet.
    await dialog.getByRole('button', { name: /Review the lossy alternative/ }).click();
    text = await dialog.innerText();
    for (const expected of ['This file will not keep', 'Front', 'PETG · Elegoo PETG @ECC', 'ASA · Workshop ASA', 'material changed', 'Material differences', 'filament_type',
      'Plate settings differences', 'wall_loops', 'Back: 5 \\(lost\\)', 'Bed differences', 'curr_bed_type', 'Smooth PEI Plate', 'Bracket: sparse_infill_density = 40%',
      'workshop_only_setting', 'Clip: Painted supports', 'cannot reproduce that transformation', 'separate download', 'Faithful alternatives', 'not available', 'sliced G-code of each Plate']) {
      assert.match(text, new RegExp(expected), expected);
    }
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.act), 'lossy-cancel', 'the safe choice has the focus');
    const button = dialog.getByRole('button', { name: /Download Bracket set \(lossy\)\.3mf/ });
    assert.equal(await button.isDisabled(), true, 'no download before the acknowledgement');
    assert.deepEqual(state.posted, []);
    console.log('PASS: The review lists per-scope differences, BrickLayers limits and faithful alternatives');

    // Cancel exports nothing and returns to the plan.
    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await dialog.getByText('cannot faithfully keep').waitFor();
    assert.deepEqual(state.posted, []);
    assert.equal(state.requests.includes('faithful-download'), false);
    console.log('PASS: Cancel leaves without exporting');

    // The acknowledgement binds the plan and the report and downloads a truthfully named 3MF.
    await dialog.getByRole('button', { name: /Review the lossy alternative/ }).click();
    await dialog.getByRole('checkbox').check();
    assert.equal(await button.isDisabled(), false);
    const [download] = await Promise.all([page.waitForEvent('download'), button.click()]);
    assert.equal(download.suggestedFilename(), 'Bracket set (lossy).3mf');
    assert.deepEqual(state.posted, [{ representation: 'lossy-3mf', revision: 3, fingerprint: 'f'.repeat(64), report_version: 1, report_fingerprint: 'a'.repeat(64), acknowledged: true }]);
    assert.equal(state.requests.includes('faithful-download'), false, 'the faithful route is never used for the lossy file');
    console.log('PASS: An acknowledged download is bound to revision, fingerprint, report and representation');

    // A revision conflict discards the acknowledgement and asks for a new review.
    state.stale = true;
    state.plan = plan({ revision: 4, fingerprint: 'e'.repeat(64), lossy: lossy({ report_fingerprint: 'b'.repeat(64) }) });
    state.requests.length = 0;
    await button.click();
    await dialog.getByText('The Project changed since you reviewed these losses').waitFor();
    assert.deepEqual(state.requests, ['plan'], 'the plan is fetched again');
    await dialog.getByText('Convenient alternative: one lossy file').waitFor();
    assert.equal(await dialog.getByRole('button', { name: /Download/ }).count(), 0, 'back at the plan, the acknowledgement is gone');
    state.stale = false;
    state.posted.length = 0;
    await dialog.getByRole('button', { name: /Review the lossy alternative/ }).click();
    assert.equal(await dialog.getByRole('checkbox').isChecked(), false, 'a new review needs a new acknowledgement');
    await dialog.getByRole('checkbox').check();
    await Promise.all([page.waitForEvent('download'), dialog.getByRole('button', { name: /Download Bracket set/ }).click()]);
    assert.equal(state.posted[0].revision, 4);
    assert.equal(state.posted[0].report_fingerprint, 'b'.repeat(64));
    console.log('PASS: A stale acknowledgement is refused and must be given again');

    // A Project that is blocked or faithful offers no lossy choice.
    await dialog.getByRole('button', { name: 'Close' }).click().catch(async () => { await dialog.getByRole('button', { name: 'Cancel' }).click(); await dialog.getByRole('button', { name: 'Close' }).click(); });
    state.plan = plan({ blockers: [{ code: 'model-not-ready', message: 'Still importing: Clip' }], lossy: lossy({ available: false, reason: 'blocked', report_fingerprint: null, filename: null }) });
    await open();
    text = await dialog.innerText();
    assert.match(text, /Still importing: Clip/);
    assert.doesNotMatch(text, /Convenient alternative/);
    console.log('PASS: A blocked Project offers no lossy choice');
  } finally {
    await browser.close();
  }
})().catch(err => { console.error(err); process.exit(1); });
