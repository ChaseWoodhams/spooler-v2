// Shared UI-suite fixtures (not a spec: run.cjs only runs *.spec.cjs). Deterministic data and an app opener
// with the WebSocket and /api/** stubbed, so nothing reaches a real printer, Project, Queue or backup.
//
//   const fx = require('./fixtures.cjs');
//   const server = await fx.startServer();                       // { baseUrl, close() }
//   const { page, context, calls } = await fx.openApp(browser, server.baseUrl, {
//     width: 390, height: 844, theme: 'light',                    // theme: 'dark' (default) | 'light'
//     printers: fx.printers(6),                                   // default: fx.printers(6)
//     awaiting: [fx.awaitingOutcome()],                           // GET /api/outcome-prompts
//     prints: fx.historyRecords(),                                // GET /api/prints
//     preflight: fx.preflightReport(),                            // GET /api/jobs/:id/preflight
//     ws: (req, send) => send({ type: 'file_list', ... }),         // optional replies to other WebSocket actions
//     systemStatus: fx.systemStatus(),                            // pushed as the system_status message
//     api: (url, request) => ({ json: ... }) | undefined,         // optional extra routes, tried first
//   });
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const publicDir = process.env.UI_PUBLIC_DIR || path.join(root, 'public');

const LONG_NAMES = [
  'Workshop Centauri Carbon (left bench, firmware 1.1.40)',
  'Spare Elegoo Centauri Carbon 2 in the garage by the filament dry box',
  'Prototype farm printer number three',
  'Kids room printer',
  'Offline printer with a very long descriptive name that keeps going and going',
  'Printer 6',
  'Printer 7', 'Printer 8',
];

/** n printers (default 6): index 4 is offline, several have long names. Plain objects as the app holds them in `printers`. */
function printers(n = 6) {
  return Array.from({ length: n }, (_, i) => ({
    id: `fx-${i + 1}`, name: LONG_NAMES[i] || `Fixture printer ${i + 1}`, ip: `192.0.2.${10 + i}`,
    printer_type: 'cc1', connected: i !== 4, camera_url: null,
    status: i === 4 ? {} : { PrintInfo: { Status: 0 }, TempOfNozzle: 22, TempOfHotbed: 21, TempTargetNozzle: 0, TempTargetHotbed: 0 },
  }));
}

/** A finished Print waiting for the operator to record its Outcome (GET /api/outcome-prompts entry; printer fx-1 by default). */
function awaitingOutcome(over = {}) {
  return { printer_id: 'fx-1', printer_name: LONG_NAMES[0], print_id: 101, file_name: 'bracket.gcode',
    project_name: 'Bracket', plate_name: 'Plate 1', reported_outcome: 'completed', outcome_unconfirmed: true,
    duration_s: 5400, estimate_s: 5100, snapshot: null, ...over };
}

/** A GET /api/dashboard body: Needs-you items (waiting printers, finished prints) and one active Project. */
function dashboard({ printers = [], prints = [], projects = [{ id: 7, name: 'Bracket set', percent: 40, printed_ok: 2, total: 5 }] } = {}) {
  return { needs_you: { printers, prints }, projects };
}

/** A Needs-you printer item: a finished Print waiting on Bed cleared, with the next Job that confirming releases. */
function bedClearedItem(over = {}) {
  return { printer_id: 'fx-2', printer_name: LONG_NAMES[1], kind: 'bed_cleared', print: { id: 5, file_name: 'lid.gcode', project_name: 'Lid', outcome: 'completed' },
    auto_start: true, next_job: { id: 3, file_name: 'lid.gcode' }, ...over };
}

/** An openApp `api` option that serves the given GET /api/dashboard body and leaves every other route alone. */
function dashboardApi(body) { return url => url.pathname === '/api/dashboard' ? { json: body } : undefined; }

/** A queued Job as the Review job modal takes it (openReview(job)). */
function job(over = {}) {
  return { id: 'job-1', name: 'Fixture job', file_name: 'fixture.gcode', source: 'upload', printer_id: null, state: 'queued', print_opts: {}, ...over };
}

/** A preflight report with warnings (start allowed after acknowledgement). Pass { status: 'fail', can_start: false } to block. */
function preflightReport(over = {}) {
  return { status: 'warn', can_start: true, needs_acknowledgement: true, printer_id: 'fx-1', preview: null, checks: [
    { id: 'filament', level: 'warn', title: 'Verify material', detail: 'Filament type is unknown' },
    { id: 'bed', level: 'warn', title: 'Bed size', detail: 'The part is within 2 mm of the plate edge' },
    { id: 'file', level: 'ok', title: 'File readable', detail: 'fixture.gcode' },
  ], ...over };
}

/** Forty items { id, name } with a unique, stable name each, for list/scroll/density checks. */
function items(n = 40) {
  return Array.from({ length: n }, (_, i) => ({ id: `item-${i + 1}`, name: `Item ${String(i + 1).padStart(2, '0')} - part with a descriptive name` }));
}

