// Ticket 21: Settings, notifications and System health. Fixtures only: no printer, setting or password is ever changed.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const publicDir = process.env.UI_PUBLIC_DIR || path.resolve(__dirname, '../../public');

const printer = { id: 'p1', name: 'Bench printer', ip: '192.0.2.10', printer_type: 'cc1', connected: true, camera_url: null,
  status: { PrintInfo: { Status: 0 }, TempOfNozzle: 22, TempOfHotbed: 21, TempTargetNozzle: 0, TempTargetHotbed: 0 } };
const klipper = { id: 'p2', name: 'Klipper bench', ip: '192.0.2.11', printer_type: 'moonraker', connected: false, has_access_code: true, camera_url: null, status: {} };

async function open(browser, baseUrl, width, setup = {}) {
  const context = await browser.newContext({ viewport: { width, height: 900 }, serviceWorkers: 'block' });
  const page = await context.newPage();
  page.on('pageerror', error => console.error('Browser error:', error.message));
  if (setup.init) await page.addInitScript(setup.init, setup.arg);
  const fx = { page, context, sockets: [], commands: [], onCommand: () => {}, posts: [] };
  await page.routeWebSocket(/.*/, socket => {
    fx.sockets.push(socket);
    socket.onMessage(raw => {
      const request = JSON.parse(raw);
      fx.commands.push(request);
      if (request.action === 'list_printers') for (const p of [printer, klipper]) socket.send(JSON.stringify({ type: 'printer_update', printer: p }));
      else fx.onCommand(request, socket);
    });
  });
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/change-password') {
      fx.posts.push({ path: url.pathname, body: route.request().postDataJSON() });
      return route.fulfill({ status: 500, json: { error: 'Fixture refuses password changes' } });
    }
    if (url.pathname === '/api/notification-settings') return route.fulfill({ json: {} });
    return route.fulfill({ json: {} });
  });
  await page.goto(baseUrl);
  await page.locator('#card-p1').waitFor();
  fx.broadcast = message => { for (const s of fx.sockets) s.send(JSON.stringify(message)); };
  return fx;
}
const openSettings = async page => {
  if (page.viewportSize().width < 641) { await page.locator('#btn-mobile-more').click(); await page.locator('#btn-mobile-settings').click(); }
  else await page.locator('#btn-app-settings').click();
};
const text = locator => locator.innerText();
const boxFits = async (page, selector, label) => {   // finding 04 (source-only): the modal box must fit a 390 px phone with margins
  const r = await page.locator(selector).evaluate(el => { const b = el.getBoundingClientRect(); return { left: b.left, right: window.innerWidth - b.right }; });
  assert.ok(r.left >= 8 && r.right >= 8, `${label}: box has side margins (${JSON.stringify(r)})`);
};
const noOverflow = async (page, label) => assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), true, `${label}: no horizontal overflow`);

