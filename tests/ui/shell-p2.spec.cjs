// Ticket 18: dashboard shell P2 batch. Static assets plus deterministic HTTP/WebSocket fixtures only; nothing reaches a printer.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const publicDir = process.env.UI_PUBLIC_DIR || path.resolve(__dirname, '../../public');
const printer = { id: 'shell-fixture', name: 'Shell fixture printer', ip: '192.0.2.10', printer_type: 'cc1', connected: true, camera_url: null,
  status: { PrintInfo: { Status: 0 }, TempOfNozzle: 22, TempOfHotbed: 21, TempTargetNozzle: 0, TempTargetHotbed: 0 } };
const emptyDashboard = { needs_you: { printers: [], prints: [] }, projects: [] };

async function open(browser, base, { width = 1098, height = 700, dashboard = emptyDashboard, extra, url = base } = {}) {
  const context = await browser.newContext({ viewport: { width, height }, serviceWorkers: 'block' });
  const page = await context.newPage();
  page.on('pageerror', error => console.error('Shell browser error:', error.message));
  const fx = { page, context, sockets: [], commands: [], requests: [], dashboard };
  await page.routeWebSocket(/.*/, socket => {
    fx.sockets.push(socket);
    socket.onMessage(message => {
      const request = JSON.parse(message);
      fx.commands.push(request);
      if (request.action === 'list_printers') socket.send(JSON.stringify({ type: 'printer_update', printer: fx.printer || printer }));
    });
  });
  await page.route('**/api/**', route => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    fx.requests.push(`${request.method()} ${pathname}`);
    if (extra) { const handled = extra(route, pathname, request, fx); if (handled) return handled; }
    if (pathname === '/api/dashboard') return route.fulfill({ json: fx.dashboard });
    return route.fulfill({ json: {} });
  });
  await page.goto(url);
  fx.update = data => { for (const socket of fx.sockets) socket.send(JSON.stringify({ type: 'printer_update', printer: { ...(fx.printer || printer), ...data } })); };
  return fx;
}

// Side padding of a button's label: the gap between the button edge and the text, in px
const labelGaps = (page, selector) => page.evaluate(sel => {
  const button = document.querySelector(sel), box = button.getBoundingClientRect();
  const range = document.createRange(); range.selectNodeContents(button.querySelector('span') || button);
  const text = range.getBoundingClientRect();
  const icon = button.querySelector('svg'), hasIcon = !!icon && getComputedStyle(icon).display !== 'none';
  return { left: text.left - box.left, right: box.right - text.right, hasIcon, text: button.innerText.trim(), width: Math.round(box.width) };
}, selector);

async function header(browser, base) {
  for (const width of [320, 360, 390]) {
    const fx = await open(browser, base, { width, height: 700 });
    await fx.page.locator('#card-shell-fixture').waitFor();
    for (const id of ['#btn-quick-print', '#btn-new-project']) {
      const gaps = await labelGaps(fx.page, id);
      assert.ok(gaps.left >= 8 && gaps.right >= 8, `${id} at ${width}px keeps 8px beside its label (${gaps.left.toFixed(1)}/${gaps.right.toFixed(1)}, ${gaps.text})`);
    }
    await fx.context.close();
  }
  // One primary in the header, at every width
  for (const width of [1098, 390]) {
    const fx = await open(browser, base, { width });
    const primaries = await fx.page.evaluate(() => [...document.querySelectorAll('header .btn-primary')].filter(e => e.getClientRects().length).map(e => e.id));
    assert.deepEqual(primaries, ['btn-quick-print'], `one primary action in the header at ${width}px`);
    await fx.context.close();
  }
  console.log('PASS: Phone header keeps 8px beside its labels and the header has one primary action');
}

