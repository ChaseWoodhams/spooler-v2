// Ticket 08 · History polish: no ellipsized dates/metrics, chips on one line, status dot visible or gone.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const now = Math.floor(Date.now() / 1000);
const records = [
  { id: 1, file_name: 'benchy-in-two-colours.gcode', printer_name: 'Printer One', printer_id: 'p1', started_at: now - 3600 * 5, ended_at: now - 60,
    outcome: 'unknown', outcome_unconfirmed: true, duration_s: 13 * 3600 + 3500, filament_g: 123.4, filament_mm: null, cost: 12.34, estimate_s: 3400, estimate_g: 11, notes: '', color_hex: 'ff0000' },
  { id: 2, file_name: 'older.gcode', printer_name: 'Printer One', printer_id: 'p1', started_at: now - 400 * 86400, ended_at: now - 400 * 86400 + 600,
    outcome: 'completed', outcome_unconfirmed: false, duration_s: 600, filament_g: 2, filament_mm: null, cost: 0.5, estimate_s: 900, estimate_g: 3, notes: '', color_hex: 'ffffff' },
];
(async () => {
  const server = http.createServer(async (req, res) => {
    try {
      const rel = new URL(req.url, 'http://localhost').pathname;
      const content = await fs.readFile(path.join(root, 'public', rel === '/' ? 'index.html' : rel));
      res.setHeader('Content-Type', rel.endsWith('.js') ? 'text/javascript; charset=utf-8' : rel.endsWith('.css') ? 'text/css; charset=utf-8' : 'text/html; charset=utf-8');
      res.end(content);
    } catch { res.writeHead(404).end(); }
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const browser = await chromium.launch();
  try {
    for (const theme of ['dark', 'light']) for (const width of [1280, 1024, 768, 390]) {
      const page = await browser.newPage({ viewport: { width, height: 900 } });
      await page.addInitScript(t => localStorage.setItem('theme', t), theme);
      await page.routeWebSocket(/.*/, ws => ws.onMessage(m => {
        if (JSON.parse(m).action === 'list_printers') ws.send(JSON.stringify({ type: 'printer_update', printer: { id: 'p1', name: 'Printer One', connected: false, printer_type: 'cc1', status: {}, camera_url: null } }));
      }));
      await page.route('**/api/**', route => {
        const url = new URL(route.request().url());
        if (url.pathname === '/api/prints') return route.fulfill({ json: { prints: records, total: 2, summary: { prints: 2, duration_s: 0, filament_g: 0, cost: 0 } } });
        return route.fulfill({ json: {} });
      });
      await page.goto(`http://127.0.0.1:${server.address().port}`);
      await page.locator('#card-p1').waitFor();
      await page.evaluate(() => openHistory());
      await page.locator('.history-row').first().waitFor();
      const label = `${theme} ${width}`;
      for (const sel of ['.history-date', '.history-stats']) {
        const clipped = await page.locator(sel).evaluateAll(els => els.filter(e => e.scrollWidth > e.clientWidth).map(e => e.textContent));
        assert.deepEqual(clipped, [], `${sel} ellipsized at ${label}`);
      }
      const wrapped = await page.locator('.history-badges').first().evaluate(el => {
        const lines = new Set([...el.querySelectorAll('.status-badge')].map(b => Math.round(b.getBoundingClientRect().top)));
        const tops = [...el.querySelectorAll('.status-badge')].map(b => b.getBoundingClientRect());
        return { lines: lines.size, overflow: el.scrollWidth > el.clientWidth, h: tops.map(r => r.height) };
      });
      assert.equal(wrapped.lines, 1, `chips share a line at ${label}`);
      assert.equal(wrapped.overflow, false, `chips not clipped at ${label}`);
      const rowH = await page.locator('.history-badges .status-badge').first().evaluate(b => b.getBoundingClientRect().height);
      for (const h of wrapped.h) assert.ok(h <= rowH + 1, `chip single-line height at ${label}`);
      // the colour dot beside the filename: visible against the row, or absent
      const dot = await page.locator('.history-swatch').first().evaluate(el => {
        const s = getComputedStyle(el); return { bg: s.backgroundColor, border: s.borderTopColor, w: s.width };
      }).catch(() => null);
      if (dot) assert.notEqual(dot.border, 'rgba(0, 0, 0, 0)', `dot has an edge at ${label}`);
      assert.equal(await page.locator('#panel-history').evaluate(el => el.scrollWidth <= el.clientWidth), true, `no h-scroll at ${label}`);
      await page.close();
    }
    console.log('history polish ok');
  } finally { await browser.close(); server.close(); }
})().catch(e => { console.error(e); process.exit(1); });
