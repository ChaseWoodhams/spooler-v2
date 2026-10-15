// Purpose controls: the Model row, the inspector and project settings show and edit a Model's Purpose and note,
// copies share it, and a choice is saved through the project API as data.purposes / data.purpose.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = require('playwright');

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    const { page, context } = await sf.openSlicer(browser, server.baseUrl, { step: 'plates' });
    const saved = [];
    await page.route('**/api/projects/1', route => {
      if (route.request().method() !== 'PATCH') return route.fallback();
      const body = route.request().postDataJSON();
      saved.push(body.data);
      return route.fulfill({ json: { ...sf.makeProject(), revision: 4, data: body.data } });
    });
    try {
      await page.waitForSelector('.slicer-step.active[data-step="plates"]', { timeout: 10000 });
      const row = page.locator('[data-object="1"] .slicer-purpose');
      await row.waitFor();
      assert.equal(await row.inputValue(), '', 'a Model without its own Purpose follows the project default');
      assert.match(await row.locator('option').first().textContent(), /Project default \(General\)/);

      // project settings: default Purpose
      await page.locator('#fold-purpose summary').click();
      await page.selectOption('#project-purpose', 'display');
      await page.fill('#project-purpose-note', 'shelf pieces');
      assert.match(await row.locator('option').first().textContent(), /Project default \(Display\)/, 'the Model row shows the new default');

      // Model row: own Purpose and note
      await row.selectOption('functional');
      const note = page.locator('[data-object="1"] .slicer-purpose-note');
      await note.fill('carries motor thrust');

      // inspector shows the same
      await page.locator('[data-object="1"] .slicer-object-select').click();
      assert.equal(await page.locator('#object-panel .slicer-purpose').inputValue(), 'functional');
      assert.equal(await page.locator('#object-panel .slicer-purpose-note').inputValue(), 'carries motor thrust');

      await page.waitForTimeout(1200);
      const last = saved.at(-1);
      assert.deepEqual(last.purpose, { value: 'display', note: 'shelf pieces' });
      assert.deepEqual(last.purposes, { 11: { value: 'functional', note: 'carries motor thrust' } });

      // back to following the project removes the Model's own entry
      await page.locator('#object-panel .slicer-purpose').selectOption('');
      await page.waitForTimeout(1200);
      assert.equal(saved.at(-1).purposes, undefined);
      console.log('PASS: Purpose is visible and editable in the Model row, the inspector and project settings');
    } finally { await context.close(); }
  } finally { await browser.close(); await server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
