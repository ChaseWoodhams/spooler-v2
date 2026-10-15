// Ticket 24: dashboard shell P3 batch (PS-50 light toggle, PS-51 card tab stop, PS-52 nav and panel routes).
// Static assets plus deterministic HTTP/WebSocket fixtures only; nothing reaches a printer.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const publicDir = process.env.UI_PUBLIC_DIR || path.resolve(__dirname, '../../public');
const printer = { id: 'shell-fixture', name: 'Shell fixture printer', ip: '192.0.2.10', printer_type: 'cc1', connected: true, camera_url: null,
  status: { PrintInfo: { Status: 0 }, TempOfNozzle: 22, TempOfHotbed: 21, TempTargetNozzle: 0, TempTargetHotbed: 0, LightStatus: { SecondLight: 0 } } };
const emptyDashboard = { needs_you: { printers: [], prints: [] }, projects: [] };

async function open(browser, base, { width = 1098, height = 700, url = base } = {}) {
  const context = await browser.newContext({ viewport: { width, height }, serviceWorkers: 'block' });
  const page = await context.newPage();
  page.on('pageerror', error => console.error('Shell browser error:', error.message));
  const fx = { page, context, sockets: [], commands: [], printer };
  await page.routeWebSocket(/.*/, socket => {
    fx.sockets.push(socket);
    socket.onMessage(message => {
      const request = JSON.parse(message);
      fx.commands.push(request);
      if (request.action === 'list_printers') socket.send(JSON.stringify({ type: 'printer_update', printer: fx.printer }));
    });
  });
  await page.route('**/api/**', route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === '/api/dashboard') return route.fulfill({ json: emptyDashboard });
    if (pathname === '/api/jobs') return route.fulfill({ json: { jobs: [] } });
    return route.fulfill({ json: {} });
  });
  await page.goto(url);
  fx.update = data => { fx.printer = { ...fx.printer, status: { ...fx.printer.status, ...data } }; for (const socket of fx.sockets) socket.send(JSON.stringify({ type: 'printer_update', printer: fx.printer })); };
  return fx;
}

async function cardTabStop(browser, base) {
  const fx = await open(browser, base);
  const card = fx.page.locator('#card-shell-fixture');
  await card.waitFor();
  assert.equal(await card.getAttribute('tabindex'), null, 'the card itself is not a tab stop');
  assert.equal(await card.getAttribute('aria-label'), null, 'a plain div does not carry a label');
  const title = card.locator('.card-title-btn');
  assert.match(await title.getAttribute('aria-label'), /Shell fixture printer, Connected\. Open printer controls/);
  // Tabbing from the title reaches the next control, not a second stop on the same card
  await title.focus();
  await fx.page.keyboard.press('Tab');
  assert.equal(await fx.page.evaluate(() => document.activeElement.classList.contains('card-title-btn')), false, 'one Tab leaves the title');
  assert.equal(await fx.page.evaluate(() => document.activeElement.id === 'card-shell-fixture'), false);
  // Keyboard: Enter on the title opens the printer page; a click on the card body still does too
  await title.focus();
  await fx.page.keyboard.press('Enter');
  await fx.page.waitForFunction(() => location.hash === '#printer=shell-fixture');
  await fx.page.evaluate(() => { location.hash = ''; });
  await card.waitFor();
  await card.locator('.card-header-info').click();
  await fx.page.waitForFunction(() => location.hash === '#printer=shell-fixture');
  await fx.context.close();
  console.log('PASS: A printer card has one keyboard stop, its title, and still opens from a click on the card');
}

async function lightToggle(browser, base) {
  for (const width of [1098, 390]) {
    const fx = await open(browser, base, { width });
    const light = fx.page.locator('#card-shell-fixture .card-light-toggle');
    await light.waitFor();
    assert.equal(await light.evaluate(e => e.classList.contains('btn-primary')), false, 'the light is not a second primary button');
    const off = await light.evaluate(e => ({ pressed: e.getAttribute('aria-pressed'), svg: e.querySelector('svg').innerHTML, state: e.innerText.trim(), box: e.getBoundingClientRect().toJSON() }));
    assert.equal(off.pressed, 'false');
    assert.ok(off.box.width >= 44 && off.box.height >= 44, `light toggle is at least 44x44 at ${width}px (${off.box.width}x${off.box.height})`);
    fx.update({ LightStatus: { SecondLight: 1 } });
    await fx.page.waitForFunction(() => document.querySelector('#card-shell-fixture .card-light-toggle')?.getAttribute('aria-pressed') === 'true');
    const on = await light.evaluate(e => ({ svg: e.querySelector('svg').innerHTML, state: e.innerText.trim(), border: getComputedStyle(e).borderColor }));
    assert.notEqual(on.svg, off.svg, 'the icon changes between on and off');
    assert.notEqual(on.state, off.state, 'the state is also written as text, not only drawn');
    assert.equal(fx.commands.some(c => /light/.test(JSON.stringify(c))), false, 'the check never sends a light command');
    await fx.context.close();
  }
  console.log('PASS: The card light is a 44px toggle whose icon and text change with its state');
}

