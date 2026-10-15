// Planning progress is announced and says when it finished; actions blocked by a running stage explain themselves.
// The server is stubbed (reads only); a later route swaps the polled Split from "planning" to "ready".
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = require('playwright');

const cut = (id, offset) => ({ id, parent: null, normal: [1, 0, 0], offset, scope: null });
const piece = () => ({ fits: true, tiny: false, bbox: [0, 0, 0, 100, 60, 30], size: [30, 60, 100], volume_cm3: 180, made_by_cut: 1 });
const running = (state, message) => ({ state, result: null, stage: state, progress: { message, percent: null },
  cuts: { cuts: [cut(1, 100)] }, check: { cuts: [cut(1, 100)], pieces: [piece(), piece()] } });
const ready = () => ({ state: 'ready', result: null, stage: 'ready', progress: null,
  cuts: { cuts: [cut(1, 100)] }, check: { cuts: [cut(1, 100)], pieces: [piece(), piece()] } });

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    const open = async override => {
      const { page, context } = await sf.openSlicer(browser, server.baseUrl, { step: 'split', splitOverride: override });
      await page.waitForSelector('.slicer-step.active[data-step="split"]', { timeout: 10000 });
      await page.waitForSelector('[data-action="plan"]', { timeout: 10000 });
      return { page, context };
    };

    const a = await open(running('checking', 'Checking the cuts'));
    try {
      const plan = a.page.locator('[data-action="plan"]');
      assert.equal(await plan.getAttribute('aria-disabled'), 'true', 'Plan cuts is blocked while checking');
      const reason = a.page.locator('#split-plan-why');
      assert.equal((await reason.innerText()).trim(), "Can't plan cuts while the split is checking");
      assert.equal(await plan.getAttribute('aria-describedby'), 'split-plan-why', 'the reason is tied to the control');
      assert.ok(await reason.isVisible(), 'the reason is visible text');
      await plan.click({ force: true });
      assert.ok(await reason.isVisible(), 'trying the blocked action still shows its reason');
      console.log('PASS: Blocked Plan cuts explains itself');
    } finally { await a.context.close(); }

    const b = await open(running('planning', 'Choosing cut 2 — 3 pieces still too big'));
    try {
      const live = b.page.locator('#split-live');
      assert.equal(await live.getAttribute('aria-live'), 'polite', 'progress is a live region');
      assert.match(await live.innerText(), /Choosing cut 2 — 3 pieces still too big/);
      await b.page.route('**/api/splits/1', route => route.request().method() === 'GET' ? route.fulfill({ json: { id: 1, source_name: 'bracket.stl', settings: { scale: 1, up: 'z', printer: { bed_x: 240, bed_y: 240, height: 250 }, planner: { mode: 'smart', balanced: false }, connectors: { kind: 'none' } }, analysis: { raw_size: [360, 60, 30], size: [360, 60, 30], volume_cm3: 120, source_triangles: 4800, triangles: 4800, repairs: [], mean_wall_mm: 2.4, fits_whole: false, estimated_parts: 2 }, ...ready() } }) : route.fallback());
      await b.page.waitForFunction(() => /finished/i.test(document.getElementById('split-live')?.textContent || ''), null, { timeout: 8000 });
      assert.match(await live.innerText(), /Planning finished.*2 parts/);
      assert.equal(await b.page.locator('[data-action="plan"]').getAttribute('aria-disabled'), null, 'Plan cuts is available again');
      assert.equal(await b.page.locator('#split-plan-why').count(), 0, 'no reason once nothing blocks');
      console.log('PASS: Planning progress is announced and completion is stated');
    } finally { await b.context.close(); }
  } finally { await browser.close(); await server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
