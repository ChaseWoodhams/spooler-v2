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
    if (req.action === 'list_printers') for (const printer of list) ws.send(JSON.stringify({ type: 'printer_update', printer }));
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

module.exports = { startServer, openApp, printers, awaitingOutcome, job, preflightReport, items, historyRecords, publicDir };
