// Review job modal reads as a pre-print safety gate: one labelled "Before you start" group holding both
// acknowledgements, the disabled-Start reason beside Start, and no empty preview column without a toolpath.
// Fixtures only: nothing reaches a printer.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch();
  try {
    for (const theme of ['dark', 'light']) {
      for (const [width, height] of [[1280, 800], [390, 844]]) {
        const tag = `${theme} ${width}`;
        const { page, context } = await fx.openApp(browser, server.baseUrl, { width, height, theme, hasTouch: width < 600, preflight: fx.preflightReport() });
        await page.evaluate(j => openReview(j), fx.job({ printer_id: 'fx-1' }));
        await page.locator('#review-check-list li').first().waitFor();
        const modal = page.locator('#modal-review');

        // One bordered, named group holding both acknowledgements
        const group = modal.getByRole('group', { name: 'Before you start' });
        assert.equal(await group.count(), 1, `Before you start group (${tag})`);
        assert.equal(await group.locator('input[type=checkbox]:visible').count(), 2, `both acknowledgements inside the group (${tag})`);
        assert.notEqual(await group.evaluate(el => getComputedStyle(el).borderTopWidth), '0px', `group is bordered (${tag})`);
        const sizes = await group.evaluate(el => ({ label: parseFloat(getComputedStyle(el.querySelector('label')).fontSize), body: parseFloat(getComputedStyle(document.body).fontSize) }));
        assert.ok(sizes.label > sizes.body, `labels larger than body text (${tag}): ${JSON.stringify(sizes)}`);
        for (const label of await group.locator('label:visible').all()) {
          const box = await label.boundingBox();
          assert.ok(box.height >= 44 - 0.5, `acknowledgement target >= 44px high (${tag}): ${box.height}`);
        }

        // Start disabled: the reason sits beside Start, not between the checkboxes
        assert.ok(await page.locator('#btn-review-start').isDisabled());
        assert.equal(await group.locator('#review-start-reason').count(), 0, `reason not inside the group (${tag})`);
        const reason = await page.locator('#review-start-reason').boundingBox();
        const start = await page.locator('#btn-review-start').boundingBox();
        assert.ok(reason && reason.height > 0, `reason visible (${tag})`);
        const overlapY = Math.min(reason.y + reason.height, start.y + start.height) - Math.max(reason.y, start.y);
        const gapAbove = start.y - (reason.y + reason.height);
        assert.ok(overlapY > 0 || (gapAbove >= -1 && gapAbove <= 16), `reason adjacent to Start (${tag}): ${JSON.stringify({ reason, start })}`);
        assert.ok(await page.locator('.modal-actions #review-start-reason').count() === 1, `reason in the action row (${tag})`);

        // No toolpath: the preview column collapses and checks + Start are in view
        assert.equal(await page.locator('.review-preview').isVisible(), false, `empty preview takes no space (${tag})`);
        const printer = await page.locator('#review-printer').boundingBox();
        assert.ok(printer.y + printer.height <= height, `checks start inside the viewport (${tag})`);
        const ack = await group.boundingBox();
        assert.ok(ack.y + ack.height <= height && start.y + start.height <= height, `acknowledgements and Start visible without scrolling (${tag})`);
        await context.close();
      }
    }
    console.log('Review modal gate PASS');
  } finally { await browser.close(); await server.close(); }
})().catch(err => { console.error(err); process.exitCode = 1; });