// PS-22 + PS-21 -------------------------------------------------------------------------------------------------
async function printersAndEditForm(fx) {
  const { page } = fx;
  await openSettings(page);
  await page.locator('#btn-settings-goto-printers').click();
  const list = page.locator('#settings-printer-list');
  // PS-22: connected / offline as a dot and a word, and a friendly type name instead of "CC1"
  const row = name => list.locator('.settings-printer-row', { hasText: name });
  assert.match(await text(row('Bench printer')), /Connected/);
  assert.match(await text(row('Bench printer')), /Centauri Carbon/);
  assert.doesNotMatch(await text(row('Bench printer')), /\bCC1\b/);
  assert.match(await text(row('Klipper bench')), /Offline/);
  assert.match(await text(row('Klipper bench')), /Klipper/);
  assert.doesNotMatch(await text(row('Klipper bench')), /MOONRAKER/);
  // the list follows a live connection change, and a focused Edit button keeps focus
  await row('Bench printer').getByRole('button', { name: /^Edit/ }).focus();
  fx.broadcast({ type: 'printer_update', printer: { ...printer, connected: false } });
  await page.waitForFunction(() => /Offline/.test(document.querySelector('#settings-printer-list .settings-printer-row').innerText));
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute('aria-label')), 'Edit Bench printer');
  fx.broadcast({ type: 'printer_update', printer });

  // PS-21: a new CC1 has no experimental note, no fake key, an IP hint, and a numeric keypad
  await page.locator('#btn-settings-add-printer').click();
  assert.equal(await page.locator('#settings-edit-ip').getAttribute('inputmode'), 'decimal');
  assert.match(await text(page.locator('#settings-edit-ip-hint')), /192\.168\.1\.100/);
  assert.equal(await page.locator('#settings-edit-experimental').isVisible(), false, 'no experimental note for CC1');
  assert.equal(await page.locator('#settings-edit-type-note').isVisible(), false);
  await page.locator('#settings-edit-type').selectOption('cc2');
  assert.equal(await page.locator('#settings-edit-access-code').getAttribute('placeholder'), '', 'no made-up key as a placeholder');
  assert.equal(await page.locator('#settings-edit-experimental').isVisible(), false, 'CC2 is not experimental');
  for (const type of ['prusa', 'moonraker']) {
    await page.locator('#settings-edit-type').selectOption(type);
    assert.equal(await page.locator('#settings-edit-experimental').isVisible(), true, `${type} is experimental`);
  }
  await page.locator('#btn-settings-back-printer-edit').click();

  // An existing printer: the type is read-only and says why
  await row('Klipper bench').getByRole('button', { name: /^Edit/ }).click();
  assert.equal(await page.locator('#settings-edit-type-note').isVisible(), true);
  assert.match(await text(page.locator('#settings-edit-type-note')), /can't be changed after adding/);
  assert.equal(await page.locator('#settings-edit-experimental').isVisible(), true);
  assert.equal(await page.locator('#settings-edit-access-code').getAttribute('placeholder'), 'Leave blank to keep current');

  // Save waits for Spooler: pending, then the form stays with an inline error when Spooler refuses
  fx.onCommand = (request, socket) => {
    if (request.action === 'update_printer') socket.send(JSON.stringify({ type: 'error', message: 'A printer with that IP is already added' }));
  };
  await page.locator('#settings-edit-ip').fill('192.0.2.10');
  const save = page.locator('#btn-settings-printer-save');
  await save.click();
  await page.locator('#settings-edit-status[data-state="error"]').waitFor();
  assert.match(await text(page.locator('#settings-edit-status')), /already added/);
  assert.equal(await page.locator('#settings-printer-edit').isVisible(), true, 'the form stays on a refusal');
  assert.equal(await save.innerText(), 'Save');
  assert.equal(await save.getAttribute('aria-disabled'), 'false');

  // Success: Spooler answers with the printer carrying the new values; only then does the form leave
  fx.onCommand = () => {};   // hold the answer back to see the pending state
  await page.locator('#settings-edit-ip').fill('192.0.2.77');
  await page.locator('#settings-edit-name').fill('Klipper renamed');
  await save.click();
  assert.equal(await save.innerText(), 'Saving…');
  assert.equal(await save.getAttribute('aria-disabled'), 'true');
  assert.equal(await save.evaluate(el => document.activeElement === el), true, 'focus stays on the pressed button');
  assert.match(await text(page.locator('#settings-edit-status')), /Saving/);
  const sent = fx.commands.filter(c => c.action === 'update_printer').at(-1);
  assert.deepEqual([sent.printer_id, sent.ip, sent.name], ['p2', '192.0.2.77', 'Klipper renamed']);
  await save.click({ force: true });   // a second press while pending sends nothing (force: Playwright treats aria-disabled as not clickable)
  assert.equal(fx.commands.filter(c => c.action === 'update_printer').length, 2);
  fx.broadcast({ type: 'printer_update', printer: { ...klipper } });   // a routine status report with the OLD values is not an answer
  await page.waitForTimeout(150);
  assert.equal(await page.locator('#settings-printer-edit').isVisible(), true);
  fx.broadcast({ type: 'printer_update', printer: { ...klipper, ip: '192.0.2.77', name: 'Klipper renamed' } });
  await page.locator('#settings-printers').waitFor({ state: 'visible' });
  assert.match(await text(page.locator('#settings-printer-list')), /Klipper renamed/);

  // No answer at all: after 8 s the form says so and lets the person retry
  await page.clock.install();
  await row('Klipper renamed').getByRole('button', { name: /^Edit/ }).click();
  await page.locator('#settings-edit-name').fill('Never answered');
  await save.click();
  assert.equal(await save.innerText(), 'Saving…');
  await page.clock.fastForward(8100);
  await page.locator('#settings-edit-status[data-state="error"]').waitFor();
  assert.match(await text(page.locator('#settings-edit-status')), /did not confirm.*try again/s);
  assert.equal(await save.innerText(), 'Save');

  // Remove waits for printer_removed the same way
  fx.onCommand = (request, socket) => { if (request.action === 'remove_printer') socket.send(JSON.stringify({ type: 'printer_removed', printer_id: request.printer_id })); };
  await page.locator('#btn-settings-printer-remove').click();
  await page.getByRole('dialog', { name: /^Remove / }).getByRole('button', { name: 'Remove printer' }).click();
  await page.locator('#settings-printers').waitFor({ state: 'visible' });
  assert.doesNotMatch(await text(page.locator('#settings-printer-list')), /Klipper/);
  assert.equal(fx.commands.filter(c => c.action === 'remove_printer').length, 1);
  console.log(`PS-21/22 printers list and Edit Printer PASS ${page.viewportSize().width}`);
}