async function needsYou(browser, base) {
  const fx = await open(browser, base);
  await fx.page.locator('#card-shell-fixture').waitFor();
  await fx.page.locator('#needs-you .needs-empty').waitFor();
  assert.match(await fx.page.locator('#needs-you').innerText(), /Nothing needs you/);
  assert.equal(await fx.page.locator('#needs-you-status').innerText(), 'Nothing needs you', 'the live region is in the DOM while the section is empty');
  assert.equal(await fx.page.locator('#needs-you').getAttribute('aria-live'), null, 'the section is not itself re-announced on every redraw');
  // Rows appear, the poll repeats with the same answer: the row (and the focus on it) is not rebuilt
  fx.dashboard = { needs_you: { printers: [{ printer_id: 'shell-fixture', printer_name: 'Shell fixture printer', kind: 'start_job', auto_start: false, next_job: { id: 7, file_name: 'next.gcode' } }], prints: [] }, projects: [] };
  await fx.page.evaluate(() => loadDashboard());
  await fx.page.locator('#needs-you .needs-row').waitFor();
  assert.equal(await fx.page.locator('#needs-you-status').innerText(), '1 item needs you');
  const review = fx.page.getByRole('button', { name: 'Review & start' });
  await review.focus();
  await fx.page.evaluate(() => { window.__row = document.querySelector('#needs-you .needs-row'); });
  await fx.page.evaluate(async () => { await loadDashboard(); await loadDashboard(); });
  assert.equal(await fx.page.evaluate(() => window.__row === document.querySelector('#needs-you .needs-row')), true, 'an unchanged answer leaves the rows in place');
  assert.equal(await fx.page.evaluate(() => document.activeElement.dataset.focusKey), 'review-7', 'focus stays on the button');
  // A changed answer rebuilds, and focus returns to the same action by its key
  fx.dashboard = { needs_you: { printers: [...fx.dashboard.needs_you.printers, { printer_id: 'other', printer_name: 'Other', kind: 'start_job', auto_start: false, next_job: { id: 8, file_name: 'b.gcode' } }], prints: [] }, projects: [] };
  await fx.page.evaluate(() => loadDashboard());
  await fx.page.waitForFunction(() => document.querySelectorAll('#needs-you .needs-row').length === 2);
  assert.equal(await fx.page.evaluate(() => document.activeElement.dataset.focusKey), 'review-7', 'focus is restored by key after a rebuild');
  await fx.context.close();
  console.log('PASS: Needs you keeps a slim empty line, an always-present live region, and focus across polls');
}

async function cardFocus(browser, base) {
  const fx = await open(browser, base);
  await fx.page.locator('#card-shell-fixture').waitFor();
  const assign = fx.page.locator('#card-shell-fixture .spool-assign-btn');
  await assign.focus();
  assert.equal(await fx.page.evaluate(() => document.activeElement.classList.contains('spool-assign-btn')), true);
  fx.update({ status: { ...printer.status, TempOfNozzle: 25 } });
  await fx.page.waitForFunction(() => /25/.test(document.querySelector('#card-shell-fixture').innerText));
  assert.equal(await fx.page.evaluate(() => document.activeElement.classList.contains('spool-assign-btn')), true, 'a printer update keeps focus on the card button');
  await fx.context.close();
  console.log('PASS: A printer update does not drop focus from a card button');
}

