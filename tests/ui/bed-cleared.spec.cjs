// Bed cleared is offered only when a Print is waiting on it, says what starts next, and the result toast offers Fix outcome.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const publicDir = process.env.UI_PUBLIC_DIR || path.join(root, 'public');
const shots = process.env.UI_SHOTS;
const printer = { id: 'bc', name: 'Bed fixture', ip: '192.0.2.10', printer_type: 'cc1', connected: true, camera_url: null,
  status: { PrintInfo: { Status: 0 }, TempOfNozzle: 22, TempOfHotbed: 21, TempTargetNozzle: 0, TempTargetHotbed: 0 } };
const record = { id: 7, outcome: 'completed', file_name: 'bracket.gcode', printer_name: 'Bed fixture' };

async function run(browser, baseUrl, width, height) {
  const context = await browser.newContext({ viewport: { width, height }, serviceWorkers: 'block' });
  const page = await context.newPage();
  const fx = { waiting: false, posts: [], awaitingItem: null };
  await page.routeWebSocket(/.*/, socket => socket.onMessage(m => {
    if (JSON.parse(m).action === 'list_printers') socket.send(JSON.stringify({ type: 'printer_update', printer }));
  }));
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/dashboard') return route.fulfill({ json: { projects: [], needs_you: { prints: [], printers: fx.waiting
      ? [{ printer_id: 'bc', printer_name: 'Bed fixture', kind: 'bed_cleared', print: record, auto_start: true, next_job: { id: 3, file_name: 'lid.gcode' } }] : [] } } });
    if (url.pathname === '/api/outcome-prompts') return route.fulfill({ json: { awaiting: fx.awaitingItem ? [fx.awaitingItem] : [] } });
    if (url.pathname === '/api/failure-reasons') return route.fulfill({ json: { failure_reasons: ['spaghetti'], labels: { spaghetti: 'Spaghetti' } } });
    if (url.pathname === '/api/printers/bc/bed-cleared' && route.request().method() === 'POST') {
      fx.posts.push(route.request().postDataJSON());
      return route.fulfill({ json: { started: { file_name: 'lid.gcode' } } });
    }
    if (url.pathname === '/api/prints') return route.fulfill({ json: { prints: [], summary: {}, total: 0 } });
    return route.fulfill({ json: {} });
  });
  await page.goto(baseUrl);
  const card = page.locator('#card-bc');
  await card.waitFor();
  const button = card.getByRole('button', { name: 'Printed OK · Bed cleared' });
  await page.waitForTimeout(400);
  assert.equal(await button.count(), 0, 'an idle printer with nothing to clear shows no Bed cleared button');

  fx.waiting = true;
  await page.evaluate(() => loadDashboard());
  await button.waitFor();
  assert.match(await card.innerText(), /Next up: lid\.gcode starts on Bed fixture/i, 'the card says what confirming starts');
  if (shots) await page.screenshot({ path: path.join(shots, 'card-' + width + '.png') });

  // Outcome prompt: names the next Job, and does not pre-tick Bed cleared on the Failed sheet
  fx.awaitingItem = { print_id: 7, printer_id: 'bc', printer_name: 'Bed fixture', file_name: 'bracket.gcode', reported_outcome: 'completed',
    outcome_unconfirmed: true, duration_s: 600, quick: true, next_job: 'lid.gcode' };
  await button.click();
  const prompt = page.locator('.outcome-prompt');
  await prompt.waitFor();
  assert.match(await prompt.innerText(), /Next up: lid\.gcode starts on Bed fixture when you confirm/);
  if (shots) await page.screenshot({ path: path.join(shots, 'prompt-' + width + '.png') });
  await prompt.getByRole('button', { name: 'Failed…' }).click();
  assert.equal(await prompt.locator('[data-bed]').isChecked(), false, 'Bed cleared is not pre-ticked on the Failed sheet');
  await prompt.getByRole('button', { name: 'Back' }).click();

  // Confirming: the toast offers Fix outcome, which opens History for this printer
  fx.awaitingItem = { print_id: 7, printer_id: 'bc', printer_name: 'Bed fixture', file_name: 'bracket.gcode', reported_outcome: 'completed', outcome_unconfirmed: true, quick: true, next_job: 'lid.gcode' };
  await page.evaluate(() => OutcomePrompt.open('bc'));
  await prompt.getByRole('button', { name: 'Printed OK · Bed cleared' }).click();
  const fix = page.locator('#toast-area').getByRole('button', { name: 'Fix outcome' });
  await fix.waitFor();
  assert.equal(fx.posts.length, 1);
  if (shots) await page.screenshot({ path: path.join(shots, 'toast-' + width + '.png') });
  await fix.click();
  await page.locator('#panel-history.open, #panel-history:not([hidden])').first().waitFor();
  assert.equal(await page.locator('#history-printer').inputValue(), 'bc');
  await context.close();
  console.log('Bed cleared PASS ' + width);
}

(async () => {
  const server = http.createServer(async (request, response) => {
    try {
      const name = new URL(request.url, 'http://localhost').pathname;
      if (name === '/config.js') { response.setHeader('Content-Type', 'text/javascript'); return response.end('window.SPOOLER_WS_PORT = 1;'); }
      const file = path.join(publicDir, name === '/' ? 'index.html' : name);
      response.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.json') ? 'application/json' : 'text/html');
      response.end(await fs.readFile(file));
    } catch { response.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true });
  try {
    for (const [w, h] of [[1098, 507], [390, 844]]) await run(browser, 'http://127.0.0.1:' + server.address().port, w, h);
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exit(1); });
