// Guards for defects the UI critique found already fixed: History text encoding, Start print reachable in the
// Review job modal, and the hit-target minimum on primary controls. Dark and light themes. Fixtures only.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const fx = require('./fixtures.cjs');

const HIT_MIN = 44;
const themes = ['dark', 'light'];

function assertValidUtf8Sources() {
  const decoder = new TextDecoder('utf-8', { fatal: true });
  for (const name of ['history.js', 'history-quality.css', 'index.html', 'outcome-prompt.js']) {
    const text = decoder.decode(fs.readFileSync(path.join(fx.publicDir, name)));   // throws on invalid UTF-8
    assert.ok(!text.includes('�'), `${name} contains a replacement character`);
  }
}

(async () => {
  assertValidUtf8Sources();
  const server = await fx.startServer();
  const browser = await chromium.launch();
  try {
    for (const theme of themes) {
      // History text has no replacement characters (list and detail, phone and desktop)
      for (const width of [390, 1280]) {
        const { page, context } = await fx.openApp(browser, server.baseUrl, { width, height: 800, theme, prints: fx.historyRecords(),
          api: url => /\/api\/prints\/\d+$/.test(url.pathname) ? { json: { ...fx.historyRecords()[0], job_events: [{ at: 1700000000, event: 'Started' }] } } : undefined });
        await page.evaluate(() => openHistory());
        await page.locator('.history-row').first().waitFor();
        await page.locator('.history-row').first().click();
        await page.locator('.history-detail').waitFor();
        const text = await page.locator('#panel-history').innerText();
        assert.ok(text.includes('part-1.gcode'), `History rendered (${theme} ${width})`);
        assert.ok(!text.includes('�'), `History text has a replacement character (${theme} ${width})`);
        await context.close();
      }

      // Start print in the Review job modal is inside the viewport
      for (const [width, height] of [[1280, 800], [390, 844]]) {
        const { page, context } = await fx.openApp(browser, server.baseUrl, { width, height, theme });
        await page.evaluate(j => openReview(j), fx.job({ printer_id: 'fx-1' }));
        const start = page.locator('#btn-review-start');
        await start.waitFor({ state: 'visible' });
        await page.locator('#review-check-list li').first().waitFor();
        const box = await start.boundingBox();
        assert.ok(box && box.x >= 0 && box.y >= 0 && box.x + box.width <= width && box.y + box.height <= height,
          `Start print outside the ${width}x${height} viewport (${theme}): ${JSON.stringify(box)}`);
        await context.close();
      }

      // Primary controls meet the hit-target minimum on a touch device
      for (const [width, height] of [[390, 844], [768, 1024]]) {
        const { page, context } = await fx.openApp(browser, server.baseUrl, { width, height, theme, hasTouch: true });
        await page.evaluate(j => openReview(j), fx.job({ printer_id: 'fx-1' }));
        await page.locator('#btn-review-start').waitFor({ state: 'visible' });
        const small = await page.evaluate(min => ['#btn-review-start', '#btn-review-recheck', '#modal-review .btn-primary', '#modal-review select']
          .flatMap(sel => [...document.querySelectorAll(sel)].filter(el => el.getClientRects().length).map(el => {
            const r = el.getBoundingClientRect();
            return { sel, w: Math.round(r.width), h: Math.round(r.height) };
          })).filter(m => m.h < min), HIT_MIN);
        assert.deepEqual(small, [], `Primary controls under ${HIT_MIN}px at ${width} (${theme})`);
        await context.close();
      }
    }
    // Fixture smoke: the states the critique could not reach render
    {
      const { page, context } = await fx.openApp(browser, server.baseUrl, { awaiting: [fx.awaitingOutcome()] });
      assert.equal(await page.locator('[id^=card-fx-]').count(), 6, 'six printer cards');
      await page.evaluate(() => OutcomePrompt.open('fx-1'));
      await page.getByRole('heading', { name: /finished Bracket/ }).waitFor();
      assert.equal(fx.items().length, 40);
      await context.close();
    }
    console.log('Regression guards PASS');
  } finally { await browser.close(); await server.close(); }
})().catch(err => { console.error(err); process.exitCode = 1; });
