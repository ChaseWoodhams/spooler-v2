// Assistant panel: asking. Send, the thinking state with progress steps, the plain-text answer, history sent with each ask
// (client-held and capped), Clear, and a refused ask. The status, ask and run routes are stubbed; nothing real is called.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = require('playwright');

const PURPOSE = { default: { value: 'general', note: '' }, models: [{ id: 11, name: 'Bracket part 1', value: 'general', note: '', source: 'default' }] };

// Stubs the assistant routes. `hold`: keep every run thinking until the test sets hold = false.
async function stubAssistant(page, s) {
  s.asks = []; s.polls = 0; s.hold = false; s.refuse = null; s.final = { state: 'answer', answer: 'Plate 1 holds both brackets.', error: null };
  await page.route('**/api/projects/1/assistant/status**', route => route.fulfill({ json: { available: true, reason: null, purpose: PURPOSE } }));
  await page.route('**/api/projects/1/assistant/ask', route => {
    s.asks.push(route.request().postDataJSON());
    if (s.refuse) return route.fulfill({ status: s.refuse.status, json: { error: s.refuse.error } });
    return route.fulfill({ json: { id: `run${s.asks.length}`, project_id: 1, state: 'thinking', steps: [], answer: null, error: null } });
  });
  await page.route('**/api/projects/1/assistant/runs/*', route => {
    s.polls += 1;
    const id = new URL(route.request().url()).pathname.split('/').pop();
    if (s.hold) return route.fulfill({ json: { id, project_id: 1, state: 'thinking', steps: [{ label: 'Reading project', tool: 'get_project' }], answer: null, error: null } });
    return route.fulfill({ json: { id, project_id: 1, steps: [{ label: 'Reading project', tool: 'get_project' }], answer: null, error: null, ...s.final } });
  });
}

async function openPanel(browser, baseUrl, s, opts = {}) {
  const { page, context } = await sf.openSlicer(browser, baseUrl, { step: 'plates', ...opts });
  await stubAssistant(page, s);
  await page.waitForSelector('.slicer-step.active[data-step="plates"]', { timeout: 10000 });
  await page.locator('#btn-assistant').click();
  const panel = page.locator('#assistant-panel');
  await panel.locator('.assistant-empty').waitFor();
  return { page, context, panel };
}

async function askAndAnswer(browser, baseUrl) {
  const s = {};
  const { page, context, panel } = await openPanel(browser, baseUrl, s);
  try {
    const send = panel.getByRole('button', { name: 'Send' });
    const input = panel.locator('.assistant-input');
    assert.equal(await send.isDisabled(), true, 'nothing to send yet');
    await input.fill('Make the arm brackets <b>stronger</b>');
    assert.equal(await send.isDisabled(), false);

    // thinking: progress steps, Send disabled, the question is in the thread
    s.hold = true;
    await send.click();
    await panel.locator('.assistant-steps', { hasText: 'Reading project' }).waitFor();
    assert.match(await panel.locator('.assistant-steps').innerText(), /Reading project/);
    assert.equal(await send.isDisabled(), true, 'Send is disabled while a run is working');
    await input.fill('a second question');
    assert.equal(await send.isDisabled(), true, 'typing does not re-enable Send while working');
    assert.equal(await panel.locator('.assistant-msg.me').innerText(), 'Make the arm brackets <b>stronger</b>', 'the question shows as plain text');
    assert.equal(await panel.locator('.assistant-empty').count(), 0, 'the examples go once the thread starts');
    assert.deepEqual(s.asks[0], { message: 'Make the arm brackets <b>stronger</b>', history: [] });
    assert.equal(await input.inputValue(), 'a second question', 'polling never steals the draft');
    assert.equal(await panel.getByRole('button', { name: 'Clear' }).isDisabled(), true, 'Clear waits for the run');

    // the answer: plain text, escaped, steps and spinner gone, Send back
    s.final = { state: 'answer', answer: 'Both fit on <i>Plate 1</i>.\nSecond line.', error: null };
    s.hold = false;
    await panel.locator('.assistant-msg.answer').waitFor();
    assert.equal(await panel.locator('.assistant-msg.answer').innerText(), 'Both fit on <i>Plate 1</i>.\nSecond line.');
    assert.equal(await panel.locator('.assistant-steps').count(), 0);
    assert.equal(await send.isDisabled(), false, 'the typed follow-up can be sent');

    // a follow-up carries what was said on this visit
    await send.click();
    await panel.locator('.assistant-msg.answer').nth(1).waitFor();
    assert.deepEqual(s.asks[1], { message: 'a second question', history: [
      { role: 'user', text: 'Make the arm brackets <b>stronger</b>' }, { role: 'assistant', text: 'Both fit on <i>Plate 1</i>.\nSecond line.' }] });

    // nothing is stored: no thread in browser storage
    const stored = await page.evaluate(() => JSON.stringify([{ ...localStorage }, { ...sessionStorage }]));
    assert.doesNotMatch(stored, /stronger|Plate 1/, 'the conversation is not kept');

    // Clear empties the thread and the next ask sends no history
    await panel.getByRole('button', { name: 'Clear' }).click();
    await panel.locator('.assistant-empty').waitFor();
    assert.equal(await panel.locator('.assistant-msg').count(), 0);
    await input.fill('fresh start');
    await send.click();
    await panel.locator('.assistant-msg.answer').waitFor();
    assert.deepEqual(s.asks[2], { message: 'fresh start', history: [] });
    console.log('PASS: ask, thinking steps, answer, follow-up history and Clear');
  } finally { await context.close(); }
}

