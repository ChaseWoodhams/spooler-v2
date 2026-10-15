// Print step: the selected Plate shows the picture embedded in its Slice first, the Plate as laid out when the Slice has none
// (or the Plate has no current Slice), and "No preview" with a reason when neither loads. The server is stubbed.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = require('playwright');

const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==', 'base64');
const serve = (page, { slice, layout }) => Promise.all([
  page.route('**/slice-thumbnail.png*', r => slice ? r.fulfill({ body: PNG, contentType: 'image/png' }) : r.fulfill({ status: 404, json: { error: 'no picture' } })),
  page.route('**/preview.png*', r => layout ? r.fulfill({ body: PNG, contentType: 'image/png' }) : r.fulfill({ status: 404, json: { error: 'none' } })),
]);

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    const open = async (routes, statusOverride) => {
      const t = await sf.openSlicer(browser, server.baseUrl, { step: 'print', statusOverride });
      await serve(t.page, routes);
      await t.page.reload();
      await t.page.locator('#project-page [data-plate="1"]').click();
      return t;
    };
    const src = page => page.locator('img.pp-preview').getAttribute('src');

    let t = await open({ slice: true, layout: true });
    try {
      await t.page.locator('img.pp-preview').waitFor();
      assert.match(await src(t.page), /\/plates\/1\/slice-thumbnail\.png/, 'the Slice picture comes first');
      assert.match(await t.page.locator('img.pp-preview').getAttribute('alt'), /sliced model/);
      console.log('PASS: A sliced Plate shows the picture embedded in its Slice');
    } finally { await t.context.close(); }

    t = await open({ slice: false, layout: true });
    try {
      await t.page.waitForFunction(() => /preview\.png/.test(document.querySelector('img.pp-preview')?.getAttribute('src') || ''));
      assert.match(await t.page.locator('img.pp-preview').getAttribute('alt'), /as laid out/);
      console.log('PASS: A Slice without a picture falls back to the Plate as laid out');
    } finally { await t.context.close(); }

    t = await open({ slice: false, layout: false });
    try {
      await t.page.locator('.pp-nopreview').waitFor();
      assert.match(await t.page.locator('.pp-nopreview').innerText(), /No preview\s+the picture could not be loaded/);
      console.log('PASS: With no picture at all the card says No preview and why');
    } finally { await t.context.close(); }

    // An outdated Slice no longer matches the Plate: show the layout, not the old picture
    t = await open({ slice: true, layout: true }, base => { base.plates[0].outdated = true; return base; });
    try {
      await t.page.locator('img.pp-preview').waitFor();
      assert.match(await src(t.page), /\/plates\/1\/preview\.png/);
      console.log('PASS: An outdated Slice shows the current layout');
    } finally { await t.context.close(); }
  } finally { await browser.close(); await server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
