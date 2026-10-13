// Guard: raw browser defaults (font, select, checkbox, link) must not leak into the UI.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const lum = ([r, g, b]) => { const f = v => (v /= 255) <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; return .2126 * f(r) + .7152 * f(g) + .0722 * f(b); };
const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + .05) / (y + .05); };
const rgb = s => s.match(/\d+(\.\d+)?/g).slice(0, 3).map(Number);
(async () => {
  for (const f of ['public/slicer.css', 'public/login.html', 'public/style.css'])
    assert.match(await fs.readFile(path.join(root, f), 'utf8'), /button, input, select, textarea \{ font: inherit; \}/, `${f} resets control fonts`);
  assert.match(await fs.readFile(path.join(root, 'public/slicer.css'), 'utf8'), /\.pp-queue-link \{[^}]*color: var\(--link\)/);
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
        document.body.insertAdjacentHTML('beforeend', '<div id="bd-fx"><a href="/x">plain</a><select><option>a</option></select><input type="checkbox"><button class="btn btn-secondary print-plate-btn" aria-pressed="false">Smooth</button><button>raw</button></div>');
        const cs = s => getComputedStyle(document.querySelector('#bd-fx ' + s));
        const body = getComputedStyle(document.body);
        return { bodyFont: body.fontFamily, bg: body.backgroundColor, surface: getComputedStyle(document.documentElement).getPropertyValue('--surface'),
          link: cs('a').color, select: cs('select').backgroundColor, selectFont: cs('select').fontFamily, check: cs('input').width, accent: cs('input').accentColor,
          plateFont: cs('.print-plate-btn').fontFamily, plateBorder: cs('.print-plate-btn').borderTopWidth, rawFont: cs('button:not(.btn)').fontFamily, surface2: getComputedStyle(document.body).getPropertyValue('--surface2') };
      });
      const light = theme === 'light';
      if (light) assert.ok(await page.evaluate(() => document.body.classList.contains('light-mode')), 'light theme applied');
      const surfaces = await page.evaluate(() => ['--bg', '--surface', '--surface2'].map(v => { const e = document.createElement('i'); e.style.color = `var(${v})`; document.body.append(e); const c = getComputedStyle(e).color; e.remove(); return c; }));
      for (const s of surfaces) assert.ok(ratio(rgb(m.link), rgb(s)) >= 4.5, `${theme}: link ${m.link} on ${s} >= 4.5`);
      assert.notEqual(m.link, 'rgb(0, 0, 238)');
      assert.notEqual(m.select, 'rgb(255, 255, 255)', `${theme}: select is token-styled`);
      assert.equal(m.selectFont, m.bodyFont); assert.equal(m.plateFont, m.bodyFont); assert.equal(m.rawFont, m.bodyFont);
      assert.equal(m.plateBorder, '1px'); assert.ok(['16px', '20px'].includes(m.check), 'checkbox is sized by token, not 13px');
      assert.notEqual(m.accent, 'auto');
      await page.close();
    }
  } finally { await browser.close(); server.close(); }
  console.log('browser-defaults ok');
})().catch(e => { console.error(e); process.exit(1); });
