// Assistant panel: Proposal cards. The card in the thread (title, scope, reason, diff, Pinned report, notes), "Review changes…"
// opening the Plan plates review sheet with placement before and after, Accept with one Undo (and an Undo button on the toast),
// Dismiss, Superseded with "Ask again", and a newer Proposal of the same kind replacing an older one. The status, ask, run and
// proposal routes are stubbed; nothing real is called.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = require('playwright');

const PURPOSE = { default: { value: 'general', note: '' }, models: [{ id: 11, name: 'Bracket part 1', value: 'general', note: '', source: 'default' }] };
const clone = value => JSON.parse(JSON.stringify(value));
const BOX = (cx, cy, w, h) => [[cx - w / 2, cy - h / 2], [cx + w / 2, cy - h / 2], [cx + w / 2, cy + h / 2], [cx - w / 2, cy + h / 2]];

const card = (token, over = {}) => ({
  token, kind: 'orientation', project_id: 1, base_revision: 3, review: 'placement',
  title: 'Better orientation for 1 object', scope: '1 object on Plate 1', reason: 'Lies <b>Bracket part 1</b> on its wide face',
  diff: [{ label: 'Bracket part 1', text: 'rotation 0, 0, 0 → 90, 0, 0 degrees; estimated support 12.3 → 0.4 cm³; height 60 → 30 mm' }],
  notes: ['Weighting: strength for 1 (from each Model\'s Purpose)'], pinned: [{ object: 2, label: 'Bracket part 2' }], problems: [], ...over,
});

const plate = (rotation, outline, turned) => ({ id: 1, name: 'Plate 1', new: false, filament: null, turned, time_estimate: null,
  objects: [{ id: 1, name: 'Bracket part 1', model: 11, position: [120, 120], rotation, pinned: false, outline }] });
const review = (c) => {
  const before = [plate([0, 0, 0], BOX(120, 120, 180, 60), 0)], after = [plate([90, 0, 0], BOX(120, 120, 180, 30), 1)];
  return { ...c, revision: c.base_revision, summary: c.scope, kept: [], refused: null, settings: null,
    bed: [0, 0, 256, 256], pack: { preset: 'normal', gap: 6 }, before: { plates: before }, after: { plates: after }, plates: after };
};

