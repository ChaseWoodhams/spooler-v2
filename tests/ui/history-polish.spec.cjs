// History polish: dates and metrics are never ellipsized or spill sideways (long-metric fixture), chips share one line, the status dot is visible or gone.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
// The first record carries long metrics (13 h, 123.4 g, 12.34) and a colour dot.
const records = fx.historyRecords().map((r, i) => i ? r : { ...r, duration_s: 13 * 3600 + 3500, filament_g: 123.4, filament_mm: null, cost: 12.34, color_hex: 'ff0000' });
(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch();
  try {
    for (const theme of ['dark', 'light']) for (const width of [1280, 1024, 768, 390]) {
      const { page, context } = await fx.openApp(browser, server.baseUrl, { width, height: 900, theme, printers: fx.printers(2), prints: records });
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
      assert.equal(await page.locator('#panel-history').evaluate(el => el.scrollWidth <= el.clientWidth), true, `no h-scroll in the drawer at ${label}`);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `no h-scroll on the page at ${label}`);
      await context.close();
    }
    console.log('history polish ok');
  } finally { await browser.close(); await server.close(); }
})().catch(e => { console.error(e); process.exit(1); });
