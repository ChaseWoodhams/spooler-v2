// Print step: "Send Plates" says why it is disabled, tied to the button; ticking a Plate clears the reason.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = require('playwright');

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    const { page, context } = await sf.openSlicer(browser, server.baseUrl, { step: 'print' });
    await page.locator('#pp-send').waitFor();
    assert.equal(await page.locator('#pp-send').getAttribute('aria-disabled'), 'true');
    const why = page.locator('#' + await page.locator('#pp-send').getAttribute('aria-describedby'));
    assert.match(await why.innerText(), /Tick the Plates to send/);
    await page.locator('[data-tick]').first().check();
    assert.equal(await page.locator('#pp-send').getAttribute('aria-disabled'), null);
    assert.equal(await why.isVisible(), false, 'reason hides once a Plate is ticked');
    await context.close();
    console.log('PASS: Send Plates names why it is disabled');
  } finally { await browser.close(); await server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
