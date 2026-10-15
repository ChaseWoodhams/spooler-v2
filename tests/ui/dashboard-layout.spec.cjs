// Dashboard layout: Active projects is reachable without scrolling past printer cards, and an offline printer is a
// compact row (name, reason, open action) while online printers keep the full card. Fixtures only.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');

const dashboard = { needs_you: { printers: [], prints: [] },
  projects: [{ id: 7, name: 'Bracket set', percent: 40, printed_ok: 2, total: 5 }] };
const api = url => url.pathname === '/api/dashboard' ? { json: dashboard } : undefined;

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch();
  try {
    for (const theme of ['dark', 'light']) {
      for (const width of [1280, 390]) {
        const { page, context } = await fx.openApp(browser, server.baseUrl, { width, height: 800, theme, printers: fx.printers(5), api });
        const projects = page.locator('#dash-projects');
        await projects.locator('.dash-project').waitFor();
        const tag = `${theme} ${width}`;

        // Active projects sits above the printer cards, so it is on screen with five printers
        const p = await projects.boundingBox();
        const firstCard = await page.locator('#card-fx-1').boundingBox();
        assert.ok(p.y + p.height <= 800, `Active projects is visible without scrolling (${tag}): bottom ${p.y + p.height}`);
        assert.ok(p.y < firstCard.y, `Active projects comes before the printer cards (${tag})`);

        // Offline printer: one compact row with name, reason and an open action
        const off = page.locator('#card-fx-5');
        const offBox = await off.boundingBox();
        const liveBox = await page.locator('#card-fx-1').boundingBox();
        assert.ok(offBox.height <= 96, `offline printer is a compact row (${tag}): ${offBox.height}px`);
        assert.ok(offBox.height < liveBox.height / 2, `offline row is much shorter than a live card (${tag})`);
        const text = await off.innerText();
        assert.ok(text.includes('Offline printer with a very long'), `offline row keeps the name (${tag})`);
        assert.ok(/offline/i.test(text), `offline row says why (${tag})`);
        assert.equal(await off.getByRole('button', { name: /open/i }).count() > 0, true, `offline row has an open action (${tag})`);

        // Online printers keep their card
        for (const id of ['fx-1', 'fx-2', 'fx-3', 'fx-4']) {
          assert.ok(await page.locator(`#card-${id} .card-temps`).count(), `online card ${id} keeps its full card (${tag})`);
        }

        // Nothing spills sideways
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        assert.ok(overflow <= 0, `no horizontal overflow (${tag}): ${overflow}px`);

        // The open action works
        await off.getByRole('button', { name: /open/i }).first().click();
        await page.waitForFunction(() => location.hash.startsWith('#printer=fx-5'));
        await context.close();
      }
    }
    console.log('dashboard-layout: ok');
  } finally { await browser.close(); await server.close(); }
})().catch(e => { console.error(e); process.exit(1); });
