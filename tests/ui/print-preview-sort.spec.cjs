// Print step: the selected Plate always shows a picture or "No preview" with a reason; the Ready list names and remembers its sort;
// Send Plates and the progress summary stay in view.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = require('playwright');

const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==', 'base64');
// Eleven sliced Plates; times (min): 10, 50, 30, ... so each sort gives a different order than Plate number
const TIMES = [600, 3000, 1800, 4200, 1200, 2400, 3600, 900, 1500, 2100, 300];
const GRAMS = [5, 9, 7, 1, 8, 2, 6, 3, 10, 4, 11];
const many = base => {
  base.plates = TIMES.map((t, i) => ({ ...base.plates[0], id: i + 1, name: `Plate ${i + 1}`, estimate_s: t, estimate_g: GRAMS[i], parts: i === 2 ? 0 : 1 }));
  base.print_order = base.plates.map(p => p.id).sort((a, b) => TIMES[b - 1] - TIMES[a - 1]);
  base.progress = { ...base.progress, total: 11, counts: { ...base.progress.counts, sliced: 11, unsliced: 0 } };
  return base;
};
const readyNames = page => page.locator('[data-group="ready"] .pp-plate-btn').allInnerTexts();

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    const { page, context } = await sf.openSlicer(browser, server.baseUrl, { step: 'print', statusOverride: many });
    await page.locator('[data-group="ready"]').waitFor();

    // Preview: the fixture serves no picture, so the card says why instead of showing an empty box
    await page.locator('#project-page [data-plate="1"]').click();
    const noPic = page.locator('.pp-nopreview');
    await noPic.waitFor();
    assert.match(await noPic.innerText(), /No preview/);
    assert.match(await noPic.innerText(), /\S{4,}/);
    // A Plate with no objects explains that
    await page.locator('#project-page [data-plate="3"]').click();
    assert.match(await page.locator('.pp-nopreview').innerText(), /no objects/i);

    // Sorting: labelled, defaults to longest first
    assert.match(await page.locator('#pp-sort-label').innerText(), /Longest first/);
    assert.deepEqual((await readyNames(page)).slice(0, 3), ['Plate 4', 'Plate 7', 'Plate 2']);
    const sort = page.locator('#pp-sort');
    await sort.selectOption('number');
    assert.match(await page.locator('#pp-sort-label').innerText(), /Plate number/);
    assert.deepEqual((await readyNames(page)).slice(0, 3), ['Plate 1', 'Plate 2', 'Plate 3']);
    await sort.selectOption('time');
    assert.deepEqual((await readyNames(page)).slice(0, 3), ['Plate 11', 'Plate 1', 'Plate 8']);
    await sort.selectOption('filament');
    assert.match(await page.locator('#pp-sort-label').innerText(), /filament/i);
    assert.deepEqual((await readyNames(page)).slice(0, 3), ['Plate 4', 'Plate 6', 'Plate 8']);

    // The choice is remembered per browser (also in another Project page load)
    await page.reload();
    await page.locator('[data-group="ready"]').waitFor();
    assert.equal(await page.locator('#pp-sort').inputValue(), 'filament');
    assert.deepEqual((await readyNames(page)).slice(0, 2), ['Plate 4', 'Plate 6']);

    // Layout: progress at the top, Send Plates in view without scrolling, no collapsed area to open
    const box = async sel => page.locator(sel).boundingBox();
    const send = await box('#pp-send'), head = await box('.pp-progress');
    assert.ok(send && send.y >= 0 && send.y + send.height <= 900, 'Send Plates is inside the window');
    assert.ok(head.y < 200, 'progress summary is at the top');
    assert.match(await page.locator('.pp-progress').innerText(), /0 of 11 plates completed/);
    assert.match(await page.locator('.pp-progress').innerText(), /Filament:.*needed/);
    await context.close();

    // A real picture shows when one exists
    const second = await sf.openSlicer(browser, server.baseUrl, { step: 'print', statusOverride: many });
    await second.page.route('**/preview.png*', r => r.fulfill({ body: PNG, contentType: 'image/png' }));
    await second.page.reload();
    await second.page.locator('#project-page [data-plate="1"]').click();
    await second.page.locator('img.pp-preview').waitFor();
    assert.equal(await second.page.locator('.pp-nopreview').count(), 0);
    await second.context.close();
    console.log('PASS: Print step preview fallbacks, sort and layout');
  } finally { await browser.close(); await server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
