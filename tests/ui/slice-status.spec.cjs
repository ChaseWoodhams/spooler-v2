// Slice step status: one per-Plate status drives the header, the sidebar list and the summary.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = require('playwright');

const sliced = { state: 'sliced', outdated: false, layers: 10, estimate_s: 600, estimate_g: 5 };
const cases = {
  allSlicing: { slices: { 1: { state: 'slicing', started_at: 1 }, 2: { state: 'queued' } } },
  oneDoneOneSlicing: { slices: { 1: sliced, 2: { state: 'slicing', started_at: 1 } } },
  oneDoneOneFailed: { slices: { 1: sliced, 2: { state: 'failed', error: 'boom' } } },
  oneDoneOneOutdated: { slices: { 1: sliced, 2: { ...sliced, outdated: true } } },
  allDone: { slices: { 1: sliced, 2: sliced } },
};

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  const open = async name => {
    const { page, context } = await sf.openSlicer(browser, server.baseUrl, { step: 'slice', slices: cases[name].slices });
    await page.locator('#slicing-list [data-slice-plate]').first().waitFor();
    return { page, context, summary: await page.locator('#slicing-totals').innerText(), actions: await page.locator('#slicing-actions').innerText() };
  };
  try {
    let r = await open('allSlicing');
    assert.match(r.summary, /0 of 2 plates sliced/);
    assert.doesNotMatch(r.actions, /All plates sliced/);
    assert.equal(await r.page.locator('#slicing-totals').getAttribute('aria-live'), 'polite');
    await r.page.locator('#slicing-list [data-select-plate="1"]').click();
    assert.match(await r.page.locator('#slice-state').innerText(), /Slice in progress/);
    assert.match(await r.page.locator('#slicing-list [data-select-plate="1"]').innerText(), /Slice in progress/);
    assert.ok(await r.page.locator('#slicing-list [data-select-plate="1"] .dot.busy').count(), 'sidebar dot shows busy');
    await r.context.close();
    console.log('PASS: all slicing reads 0 of 2, no success message, header and list agree');

    r = await open('oneDoneOneSlicing');
    assert.match(r.summary, /1 of 2 plates sliced/);
    assert.doesNotMatch(r.actions, /All plates sliced/);
    await r.context.close();

    r = await open('oneDoneOneFailed');
    assert.match(r.summary, /1 of 2 plates sliced/);
    assert.match(r.summary, /1 failed/);
    assert.doesNotMatch(r.actions, /All plates sliced/);
    assert.ok(await r.page.locator('[data-slice-all]').count(), 'Slice N plates still offered');
    await r.context.close();

    r = await open('oneDoneOneOutdated');
    assert.match(r.summary, /1 of 2 plates sliced/);
    assert.doesNotMatch(r.actions, /All plates sliced/);
    await r.context.close();
    console.log('PASS: failed, outdated and slicing Plates keep the success message hidden');

    r = await open('allDone');
    assert.match(r.summary, /2 of 2 plates sliced/);
    assert.match(r.actions, /All plates sliced/);
    await r.context.close();
    console.log('PASS: success message when every Plate is up to date');
  } finally { await browser.close(); await server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
