// Slicer steps (Model, Split, Plates, Slice, Print): no horizontal scroll at 320 px, and AA text contrast in both
// themes. Contrast is computed from computed styles over the composited ancestor backgrounds. Not measured: text over
// images/canvas (skipped, counted; gradients are judged against their worst colour stop), non-text contrast (borders, icons), hover/focus states.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = require('playwright');
const STEPS = ['model', 'split', 'plates', 'slice', 'print'];

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  const summary = [];
  try {
    for (const theme of ['dark', 'light']) for (const step of STEPS) {
      for (const width of [320, 1280]) {
        let ctx;
        for (let attempt = 0; attempt < 3; attempt++) {
          try {
            ctx = await sf.openSlicer(browser, server.baseUrl, { width, height: width === 320 ? 640 : 900, theme, step });
            await ctx.page.waitForSelector(`.slicer-step.active[data-step="${step}"]`, { timeout: 10000 });
            break;
          } catch (e) { await ctx?.context.close(); ctx = null; if (attempt === 2) throw e; }
        }
        const { page, context } = ctx;
        try {
          await page.waitForTimeout(600);
          if (step === 'print') { await page.locator('[data-tick]').first().check(); await page.evaluate(() => document.querySelectorAll('details:not(.tool-more)').forEach(d => { d.open = true; })); await page.waitForTimeout(700); }
          if (width === 320) assert.deepEqual(await page.evaluate(sf.overflow), [], `${theme}/${step}: horizontal overflow at 320 px`);
          {
            const c = await page.evaluate(sf.contrast);
            summary.push(`${theme}/${step}/${width}: ${c.checked} checked, ${c.skipped} skipped`);
            assert.deepEqual(c.fails, [], `${theme}/${step}/${width}: text under AA`);
          }
        } finally { await context.close(); }
      }
    }
    console.log('PASS: no horizontal scroll at 320 px; text contrast AA in both themes\n' + summary.join('\n'));
  } finally { await browser.close(); await server.close(); }
})().catch(e => { console.error(e.message || e); process.exit(1); });
