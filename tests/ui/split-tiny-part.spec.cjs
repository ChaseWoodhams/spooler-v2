// A cut plan with a Part too small to print: the Split step names that Part and its cut, and says why Split is disabled.
// The split check result is stubbed (reads only); the geometry that decides "tiny" is covered by tests/test_split_engine.py.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = require('playwright');

const cut = (id, offset) => ({ id, parent: null, normal: [1, 0, 0], offset, scope: null });
const piece = (extra = {}) => ({ fits: true, tiny: false, bbox: [0, 0, 0, 100, 60, 30], size: [30, 60, 100], volume_cm3: 180, made_by_cut: 1, ...extra });
const tinySplit = () => ({
  state: 'ready', result: null,
  cuts: { cuts: [cut(1, 100), cut(2, 101)] },
  check: { cuts: [cut(1, 100), cut(2, 101)], pieces: [piece(), piece({ tiny: true, size: [1, 60, 30], volume_cm3: 0.6, made_by_cut: 2 }), piece({ made_by_cut: 2 })] },
});
const okSplit = () => ({ state: 'ready', result: null, cuts: { cuts: [cut(1, 100)] }, check: { cuts: [cut(1, 100)], pieces: [piece(), piece()] } });

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    const open = async (splitOverride, onPost) => {
      const { page, context, split } = await sf.openSlicer(browser, server.baseUrl, { step: 'split', splitOverride, onPost });
      await page.waitForSelector('.slicer-step.active[data-step="split"]', { timeout: 10000 });
      await page.waitForSelector('[data-action="split"]', { timeout: 10000 });
      return { page, context, split };
    };

    const a = await open(tinySplit());
    try {
      const button = a.page.locator('[data-action="split"]');
      assert.ok(await button.isDisabled() || (await button.getAttribute('aria-disabled')) === 'true', 'Split is disabled');
      const reason = a.page.locator('#split-why');
      assert.equal((await reason.locator('span').first().innerText()).trim(), 'Part 2 is too small to print, move or remove Cut 2');
      assert.equal(await button.getAttribute('aria-describedby'), 'split-why', 'the reason is tied to the button');
      const row = a.page.locator('[data-check-part="2"]');
      assert.match(await row.innerText(), /too small/i, 'the tiny Part is marked in the parts list');
      assert.equal(await a.page.locator('[data-check-part="1"][data-tiny]').count(), 0, 'a fine Part is not marked');
      assert.equal(await a.page.locator('#split-viewer canvas').evaluate(c => c.dataset.flaggedParts), '1', 'the tiny Part is highlighted in the preview');
      await a.page.locator('#split-why [data-action="select-cut"]').click();
      assert.equal(await a.page.locator('.split-cut.selected').getAttribute('data-cut'), '2', 'Select cut selects the creating cut');
      console.log('PASS: A tiny Part disables Split with a named reason, a marked row and a Select cut action');
    } finally { await a.context.close(); }

    const b = await open(okSplit());
    try {
      assert.equal(await b.page.locator('[data-action="split"]').isDisabled(), false, 'Split is enabled when every Part is printable');
      assert.equal(await b.page.locator('#split-why').count(), 0, 'no reason is shown when nothing blocks Split');
      console.log('PASS: No reason is shown when every Part is printable');
    } finally { await b.context.close(); }

    const c = await open(tinySplit());
    try {
      const fix = c.page.locator('[data-action="autofix"]');
      assert.equal(await fix.isDisabled(), false, 'Auto-fix is enabled when only a Part is too small');
      assert.equal(await c.page.locator('#split-autofix-why').count(), 0, 'no disabled reason while enabled');
      console.log('PASS: Auto-fix is enabled for a tiny Part');
    } finally { await c.context.close(); }

    const d = await open(okSplit());
    try {
      const fix = d.page.locator('[data-action="autofix"]');
      assert.equal(await fix.isDisabled(), true, 'Auto-fix is disabled when nothing needs fixing');
      assert.equal(await fix.getAttribute('aria-describedby'), 'split-autofix-why');
      assert.match(await d.page.locator('#split-autofix-why').innerText(), /already fit/i, 'the reason is visible text');
      const grid = await d.page.locator('#even-grid-why').innerText();
      assert.match(grid, /small parts/i, 'Even grid explains that it can create small Parts');
      assert.equal(await d.page.locator('#split-balanced').getAttribute('aria-describedby'), 'even-grid-why');
      console.log('PASS: A disabled Auto-fix and Even grid explain themselves');
    } finally { await d.context.close(); }

    const e = await open(okSplit(), (path, body, split) => {
      if (path.endsWith('/plan')) { split.state = 'planning'; return split; }
    });
    try {
      await e.page.locator('[data-action="plan"]').click();
      await e.page.waitForFunction(() => document.querySelector('[data-action="plan"]')?.getAttribute('aria-disabled') === 'true');
      e.split.state = 'ready';  // the planner finishes with the same cuts
      await e.page.waitForSelector('#plan-result', { timeout: 10000 });
      assert.match(await e.page.locator('#plan-result').innerText(), /same plan/i, 'Plan again says the plan is the same');
      console.log('PASS: Plan again says when the result is the same plan');
    } finally { await e.context.close(); }
  } finally { await browser.close(); await server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
