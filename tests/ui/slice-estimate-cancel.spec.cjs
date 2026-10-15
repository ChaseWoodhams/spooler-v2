// Slice step: a labelled slicing-time estimate, queue positions, and cancelling queued or running slices.
// The fixture answers the cancel POST itself; no real slicer or printer is involved.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = require('playwright');

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  const row = (page, id) => page.locator(`#slicing-list [data-select-plate="${id}"]`);
  try {
    // Estimate: every unsliced Plate says how long slicing will take, and that it is a guess
    let { page, context } = await sf.openSlicer(browser, server.baseUrl, { step: 'slice', slices: {} });
    await row(page, 1).waitFor();
    for (const id of [1, 2]) {
      const text = await row(page, id).innerText();
      assert.match(text, /Estimate: about \d+ s to slice/i, `plate ${id} shows a labelled estimate`);
      assert.match(text, /Not sliced yet/);
    }
    await context.close();
    console.log('PASS: unsliced Plates show a labelled slicing-time estimate');

    // Queue position, and cancel through the API
    const cancelled = [];
    const writes = (method, path, project) => {
      const m = path.match(/^\/api\/projects\/1\/plates\/(\d+)\/cancel-slice$/);
      if (method === 'POST' && m) { cancelled.push(Number(m[1])); delete project.slices[m[1]]; return project; }
    };
    ({ page, context } = await sf.openSlicer(browser, server.baseUrl, { step: 'slice', writes,
      slices: { 1: { state: 'slicing', started_at: 1 }, 2: { state: 'queued', queued_at: 2 } } }));
    await row(page, 2).waitFor();
    assert.match(await row(page, 2).innerText(), /position 1 in line/i, 'queued Plate shows its place in the line');
    assert.doesNotMatch(await row(page, 1).innerText(), /position \d+ in line/i);
    assert.doesNotMatch(await row(page, 2).innerText(), /Estimate: about/, 'a queued Plate is no longer just an estimate');

    await page.locator('[data-cancel-slice="2"]').click();
    await page.waitForFunction(() => /Not sliced yet/.test(document.querySelector('#slicing-list [data-select-plate="2"]').innerText));
    assert.deepEqual(cancelled, [2]);
    assert.equal(await page.locator('[data-cancel-slice="2"]').count(), 0, 'nothing left to cancel on Plate 2');
    assert.match(await page.locator('#slicing-totals').innerText(), /0 of 2 plates sliced/);

    await page.locator('[data-cancel-slice="1"]').click();   // the running one
    await page.waitForFunction(() => /Not sliced yet/.test(document.querySelector('#slicing-list [data-select-plate="1"]').innerText));
    assert.deepEqual(cancelled, [2, 1]);
    assert.doesNotMatch(await page.locator('#slicing-actions').innerText(), /All plates sliced/);
    assert.ok(await page.locator('[data-slice-all]').count(), 'Slice all offered again');
    await context.close();
    console.log('PASS: queue positions shown; cancelling queued and running slices returns Plates to Not sliced yet');
  } finally { await browser.close(); await server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
