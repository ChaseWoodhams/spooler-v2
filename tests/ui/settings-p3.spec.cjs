// Ticket 27: Settings P3 (PS-57 settings menu, PS-58 notification thresholds). Fixtures only: nothing is ever saved for real.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const publicDir = process.env.UI_PUBLIC_DIR || path.resolve(__dirname, '../../public');

const printer = { id: 'p1', name: 'Bench printer', ip: '192.0.2.10', printer_type: 'cc1', connected: true, camera_url: null,
  status: { PrintInfo: { Status: 0 }, TempOfNozzle: 22, TempOfHotbed: 21, TempTargetNozzle: 0, TempTargetHotbed: 0 } };

async function open(browser, baseUrl, width) {
  const context = await browser.newContext({ viewport: { width, height: 900 }, serviceWorkers: 'block' });
  const page = await context.newPage();
  page.on('pageerror', error => console.error('Browser error:', error.message));
  // headless Chromium reports 'denied'; 'default' is the state where Save stores the rules and the device is enabled separately
  await page.addInitScript(() => Object.defineProperty(window, 'Notification', { configurable: true, value: { permission: 'default', requestPermission: async () => 'default' } }));
  const fx = { page, context, saved: [] };
  await page.routeWebSocket(/.*/, socket => {
    socket.onMessage(raw => { if (JSON.parse(raw).action === 'list_printers') socket.send(JSON.stringify({ type: 'printer_update', printer })); });
  });
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/notification-settings' && route.request().method() === 'POST') {
      fx.saved.push(route.request().postDataJSON());
      return route.fulfill({ json: { ok: true } });
    }
    return route.fulfill({ json: {} });
  });
  await page.goto(baseUrl);
  await page.locator('#card-p1').waitFor();
  return fx;
}
const openSettings = async page => {
  if (page.viewportSize().width < 641) { await page.locator('#btn-mobile-more').click(); await page.locator('#btn-mobile-settings').click(); }
  else await page.locator('#btn-app-settings').click();
};

// PS-57 ---------------------------------------------------------------------------------------------------------
async function settingsMenu(fx, width) {
  const { page } = fx;
  await openSettings(page);
  const menu = page.locator('#settings-menu');
  // the whole Light mode row is the control: a label around the icon, the words and the switch
  const row = menu.locator('label.settings-toggle-row');
  assert.equal(await row.count(), 1, 'Light mode row is one <label>');
  assert.ok(await row.locator('#toggle-light-mode').count() === 1, 'the switch is inside the row label');
  const before = await page.evaluate(() => document.body.classList.contains('light-mode'));
  await row.getByText('Light mode', { exact: true }).click();   // the words, not the switch
  assert.equal(await page.evaluate(() => document.body.classList.contains('light-mode')), !before, 'clicking the words toggles the theme');
  await row.getByText('Light mode', { exact: true }).click();
  assert.equal(await page.evaluate(() => document.body.classList.contains('light-mode')), before, 'and back');
  assert.match(await row.innerText(), /Saved on this device/, 'says where the choice is kept');
  // rows and Close are at least 44 px tall
  const heights = await page.evaluate(() => [...document.querySelectorAll('#settings-menu .settings-menu-item, #btn-app-settings-cancel')]
    .filter(e => e.getClientRects().length).map(e => ({ id: e.id || e.className, h: Math.round(e.getBoundingClientRect().height) })));
  assert.ok(heights.length >= 6, `found the menu rows and Close (${heights.length})`);
  assert.deepEqual(heights.filter(x => x.h < 44), [], `every menu row and Close is >=44px at ${width}px`);
  console.log(`PS-57 settings menu PASS ${width}`);
}

// PS-58 ---------------------------------------------------------------------------------------------------------
const enable = (page, id) => page.evaluate(i => { const c = document.getElementById(i); c.checked = true; c.dispatchEvent(new Event('change', { bubbles: true })); }, id);

async function thresholds(fx, width) {
  const { page } = fx;
  await openSettings(page);
  await page.locator('#btn-settings-goto-notifications').click();
  await page.waitForFunction(() => document.getElementById('btn-notif-save').disabled === false);   // the form has loaded its saved values
  const fields = [['notif-nozzle-idle', 'notif-nozzle-idle-threshold', '50', /°C/, /1.{1,3}400/],
    ['notif-nozzle-printing', 'notif-nozzle-printing-threshold', '260', /°C/, /1.{1,3}400/],
    ['notif-spool-low', 'notif-spool-low-threshold', '100', /g/, /1.{1,3}5000/]];
  for (const [on] of fields) await enable(page, `${on}-on`);
  await enable(page, 'notif-layer-on');
  // units and range are helper text, tied to the field
  for (const [, id, , unit, range] of fields) {
    const described = await page.locator(`#${id}`).getAttribute('aria-describedby');
    assert.ok(described, `${id} has aria-describedby`);
    const hint = await page.locator(`#${described.split(' ')[0]}`).innerText();
    assert.match(hint, unit, `${id} hint names the unit`);
    assert.match(hint, range, `${id} hint gives the range`);
  }
  // out of range: an inline error beside the field, aria-invalid, nothing saved
  await page.locator('#notif-nozzle-idle-threshold').fill('999');
  await page.locator('#btn-notif-save').click();
  const bad = page.locator('#notif-nozzle-idle-threshold');
  assert.equal(await bad.getAttribute('aria-invalid'), 'true', 'out-of-range field is aria-invalid');
  const errId = (await bad.getAttribute('aria-describedby')).split(' ').find(i => i.endsWith('-error'));
  assert.ok(errId, 'the field points at its error text');
  assert.match(await page.locator(`#${errId}`).innerText(), /between 1 and 400/i);
  assert.equal(fx.saved.length, 0, 'an out-of-range value is not saved');
  // empty: keeps the default instead of saving 0
  await bad.fill('');
  await page.locator('#notif-spool-low-threshold').fill('');
  await page.locator('#btn-notif-save').click();
  await page.waitForFunction(() => document.getElementById('btn-notif-save').textContent.trim() !== 'Saving…');
  assert.equal(fx.saved.length, 1, 'an empty field is accepted');
  assert.equal(fx.saved[0].nozzle_idle.threshold, 50, 'empty nozzle-idle keeps its default 50, not 0');
  assert.equal(fx.saved[0].spool_low.threshold, 100, 'empty spool-low keeps its default 100, not 0');
  assert.equal(await page.locator('#notif-nozzle-idle-threshold').inputValue(), '50', 'the field shows the default it kept');
  assert.equal(await page.locator('#notif-nozzle-idle-threshold').getAttribute('aria-invalid'), null, 'error cleared once corrected');
  console.log(`PS-58 notification thresholds PASS ${width}`);
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
  const baseUrl = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch();
  try {
    for (const width of [1098, 390]) {
      for (const scenario of [settingsMenu, thresholds]) {
        const fx = await open(browser, baseUrl, width);
        try { await scenario(fx, width); } finally { await fx.context.close(); }
      }
    }
  } finally {
    await browser.close();
    server.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