// Stubs the assistant routes. Every write the page makes is recorded in `s`.
async function stubAssistant(page, s) {
  Object.assign(s, { asks: [], accepts: [], dismisses: [], patches: [], reviews: [], revision: 3, run: null, acceptStatus: 200 });
  s.base = sf.makeProject();
  await page.route('**/api/projects/1/assistant/status**', route => route.fulfill({ json: { available: true, reason: null, purpose: PURPOSE, limits: { deadline_seconds: 60, question_chars: 2000, history_turns: 12, history_chars: 6000, turn_chars: 1500 } } }));
  await page.route('**/api/projects/1/assistant/ask', route => {
    s.asks.push(route.request().postDataJSON());
    return route.fulfill({ json: { id: `run${s.asks.length}`, project_id: 1, state: 'thinking', steps: [], answer: null, error: null, proposals: [] } });
  });
  await page.route('**/api/projects/1/assistant/runs/*', route => {
    const id = new URL(route.request().url()).pathname.split('/').pop();
    return route.fulfill({ json: { id, project_id: 1, steps: [{ label: 'Writing proposal', tool: 'propose_orientation' }], error: null, answer: 'I turned the bracket.', ...s.run } });
  });
  await page.route('**/api/projects/1/assistant/proposals/**', route => {
    const url = new URL(route.request().url());
    const [token, action] = url.pathname.split('/').slice(-action3(url.pathname));
    const request = route.request();
    if (request.method() === 'GET') { s.reviews.push(token); return route.fulfill({ json: review(s.cards[token]) }); }
    if (action === 'dismiss') { s.dismisses.push(token); return route.fulfill({ json: { ok: true } }); }
    s.accepts.push({ token, body: request.postDataJSON() });
    if (s.acceptStatus !== 200) return route.fulfill({ status: s.acceptStatus, json: { error: 'The project changed since this proposal was made, so it was not applied. Ask again', superseded: true } });
    const project = clone(s.base);
    project.revision = 4;
    project.data.objects[0].rotation = [90, 0, 0];
    s.revision = 4;
    return route.fulfill({ json: { ...project, proposal: s.cards[token] } });
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
const action3 = pathname => pathname.endsWith('/accept') || pathname.endsWith('/dismiss') ? 2 : 1;

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

async function cardAndReview(browser, baseUrl) {
  const s = {};
  const { page, context, panel } = await openPanel(browser, baseUrl, s);
  try {
    s.cards = { aaa: card('aaa') };
    s.run = { state: 'proposal', proposals: [s.cards.aaa] };
    await ask(panel, 'Make the bracket stronger');
    const cardEl = panel.locator('.assistant-card[data-token="aaa"]');
    await cardEl.waitFor();
    assert.match(await panel.locator('.assistant-msg.answer').innerText(), /I turned the bracket/, 'the answer text shows with the card');
    const text = await cardEl.innerText();
    for (const part of ['Better orientation for 1 object', '1 object on Plate 1', 'Lies <b>Bracket part 1</b> on its wide face',
      'rotation 0, 0, 0 → 90, 0, 0 degrees', 'Pinned, left alone: Bracket part 2', 'Weighting: strength']) assert.ok(text.includes(part), `card shows ${part}`);
    assert.equal(await cardEl.locator('b', { hasText: 'Bracket part 1' }).count() >= 1, true);
    assert.equal(await cardEl.locator('.assistant-reason b').count(), 0, 'the reason is text, never markup');
    assert.equal(await cardEl.getByRole('button', { name: 'Accept' }).count(), 0, 'Accept happens in the review sheet, not on the card');
    assert.deepEqual(s.asks[0], { message: 'Make the bracket stronger', history: [] });

    // Review changes… opens the Plan plates sheet with the plate before and after
    await cardEl.getByRole('button', { name: 'Review changes…' }).click();
    const sheet = page.locator('#dlg-plan');
    await sheet.locator('.proposal-pair').waitFor();
    assert.equal(await page.locator('#plan-title').innerText(), 'Better orientation for 1 object');
    assert.deepEqual(await sheet.locator('.plan-card').evaluateAll(els => els.map(e => e.dataset.side)), ['now', 'after']);
    assert.equal(await sheet.locator('.plan-card[data-side="now"] polygon').count(), 1);
    assert.equal(await sheet.locator('.plan-card[data-side="after"] polygon').count(), 1);
    const points = side => sheet.locator(`.plan-card[data-side="${side}"] polygon`).getAttribute('points');
    assert.notEqual(await points('now'), await points('after'), 'before and after draw differently');
    assert.match(await sheet.innerText(), /1 turned/);
    assert.equal(await sheet.getByRole('button', { name: 'Accept' }).isEnabled(), true);
    await sheet.getByRole('button', { name: 'Cancel' }).click();
    assert.equal(await sheet.evaluate(el => el.open), false);
    await page.waitForFunction(() => document.getElementById('plan-title').textContent === 'Plan plates');   // the sheet goes back to being the plan sheet
    assert.equal(s.accepts.length, 0, 'cancelling the review changes nothing');
    assert.equal(await cardEl.getAttribute('data-status'), 'pending');
    console.log('PASS: card contents and the Review changes… hand-off with before and after');
  } finally { await context.close(); }
}

async function acceptWithUndo(browser, baseUrl) {
  const s = {};
  const { page, context, panel } = await openPanel(browser, baseUrl, s);
  try {
    s.cards = { aaa: card('aaa'), bbb: card('bbb', { kind: 'arrange', title: 'Arrange plate 1', scope: 'Plate 1' }) };
    s.run = { state: 'proposal', proposals: [s.cards.aaa, s.cards.bbb] };
    await ask(panel, 'Orient and arrange');
    await panel.locator('.assistant-card[data-token="bbb"]').waitFor();
    await panel.locator('.assistant-card[data-token="aaa"]').getByRole('button', { name: 'Review changes…' }).click();
    await page.locator('#dlg-plan .proposal-pair').waitFor();
    await page.locator('#plan-accept').click();
    await panel.locator('.assistant-card[data-token="aaa"][data-status="accepted"]').waitFor();
    assert.deepEqual(s.accepts, [{ token: 'aaa', body: { revision: 3 } }], 'Accept sends the revision the page showed');
    assert.equal(await page.locator('#dlg-plan').evaluate(el => el.open), false, 'the sheet closes');
    const toast = page.locator('#toast-area .toast', { hasText: 'applied' });
    await toast.waitFor();
    assert.equal(await toast.locator('.toast-btn').innerText(), 'Undo');
    assert.match(await panel.locator('.assistant-card[data-token="aaa"]').innerText(), /Applied/);
    // the other card was made for revision 3, which is gone
    await panel.locator('.assistant-card[data-token="bbb"][data-status="superseded"]').waitFor();
    assert.equal(await panel.locator('.assistant-card[data-token="bbb"]').getByRole('button', { name: 'Ask again' }).isVisible(), true);
    assert.equal(await page.locator('#btn-undo').isEnabled(), true, 'one Undo step is on the stack');

    // Undo from the toast puts the prior data back (one save of the prior data)
    await toast.locator('.toast-btn').click();
    await page.waitForFunction(() => document.getElementById('save-state').textContent === 'Saved');
    assert.equal(s.patches.length, 1);
    assert.deepEqual(s.patches[0].data.objects[0].rotation, [0, 0, 0], 'the prior data is restored');
    assert.equal(s.patches[0].revision, 4);
    assert.equal(await page.locator('#btn-undo').isEnabled(), false, 'accepting was exactly one Undo step');
    console.log('PASS: Accept applies one revision with one Undo, the toast Undo restores the prior data');
  } finally { await context.close(); }
}

async function supersededAndAskAgain(browser, baseUrl) {
  const s = {};
  const { page, context, panel } = await openPanel(browser, baseUrl, s);
  try {
    s.cards = { aaa: card('aaa'), ccc: card('ccc', { title: 'Better orientation for 1 object (again)' }) };
    s.run = { state: 'proposal', proposals: [s.cards.aaa] };
    await ask(panel, 'Make the bracket stronger');
    await panel.locator('.assistant-card[data-token="aaa"]').waitFor();
    // the project changed before Accept: the server refuses with 409 and the card is Superseded
    s.acceptStatus = 409;
    await panel.locator('.assistant-card[data-token="aaa"]').getByRole('button', { name: 'Review changes…' }).click();
    await page.locator('#dlg-plan .proposal-pair').waitFor();
    await page.locator('#plan-accept').click();
    await page.locator('#plan-error', { hasText: 'changed since this proposal was made' }).waitFor();
    assert.equal(await page.locator('#plan-accept').isDisabled(), true, 'nothing to accept any more');
    await page.locator('#dlg-plan').getByRole('button', { name: 'Cancel' }).click();
    const superseded = panel.locator('.assistant-card[data-token="aaa"]');
    assert.equal(await superseded.getAttribute('data-status'), 'superseded');
    assert.match(await superseded.innerText(), /Superseded/);
    assert.match(await superseded.innerText(), /Nothing was changed/);
    assert.equal(await superseded.getByRole('button', { name: 'Review changes…' }).count(), 0, 'a Superseded card cannot be reviewed or accepted');
    assert.equal(await superseded.evaluate(el => getComputedStyle(el).opacity), '0.7', 'greyed');
    assert.equal(s.patches.length, 0, 'the page rebased nothing');

    // Ask again re-asks the original question
    s.run = { state: 'proposal', proposals: [s.cards.ccc] };
    await superseded.getByRole('button', { name: 'Ask again' }).click();
    await panel.locator('.assistant-card[data-token="ccc"]').waitFor();
    assert.equal(s.asks[1].message, 'Make the bracket stronger', 'the original question is asked once more');
    assert.match(s.asks[1].history.map(t => t.text).join('\n'), /Proposal "Better orientation for 1 object" .*superseded/, 'the history says the first one was superseded');
    // the newer card is live, the Superseded one is not
    assert.equal(await panel.locator('.assistant-card[data-token="ccc"]').getAttribute('data-status'), 'pending');
    assert.equal(await panel.locator('.assistant-card[data-token="aaa"]').getAttribute('data-status'), 'superseded');
    console.log('PASS: Accept refused (409) marks the card Superseded; Ask again re-asks the original question');
  } finally { await context.close(); }
}

async function dismissAndReplace(browser, baseUrl) {
  const s = {};
  const { page, context, panel } = await openPanel(browser, baseUrl, s);
  try {
    s.cards = { aaa: card('aaa'), ddd: card('ddd', { reason: 'A second look' }), eee: card('eee', { reason: 'Third' }) };
    s.run = { state: 'proposal', proposals: [s.cards.aaa] };
    await ask(panel, 'first');
    await panel.locator('.assistant-card[data-token="aaa"]').waitFor();
    // a newer Proposal of the same kind replaces the older one: only the latest of each kind is live
    s.run = { state: 'proposal', proposals: [s.cards.ddd] };
    await ask(panel, 'second');
    await panel.locator('.assistant-card[data-token="ddd"]').waitFor();
    assert.equal(await panel.locator('.assistant-card[data-token="aaa"]').getAttribute('data-status'), 'replaced');
    assert.equal(await panel.locator('.assistant-card[data-token="aaa"]').getByRole('button').count(), 0);
    assert.deepEqual(s.dismisses, ['aaa'], 'the replaced one is swept');
    // Dismiss changes nothing
    await panel.locator('.assistant-card[data-token="ddd"]').getByRole('button', { name: 'Dismiss' }).click();
    await panel.locator('.assistant-card[data-token="ddd"][data-status="dismissed"]').waitFor();
    assert.match(await panel.locator('.assistant-card[data-token="ddd"]').innerText(), /Nothing was changed/);
    assert.deepEqual(s.dismisses, ['aaa', 'ddd']);
    assert.equal(s.accepts.length + s.patches.length, 0);
    console.log('PASS: newer Proposal replaces an older one of its kind; Dismiss changes nothing');
  } finally { await context.close(); }
}

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    await cardAndReview(browser, server.baseUrl);
    await acceptWithUndo(browser, server.baseUrl);
    await supersededAndAskAgain(browser, server.baseUrl);
    await dismissAndReplace(browser, server.baseUrl);
  } finally {
    await browser.close();
    await server.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
