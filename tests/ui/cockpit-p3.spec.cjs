// Ticket 25: cockpit P3 batch (PS-53 SVG fullscreen/back glyphs, PS-54 remembered tab and jog step with the tab in the URL,
// PS-55 Files tab shows recent prints, PS-56 phone header dot lines up with a wrapped name).
// Fixtures only: every printer message is recorded and none is sent anywhere.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const publicDir = process.env.UI_PUBLIC_DIR || path.join(root, 'public'); // point at another checkout to see a guard fail there

const mk = (id, name, printing = false) => ({ id, name, ip: '192.0.2.20', printer_type: 'cc1', connected: true, camera_url: '/cam', attrs: { FirmwareVersion: '01.04.00' },
  status: { PrintInfo: { Status: printing ? 13 : 0, ...(printing ? { Filename: '/usb/benchy.gcode', CurrentLayer: 4, TotalLayer: 100, RemainTime: 600 } : {}) }, TempOfNozzle: 22, TempOfHotbed: 21, TempOfBox: 24, TempTargetNozzle: 0, TempTargetHotbed: 0, CurrentCoord: '202.00,150.00,10.00' } });
const LONG = 'Centauri Carbon in the workshop by the window, second shelf';
const prints = [
  { id: 3, file_name: 'benchy.gcode', outcome: 'completed', started_at: 1760000000, ended_at: 1760003600 },
  { id: 2, file_name: 'cube <b>.gcode', outcome: 'failed', started_at: 1759900000, ended_at: 1759901000 },
  { id: 1, file_name: 'old.gcode', outcome: null, started_at: 1759800000 },
];

async function open(browser, base, { width = 1098, height = 800, hash = '#printer=p1', recent = prints, recentFails = false, storage, printing = false } = {}) {
  const context = await browser.newContext({ viewport: { width, height }, serviceWorkers: 'block' });
  if (storage) await context.addInitScript(s => { for (const [k, v] of Object.entries(s)) localStorage.setItem(k, v); }, storage);
  const page = await context.newPage();
  page.on('pageerror', e => console.error('Browser error:', e.message));
  const fx = { page, context, commands: [], recentRequests: [] };
  await page.routeWebSocket(/.*/, socket => {
    socket.onMessage(raw => {
      const msg = JSON.parse(raw);
      fx.commands.push(msg);
      if (msg.action === 'list_printers') {
        socket.send(JSON.stringify({ type: 'printer_update', printer: mk('p1', 'Cockpit fixture', printing) }));
        socket.send(JSON.stringify({ type: 'printer_update', printer: mk('p2', LONG) }));
      }
    });
  });
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith('/api/camera/')) return route.abort();
    if (url.pathname === '/api/outcome-prompts') return route.fulfill({ json: { awaiting: [] } });
    if (url.pathname === '/api/prints') {
      fx.recentRequests.push(url.searchParams.get('printer_id'));
      return recentFails ? route.fulfill({ status: 500, json: { error: 'boom' } }) : route.fulfill({ json: { prints: recent, total: recent.length } });
    }
    return route.fulfill({ json: {} });
  });
  await page.goto(`${base}/${hash}`);
  await page.getByRole('tab', { name: 'Motion', exact: true }).waitFor();
  return fx;
}

