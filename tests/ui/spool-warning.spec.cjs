// The "No spool is assigned" warning is a labelled warning that says it does not block sending and links to spool assignment, once per printer.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = require('playwright');

const TEXT = 'No spool is assigned to Fixture printer 1 in Spoolman';
const plateFit = () => ({ printer_id: 'fx-1', printer: 'Fixture printer 1', warnings: [{ level: 'info', text: TEXT }, { level: 'info', text: TEXT }] });
const assignPrinter = project => { project.data.plates.forEach(p => { p.printer_id = 'fx-1'; }); };

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    for (const step of ['plates', 'slice']) {
      const { page, context } = await sf.openSlicer(browser, server.baseUrl, { step, plateFit, mutate: assignPrinter });
      try {
        await page.waitForSelector('#plate-fit .fit', { timeout: 10000 });
        const warnings = page.locator('#plate-fit .fit');
        assert.equal(await warnings.count(), 1, `${step}: shown once per printer`);
        const w = warnings.first();
        const text = await w.innerText();
        assert.ok(text.includes(TEXT), `${step}: keeps the server text`);
        assert.ok(/\bWarning\b/.test(text), `${step}: labelled with the word Warning`);
        assert.ok(await w.locator('svg, .fit-icon').count() > 0, `${step}: has an icon`);
        assert.ok(/does not block sending|doesn't block sending/i.test(text), `${step}: says it does not block sending`);
        const link = w.locator('a');
        assert.equal(await link.count(), 1, `${step}: one link`);
        assert.equal(await link.getAttribute('href'), '/#spools', `${step}: links to the Spools panel, where each spool offers Assign to <printer>`);
        assert.ok(/assign/i.test(await link.innerText()), `${step}: link says assign`);
        // Choosing another Plate on the same printer replaces the warning rather than adding a second one
        const nextPlate = step === 'plates' ? '.slicer-plate-tab[data-plate="2"]' : '[data-select-plate="2"]';
        const refreshed = page.waitForResponse(r => /\/plates\/2\/fit/.test(r.url()));
        await page.locator(nextPlate).click();
        await refreshed;
        await page.waitForFunction(() => document.querySelectorAll('#plate-fit .fit').length === 1);
        assert.ok((await page.locator('#plate-fit .fit').innerText()).includes(TEXT), `${step}: still shown after choosing another Plate`);
        assert.equal(await page.locator('#plate-fit .fit').count(), 1, `${step}: not repeated per Plate`);
        console.log(`PASS: ${step} step shows a labelled, actionable spool warning once`);
      } finally { await context.close(); }
    }
  } finally { await browser.close(); await server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
