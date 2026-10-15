// Every slicer step (Model, Split, Plates, Slice, Print) renders a Project that has models, a Split, Plates and a Slice.
// Also the capture source for the step review: SLICER_SHOTS_DIR=<dir> node tests/ui/slicer-steps.spec.cjs saves PNGs outside the repo.
const assert = require('node:assert/strict');
const path = require('node:path');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = (() => { try { return require(process.env.PLAYWRIGHT_MODULE || 'playwright'); } catch { return require(path.resolve(__dirname, '../../../../spooler-v2/node_modules/playwright')); } })();

const STEP_LABELS = { model: 'Model', split: 'Split', plates: 'Plates', slice: 'Slice', print: 'Print' };
const shots = process.env.SLICER_SHOTS_DIR;
const views = shots ? [[1280, 900, 'dark'], [1280, 900, 'light'], [390, 844, 'dark'], [390, 844, 'light']] : [[1280, 900, 'dark'], [390, 844, 'light']];

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    for (const [width, height, theme] of views) {
      for (const step of Object.keys(STEP_LABELS)) {
        const errors = [];
        const { page, context } = await sf.openSlicer(browser, server.baseUrl, { width, height, theme, step });
        page.on('pageerror', e => errors.push(e.message));
        try {
          await page.waitForSelector(`.slicer-step.active[data-step="${step}"]`, { timeout: 10000 });
          await page.waitForTimeout(600);
          const label = `${STEP_LABELS[step]} @${width} ${theme}`;
          const visibleText = await page.evaluate(() => {
            const out = [];
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            for (let n = walker.nextNode(); n; n = walker.nextNode()) {
              const el = n.parentElement;
              if (!el || ['SCRIPT', 'STYLE'].includes(el.tagName) || !el.getClientRects().length) continue;
              if (getComputedStyle(el).visibility === 'hidden') continue;
              out.push(n.textContent);
            }
            return out.join(' ');
          });
          assert.ok(!/\b(undefined|NaN|\[object Object\])\b/.test(visibleText), `${label}: shows a placeholder value: ${visibleText.match(/.{0,40}\b(undefined|NaN|\[object Object\])\b.{0,40}/)?.[0]}`);
          for (const needle of sf.EXPECT[step]) assert.ok(visibleText.includes(needle), `${label}: expected "${needle}" on screen`);
          assert.equal(errors.length, 0, `${label}: page errors ${errors.join('; ')}`);
          if (shots) await page.screenshot({ path: path.join(shots, `${step}-${width}-${theme}.png`) });
        } finally { await context.close(); }
      }
      console.log(`PASS: All five steps render their data at ${width}px ${theme}`);
    }
  } finally { await browser.close(); await server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
