// "Start a print" dialog (Files > Print): names the printer, summarises the job, checks Bed cleared, remembers options.
// Fixtures only: the WebSocket and /api/** are stubbed, nothing reaches a printer.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const publicDir = process.env.UI_PUBLIC_DIR || path.join(root, 'public');
const shots = process.env.UI_SHOTS_DIR;
const printer = { id: 'fx-1', name: 'Fixture CC1', ip: '192.0.2.10', printer_type: 'cc1', connected: true, camera_url: null,
  status: { PrintInfo: { Status: 0 }, TempOfNozzle: 22, TempOfHotbed: 21, TempTargetNozzle: 0, TempTargetHotbed: 0 } };

async function open(browser, baseUrl, width, height, bed) {
  const context = await browser.newContext({ viewport: { width, height }, serviceWorkers: 'block' });
  const page = await context.newPage();
  const fx = { commands: [], bedGets: 0, bedPosts: 0, bed };
  page.on('pageerror', e => console.error('Browser error:', e.message));
  await page.routeWebSocket(/.*/, socket => {
    socket.onMessage(message => {
      const req = JSON.parse(message);
      fx.commands.push(req);
      if (req.action === 'list_printers') socket.send(JSON.stringify({ type: 'printer_update', printer }));
    });
  });
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/printers/fx-1/bed-cleared') {
      if (route.request().method() === 'POST') { fx.bedPosts++; fx.bed = { valid: true }; return route.fulfill({ json: { ...fx.bed, started: null } }); }
      fx.bedGets++;
      if (fx.bed === 'error') return route.fulfill({ status: 503, json: { error: 'down' } });
      return route.fulfill({ json: fx.bed });
    }
    return route.fulfill({ json: {} });
  });
  await page.goto(baseUrl);
  await page.locator('#card-fx-1').waitFor();
  await page.evaluate(() => { _currentFilePrinterId = 'fx-1'; });
  return { page, context, fx };
}
const openDialog = (page, meta = '{}') => page.evaluate(m => openPrintOpts('/fx/a.gcode', 'a.gcode', JSON.parse(m)), meta);
const starts = fx => fx.commands.filter(c => c.action === 'start_print');

async function summary(browser, baseUrl, w, h) {
  const { page, context, fx } = await open(browser, baseUrl, w, h, { valid: false });
  await openDialog(page);
  const modal = page.locator('#modal-print-opts');
  await modal.locator('#print-opts-bed[data-state="blocked"]').waitFor();
  assert.equal(await modal.locator('h3').innerText(), 'Start a print');
  const text = await modal.locator('#print-opts-summary').innerText();
  assert.match(text, /Fixture CC1/); assert.match(text, /a\.gcode/); assert.match(text, /Textured/);
  assert.match(text, /Estimate unavailable/); assert.doesNotMatch(text, /--/);
  const send = page.locator('#btn-print-opts-send');
  assert.equal((await send.innerText()).trim(), 'Start print on Fixture CC1');
  // not confirmed clear: Start is blocked and Bed cleared is offered
  assert.equal(await send.isDisabled(), true);
  assert.match(await page.locator('#print-opts-bed').innerText(), /not confirmed clear/);
  if (shots) await page.screenshot({ path: path.join(shots, `blocked-${w}.png`) });
  await page.locator('#btn-print-opts-bed').click();
  await page.locator('#print-opts-bed[data-state="clear"]').waitFor();
  assert.equal(fx.bedPosts, 1);
  assert.equal(await send.isDisabled(), false);
  // summary follows the options
  await page.locator('#print-plate-smooth').click();
  await page.locator('#print-opt-timelapse').evaluate(el => el.click());
  const live = await modal.locator('#print-opts-summary').innerText();
  assert.match(live, /Smooth/); assert.match(live, /Time-lapse/);
  assert.equal(starts(fx).length, 0, 'nothing started before the button is pressed');
  if (shots) await page.screenshot({ path: path.join(shots, `clear-${w}.png`) });
  await send.click();
  assert.equal(starts(fx).length, 1);
  assert.deepEqual(starts(fx)[0].print_opts, { timelapse: true, leveling: false, smooth_plate: true });
  assert.equal(starts(fx)[0].printer_id, 'fx-1');
  // last-used options come back for this printer
  await openDialog(page, JSON.stringify({ print_time: 3600, filament_g: 12.5, layers: 40 }));
  await page.locator('#print-opts-bed[data-state="clear"]').waitFor();
  const again = await modal.locator('#print-opts-summary').innerText();
  assert.match(again, /Smooth/); assert.match(again, /Time-lapse/);
  assert.match(again, /1h/); assert.match(again, /12\.50g/); assert.match(again, /40 layers/);
  assert.equal(await page.locator('#print-opt-timelapse').isChecked(), true);
  await context.close();
  console.log(`Start-print summary PASS ${w}x${h}`);
}

async function bedStates(browser, baseUrl) {
  // a bed that is already confirmed clear needs no extra click; a failed check blocks and can be retried
  let r = await open(browser, baseUrl, 1098, 507, { valid: true });
  await openDialog(r.page);
  await r.page.locator('#print-opts-bed[data-state="clear"]').waitFor();
  assert.equal(await r.page.locator('#btn-print-opts-send').isDisabled(), false);
  await r.context.close();
  r = await open(browser, baseUrl, 1098, 507, 'error');
  await openDialog(r.page);
  await r.page.locator('#print-opts-bed[data-state="error"]').waitFor();
  assert.equal(await r.page.locator('#btn-print-opts-send').isDisabled(), true);
  r.fx.bed = { valid: true };
  await r.page.locator('#btn-print-opts-bed').click();
  await r.page.locator('#print-opts-bed[data-state="clear"]').waitFor();
  assert.equal(starts(r.fx).length, 0);
  await r.context.close();
  console.log('Start-print bed check states PASS');
}

(async () => {
  const server = http.createServer(async (request, response) => {
    try {
      const pathname = new URL(request.url, 'http://localhost').pathname;
      const filename = path.join(publicDir, pathname === '/' ? 'index.html' : pathname);
      const content = await fs.readFile(filename);
      response.setHeader('Content-Type', filename.endsWith('.js') ? 'text/javascript' : filename.endsWith('.css') ? 'text/css' : 'text/html');
      response.end(content);
    } catch { response.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch();
  try {
    const base = `http://127.0.0.1:${server.address().port}/`;
    await summary(browser, base, 1098, 507);
    await summary(browser, base, 390, 844);
    await bedStates(browser, base);
  } finally {
    await browser.close();
    server.close();
  }
})().catch(e => { console.error(e); process.exit(1); });
