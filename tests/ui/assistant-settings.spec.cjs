// Assistant panel: a settings Proposal. The card shows `key: old → new` with the layer of the old value, "Review changes…" opens the
// Plan plates review sheet with a table of Setting, Now, After and Layer (and a note under a change that would have no effect, and
// what was not proposed), Accept applies one revision with one Undo. Routes are stubbed; nothing real is called.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = require('playwright');

const PURPOSE = { default: { value: 'functional', note: '' }, models: [{ id: 11, name: 'Bracket part 1', value: 'functional', note: '', source: 'default' }] };
const clone = value => JSON.parse(JSON.stringify(value));

const rows = [
  { key: 'wall_loops', label: 'Wall loops', old: '2', new: '5', layer: 'preset', notes: [] },
  { key: 'sparse_infill_density', label: 'Sparse infill density', old: '15%', new: '40%', layer: 'project', notes: [] },
  { key: 'support_threshold_angle', label: 'Threshold angle', old: '30', new: '45', layer: 'plate',
    notes: ['No effect because supports are off; support_threshold_angle is greyed out by enable_support=0'] },
];
const card = (token, over = {}) => ({
  token, kind: 'settings', project_id: 1, base_revision: 3, review: 'settings', title: 'Change 3 settings for the project',
  scope: 'Whole project', reason: 'Stronger <i>arm</i> brackets',
  diff: rows.map(r => ({ label: `${r.key}:`, text: `${r.old} → ${r.new} (was ${{ preset: "the profile's value", project: 'set for the project', plate: 'set on the plate' }[r.layer]})`, key: r.key, layer: r.layer })),
  notes: ['support_threshold_angle: no effect because supports are off'], pinned: [], problems: ['Not proposed: nozzle_temperature: nozzle_temperature is a temperature. Temperatures come from the printer and filament profile and are never proposed'], ...over,
});
const review = c => ({ ...c, revision: c.base_revision, summary: c.scope, kept: [], refused: null, settings: rows });

async function stubAssistant(page, s) {
  Object.assign(s, { asks: [], accepts: [], dismisses: [], patches: [], reviews: [], acceptStatus: 200 });
  s.base = sf.makeProject();
  await page.route('**/api/projects/1/assistant/status**', route => route.fulfill({ json: { available: true, reason: null, purpose: PURPOSE } }));
  await page.route('**/api/projects/1/assistant/ask', route => {
    s.asks.push(route.request().postDataJSON());
    return route.fulfill({ json: { id: 'run1', project_id: 1, state: 'thinking', steps: [], answer: null, error: null, proposals: [] } });
  });
  await page.route('**/api/projects/1/assistant/runs/*', route => route.fulfill({
    json: { id: 'run1', project_id: 1, state: 'proposal', steps: [{ label: 'Writing proposal', tool: 'propose_settings' }], error: null, answer: 'I proposed stronger settings.', proposals: [s.card] } }));
  await page.route('**/api/projects/1/assistant/proposals/**', route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const token = path.split('/').slice(-(path.endsWith('/accept') || path.endsWith('/dismiss') ? 2 : 1))[0];
    if (request.method() === 'GET') { s.reviews.push(token); return route.fulfill({ json: review(s.card) }); }
    if (path.endsWith('/dismiss')) { s.dismisses.push(token); return route.fulfill({ json: { ok: true } }); }
    s.accepts.push({ token, body: request.postDataJSON() });
    if (s.acceptStatus !== 200) return route.fulfill({ status: s.acceptStatus, json: { error: 'The project changed since this proposal was made, so it was not applied. Ask again', superseded: true } });
    const project = clone(s.base);
    project.revision = 4;
    project.data.profile.process = { wall_loops: '5', sparse_infill_density: '40%' };
    return route.fulfill({ json: { ...project, proposal: s.card } });
  });
  await page.route('**/api/projects/1', route => {
    if (route.request().method() !== 'PATCH') return route.fallback();
    const body = route.request().postDataJSON();
    s.patches.push(body);
    const project = clone(s.base);
    project.data = body.data;
    project.revision = body.revision + 1;
    return route.fulfill({ json: project });
  });
}

async function openPanel(browser, baseUrl, s) {
  const { page, context } = await sf.openSlicer(browser, baseUrl, { step: 'plates' });
  await stubAssistant(page, s);
  await page.waitForSelector('.slicer-step.active[data-step="plates"]', { timeout: 10000 });
  await page.locator('#btn-assistant').click();
  const panel = page.locator('#assistant-panel');
  await panel.locator('.assistant-empty').waitFor();
  return { page, context, panel };
}

async function ask(panel, text) {
  await panel.locator('.assistant-input').fill(text);
  await panel.getByRole('button', { name: 'Send' }).click();
}

