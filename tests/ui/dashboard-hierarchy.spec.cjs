// Dashboard hierarchy: printer state leads. At most two filled-primary buttons in the first viewport, a Needs-you strip
// that is distinct but quiet, one page-title level, and dashboard secondary text of at least 13px. Fixtures only.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');

const dashboard = { needs_you: { prints: [], printers: [{ printer_id: 'fx-2', printer_name: 'Prototype farm printer number three', kind: 'bed_cleared',
  print: { id: 5, file_name: 'lid.gcode', project_name: 'Lid' }, auto_start: true, next_job: { id: 3, file_name: 'lid.gcode' } }] },
  projects: [{ id: 7, name: 'Bracket set', percent: 40, printed_ok: 2, total: 5 }] };
const api = url => url.pathname === '/api/dashboard' ? { json: dashboard } : undefined;

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch();
  try {
    for (const theme of ['dark', 'light']) {
      for (const width of [1280, 390]) {
        const { page, context } = await fx.openApp(browser, server.baseUrl, { width, height: 800, theme, printers: fx.printers(5), api });
        await page.locator('#needs-you .needs-row').waitFor();
        const tag = `${theme} ${width}`;
        const m = await page.evaluate(() => {
          const visible = e => { const r = e.getBoundingClientRect(); const x = Math.min(Math.max(r.left + r.width / 2, 0), innerWidth - 1), y = r.top + r.height / 2;
            return r.width > 0 && r.height > 0 && r.top < innerHeight && r.left < innerWidth && r.right > 0 && !e.closest('[hidden]') && e.contains(document.elementFromPoint(x, y)); };
          const alpha = c => { const p = (c.match(/rgba?\(([^)]+)\)/) || [])[1]; if (!p) return 0; const a = p.split(/[ ,/]+/); return a.length > 3 ? +a[3] : 1; };
          const filled = [...document.querySelectorAll('button.btn-primary, a.btn-primary')].filter(visible).filter(e => alpha(getComputedStyle(e).backgroundColor) > 0.5).map(e => e.textContent.trim());
          const ny = document.getElementById('needs-you'), cs = getComputedStyle(ny);
          const row = ny.querySelector('.needs-row'), rr = row.getBoundingClientRect();
          const btn = row.querySelector('button').getBoundingClientRect();
          const size = e => parseFloat(getComputedStyle(e).fontSize);
          const h1 = document.querySelector('#dash-bar h1'), h2s = [...document.querySelectorAll('#needs-you h2, #dash-projects h2')];
          let total = 0, small = 0; const offenders = {};
          const walker = document.createTreeWalker(document.getElementById('main'), NodeFilter.SHOW_TEXT);
          while (walker.nextNode()) {
            const n = walker.currentNode, t = n.textContent.trim(), e = n.parentElement;
            if (!t || !e.getClientRects().length || e.closest('[hidden],#printer-detail,#projects-view,.sr-only')) continue;
            total += t.length;
            if (size(e) < 13) { small += t.length; offenders[(e.className || e.tagName) + '@' + size(e)] = 1; }
          }
          return { filled, border: cs.borderTopColor, borderLeftWidth: parseFloat(cs.borderLeftWidth), bgAlpha: alpha(cs.backgroundColor),
            sameRow: Math.abs((btn.top + btn.bottom) / 2 - (rr.top + rr.bottom) / 2) < rr.height / 2 || rr.height < 140,
            text: row.innerText, h1: size(h1), h2: h2s.map(size), small, total, offenders: Object.keys(offenders),
            accent: getComputedStyle(document.body).getPropertyValue('--accent').trim(), headings: [...document.querySelectorAll('#main h1')].filter(visible).length };
        });

        assert.ok(m.filled.length <= 2, `at most two filled-primary buttons in the first viewport (${tag}): ${m.filled.join(', ')}`);

        // Needs-you: still distinct (accent edge) but the full orange frame and heavy tint are gone; row is intact
        assert.ok(m.borderLeftWidth >= 2, `Needs-you keeps a visible accent edge (${tag})`);
        assert.ok(m.bgAlpha <= 0.05, `Needs-you tint is subtle (${tag}): alpha ${m.bgAlpha}`);
        const accent = await page.evaluate(() => { const s = document.createElement('i'); s.style.color = getComputedStyle(document.body).getPropertyValue('--accent'); document.body.append(s); const c = getComputedStyle(s).color; s.remove(); return c; });
        assert.notEqual(m.border, accent, `Needs-you top/right/bottom border is not the full accent (${tag})`);
        assert.ok(/Prototype farm printer/.test(m.text) && /bed/i.test(m.text), `Needs-you row keeps decision and reason (${tag}): ${m.text}`);
        assert.ok(await page.locator('#needs-you .needs-row').getByRole('button', { name: /Bed cleared/ }).isVisible(), `Needs-you row keeps its button (${tag})`);

        // One page title level: the h1 is larger than every section heading, and only one h1 is on screen
        assert.equal(m.headings, 1, `one visible page title (${tag})`);
        for (const h of m.h2) assert.ok(m.h1 > h, `page title (${m.h1}px) is larger than section headings (${h}px) (${tag})`);

        // Secondary text is at least 13px
        assert.equal(m.small, 0, `no dashboard text under 13px (${tag}): ${m.offenders.join(', ')}`);
        await context.close();
      }
    }
    console.log('dashboard-hierarchy: ok');
  } finally { await browser.close(); await server.close(); }
})().catch(e => { console.error(e); process.exit(1); });
