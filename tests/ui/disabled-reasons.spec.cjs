// Guard: a disabled or blocked control says why in visible text, and looks disabled in both themes.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const read = f => fs.readFile(path.join(root, f), 'utf8');
const lum = ([r, g, b]) => { const f = v => (v /= 255) <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; return .2126 * f(r) + .7152 * f(g) + .0722 * f(b); };
const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + .05) / (y + .05); };
const rgb = s => s.match(/\d+(\.\d+)?/g).slice(0, 3).map(Number);
(async () => {
  // Source guards: the reasons are visible text linked by aria-describedby, never title-only
  const history = await read('public/history.js'), app = await read('public/app.js'), cockpit = await read('public/printer-cockpit.js');
  const pages = await read('public/slicer/project-page.js'), main = await read('public/slicer/main.js');
  assert.doesNotMatch(history, /disabled title=/, 'History quick-print buttons keep their reason out of title-only');
  assert.match(history, /aria-describedby="\$\{[^}]+\}"[^`]*aria-disabled="true"|aria-disabled="true"[^`]*aria-describedby/, 'History buttons are aria-disabled and described');
  assert.match(history, /class="reason-line"/, 'History shows the reason line');
  assert.match(history, /getAttribute\("aria-disabled"\) === "true"/, 'History ignores clicks on aria-disabled buttons');
  assert.doesNotMatch(pages, /disabled title=/, 'Plate card actions keep their reason out of title-only');
  assert.match(pages, /class="reason-line"/, 'Plate card shows the reason line');
  assert.doesNotMatch(main, /data-send-this-plate\$\{send\.why \? " disabled" : ""\}/, 'Send this Plate stays focusable when blocked');
  const blocked = app.match(/spool-pick-blocked[\s\S]{0,900}?<\/div>\s*<\/div>/)[0];
  assert.match(blocked, /tabindex="0"/, 'blocked spool rows are focusable');
  assert.match(blocked, /aria-disabled="true"/);
  assert.doesNotMatch(blocked, /opacity:\s*\.5/, 'the row text is not dimmed inline');
  assert.match(blocked, /Unload filament before reassigning/, 'the reason is inline');
  assert.match(cockpit, /aria-describedby="detail-speed-reason"/, 'the speed slider is tied to its reason');
  assert.match(cockpit, /id="detail-speed-reason"/);

  const server = http.createServer(async (req, res) => {
    try {
      const p = new URL(req.url, 'http://localhost').pathname;
      res.setHeader('Content-Type', p.endsWith('.js') ? 'text/javascript' : p.endsWith('.css') ? 'text/css' : 'text/html');
      res.end(await fs.readFile(path.join(root, 'public', p === '/' ? 'index.html' : p)));
    } catch { res.writeHead(404).end(); }
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const browser = await chromium.launch();
  try {
    for (const theme of ['dark', 'light']) {
      const page = await browser.newPage({ viewport: { width: 1098, height: 507 } });
      await page.addInitScript(t => localStorage.setItem('theme', t), theme);
      await page.routeWebSocket(/.*/, ws => ws.onMessage(() => {}));
      await page.route('**/api/**', r => r.fulfill({ json: {} }));
      await page.goto(`http://127.0.0.1:${server.address().port}`);
      const m = await page.evaluate(() => {
        document.body.insertAdjacentHTML('beforeend', `<div id="dr-fx"><input type="checkbox" disabled><select disabled><option>a</option></select><input type="range" disabled>
          <input type="text" disabled><button class="btn btn-secondary" aria-disabled="true">x</button><div class="spool-pick-item spool-pick-blocked" tabindex="0" aria-disabled="true"><span class="spool-swatch">s</span><span class="spool-pick-name">n</span></div>
          <p class="reason-line">why</p><input type="checkbox" id="dr-on"></div>`);
        const cs = s => getComputedStyle(document.querySelector('#dr-fx ' + s));
        const op = s => Number(cs(s).opacity);
        const bg = getComputedStyle(document.body).backgroundColor;
        return { bg, check: op('input[type=checkbox]'), select: op('select'), range: op('input[type=range]'), text: op('input[type=text]'), btn: op('button'),
          btnCursor: cs('button').cursor, checkCursor: cs('input[type=checkbox]').cursor, enabled: op('#dr-on'), blockedName: op('.spool-pick-name'), blockedRow: op('.spool-pick-blocked'),
          reasonSize: parseFloat(cs('.reason-line').fontSize), reasonColor: cs('.reason-line').color };
      });
      for (const k of ['check', 'select', 'range', 'text', 'btn']) assert.ok(m[k] < 1, `${theme}: disabled ${k} looks disabled (${m[k]})`);
      assert.equal(m.enabled, 1);
      assert.equal(m.btnCursor, 'not-allowed'); assert.equal(m.checkCursor, 'not-allowed');
      assert.equal(m.blockedName, 1, `${theme}: blocked row keeps its text at full strength`);
      assert.equal(m.blockedRow, 1, `${theme}: the row itself is not dimmed (only its swatch)`);
      assert.ok(m.reasonSize >= 12, `${theme}: reason line is at least 12px`);
      assert.ok(ratio(rgb(m.reasonColor), rgb(m.bg)) >= 4.5, `${theme}: reason line contrast ${m.reasonColor} on ${m.bg}`);
      await page.close();
    }
    // A quick print still printing: its buttons keep focus, show the reason, and do nothing when pressed
    {
      const page = await browser.newPage({ viewport: { width: 1098, height: 800 } });
      const record = { id: 1, file_name: 'q.gcode', printer_name: 'Printer 1', printer_id: 'p1', started_at: 1700000000, ended_at: null, outcome: null, outcome_unconfirmed: false,
        quick: true, project_available: true, reprint_available: true, notes: '', duration_s: null, filament_g: null, filament_mm: null, cost: null, estimate_s: 120, estimate_g: 10 };
      const calls = [];
      await page.routeWebSocket(/.*/, ws => ws.onMessage(m => {
        if (JSON.parse(m).action === 'list_printers') ws.send(JSON.stringify({ type: 'printer_update', printer: { id: 'p1', name: 'Printer 1', connected: false, printer_type: 'cc1', status: {}, camera_url: null } }));
      }));
      await page.route('**/api/**', r => {
        const u = new URL(r.request().url());
        if (r.request().method() !== 'GET') calls.push(u.pathname);
        if (u.pathname === '/api/prints') return r.fulfill({ json: { prints: [record], total: 1, summary: { prints: 1, duration_s: 0, filament_g: 0, cost: 0 } } });
        if (/^\/api\/prints\/\d+$/.test(u.pathname)) return r.fulfill({ json: { ...record, job_events: [] } });
        return r.fulfill({ json: {} });
      });
      await page.goto(`http://127.0.0.1:${server.address().port}`);
      await page.locator('#card-p1').waitFor();
      await page.evaluate(() => openHistory());
      await page.locator('.history-row').first().click();
      const reprint = page.locator('[data-history-action="reprint"]');
      await reprint.waitFor();
      assert.equal(await reprint.getAttribute('aria-disabled'), 'true');
      assert.equal(await reprint.evaluate(el => el.disabled), false, 'not natively disabled, so still focusable');
      const why = await reprint.getAttribute('aria-describedby');
      assert.match(await page.locator('#' + why).innerText(), /Still printing/);
      assert.equal(await reprint.getAttribute('title'), null, 'the reason is not title-only');
      await reprint.focus(); await page.keyboard.press('Enter'); await reprint.click({ force: true });
      await page.waitForTimeout(200);
      assert.deepEqual(calls, [], 'a blocked button sends nothing');
      await page.close();
    }
  } finally { await browser.close(); server.close(); }
  console.log('disabled-reasons ok');
})().catch(e => { console.error(e); process.exit(1); });
