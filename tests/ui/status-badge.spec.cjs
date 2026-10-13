// Guard for ticket 13: every Outcome badge has an icon and a label, its colour is its own (not a connection-state
// colour), and the header health pill shows a different icon shape per state even on the phone width.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
let playwright;
try { playwright = require(process.env.PLAYWRIGHT_MODULE || 'playwright'); }
catch (error) { playwright = require(path.resolve(__dirname, '../../../../spooler-v2/node_modules/playwright')); }
const root = path.resolve(__dirname, '../../public');
const SHOTS = process.env.SHOT_DIR;

(async () => {
  const browser = await playwright.chromium.launch();
  try {
    for (const theme of ['dark', 'light']) for (const viewport of [{ width: 1098, height: 507 }, { width: 390, height: 844 }]) {
      const page = await browser.newPage({ viewport });
      await page.addInitScript(t => { try { localStorage.setItem('theme', t); } catch (_) { /* blocked */ } }, theme);
      await page.route('http://spooler.test/**', route => {
        const url = new URL(route.request().url());
        if (url.pathname.startsWith('/api/')) return route.fulfill({ json: {} });
        const file = path.join(root, url.pathname === '/' ? 'index.html' : url.pathname);
        if (!fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404 });
        return route.fulfill({ body: fs.readFileSync(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html' });
      });
      await page.routeWebSocket(/.*/, () => {});
      await page.goto('http://spooler.test/');
      const result = await page.evaluate(() => {
        const host = document.createElement('div');
        host.innerHTML = ['completed', 'failed', 'cancelled', 'unknown', null].map(outcomeBadge).join('') + unconfirmedBadge()
          + '<span class="status-badge complete">c</span><span class="status-badge offline">o</span><span class="status-badge paused">p</span><span class="status-badge idle">i</span><span class="status-badge warmingup">w</span>';
        host.id = 'probe';
        document.body.appendChild(host);
        const badges = [...host.querySelectorAll('.outcome-badge')].map(b => ({ text: b.textContent.trim(), icon: !!b.querySelector('svg path, svg circle'), color: getComputedStyle(b).color }));
        const connection = [...host.querySelectorAll('.status-badge:not(.outcome-badge)')].map(b => getComputedStyle(b).color);
        const pill = document.getElementById('btn-system-health');
        const icons = {};
        for (const level of ['ok', 'warn', 'down', 'unknown']) {
          pill.dataset.state = level; document.getElementById('health-pill-icon').innerHTML = HEALTH_ICON[level];
          icons[level] = document.getElementById('health-pill-icon').innerHTML;
        }
        const iconBox = document.getElementById('health-pill-icon').getBoundingClientRect();
        return { badges, connection, icons, iconVisible: iconBox.width > 0 && iconBox.height > 0 };
      });
      assert.deepEqual(result.badges.map(b => b.text), ['Completed', 'Failed', 'Cancelled', 'Outcome unknown', 'Printing', 'Unconfirmed']);
      assert.ok(result.badges.every(b => b.icon), 'every badge has an icon');
      const colours = result.badges.map(b => b.color);
      assert.equal(new Set(colours).size, colours.length, `badge colours are distinct (${theme}): ${colours}`);
      for (const c of colours) assert.ok(!result.connection.includes(c), `badge colour ${c} is not a connection-state colour`);
      assert.equal(new Set(Object.values(result.icons)).size, 4, 'health pill has a different shape per state');
      assert.ok(result.iconVisible, `health pill icon visible at ${viewport.width}px`);
      if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await page.locator('#probe').evaluate(e => { e.style.cssText = 'position:fixed;top:60px;left:8px;z-index:99999;background:var(--surface);padding:8px;display:flex;flex-wrap:wrap;gap:6px'; }); await page.screenshot({ path: path.join(SHOTS, `badges-${theme}-${viewport.width}.png`) }); }
      await page.close();
    }
    console.log('status-badge: ok');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