async function discover(browser, base) {
  const fx = await open(browser, base);
  await fx.page.locator('#card-shell-fixture').waitFor();
  await fx.page.locator('#btn-discover').click();
  await fx.page.locator('#panel-printers.open, #panel-printers[aria-hidden="false"]').waitFor();
  assert.equal(fx.commands.filter(c => c.action === 'discover').length, 1);
  assert.equal(await fx.page.locator('#btn-discover-panel').isDisabled(), true, 'the panel button shows the shared scan state');
  assert.match(await fx.page.locator('#btn-discover').innerText(), /Scanning/, 'the header button shows the same state');
  await fx.page.waitForTimeout(7600);
  assert.equal(await fx.page.locator('#btn-discover-panel').isDisabled(), true, 'the scan ends when the server says so, not after a fixed 7 s');
  for (const socket of fx.sockets) socket.send(JSON.stringify({ type: 'discovery_done', added: [], cc2: [] }));
  await fx.page.waitForFunction(() => !document.getElementById('btn-discover-panel').disabled);
  assert.match(await fx.page.locator('#discover-results').innerText(), /No printers found/);
  assert.match(await fx.page.locator('#discover-results').innerText(), /add by IP|add one by IP/i);
  // Results: a CC2 shows with an Add button that fills the form
  await fx.page.locator('#btn-discover-panel').click();
  for (const socket of fx.sockets) socket.send(JSON.stringify({ type: 'discovery_done', added: [{ id: 'new1', name: 'Found CC1', ip: '192.0.2.50' }], cc2: ['192.0.2.60'] }));
  await fx.page.locator('#discover-results li').first().waitFor();
  assert.match(await fx.page.locator('#discover-results').innerText(), /Found CC1/);
  await fx.page.locator('#discover-results').getByRole('button', { name: /Add 192\.0\.2\.60/ }).click();
  assert.equal(await fx.page.locator('#input-ip').inputValue(), '192.0.2.60');
  assert.doesNotMatch(await fx.page.locator('#printer-discover-section .section-hint').innerText(), /^Automatically finds Elegoo Centauri Carbon printers on your local network\.$/);
  assert.match(await fx.page.locator('#printer-discover-section .section-hint').innerText(), /PrusaLink|Klipper/);
  await fx.context.close();
  console.log('PASS: Discover shares one completion-driven scan state and lists results in the panel');
}

async function phoneMore(browser, base) {
  const fx = await open(browser, base, { width: 390, height: 800 });
  await fx.page.locator('#card-shell-fixture').waitFor();
  const bar = await fx.page.evaluate(() => [...document.querySelectorAll('nav.sidenav .sidenav-item')].filter(e => e.getClientRects().length).map(e => e.getAttribute('aria-label')));
  assert.deepEqual(bar, ['Printers', 'Projects', 'History', 'Spools', 'More'], 'History is in the bottom bar');
  await fx.page.locator('#btn-mobile-more').click();
  const items = await fx.page.evaluate(() => [...document.querySelectorAll('#mobile-more-menu [role=menuitem]')].map(e => ({ text: e.innerText.trim(), icon: !!e.querySelector('svg') })));
  assert.deepEqual(items.map(i => i.text), ['Settings', 'Sign out']);
  assert.ok(items.every(i => i.icon), 'menu items have icons');
  assert.equal(await fx.page.evaluate(() => document.getElementById('btn-mobile-more').classList.contains('active')), true, 'More shows its active state while open');
  await fx.page.locator('#btn-mobile-signout').click();
  await fx.page.getByRole('dialog').waitFor();
  assert.equal(fx.requests.includes('POST /api/logout'), false, 'Sign out waits for confirmation');
  await fx.page.getByRole('button', { name: 'Cancel' }).click();
  assert.equal(fx.requests.includes('POST /api/logout'), false, 'Cancel does not sign out');
  await fx.context.close();
  console.log('PASS: Phone More menu has History in the bar, icons, an active state and a confirmed Sign out');
}

async function sideNav(browser, base) {
  for (const width of [768, 1024]) {
    const fx = await open(browser, base, { width });
    await fx.page.locator('#card-shell-fixture').waitFor();
    const labels = await fx.page.evaluate(() => [...document.querySelectorAll('nav.sidenav .sidenav-item span')].filter(e => e.closest('.sidenav-item').getClientRects().length && !e.closest('.mobile-only')).map(e => ({ text: e.textContent.trim(), shown: e.getClientRects().length > 0, size: parseFloat(getComputedStyle(e).fontSize) })));
    assert.ok(labels.length >= 4 && labels.every(l => l.shown && l.size >= 12), `side nav labels are visible and >=12px at ${width}px: ${JSON.stringify(labels)}`);
    await fx.context.close();
  }
  console.log('PASS: Side nav keeps visible labels between 641 and 1100px');
}