(async () => {
  const server = http.createServer(async (req, res) => {
    try {
      const p = new URL(req.url, 'http://localhost').pathname;
      const file = path.join(publicDir, p === '/' ? 'index.html' : p);
      res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
      res.end(await fs.readFile(file));
    } catch { res.writeHead(404).end(); }
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch();
  try {
    // PS-53: the full screen, compact and back controls are SVG icons, named, and 44x44
    for (const width of [1098, 390]) {
      const { page, context } = await open(browser, base, { width });
      const found = await page.evaluate(() => [...document.querySelectorAll('.pv-fsbtn, .pv-back')].map(b => {
        const r = b.getBoundingClientRect();
        return { cls: b.className, svg: !!b.querySelector('svg'), name: b.getAttribute('aria-label') || b.innerText.trim(), glyph: /[‹⤡⛶]/u.test(b.innerText), w: Math.round(r.width), h: Math.round(r.height) };
      }));
      assert.ok(found.length >= 3, `back, compact and full screen buttons are there at ${width}px: ${JSON.stringify(found)}`);
      for (const b of found) {
        assert.ok(b.svg && !b.glyph, `SVG icon, no text glyph: ${JSON.stringify(b)}`);
        assert.ok(b.name, `named: ${JSON.stringify(b)}`);
        assert.ok(b.w >= 44 && b.h >= 44, `44x44 at ${width}px: ${JSON.stringify(b)}`);
      }
      assert.ok(found.some(b => /Full screen/.test(b.name)), 'full screen keeps its aria-label');
      assert.ok(found.some(b => /All printers/.test(b.name)), 'back is still named "All printers"');
      await context.close();
    }
    console.log('PASS: Full screen, compact and back controls are named SVG icons at 44x44');

    // PS-54: the tab goes in the URL, and the tab and jog step are remembered per printer
    {
      const { page, context } = await open(browser, base);
      await page.getByRole('tab', { name: 'Temperatures', exact: true }).click();
      assert.equal(await page.evaluate(() => location.hash), '#printer=p1&tab=temps', 'the tab is in the URL');
      await page.keyboard.press('ArrowRight');
      assert.equal(await page.evaluate(() => location.hash), '#printer=p1&tab=air', 'arrow keys update it too');
      await page.getByRole('tab', { name: 'Motion', exact: true }).click();
      await page.getByRole('button', { name: 'Jog distance 10 mm' }).click();
      await page.getByRole('tab', { name: 'Air & light', exact: true }).click();
      // a reload on the deep link lands on the same tab
      await page.reload();
      await page.getByRole('tab', { name: 'Air & light', exact: true }).waitFor();
      assert.equal(await page.getByRole('tab', { name: 'Air & light', exact: true }).getAttribute('aria-selected'), 'true', 'deep link opens its tab');
      // back to the grid and into the printer again: last tab and jog step are remembered
      await page.getByRole('button', { name: /All printers/ }).click();
      await page.locator('#card-p1').waitFor();
      await page.evaluate(() => openPrinterDashboard('p1'));
      await page.getByRole('tab', { name: 'Air & light', exact: true }).waitFor();
      assert.equal(await page.getByRole('tab', { name: 'Air & light', exact: true }).getAttribute('aria-selected'), 'true', 'last tab is remembered');
      await page.getByRole('tab', { name: 'Motion', exact: true }).click();
      assert.equal(await page.getByRole('button', { name: 'Jog distance 10 mm' }).getAttribute('aria-pressed'), 'true', 'jog step is remembered');
      // another printer has its own tab and step, and the id still parses from a hash with a tab
      await page.evaluate(() => { location.hash = '#printer=p2'; });
      await page.getByRole('tab', { name: 'Motion', exact: true }).waitFor();
      assert.equal(await page.getByRole('tab', { name: 'Motion', exact: true }).getAttribute('aria-selected'), 'true', 'p2 starts on Motion');
      assert.equal(await page.getByRole('button', { name: 'Jog distance 1 mm' }).getAttribute('aria-pressed'), 'true', 'p2 starts on 1 mm');
      assert.match(await page.locator('.pv-title').innerText(), /Centauri Carbon in the workshop/);
      await context.close();
    }
    {
      const { page, context } = await open(browser, base, { hash: '#printer=p1&tab=files' });
      assert.equal(await page.getByRole('tab', { name: 'Files', exact: true }).getAttribute('aria-selected'), 'true', 'a hash with a tab names the printer by its id only');
      await context.close();
    }
    {
      const { page, context } = await open(browser, base, { hash: '#printer=p1&tab=nonsense' });
      assert.equal(await page.getByRole('tab', { name: 'Motion', exact: true }).getAttribute('aria-selected'), 'true', 'an unknown tab falls back to Motion');
      await context.close();
    }
    console.log('PASS: Tab in the URL; tab and jog step remembered per printer');

    // PS-55: the Files tab shows recent prints next to Browse
    {
      const fx = await open(browser, base, { hash: '#printer=p1&tab=files' });
      const { page, context } = fx;
      const panel = page.getByRole('tabpanel');
      await panel.getByText('benchy.gcode').waitFor();
      const text = await panel.innerText();
      assert.match(text, /Recent prints/);
      assert.match(text, /Completed/);
      assert.match(text, /Failed/);
      assert.match(text, /Printing/, 'a print with no outcome is still printing');
      assert.equal(await panel.locator('b').count(), 0, 'file names are escaped, not parsed as markup');
      assert.match(text, /cube <b>\.gcode/);
      assert.ok(fx.recentRequests.includes('p1') && fx.recentRequests.filter(Boolean).every(id => id === 'p1'), 'asks for this printer only');
      await panel.getByRole('button', { name: 'Browse & start print', exact: true }).waitFor();
      await context.close();
    }
    {
      const { page, context } = await open(browser, base, { hash: '#printer=p1&tab=files', recent: [] });
      await page.getByRole('tabpanel').getByText(/No prints recorded for this printer yet/).waitFor();
      await context.close();
    }
    {
      const { page, context } = await open(browser, base, { hash: '#printer=p1&tab=files', recentFails: true });
      await page.getByRole('tabpanel').getByText(/Couldn't load recent prints/).waitFor();
      await page.getByRole('tabpanel').getByRole('button', { name: 'Browse & start print', exact: true }).waitFor();
      await context.close();
    }
    console.log('PASS: Files tab lists recent prints, an empty state and an error that keeps Browse');

    // PS-56: on a phone the status dot sits on the first line of a wrapped printer name
    {
      const { page, context } = await open(browser, base, { width: 390, height: 844, hash: '#printer=p2' });
      await page.getByRole('tab', { name: 'Motion', exact: true }).waitFor();
      const m = await page.evaluate(() => {
        const dot = document.querySelector('.pv-title .status-dot').getBoundingClientRect();
        const b = document.querySelector('.pv-title b');
        const range = document.createRange(); range.selectNodeContents(b);
        const lines = [...range.getClientRects()];
        const first = lines.reduce((a, r) => (r.top < a.top ? r : a), lines[0]);
        return { lines: new Set(lines.map(r => Math.round(r.top))).size, width: Math.round(b.getBoundingClientRect().width), dotMid: dot.top + dot.height / 2, firstMid: first.top + first.height / 2 };
      });
      assert.ok(m.lines >= 2 && m.lines <= 5, `the long name wraps on a phone in a readable column: ${JSON.stringify(m)}`);
      assert.ok(m.width >= 120, `the name column is not squeezed to a sliver: ${JSON.stringify(m)}`);
      assert.ok(Math.abs(m.dotMid - m.firstMid) <= 3, `dot lines up with the first line: ${JSON.stringify(m)}`);
      await context.close();
    }
    console.log('PASS: Phone header dot lines up with a wrapped name');

    // UCF-09: while a Print is running the cockpit opens on Temperatures, not on disabled jog buttons; idle is unchanged
    {
      const { page, context } = await open(browser, base, { printing: true });
      assert.equal(await page.getByRole('tab', { name: 'Temperatures', exact: true }).getAttribute('aria-selected'), 'true', 'printing opens on Temperatures');
      await page.getByRole('tab', { name: 'Motion', exact: true }).click();
      assert.equal(await page.getByRole('tab', { name: 'Motion', exact: true }).getAttribute('aria-selected'), 'true', 'Motion is still reachable');
      await context.close();
    }
    {
      const { page, context } = await open(browser, base, { printing: true, hash: '#printer=p1&tab=motion' });
      assert.equal(await page.getByRole('tab', { name: 'Motion', exact: true }).getAttribute('aria-selected'), 'true', 'a link that names Motion is honoured');
      await context.close();
    }
    {
      const { page, context } = await open(browser, base);
      assert.equal(await page.getByRole('tab', { name: 'Motion', exact: true }).getAttribute('aria-selected'), 'true', 'idle still opens on Motion');
      await context.close();
    }
    console.log('PASS: Cockpit opens on Temperatures during a Print and on Motion when idle');
  } finally { await browser.close(); await new Promise(r => server.close(r)); }
})().catch(e => { console.error(e); process.exit(1); });