/** History records covering every outcome; plain text, so a replacement character can only come from the app. */
function historyRecords() {
  return ['completed', 'cancelled', 'failed', 'unknown'].map((outcome, i) => ({
    id: i + 1, file_name: `part-${i + 1}.gcode`, printer_name: LONG_NAMES[i], printer_id: `fx-${i + 1}`,
    started_at: 1700000000 + i * 86400, ended_at: 1700003600 + i * 86400, outcome, outcome_unconfirmed: i === 3,
    duration_s: 3600, filament_g: 12, filament_mm: 4000, cost: 0.5, estimate_s: 3500, estimate_g: 11, notes: '' }));
}

/** A CC1 printer's file list as the file_list message carries it: a folder, then files of mixed size and name length. */
function files() {
  return [
    { name: 'archive', path: '/local/archive', size: 0, is_dir: true },
    { name: 'bracket_v3_0.2mm_PLA_1h30m.gcode', path: '/local/bracket_v3_0.2mm_PLA_1h30m.gcode', size: 4823104, is_dir: false, print_time: 5400, layers: 150, filament_mm: 5200, filament_g: null },
    { name: 'a-very-long-descriptive-file-name-for-the-cable-comb-organiser-with-lid_0.16mm_PETG_6h12m.gcode', path: '/local/a-very-long-descriptive-file-name-for-the-cable-comb-organiser-with-lid_0.16mm_PETG_6h12m.gcode', size: 31457280, is_dir: false, print_time: 22320, layers: 480, filament_mm: 18400, filament_g: null },
    { name: 'benchy.gcode', path: '/local/benchy.gcode', size: 912384, is_dir: false, print_time: null, layers: null, filament_mm: null, filament_g: null },
  ];
}

/** Spoolman inventory (GET /api/spoolman/api/v1/spool): one assigned to printer fx-1 by name, one half full, one low, one empty, one without a weight. */
function spools() {
  const spool = (id, vendor, material, name, hex, remaining, extra = {}) => ({ id, remaining_weight: remaining, initial_weight: 1000, location: null,
    filament: { name, material, color_hex: hex, vendor: { name: vendor } }, ...extra });
  return [
    spool(1, 'Polymaker', 'PLA', 'Jet Black', '1a1a1a', 742, { location: LONG_NAMES[0] }),
    spool(2, 'eSUN', 'PETG', 'Signal Orange', 'ff7a00', 640),
    spool(3, 'Prusament', 'PLA', 'Galaxy Silver with a long colour name', 'c0c0c8', 60),
    spool(4, 'Generic', 'TPU', 'Clear', 'dddddd', 0),
    spool(5, 'Bambu Lab', 'ASA', 'Grey', '888888', null),
  ];
}

/** The brand and material catalogue the Add Spool dialog loads (GET /api/filament-meta). */
function filamentMeta() {
  return { brands: ['Bambu Lab', 'eSUN', 'Polymaker', 'Prusament'], materials: [{ name: 'PLA', density: 1.24 }, { name: 'PETG', density: 1.27 }, { name: 'ASA', density: 1.07 }, { name: 'TPU', density: 1.21 }] };
}

/** The system_status snapshot the health dialog renders (health.snapshot()): Spoolman fine, backups current, one printer offline. */
function systemStatus(over = {}) {
  const now = Date.now() / 1000;
  return { generated_at: now,
    spooler: { started_at: now - 93784, uptime_s: 93784, pid: 4242, loop_lag_ms: 12, browser_clients: 2 },
    spoolman: { ok: true, version: '0.22.1', latency_ms: 18, down_since: null, error: null, url: 'http://127.0.0.1:7912', pending_deductions: 0 },
    printers: printers(6).map(p => ({ id: p.id, name: p.name, printer_type: 'cc1', connected: p.connected, connected_since: p.connected ? now - 7200 : null,
      disconnected_since: p.connected ? null : now - 600, last_message_age_s: p.connected ? 1.2 : null, reconnect_attempts: p.connected ? 0 : 4,
      next_retry_in_s: p.connected ? null : 30, last_error: p.connected ? null : 'Connection timed out' })),
    backups: { dir: 'C:\\Spooler\\backups', count: 6, keep: 14, interval_hours: 24, latest: { name: 'spooler-2026-10-14.zip', size: 18874368, created_at: now - 3600 * 20 },
      overdue: false, running: false, last_attempt_at: now - 3600 * 20, last_error: null, spoolman_db: 'C:\\Spoolman\\spoolman.db' },
    supervisor: { stale: false, started_at: now - 93000, updated_at: now - 4, services: [{ name: 'spooler', restarts: 0 }, { name: 'spoolman', restarts: 1 }] },
    ...over };
}

/** Quick print routes for a model Project 'fixture' (printer fx-1); an `api` handler for openApp. With state.slicing it stays on the
 *  slicing screen; otherwise the slice finishes at once and the review (Slice & print step) shows. Nothing is queued or sent. */