async function releaseNotes(browser, base) {
  const fx = await open(browser, base, { width: 390, height: 800, extra: route => {
    if (new URL(route.request().url()).pathname === '/changelog.json') return route.fulfill({ json: [{ version: '9.9.9', date: '<b id="x">today</b>', changes: ['One change'] }] });
  } });
  await fx.page.route('**/changelog.json*', route => route.fulfill({ json: [{ version: '9.9.9', date: '<b id="x">today</b>', changes: ['One change'] }] }));
  await fx.page.reload();
  await fx.page.locator('#card-shell-fixture').waitFor();
  await fx.page.locator('#btn-mobile-more').click();
  await fx.page.locator('#btn-mobile-settings').click();
  await fx.page.locator('#btn-settings-goto-about').click();
  await fx.page.locator('#modal-changelog.open').waitFor();
  assert.equal(await fx.page.locator('#changelog-body #x').count(), 0, 'the date is text, not markup');
  assert.match(await fx.page.locator('#changelog-body').innerText(), /<b id="x">today<\/b>/);
  assert.ok(await fx.page.evaluate(() => parseFloat(getComputedStyle(document.getElementById('version-badge')).fontSize)) >= 12);
  await fx.context.close();
  console.log('PASS: Release notes open from Settings on a phone and escape the date');
}

async function shellText(browser, base) {
  const fx = await open(browser, base, { width: 390, height: 800 });
  await fx.page.locator('#card-shell-fixture').waitFor();
  const small = await fx.page.evaluate(() => [...document.querySelectorAll('nav.sidenav .sidenav-item span, #card-shell-fixture .temp-label, #card-shell-fixture .temp-target, #card-shell-fixture .status-badge, #btn-header-more')]
    .filter(e => e.getClientRects().length).map(e => ({ cls: e.className || e.id, size: parseFloat(getComputedStyle(e).fontSize) })).filter(x => x.size < 12));
  assert.deepEqual(small, [], 'shell text is at least 12px');
  await fx.context.close();
  console.log('PASS: Shell text is at least 12px');
}

async function login(browser, base) {
  const loginExtra = state => (route, pathname, request) => {
    if (pathname === '/api/auth-status') return route.fulfill({ json: state });
    if (pathname === '/api/login') return route.fulfill({ status: 401, json: { error: 'Wrong username or password.' } });
  };
  // Already signed in: straight to the app
  let fx = await open(browser, base, { url: `${base}/login.html`, extra: loginExtra({ setup_required: false, authenticated: true }) });
  await fx.page.waitForURL(url => new URL(url).pathname === '/');
  await fx.context.close();
  // Signed out: alert, invalid state, reveal, favicon and font
  fx = await open(browser, base, { url: `${base}/login.html`, extra: loginExtra({ setup_required: false }) });
  const { page } = fx;
  await page.locator('#username').waitFor();
  assert.equal(await page.locator('#error').getAttribute('role'), 'alert');
  assert.equal(await page.locator('link[rel="icon"]').count(), 1);
  assert.match(await page.evaluate(() => getComputedStyle(document.body).fontFamily), /Inter/);
  await page.locator('#username').fill('someone');
  await page.locator('#password').fill('not-a-real-password');
  const reveal = page.locator('#reveal');
  assert.equal(await reveal.getAttribute('aria-label'), 'Show password');
  await reveal.click();
  assert.equal(await page.locator('#password').getAttribute('type'), 'text');
  assert.equal(await reveal.getAttribute('aria-pressed'), 'true');
  await page.locator('#btn').click();
  await page.locator('#error').getByText('Wrong username or password.').waitFor();
  assert.equal(await page.locator('#password').getAttribute('aria-invalid'), 'true');
  assert.match(await page.locator('#password').getAttribute('aria-describedby'), /error/);
  await fx.context.close();
  console.log('PASS: Sign-in page announces errors, marks fields invalid, can reveal the password and redirects when signed in');
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
  const only = process.env.ONLY;
  try {
    for (const scenario of [header, needsYou, cardFocus, discover, phoneMore, sideNav, releaseNotes, shellText, login]) {
      if (only && scenario.name !== only) continue;
      await scenario(browser, base);
    }
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