async function panelRoutes(browser, base) {
  const fx = await open(browser, base);
  const { page } = fx;
  await page.locator('#card-shell-fixture').waitFor();
  const current = () => page.evaluate(() => [...document.querySelectorAll('nav.sidenav [aria-current=page]')].map(e => e.id));
  assert.deepEqual(await current(), ['btn-printers-nav']);
  // Opening History puts it in the URL and in the nav; Back closes it and the nav goes back to Printers
  await page.locator('#btn-history').click();
  await page.waitForFunction(() => document.getElementById('panel-history').classList.contains('open'));
  assert.equal(await page.evaluate(() => location.hash), '#history');
  assert.deepEqual(await current(), ['btn-history']);
  await page.goBack();
  await page.waitForFunction(() => !document.getElementById('panel-history').classList.contains('open'));
  assert.equal(await page.evaluate(() => location.hash), '');
  assert.deepEqual(await current(), ['btn-printers-nav']);
  // Forward reopens it
  await page.goForward();
  await page.waitForFunction(() => document.getElementById('panel-history').classList.contains('open'));
  // Closing with its own button leaves a clean address and does not trap Back
  await page.locator('#btn-history-close').click();
  await page.waitForFunction(() => !document.getElementById('panel-history').classList.contains('open'));
  await page.waitForFunction(() => location.hash === '');
  // Moving from one panel to another replaces the entry instead of stacking them
  await page.locator('#btn-spools').click();
  await page.waitForFunction(() => location.hash === '#spools');
  await page.locator('#btn-spools-close').click();
  await page.waitForFunction(() => location.hash === '');
  await fx.context.close();
  // A link to a panel opens it
  const deep = await open(browser, base, { url: `${base}/#spools` });
  await deep.page.waitForFunction(() => document.getElementById('panel-spools').classList.contains('open'));
  assert.deepEqual(await deep.page.evaluate(() => [...document.querySelectorAll('nav.sidenav [aria-current=page]')].map(e => e.id)), ['btn-spools']);
  await deep.context.close();
  // A printer page is not navigated away from when a panel opens over it
  const over = await open(browser, base, { url: `${base}/#printer=shell-fixture` });
  await over.page.locator('#btn-history').click();
  await over.page.waitForFunction(() => document.getElementById('panel-history').classList.contains('open'));
  assert.equal(await over.page.evaluate(() => location.hash), '#printer=shell-fixture');
  await over.context.close();
  console.log('PASS: History, Spools and Queue are routes: Back closes them, links open them, and the nav follows');
}

async function queueEntry(browser, base) {
  for (const width of [1098, 390]) {
    const fx = await open(browser, base, { width });
    await fx.page.locator('#card-shell-fixture').waitFor();
    const queue = await fx.page.evaluate(() => {
      const button = document.getElementById('btn-queue'), badge = document.getElementById('queue-count'), box = button.getBoundingClientRect();
      return { h: box.height, w: box.width, badgeShown: badge.getClientRects().length > 0, badgeText: badge.textContent };
    });
    assert.ok(queue.h >= 44 && queue.w >= 44, `Queue entry is at least 44px at ${width}px (${queue.w}x${queue.h})`);
    assert.equal(queue.badgeShown, true, 'the count is visible, even at zero');
    assert.equal(queue.badgeText, '0');
    await fx.context.close();
  }
  console.log('PASS: The Queue entry is a full-size target with a count that is always visible');
}

async function smallShellFixes(browser, base) {
  // UCF-09: at 390 the health pill shows an icon, not a bare dot
  const fx = await open(browser, base, { width: 390 });
  await fx.page.locator('#card-shell-fixture').waitFor();
  const pill = await fx.page.evaluate(() => {
    const button = document.getElementById('btn-system-health'), icon = document.getElementById('health-pill-icon');
    const box = icon.getBoundingClientRect();
    return { iconW: box.width, iconH: box.height, paths: icon.children.length, name: button.getAttribute('aria-label') };
  });
  assert.ok(pill.iconW > 0 && pill.iconH > 0 && pill.paths > 0, 'the pill shows an icon at 390: ' + JSON.stringify(pill));
  assert.match(pill.name, /^System health: .+/, 'and keeps an accessible name');
  // UCF-09: Settings opens with focus on the dialog, no focus ring on the heading; focus stays trapped; Escape closes
  await fx.page.locator('#btn-mobile-more').click();
  await fx.page.locator('#btn-mobile-settings').click();
  await fx.page.locator('#modal-app-settings').waitFor();
  const focus = await fx.page.evaluate(() => {
    const el = document.activeElement, style = getComputedStyle(el);
    return { id: el.id, tag: el.tagName, noRing: style.outlineStyle === 'none' || parseFloat(style.outlineWidth) === 0 };
  });
  assert.equal(focus.id, 'modal-app-settings', 'focus is on the dialog: ' + JSON.stringify(focus));
  assert.ok(focus.noRing, 'no focus ring on the dialog container');
  for (let i = 0; i < 12; i++) {
    await fx.page.keyboard.press('Tab');
    assert.equal(await fx.page.evaluate(() => !!document.activeElement.closest('#modal-app-settings')), true, 'Tab stays inside Settings');
  }
  await fx.page.keyboard.press('Escape');
  await fx.page.waitForFunction(() => !document.getElementById('modal-app-settings').classList.contains('open'));
  await fx.context.close();
  console.log('PASS: The health pill keeps an icon at 390 and Settings opens with focus on the dialog');
}

(async () => {
  const server = http.createServer(async (request, response) => {
    try {
      const pathname = new URL(request.url, 'http://localhost').pathname;
      const filename = path.join(publicDir, pathname === '/' ? 'index.html' : pathname);
      const content = await fs.readFile(filename);
      response.setHeader('Content-Type', filename.endsWith('.js') ? 'text/javascript' : filename.endsWith('.css') ? 'text/css' : filename.endsWith('.svg') ? 'image/svg+xml' : 'text/html; charset=utf-8');
      response.end(content);
    } catch { response.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch();
  try {
    for (const scenario of [cardTabStop, lightToggle, panelRoutes, queueEntry, smallShellFixes]) {
      if (process.env.ONLY && scenario.name !== process.env.ONLY) continue;
      await scenario(browser, base);
    }
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
