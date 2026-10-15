// Stepper reasons and the forward action: a locked step says what unlocks it (tooltip and accessible description), the forward
// action sits in the header on every step, and the Plates import shows progress. Never contacts printers.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = require('playwright');

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    // A project whose model is still being checked and has nothing on a plate yet: Split, Plates, Slice and Print are locked.
    {
      const { page, context } = await sf.openSlicer(browser, server.baseUrl, { step: 'model', mutate: (p, s) => { p.data.objects = []; p.models = []; p.slices = {}; s.analysis = null; s.result = null; s.state = 'analyzing'; } });
      try {
        await page.waitForSelector('.slicer-step.active[data-step="model"]');
        const expected = { split: /model check/i, plates: /Split the model first/, slice: /plate/i, print: /plate/i };
        for (const [step, re] of Object.entries(expected)) {
          const btn = page.locator(`.slicer-step[data-step="${step}"]`);
          assert.equal(await btn.getAttribute('aria-disabled'), 'true', `${step}: announced as unavailable`);
          assert.match(await btn.getAttribute('title') || '', re, `${step}: tooltip gives the reason`);
          const described = await btn.evaluate(el => el.getAttribute('aria-describedby') && document.getElementById(el.getAttribute('aria-describedby'))?.textContent);
          assert.match(described || '', re, `${step}: accessible description gives the reason`);
          const before = await page.locator('.slicer-step.active').getAttribute('data-step');
          await btn.click({ force: true });
          assert.equal(await page.locator('.slicer-step.active').getAttribute('data-step'), before, `${step}: a locked step does not open`);
        }
        const open = page.locator('.slicer-step[data-step="model"]');
        assert.equal(await open.getAttribute('aria-disabled'), null);
        assert.equal(await open.getAttribute('aria-describedby'), null);
      } finally { await context.close(); }
      console.log('PASS: locked steps give their unlock reason as tooltip and description');
    }
    // The forward action is in the header's action area, at the same place, on every step.
    {
      const boxes = {};
      for (const [step, label] of [['model', 'Plan the cuts →'], ['split', 'Lay out on plates →'], ['plates', 'Slice →']]) {
        const { page, context } = await sf.openSlicer(browser, server.baseUrl, { step, mutate: (p, s) => { if (step === 'model') s.analysis.fits_whole = false; } });
        try {
          await page.waitForSelector(`.slicer-step.active[data-step="${step}"]`);
          const btn = page.locator('#step-actions').getByRole('button', { name: label, exact: true });
          await btn.waitFor({ timeout: 5000 });
          assert.equal(await page.getByRole('button', { name: label, exact: true }).count(), 1, `${step}: one forward action`);
          boxes[step] = await btn.boundingBox();
          const nav = await page.locator('header').first().boundingBox();
          assert.ok(boxes[step].y >= nav.y && boxes[step].y + boxes[step].height <= nav.y + nav.height + 1, `${step}: forward action sits in the header`);
        } finally { await context.close(); }
      }
      const right = b => Math.round(b.x + b.width);
      assert.ok(Math.abs(right(boxes.model) - right(boxes.split)) <= 1 && Math.abs(right(boxes.plates) - right(boxes.split)) <= 1, `forward action right edge matches: ${JSON.stringify(boxes)}`);
      assert.ok(Math.abs(boxes.model.y - boxes.plates.y) <= 1 && Math.abs(boxes.split.y - boxes.plates.y) <= 1, 'forward action has the same vertical position');
      console.log('PASS: forward action sits in the same place on every step');
    }
    // Clicking Plan the cuts from the header opens the Split step.
    {
      const { page, context } = await sf.openSlicer(browser, server.baseUrl, { step: 'model' });
      try {
        await page.locator('#step-actions').getByRole('button', { name: 'Plan the cuts →' }).click();
        await page.waitForSelector('.slicer-step.active[data-step="split"]');
      } finally { await context.close(); }
      console.log('PASS: header Plan the cuts opens Split');
    }
    // While models import onto Plates, progress shows how many are done.
    {
      const { page, context } = await sf.openSlicer(browser, server.baseUrl, { step: 'plates', mutate: p => { p.models[0].state = 'importing'; } });
      try {
        const status = page.locator('#step-actions [data-role="import-progress"]');
        await status.waitFor({ timeout: 5000 });
        assert.match(await status.innerText(), /1 of 2 models imported/);
        assert.equal(await status.getAttribute('role'), 'status');
        assert.equal(await page.locator('#step-actions progress').count(), 1);
      } finally { await context.close(); }
      console.log('PASS: import progress shown');
    }
  } finally { await browser.close(); await server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