// PS-23 ------------------------------------------------------------------------------------------------------
const notificationInit = permission => {
  let permission_ = permission;
  window.__permissionRequests = 0;
  Object.defineProperty(window, 'Notification', { configurable: true, value: {
    get permission() { return permission_; },
    requestPermission: async () => { window.__permissionRequests++; permission_ = 'denied'; return 'denied'; } } });
};
async function notificationState(fx) {
  const { page } = fx;
  await openSettings(page);
  await page.locator('#btn-settings-goto-notifications').click();
  const perm = page.locator('#notif-perm-chip'), sub = page.locator('#notif-sub-chip');
  await page.waitForFunction(() => !/checking/.test(document.getElementById('notif-perm-chip').textContent));
  assert.match(await text(perm), /Browser permission: Not asked yet/);
  assert.match(await text(sub), /This device: not subscribed/);
  const enable = page.getByRole('button', { name: 'Enable on this device' });
  assert.equal(await enable.isVisible(), true);
  // Saving a rule never opens the permission prompt
  await page.locator('#notif-finished-on').check();
  await page.locator('#btn-notif-save').click();
  await page.waitForFunction(() => /not enabled yet/.test(document.getElementById('settings-feedback').textContent));
  assert.equal(await page.evaluate(() => window.__permissionRequests), 0, 'Save did not prompt');
  assert.equal(await page.locator('#settings-notifications').isVisible(), true);
  const banner = await page.locator('#settings-feedback').evaluate(el => { const b = el.getBoundingClientRect(); return b.top >= 0 && b.bottom <= window.innerHeight; });
  assert.equal(banner, true, 'the result of Save is on screen');
  await boxFits(page, '#modal-app-settings .modal-box', 'settings');
  // The explicit button does
  await enable.click();
  await page.waitForFunction(() => window.__permissionRequests === 1);
  await page.waitForFunction(() => /Blocked/.test(document.getElementById('notif-perm-chip').textContent));
  assert.equal(await enable.isVisible(), false, 'nothing to enable once blocked');
  assert.match(await text(page.locator('#notif-device-hint')), /blocked for this site/);
  console.log(`PS-23 notification device state PASS ${page.viewportSize().width}`);
}

