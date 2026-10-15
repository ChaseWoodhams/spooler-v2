// Quick print past its first screen, and the Files, Print options, Add to queue, Spool picker, Add spool, Scanner,
// System health, Changelog and Outcome prompt dialogs, each rendered from realistic fixtures: they must show their data
// (no "undefined", no empty shell) at 1280 and 390, dark and light. Fixtures only: nothing reaches a printer, the Queue or Spoolman.
// Set UI_SHOTS_DIR to also save a screenshot of every stage (for design review; keep that folder outside the repo).
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const fx = require('./fixtures.cjs');
const shots = process.env.UI_SHOTS_DIR;
const BROKEN = /undefined|\bNaN\b|\[object|\bnull\b/;

const api = (qp, extra) => (url, request) => {
  const p = url.pathname;
  if (p === '/api/spoolman/api/v1/spool') return { json: fx.spools() };
  if (p === '/api/filament-meta') return { json: fx.filamentMeta() };
  if (p === '/api/failure-reasons') return { json: { failure_reasons: ['adhesion', 'spaghetti', 'nozzle_clog'], labels: { adhesion: 'Poor bed adhesion', spaghetti: 'Spaghetti', nozzle_clog: 'Nozzle clog' } } };
  if (p === '/api/jobs') return { json: [] };
  return qp(url, request) || (extra && extra(url, request));
};

/** Visible text of an open dialog, checked for placeholders that leaked and for an empty shell. */
async function rendered(page, locator, expected, label, minLength = 40) {
  const text = (await page.locator(locator).innerText()).trim();
  assert.ok(text.length >= minLength, `${label}: dialog is an empty shell (${JSON.stringify(text)})`);
  assert.doesNotMatch(text, BROKEN, `${label}: leaked placeholder in ${JSON.stringify(text)}`);
  for (const want of expected) assert.ok(text.includes(want), `${label}: missing ${JSON.stringify(want)} in ${JSON.stringify(text)}`);
  return text;
}
const shoot = (page, name, w, theme) => shots ? page.screenshot({ path: path.join(shots, `${name}-${w}-${theme}.png`) }) : null;
const close = async (page, id) => { await page.keyboard.press('Escape'); await page.waitForFunction(i => !document.querySelector(i)?.classList.contains('open'), id); };

async function review(browser, baseUrl, width, height, theme) {
  const label = `${width} ${theme}`;
  const qpState = {};
  const { page, context, calls } = await fx.openApp(browser, baseUrl, { width, height, theme, systemStatus: fx.systemStatus(), awaiting: [fx.awaitingOutcome()],
    api: api(fx.quickPrintApi(qpState)),
    ws: (req, send) => { if (req.action === 'list_files') send({ type: 'file_list', printer_id: req.printer_id, files: fx.files() }); } });
  await page.route(/^https?:\/\/(?!127\.0\.0\.1)/, route => route.abort());   // the barcode library comes from a CDN: unreachable here
  try {
    // Quick print: form, then slicing, then Slice & print (the review step), never a real printer
    await page.evaluate(() => { printers['fx-1'].connected = true; });
    await page.evaluate(() => QuickPrint.open({ projectId: 'fixture', printerId: 'fx-1' }));
    await page.waitForFunction(() => document.querySelector('[data-qp="go"]') && !document.querySelector('[data-qp="go"]').disabled);
    await rendered(page, '.qp-card', ['calibration-cube.stl', 'Preset', 'Spool', 'Plate side', 'Layer height', 'Slice and review'], `quick print form ${label}`, 150);
    await shoot(page, 'quick-print-form', width, theme);
    qpState.slicing = true;
    await page.getByRole('button', { name: 'Slice and review' }).click();
    await page.locator('[data-qp="stage"]').waitFor();
    await rendered(page, '.qp-card', ['Slicing'], `quick print slicing ${label}`, 30);
    await shoot(page, 'quick-print-slicing', width, theme);
    await page.keyboard.press('Escape');
    qpState.slicing = false;
    await page.evaluate(() => QuickPrint.open({ projectId: 'fixture', printerId: 'fx-1' }));
    await page.waitForFunction(() => document.querySelector('[data-qp="go"]') && !document.querySelector('[data-qp="go"]').disabled);
    await page.getByRole('button', { name: 'Slice and review' }).click();
    await page.getByRole('heading', { name: 'Job review', exact: true }).waitFor();
    await rendered(page, '.qp-card', ['calibration-cube.stl', 'Filament unknown', 'Add Job to Queue', 'I have reviewed the warnings'], `quick print review ${label}`, 150);
    await shoot(page, 'quick-print-review', width, theme);
    await page.keyboard.press('Escape');

    // Files, then Print options and the Queue editor from a listed file
    await page.evaluate(() => openFileBrowser('fx-1'));
    await page.locator('#files-list .file-item').first().waitFor();
    await rendered(page, '#modal-files', ['Files', 'bracket_v3_0.2mm_PLA_1h30m.gcode', 'benchy.gcode', 'MB'], `files ${label}`, 100);
    assert.equal(calls.ws.filter(r => r.action === 'start_print').length, 0);
    await shoot(page, 'files', width, theme);
    await close(page, '#modal-files');

    await page.evaluate(() => { _currentFilePrinterId = 'fx-1'; openPrintOpts('/local/bracket_v3_0.2mm_PLA_1h30m.gcode', 'bracket_v3_0.2mm_PLA_1h30m.gcode', { print_time: 5400, filament_g: 18, layers: 150 }); });
    await rendered(page, '#modal-print-opts', ['Start a print', 'bracket_v3_0.2mm_PLA_1h30m.gcode', 'Textured (Side A)', 'Time-lapse'], `print options ${label}`, 100);
    await shoot(page, 'print-options', width, theme);
    await close(page, '#modal-print-opts');

    await page.evaluate(() => openJobEditor({ mode: 'edit', job: { id: 7, name: 'Bracket x4', file_name: 'bracket_v3_0.2mm_PLA_1h30m.gcode', source: 'upload', printer_id: 'fx-1', state: 'queued', notes: 'Black PLA, 0.4 nozzle', print_opts: { smooth_plate: false, timelapse: true, leveling: true } } }));
    await rendered(page, '#modal-job', ['bracket_v3_0.2mm_PLA_1h30m.gcode', 'Name', 'Printer', 'Notes', 'Save'], `add to queue ${label}`, 100);
    assert.equal(await page.locator('#job-input-name').inputValue(), 'Bracket x4');
    await shoot(page, 'add-to-queue', width, theme);
    await close(page, '#modal-job');

    // Spool picker (needs the inventory), Add spool, Scanner
    await page.evaluate(() => fetchSpools());
    await page.waitForFunction(() => spools.length === 5);
    await page.evaluate(() => openSpoolPicker('fx-1'));
    await rendered(page, '#modal-spool-picker', ['Assign Spool', 'Polymaker', 'eSUN', 'Prusament'], `spool picker ${label}`, 100);
    await shoot(page, 'spool-picker', width, theme);
    await close(page, '#modal-spool-picker');

    await page.evaluate(() => document.getElementById('btn-add-spool').click());
    await page.waitForFunction(() => document.querySelectorAll('#spool-input-brand option').length > 2);
    await rendered(page, '#modal-add-spool', ['Add Spool', 'EAN', 'Brand', 'Material'], `add spool ${label}`, 100);
    assert.ok(await page.locator('#spool-input-material option', { hasText: 'PETG' }).count(), 'material catalogue is populated');
    await shoot(page, 'add-spool', width, theme);
    await close(page, '#modal-add-spool');

    await page.evaluate(() => openScanner());
    await page.locator('#scanner-error').waitFor({ state: 'visible' });
    await rendered(page, '#modal-scanner', ['Scan barcode', 'EAN'], `scanner ${label}`, 40);
    await shoot(page, 'scanner', width, theme);
    await close(page, '#modal-scanner');

    // System health and Changelog
    await page.evaluate(() => openSystemHealth());
    await page.locator('#system-health-body .health-row').first().waitFor();
    // the folded "Running fine" group holds the healthy rows; open it to read them
    await page.evaluate(() => { const d = document.querySelector('[data-fold="ok"]'); if (d) d.open = true; });
    await rendered(page, '#modal-system-health', ['System health', 'Dashboard connection', 'Spoolman', 'spooler-2026-10-14.zip'], `system health ${label}`, 150);
    await shoot(page, 'system-health', width, theme);
    await close(page, '#modal-system-health');

    await page.evaluate(() => openChangelog());
    await rendered(page, '#modal-changelog', ['Release Notes', 'v2.2.5'], `changelog ${label}`, 100);
    await shoot(page, 'changelog', width, theme);
    await close(page, '#modal-changelog');

    // Outcome prompt: the question, then the failed sheet
    await page.evaluate(() => OutcomePrompt.open('fx-1'));
    await page.locator('.outcome-prompt .modal-box').waitFor();
    await rendered(page, '.outcome-prompt', ['finished Bracket', 'completed', 'Printed OK'], `outcome prompt ${label}`, 60);
    await shoot(page, 'outcome-prompt', width, theme);
    await page.getByRole('button', { name: /^Failed/ }).click();
    await page.getByText('Spaghetti').first().waitFor();
    await rendered(page, '.outcome-prompt', ['Poor bed adhesion', 'Spaghetti'], `outcome failed sheet ${label}`, 60);
    await shoot(page, 'outcome-failed', width, theme);

    for (const call of calls.api) assert.doesNotMatch(call, /^(POST|PUT|PATCH|DELETE) \/api\/(jobs|printers|spoolman)/, `no write was sent: ${call}`);
  } finally { await context.close(); }
}

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch();
  try {
    for (const [w, h] of [[1280, 800], [390, 844]]) for (const theme of ['dark', 'light']) await review(browser, server.baseUrl, w, h, theme);
    console.log('PASS: Quick print through Slice & print and nine dialogs render their data at 1280/390, dark/light');
  } finally { await browser.close(); await server.close(); }
})().catch(err => { console.error(err); process.exitCode = 1; });