async function cardAndTable(browser, baseUrl) {
  const s = { card: card('sss') };
  const { page, context, panel } = await openPanel(browser, baseUrl, s);
  try {
    await ask(panel, 'Make the arm brackets stronger');
    const cardEl = panel.locator('.assistant-card[data-token="sss"]');
    await cardEl.waitFor();
    const lines = await cardEl.locator('.assistant-diff li').allInnerTexts();
    assert.deepEqual(lines.map(l => l.replace(/\s+/g, ' ')), [
      "wall_loops: 2 → 5 (was the profile's value)", 'sparse_infill_density: 15% → 40% (was set for the project)', 'support_threshold_angle: 30 → 45 (was set on the plate)']);
    assert.equal(await cardEl.locator('.assistant-reason i').count(), 0, 'the reason is text, never markup');
    assert.match(await cardEl.innerText(), /Not proposed: nozzle_temperature/, 'a refused key is reported on the card');
    assert.match(await cardEl.innerText(), /no effect because supports are off/);

    await cardEl.getByRole('button', { name: 'Review changes…' }).click();
    const sheet = page.locator('#dlg-plan');
    await sheet.locator('table[data-table="settings"]').waitFor();
    assert.equal(await page.locator('#plan-title').innerText(), 'Change 3 settings for the project');
    assert.deepEqual(await sheet.locator('thead th').allInnerTexts(), ['Setting', 'Now', 'After', 'Layer']);
    const body = await sheet.locator('tbody tr[data-key]').evaluateAll(trs => trs.map(tr => [...tr.children].map(td => td.innerText.replace(/\s+/g, ' ').trim())));
    assert.deepEqual(body, [
      ['Wall loops wall_loops', '2', '5', 'Profile'],
      ['Sparse infill density sparse_infill_density', '15%', '40%', 'Project'],
      ['Threshold angle support_threshold_angle', '30', '45', 'Plate']]);
    assert.equal(await sheet.locator('tbody tr[data-key="wall_loops"] td:nth-child(3) b').count(), 1, 'the new value is emphasised');
    const note = sheet.locator('tr.proposal-note');
    assert.equal(await note.count(), 1, 'only the inert change has a note');
    assert.match(await note.innerText(), /No effect because supports are off/);
    assert.match(await sheet.innerText(), /Not proposed: nozzle_temperature/, 'the refusal is on the sheet too');
    assert.equal(await sheet.locator('.assistant-diff').count(), 0, 'the table is not repeated as a list');
    assert.equal(await sheet.locator('.proposal-pair').count(), 0, 'no placement pictures for a settings Proposal');
    assert.equal(await sheet.getByRole('button', { name: 'Accept' }).isEnabled(), true);
    await sheet.getByRole('button', { name: 'Cancel' }).click();
    assert.equal(s.accepts.length, 0, 'cancelling the review changes nothing');
    console.log('PASS: settings card (key: old → new, layer) and the review sheet table with a note and the refusal');
  } finally { await context.close(); }
}

async function acceptWithUndo(browser, baseUrl) {
  const s = { card: card('sss') };
  const { page, context, panel } = await openPanel(browser, baseUrl, s);
  try {
    await ask(panel, 'Make the arm brackets stronger');
    await panel.locator('.assistant-card[data-token="sss"]').getByRole('button', { name: 'Review changes…' }).click();
    await page.locator('#dlg-plan table[data-table="settings"]').waitFor();
    await page.locator('#plan-accept').click();
    await panel.locator('.assistant-card[data-token="sss"][data-status="accepted"]').waitFor();
    assert.deepEqual(s.accepts, [{ token: 'sss', body: { revision: 3 } }]);
    assert.equal(await page.locator('#dlg-plan').evaluate(el => el.open), false);
    const toast = page.locator('#toast-area .toast', { hasText: 'applied' });
    await toast.waitFor();
    assert.equal(await page.locator('#btn-undo').isEnabled(), true, 'one Undo step is on the stack');
    await toast.locator('.toast-btn').click();
    await page.waitForFunction(() => document.getElementById('save-state').textContent === 'Saved');
    assert.equal(s.patches.length, 1);
    assert.deepEqual(s.patches[0].data.profile.process, {}, 'the prior settings are restored');
    assert.equal(await page.locator('#btn-undo').isEnabled(), false, 'accepting was exactly one Undo step');
    console.log('PASS: Accept of a settings Proposal is one revision with one Undo');
  } finally { await context.close(); }
}

async function supersededAtAccept(browser, baseUrl) {
  const s = { card: card('sss'), acceptStatus: 409 };
  const { page, context, panel } = await openPanel(browser, baseUrl, s);
  try {
    s.acceptStatus = 409;
    await ask(panel, 'Make the arm brackets stronger');
    await panel.locator('.assistant-card[data-token="sss"]').getByRole('button', { name: 'Review changes…' }).click();
    await page.locator('#dlg-plan table[data-table="settings"]').waitFor();
    await page.locator('#plan-accept').click();
    await page.locator('#plan-error', { hasText: 'changed since this proposal was made' }).waitFor();
    assert.equal(await page.locator('#plan-accept').isDisabled(), true);
    await page.locator('#dlg-plan').getByRole('button', { name: 'Cancel' }).click();
    assert.equal(await panel.locator('.assistant-card[data-token="sss"]').getAttribute('data-status'), 'superseded');
    assert.equal(s.patches.length, 0);
    console.log('PASS: a 409 at Accept marks the settings Proposal Superseded and changes nothing');
  } finally { await context.close(); }
}

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    await cardAndTable(browser, server.baseUrl);
    await acceptWithUndo(browser, server.baseUrl);
    await supersededAtAccept(browser, server.baseUrl);
  } finally {
    await browser.close();
    await server.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