// PS-24 ------------------------------------------------------------------------------------------------------
async function changePassword(fx) {
  const { page } = fx;
  await openSettings(page);
  await page.locator('#btn-settings-goto-password').click();
  assert.match(await text(page.locator('#settings-pw-hint')), /At least 8 characters/, 'the rule is always visible');
  assert.equal(await page.locator('#settings-new-password').getAttribute('placeholder'), null);
  assert.match(await page.locator('#settings-new-password').getAttribute('aria-describedby'), /settings-pw-hint/);
  assert.match(await text(page.locator('#settings-change-password')), /already signed in stay signed in/);
  await page.locator('#settings-new-password').fill('short');
  await page.locator('#settings-confirm-password').fill('short');
  await page.locator('#btn-app-settings-save').click();
  assert.equal(await page.locator('#settings-new-password').getAttribute('aria-invalid'), 'true');
  assert.match(await text(page.locator('#settings-new-password-error')), /at least 8 characters/i);
  assert.equal(await page.locator('#settings-new-password').evaluate(el => document.activeElement === el), true);
  await page.locator('#settings-new-password').fill('long enough 1');
  await page.locator('#settings-confirm-password').fill('long enough 2');
  await page.locator('#btn-app-settings-save').click();
  assert.equal(await page.locator('#settings-new-password').getAttribute('aria-invalid'), null);
  assert.equal(await page.locator('#settings-confirm-password').getAttribute('aria-invalid'), 'true');
  assert.match(await text(page.locator('#settings-confirm-password-error')), /don't match/);
  assert.equal(fx.posts.length, 0, 'nothing is sent while the fields are wrong');
  // reveal toggle
  assert.equal(await page.locator('#settings-new-password').getAttribute('type'), 'password');
  await page.getByLabel('Show passwords').check();
  assert.equal(await page.locator('#settings-new-password').getAttribute('type'), 'text');
  assert.equal(await page.locator('#settings-confirm-password').getAttribute('type'), 'text');
  // a server refusal is shown under the form, not in a banner elsewhere
  await page.locator('#settings-confirm-password').fill('long enough 1');
  await page.locator('#btn-app-settings-save').click();
  await page.waitForFunction(() => /Fixture refuses/.test(document.getElementById('settings-confirm-password-error').textContent));
  assert.equal(await page.locator('#settings-confirm-password-error').isVisible(), true);
  assert.equal(await page.locator('#settings-feedback').isVisible(), false);
  assert.equal(fx.posts.length, 1);
  console.log(`PS-24 change password PASS ${page.viewportSize().width}`);
}

// PS-25 ------------------------------------------------------------------------------------------------------
const now = () => Math.floor(Date.now() / 1000);
const status = (over = {}) => ({ generated_at: now(),
  spooler: { started_at: now() - 3600, loop_lag_ms: 5, browser_clients: 1 },
  spoolman: { ok: true, version: '0.22', latency_ms: 12, pending_deductions: 0 },
  supervisor: { updated_at: now(), started_at: now() - 3600, stale: false, services: [{ name: 'spooler', restarts: 0 }] },
  printers: [{ id: 'p1', name: 'Bench printer', connected: true, connected_since: now() - 600 }],
  backups: { running: false, overdue: false, last_error: null, spoolman_db: true, interval_hours: 24, keep: 14, count: 3, dir: 'C:\\backups',
    latest: { name: 'spooler-backup-1.zip', size: 2048, created_at: now() - 60 } }, ...over });
async function systemHealth(fx) {
  const { page } = fx;
  await page.waitForFunction(() => typeof applySystemStatus === 'function');
  await page.evaluate(() => { eval('_wsConnected = true; _wsAttempts = 0;'); });
  await page.route('**/api/backup', route => route.fulfill({ json: { name: 'spooler-backup-2.zip', size: 4096, created_at: Date.now() / 1000, errors: [] } }));
  fx.broadcast({ type: 'printer_update', printer: { ...klipper, connected: true } });   // nothing offline, so the issue count below is only what each status says
  fx.broadcast({ type: 'system_status', status: status() });
  await page.evaluate(() => openSystemHealth());
  const summary = page.locator('#system-health-summary');
  await page.waitForFunction(() => /All systems OK/.test(document.getElementById('system-health-summary').textContent));
  // healthy: a summary and a folded list, no problems section
  assert.equal(await page.locator('.health-attention').count(), 0);
  assert.equal(await page.locator('.health-ok-fold').getAttribute('open'), null, 'healthy checks are folded');
  assert.equal(await page.locator('.health-ok-fold .health-row').first().isVisible(), false);
  await page.locator('.health-ok-fold > summary').click();
  assert.equal(await page.locator('.health-ok-fold .health-row').first().isVisible(), true);
  // two problems: summary counts them, they come first, the rest stays folded and keeps its fold state across refreshes
  fx.broadcast({ type: 'system_status', status: status({
    spoolman: { ok: false, down_since: now() - 30, error: 'refused', latency_ms: 0, pending_deductions: 0 },
    backups: { ...status().backups, overdue: true } }) });
  await page.waitForFunction(() => /2 issues need attention/.test(document.getElementById('system-health-summary').textContent));
  const firstRows = await page.locator('#system-health-body > :first-child .health-row-title').allInnerTexts();
  assert.deepEqual(firstRows.sort(), ['Latest backup', 'Spoolman']);
  assert.equal(await page.locator('.health-attention').evaluate(el => el === el.parentElement.firstElementChild), true, 'problems come first');
  assert.equal(await page.locator('.health-ok-fold').evaluate(el => el.open), true, 'the fold stays as the person left it');
  // one scroll container: the dialog box scrolls, the body does not
  assert.equal(await page.locator('#system-health-body').evaluate(el => getComputedStyle(el).overflowY), 'visible');
  assert.equal(await page.locator('#system-health-body').evaluate(el => getComputedStyle(el).maxHeight), 'none');
  // backup result appears in the action row, beside the button
  await page.getByRole('button', { name: 'Back up now' }).click();
  await page.waitForFunction(() => /spooler-backup-2\.zip/.test(document.getElementById('backup-feedback').textContent));
  const placement = await page.evaluate(() => {
    const fb = document.getElementById('backup-feedback').getBoundingClientRect(), btn = document.getElementById('btn-backup-now').getBoundingClientRect();
    return { sameRow: document.getElementById('backup-feedback').parentElement === document.getElementById('btn-backup-now').parentElement,
      gap: Math.abs(fb.bottom - btn.top), inView: btn.bottom <= window.innerHeight && fb.top >= 0 };
  });
  assert.equal(placement.sameRow, true);
  assert.ok(placement.gap < 40 && placement.inView, `feedback sits next to the button and on screen: ${JSON.stringify(placement)}`);
  assert.equal(await page.locator('#btn-system-health-close').getAttribute('class').then(c => /btn-secondary/.test(c)), true, 'Close is not the primary action');
  await noOverflow(page, 'system health');
  await boxFits(page, '#modal-system-health .modal-box', 'system health');
  console.log(`PS-25 system health structure PASS ${page.viewportSize().width}`);
}

// T15 ---------------------------------------------------------------------------------------------------------
async function textFloor(fx) {
  const { page } = fx;
  await openSettings(page);
  for (const [button, label, back] of [['#btn-settings-goto-printers', 'printers', '#btn-settings-back-printers'], ['#btn-settings-goto-notifications', 'notifications', '#btn-settings-back-notif']]) {
    await page.locator(button).click();
    const small = await page.evaluate(() => [...document.querySelectorAll('#modal-app-settings *')]
      .filter(el => el.getClientRects().length && [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim()) && parseFloat(getComputedStyle(el).fontSize) < 12)
      .map(el => `${el.tagName}.${el.className} ${getComputedStyle(el).fontSize}`));
    assert.deepEqual(small, [], `${label}: no text under 12 px`);
    await page.locator(back).click();
  }
  console.log(`T15 settings text floor PASS ${page.viewportSize().width}`);
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
      for (const [scenario, setup] of [[printersAndEditForm], [notificationState, { init: notificationInit, arg: 'default' }], [changePassword], [systemHealth], [textFloor]]) {
        const fx = await open(browser, baseUrl, width, setup);
        try { await scenario(fx); } finally { await fx.context.close(); }
      }
    }
  } finally {
    await browser.close();
    server.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
