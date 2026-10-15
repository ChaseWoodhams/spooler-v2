// Assistant panel: Arrange and Plan Proposals. Both go through the same card and review sheet as orientation. A plan folds three
// plates into one, so the sheet shows the plates that disappear ("Plate removed") and the one that gains the objects; Accept is one
// revision with one Undo step; both kinds can sit in the thread together (one per kind). The routes are stubbed.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = require('playwright');

const PURPOSE = { default: { value: 'general', note: '' }, models: [{ id: 11, name: 'Bracket part 1', value: 'general', note: '', source: 'default' }] };
const clone = value => JSON.parse(JSON.stringify(value));
const BOX = (cx, cy, w, h) => [[cx - w / 2, cy - h / 2], [cx + w / 2, cy - h / 2], [cx + w / 2, cy + h / 2], [cx - w / 2, cy + h / 2]];

const card = (token, kind, over = {}) => ({
  token, kind, project_id: 1, base_revision: 3, review: 'placement', reason: 'Packs the parts together',
  title: kind === 'plan' ? 'Plan 1 plate' : 'Arrange 3 objects', scope: '3 objects across 1 plate',
  diff: [{ label: 'Bracket part 2', text: 'plate 2 → 1; at 60, 60 → 150, 105 mm' }],
  notes: ['Plates 2, 3 would be emptied and removed'], pinned: [], problems: [], ...over,
});

const object = (id, at, plate) => ({ id, name: `Bracket part ${id}`, model: 11, position: at, rotation: [0, 0, 0], pinned: false, outline: BOX(at[0], at[1], 40, 40), plate });
const plate = (id, objects) => ({ id, name: `Plate ${id}`, new: false, filament: null, turned: 0, time_estimate: null, objects });
const review = c => {
  const before = [plate(1, [object(1, [60, 60])]), plate(2, [object(2, [60, 60])]), plate(3, [object(3, [60, 60])])];
  const after = [plate(1, [object(1, [60, 60]), object(2, [120, 60]), object(3, [180, 60])])];
  return { ...c, revision: c.base_revision, summary: c.scope, kept: [], refused: null, settings: null,
    bed: [0, 0, 256, 256], pack: { preset: 'normal', gap: 6 }, before: { plates: before }, after: { plates: after }, plates: after };
};

async function stub(page, s) {
  Object.assign(s, { asks: [], accepts: [], patches: [], reviews: [] });
  s.base = sf.makeProject();
  await page.route('**/api/projects/1/assistant/status**', route => route.fulfill({ json: { available: true, reason: null, purpose: PURPOSE } }));
  await page.route('**/api/projects/1/assistant/ask', route => {
    s.asks.push(route.request().postDataJSON());
    return route.fulfill({ json: { id: 'run1', project_id: 1, state: 'thinking', steps: [], answer: null, error: null, proposals: [] } });
  });
  await page.route('**/api/projects/1/assistant/runs/*', route => route.fulfill({ json: {
    id: 'run1', project_id: 1, steps: [{ label: 'Writing proposal', tool: 'propose_plan' }], error: null, answer: 'I planned the plates.', ...s.run } }));
  await page.route('**/api/projects/1/assistant/proposals/**', route => {
    const parts = new URL(route.request().url()).pathname.split('/');
    const last = parts[parts.length - 1];
    const action = last === 'accept' || last === 'dismiss' ? last : null;
    const token = action ? parts[parts.length - 2] : last;
    if (action === 'dismiss') return route.fulfill({ json: { ok: true } });
    if (route.request().method() === 'GET') { s.reviews.push(token); return route.fulfill({ json: review(s.cards[token]) }); }
    s.accepts.push({ token, body: route.request().postDataJSON() });
    const project = clone(s.base);
    project.revision = 4;
    project.data.objects[0].position = [150, 105];
    return route.fulfill({ json: { ...project, proposal: s.cards[token] } });
  });
  await page.route('**/api/projects/1', route => {
    if (route.request().method() !== 'PATCH') return route.fallback();
    const body = route.request().postDataJSON();
    s.patches.push(body);
    return route.fulfill({ json: { ...clone(s.base), data: body.data, revision: body.revision + 1 } });
  });
}

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  const s = {};
  const { page, context } = await sf.openSlicer(browser, server.baseUrl, { step: 'plates' });
  try {
    await stub(page, s);
    await page.waitForSelector('.slicer-step.active[data-step="plates"]', { timeout: 10000 });
    await page.locator('#btn-assistant').click();
    const panel = page.locator('#assistant-panel');
    await panel.locator('.assistant-empty').waitFor();

    s.cards = { ppp: card('ppp', 'plan'), aaa: card('aaa', 'arrange') };
    s.run = { state: 'proposal', proposals: [s.cards.ppp, s.cards.aaa] };
    await panel.locator('.assistant-input').fill('Plan and arrange everything');
    await panel.getByRole('button', { name: 'Send' }).click();
    await panel.locator('.assistant-card[data-token="ppp"]').waitFor();
    assert.equal(await panel.locator('.assistant-card').count(), 2, 'one card per kind');
    const text = await panel.locator('.assistant-card[data-token="ppp"]').innerText();
    for (const part of ['Plan 1 plate', '3 objects across 1 plate', 'plate 2 → 1', 'Plates 2, 3 would be emptied and removed']) assert.ok(text.includes(part), `plan card shows ${part}`);

    // Arrange: review shows the three plates now and after; then cancel
    await panel.locator('.assistant-card[data-token="aaa"]').getByRole('button', { name: 'Review changes…' }).click();
    const sheet = page.locator('#dlg-plan');
    await sheet.locator('.proposal-pair').first().waitFor();
    assert.equal(await page.locator('#plan-title').innerText(), 'Arrange 3 objects');
    await sheet.getByRole('button', { name: 'Cancel' }).click();

    // Plan: three plates fold into one; the removed plates show only on the Now side
    await panel.locator('.assistant-card[data-token="ppp"]').getByRole('button', { name: 'Review changes…' }).click();
    await sheet.locator('.proposal-pair').first().waitFor();
    assert.equal(await sheet.locator('.proposal-pair').count(), 3);
    assert.equal(await sheet.locator('.plan-card[data-side="now"] polygon').count(), 3);
    assert.equal(await sheet.locator('.plan-card[data-side="after"] polygon').count(), 3);
    assert.equal(await sheet.locator('.plan-card[data-side="after"]', { hasText: 'Plate removed' }).count(), 2);
    assert.equal(s.accepts.length, 0, 'reviewing changes nothing');

    // Accept: one revision, one Undo step, the toast Undo puts the prior data back
    await page.locator('#plan-accept').click();
    await panel.locator('.assistant-card[data-token="ppp"][data-status="accepted"]').waitFor();
    assert.deepEqual(s.accepts, [{ token: 'ppp', body: { revision: 3 } }]);
    const toast = page.locator('#toast-area .toast', { hasText: 'applied' });
    await toast.waitFor();
    await panel.locator('.assistant-card[data-token="aaa"][data-status="superseded"]').waitFor();
    assert.equal(await page.locator('#btn-undo').isEnabled(), true);
    await toast.locator('.toast-btn').click();
    await page.waitForFunction(() => document.getElementById('save-state').textContent === 'Saved');
    assert.equal(s.patches.length, 1);
    assert.equal(s.patches[0].revision, 4);
    assert.equal(await page.locator('#btn-undo').isEnabled(), false, 'accepting was exactly one Undo step');
    console.log('PASS: plan and arrange cards, plan review across plates, Accept with one Undo, the other card Superseded');
  } finally {
    await context.close();
    await browser.close();
    await server.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
