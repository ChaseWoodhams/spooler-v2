// Assistant panel: a Proposal staged from a terminal session (no app run, no Ask) shows up as a card when the panel opens or polls,
// and Review changes… / Dismiss / Superseded work as for any card. Routes are stubbed; nothing real is called.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = require('playwright');

const PURPOSE = { default: { value: 'general', note: '' }, models: [] };
const BOX = (cx, cy, w, h) => [[cx - w / 2, cy - h / 2], [cx + w / 2, cy - h / 2], [cx + w / 2, cy + h / 2], [cx - w / 2, cy + h / 2]];
const card = (token, over = {}) => ({
  token, kind: 'orientation', project_id: 1, base_revision: 3, review: 'placement', title: 'Better orientation for 1 object', scope: '1 object on Plate 1',
  reason: 'From a terminal session', diff: [{ label: 'Bracket part 1', text: 'rotation 0, 0, 0 → 90, 0, 0 degrees' }], notes: [], pinned: [], problems: [], ...over,
});
const plate = (rotation, outline) => ({ id: 1, name: 'Plate 1', new: false, filament: null, turned: 0, time_estimate: null,
  objects: [{ id: 1, name: 'Bracket part 1', model: 11, position: [120, 120], rotation, pinned: false, outline }] });

async function open(browser, baseUrl, s) {
  const { page, context } = await sf.openSlicer(browser, baseUrl, { step: 'plates' });
  Object.assign(s, { listed: [], lists: 0, dismisses: [], reviews: [] });
  await page.route('**/api/projects/1/assistant/status**', route => route.fulfill({ json: { available: true, reason: null, purpose: PURPOSE } }));
  await page.route(/\/api\/projects\/1\/assistant\/proposals$/, route => { s.lists++; return route.fulfill({ json: { proposals: s.listed } }); });
  await page.route('**/api/projects/1/assistant/proposals/**', route => {
    const parts = new URL(route.request().url()).pathname.split('/');
    if (route.request().method() === 'GET') {
      s.reviews.push(parts.pop());
      const before = [plate([0, 0, 0], BOX(120, 120, 180, 60))], after = [plate([90, 0, 0], BOX(120, 120, 180, 30))];
      return route.fulfill({ json: { ...s.listed[0], revision: 3, summary: 'x', kept: [], refused: null, settings: null, bed: [0, 0, 256, 256],
        pack: { preset: 'normal', gap: 6 }, before: { plates: before }, after: { plates: after }, plates: after } });
    }
    s.dismisses.push(parts[parts.length - 2]);
    return route.fulfill({ json: { ok: true } });
  });
  await page.waitForSelector('.slicer-step.active[data-step="plates"]', { timeout: 10000 });
  return { page, context, panel: page.locator('#assistant-panel') };
}

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    const s = {};
    const { page, context, panel } = await open(browser, server.baseUrl, s);
    try {
      s.listed = [card('term1')];
      await page.locator('#btn-assistant').click();
      const el = panel.locator('.assistant-card[data-token="term1"]');
      await el.waitFor();
      assert.equal(await el.getAttribute('data-status'), 'pending');
      assert.match(await el.innerText(), /From a terminal session/);
      assert.equal(await el.getByRole('button', { name: 'Accept' }).count(), 0, 'Accept only happens in the review sheet');
      console.log('PASS: a terminal Proposal appears as a card when the panel opens, without any Ask');

      await el.getByRole('button', { name: 'Review changes…' }).click();
      await page.locator('#dlg-plan .proposal-pair').waitFor();
      assert.deepEqual(s.reviews, ['term1']);
      await page.locator('#dlg-plan').getByRole('button', { name: 'Cancel' }).click();

      // polling does not duplicate the card, and a Proposal staged later appears on its own
      const lists = s.lists;
      s.listed = [card('term1'), card('term2', { kind: 'arrange', title: 'Arrange plate 1', scope: 'Plate 1' })];
      await panel.locator('.assistant-card[data-token="term2"]').waitFor({ timeout: 12000 });
      assert.ok(s.lists > lists);
      assert.equal(await panel.locator('.assistant-card[data-token="term1"]').count(), 1);

      // Dismiss sweeps it and it does not come back while the list still names it
      await el.getByRole('button', { name: 'Dismiss' }).click();
      assert.deepEqual(s.dismisses, ['term1']);
      await page.waitForTimeout(100);
      assert.equal(await el.getAttribute('data-status'), 'dismissed');
      assert.equal(await panel.locator('.assistant-card[data-token="term1"]').count(), 1);

      // a Proposal made for another revision is Superseded rather than shown, and has no Ask again (no question to repeat)
      s.listed = [card('superseded', { base_revision: 1 })];
      await page.waitForTimeout(500);
      assert.equal(await panel.locator('.assistant-card[data-token="superseded"]').count(), 0);
      const other = panel.locator('.assistant-card[data-token="term2"]');
      await other.getByRole('button', { name: 'Dismiss' }).click();
      console.log('PASS: review hand-off, later arrivals, no duplicates, Dismiss, superseded ones not shown');
    } finally { await context.close(); }
  } finally {
    await browser.close();
    await server.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