function quickPrintApi(state = {}) {
  const project = () => ({ id: 'fixture', name: 'Calibration cube', data: {}, slices: { 1: state.slicing ? { state: 'slicing', stage: 'slicing' } : { state: 'sliced', estimate_s: 5400, estimate_g: 18 } } });
  return (url, request) => {
    const p = url.pathname;
    if (p === '/api/projects/fixture') return { json: project() };
    if (p.endsWith('/quick-options')) return { json: { preset: 'PLA', bed: 'textured', spool_id: 1, options: { layer_height: 0.2, infill: 15, walls: 2, supports: 'off', brim: 'off' } } };
    if (p === '/api/slicer') return { json: { bundles: [{ id: 'PLA', label: 'PLA standard' }, { id: 'PETG', label: 'PETG standard' }] } };
    if (p === '/api/slicer/spools') return { json: { spools: spools().slice(0, 3).map(s => ({ id: s.id, name: `${s.filament.vendor.name} ${s.filament.material} ${s.filament.name}`, remaining_weight: s.remaining_weight })) } };
    if (p.endsWith('/intake')) return { json: { state: 'ready', kind: 'model', file: 'calibration-cube.stl', size: [20, 20, 20], volume_cm3: 8, fits: true, scale_ok: true, can_print: true, repaired: false, estimate: { seconds: 5400, grams: 18 } } };
    if (p.endsWith('/print') && request.method() === 'POST') {
      if (state.slicing) return { json: { ...project(), review: { state: 'slicing' } } };
      const warnings = [{ level: 'warn', title: 'Filament unknown', detail: 'Verify the loaded filament matches the preset.' }];
      return { json: { ...project(), review: { can_start: true, warnings, checks: warnings } } };
    }
  };
}

/** Static server for the real frontend assets; close() when done. */
async function startServer() {
  const server = http.createServer(async (req, res) => {
    try {
      const rel = new URL(req.url, 'http://localhost').pathname;
      const file = path.join(publicDir, rel === '/' ? 'index.html' : rel);
      const content = await fs.readFile(file);
      res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript; charset=utf-8' : file.endsWith('.css') ? 'text/css; charset=utf-8' : file.endsWith('.json') ? 'application/json' : 'text/html; charset=utf-8');
      res.end(content);
    } catch { res.writeHead(404).end(); }
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  return { baseUrl: `http://127.0.0.1:${server.address().port}`, close: () => new Promise(r => server.close(r)) };
}

/** Open the app in a fresh context with all network stubbed; resolves once every printer card is rendered. */
async function openApp(browser, baseUrl, opts = {}) {
  const { width = 1280, height = 800, theme = 'dark', hasTouch = false } = opts;
  const list = opts.printers || printers(6);
  const calls = { api: [], ws: [] };
  const context = await browser.newContext({ viewport: { width, height }, hasTouch, serviceWorkers: 'block' });
  await context.addInitScript(t => { try { localStorage.setItem('theme', t); } catch (_) { /* ignore */ } }, theme);
  const page = await context.newPage();
  page.on('pageerror', e => console.error('Browser error:', e.message));
  await page.routeWebSocket(/.*/, ws => ws.onMessage(message => {
    const req = JSON.parse(message);
    calls.ws.push(req);
    if (req.action === 'list_printers') {
      for (const printer of list) ws.send(JSON.stringify({ type: 'printer_update', printer }));
      if (opts.systemStatus) ws.send(JSON.stringify({ type: 'system_status', status: opts.systemStatus }));
    }
    if (opts.ws) opts.ws(req, reply => ws.send(JSON.stringify(reply)));   // answer other actions, e.g. list_files -> file_list
  }));
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    calls.api.push(`${request.method()} ${url.pathname}`);
    const extra = opts.api && opts.api(url, request);
    if (extra) return route.fulfill(extra);
    if (url.pathname === '/api/outcome-prompts') return route.fulfill({ json: { awaiting: opts.awaiting || [] } });
    if (url.pathname === '/api/printers') return route.fulfill({ json: list });
    if (url.pathname === '/api/prints') { const prints = opts.prints || []; return route.fulfill({ json: { prints, total: prints.length, summary: { prints: prints.length, duration_s: 0, filament_g: 0, cost: 0 } } }); }
    if (/\/api\/jobs\/[^/]+\/preflight$/.test(url.pathname)) return route.fulfill({ json: opts.preflight || preflightReport() });
    if (url.pathname === '/api/failure-reasons') return route.fulfill({ json: { cancel_reasons: ['adhesion'], labels: { adhesion: 'Poor adhesion' } } });
    return route.fulfill({ json: {} });
  });
  await page.goto(baseUrl);
  if (list.length) await page.locator(`#card-${list[list.length - 1].id}`).waitFor();
  return { page, context, calls };
}

module.exports = { startServer, openApp, files, spools, filamentMeta, systemStatus, quickPrintApi, printers, awaitingOutcome, dashboard, bedClearedItem, dashboardApi, job, preflightReport, items, historyRecords, publicDir };