async function historyCap(browser, baseUrl) {
  const s = {};
  const { context, panel } = await openPanel(browser, baseUrl, s);
  try {
    const send = panel.getByRole('button', { name: 'Send' });
    for (let n = 1; n <= 9; n++) {
      s.final = { state: 'answer', answer: `answer ${n}`, error: null };
      await panel.locator('.assistant-input').fill(`question ${n}`);
      await send.click();
      await panel.locator('.assistant-msg.answer', { hasText: `answer ${n}` }).waitFor();
    }
    await panel.locator('.assistant-input').fill('question 10');
    await send.click();
    await panel.locator('.assistant-msg.me', { hasText: 'question 10' }).waitFor();
    const history = s.asks.at(-1).history;
    assert.ok(history.length <= 12, `history is capped (${history.length})`);
    assert.equal(history.at(-1).text, 'answer 9', 'the newest turns are kept');
    assert.ok(!history.some(t => t.text === 'question 1'), 'the oldest are dropped first');
    assert.equal(s.asks.at(-1).message, 'question 10');
    console.log('PASS: history sent with each ask is capped, oldest dropped first');
  } finally { await context.close(); }
}

async function failures(browser, baseUrl) {
  const s = {};
  const { context, panel } = await openPanel(browser, baseUrl, s);
  try {
    const send = panel.getByRole('button', { name: 'Send' });
    const input = panel.locator('.assistant-input');
    // a refused ask (another run on the project) says so, keeps the text and adds nothing to the thread
    s.refuse = { status: 409, error: 'The assistant is already working on this project' };
    await input.fill('please help');
    await send.click();
    await panel.getByText('already working on this project').waitFor();
    assert.equal(await input.inputValue(), 'please help', 'the question is still there to send again');
    assert.equal(await panel.locator('.assistant-msg.me').count(), 0);
    assert.equal(await send.isDisabled(), false);

    // a failed run reads as an error, not a spinner, and the next ask does not send the failure as history
    s.refuse = null;
    s.final = { state: 'error', answer: null, error: 'The assistant stopped unexpectedly (exit code 3).' };
    await send.click();
    await panel.locator('.assistant-notice', { hasText: 'stopped unexpectedly' }).waitFor();
    assert.equal(await panel.locator('.assistant-steps').count(), 0);
    assert.equal(await send.isDisabled(), true, 'the composer is empty again');
    s.final = { state: 'answer', answer: 'ok now', error: null };
    await input.fill('try again');
    await send.click();
    await panel.locator('.assistant-msg.answer', { hasText: 'ok now' }).waitFor();
    assert.deepEqual(s.asks.at(-1).history, [{ role: 'user', text: 'please help' }], 'only turns that were said, not failures');
    console.log('PASS: refused ask and failed run');
  } finally { await context.close(); }
}

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    await askAndAnswer(browser, server.baseUrl);
    await historyCap(browser, server.baseUrl);
    await failures(browser, server.baseUrl);
  } finally { await browser.close(); await server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
