// Badges and chips are readable in both themes. Measures rendered colours against the real
// composited background (not class names). Fixtures only; nothing reaches a printer.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');

// Runs in the page: contrast of an element's text colour over its composited ancestor backgrounds.
function measure(selector) {
  const parse = c => { const p = c.match(/[\d.]+/g)?.map(Number) || [0, 0, 0, 0]; return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; };
  const over = (t, b) => [0, 1, 2].map(i => t[i] * t[3] + b[i] * (1 - t[3])).concat(1);
  const lum = c => { const [r, g, b] = c.slice(0, 3).map(v => { v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }); return r * .2126 + g * .7152 + b * .0722; };
  const el = [...document.querySelectorAll(selector)].find(e => e.getClientRects().length && !e.closest('[hidden]'));
  if (!el) return null;
  const layers = [];
  for (let n = el; n; n = n.parentElement) layers.unshift(parse(getComputedStyle(n).backgroundColor));
  const bg = layers.reduce((b, t) => over(t, b), [255, 255, 255, 1]);
  const fg = over(parse(getComputedStyle(el).color), bg);
  const [a, b] = [lum(fg), lum(bg)];
  return { text: el.textContent.trim().slice(0, 30), ratio: (Math.max(a, b) + .05) / (Math.min(a, b) + .05) };
}

const printing = { ...fx.printers(1)[0], status: { PrintInfo: { Status: 3, Progress: 40, Filename: 'a.gcode', RemainTime: 600, TotalLayer: 10, CurrentLayer: 4 }, TempOfNozzle: 210, TempOfHotbed: 60, TempTargetNozzle: 210, TempTargetHotbed: 60 } };
const spool = { id: 17, location: '', initial_weight: 1000, remaining_weight: 750, filament: { id: 18, material: 'PLA', name: 'Blue', color_hex: '0000FF', vendor: { id: 5, name: 'Fixture brand' } } };

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch();
  const results = [], fails = [];
  try {
    for (const theme of ['dark', 'light']) for (const width of [1280, 390]) {
      const { page, context } = await fx.openApp(browser, server.baseUrl, {
        theme, width, printers: [printing], awaiting: [fx.awaitingOutcome({ printer_id: printing.id })],
        api: url => url.pathname === '/api/dashboard' ? { json: { needs_you: { printers: [], prints: [fx.awaitingOutcome({ id: 101, printer_name: 'P' })] }, projects: [] } }
          : url.pathname === '/api/spoolman/api/v1/spool' ? { json: [spool] }
          : url.pathname === '/api/jobs' ? { json: { jobs: [{ id: 'j1', name: 'Q', file_name: 'q.gcode', state: 'queued', print_opts: {}, position: 1 }] } } : undefined,
      });
      const check = async (name, selector, required = 4.5) => {
        const m = await page.evaluate(measure, selector);
        assert.ok(m, `${theme} ${width}: ${name} (${selector}) is rendered`);
        results.push({ theme, width, name, ratio: +m.ratio.toFixed(2) });
        assert.ok(m.ratio >= required, `${theme} ${width}: ${name} "${m.text}" ${m.ratio.toFixed(2)}:1 < ${required}:1`);
      };
      const soft = async (...args) => { try { await check(...args); } catch (e) { fails.push(e.message); } };
      await page.locator('.needs-count').waitFor();
      await soft('Needs-you count', '.needs-count');
      await soft('Queue sidenav badge', '#queue-count');
      await soft('Status badge printing', '.status-badge.printing');
      await soft('Card Pause', '.card-controls .btn-secondary');
      await page.locator('#btn-spools').click();
      await page.locator('.spool-material-badge').first().waitFor();
      await soft('Spool material badge', '.spool-material-badge');
      await page.locator('#btn-spools-close').click();
      await page.evaluate(() => { location.hash = '#printer=fx-1'; });
      await page.locator('#detail-live-chip').waitFor();
      await soft('Live chip', '#detail-live-chip');
      await soft('Cockpit Pause', '[data-focus-key="pause"]');
      await context.close();
    }
    assert.equal(fails.length, 0, fails.join(String.fromCharCode(10)));
    console.log('PASS: badge and chip contrast', JSON.stringify(results));
  } finally { console.log(JSON.stringify(results)); await browser.close(); await server.close(); }
})().catch(e => { console.error(e.message); process.exit(1); });
