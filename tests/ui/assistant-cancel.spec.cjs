// Assistant panel: Cancel, the 60 s deadline notice, usage limit and other failures. The assistant routes are stubbed.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = require('playwright');

const PURPOSE = { default: { value: 'general', note: '' }, models: [] };
const STEP = [{ label: 'Reading project', tool: 'get_project' }];

async function open(browser, baseUrl) {
  const s = { asks: [], cancels: 0, hold: true, final: null, cancelView: null };
  const { page, context } = await sf.openSlicer(browser, baseUrl, { step: 'plates' });
  await page.route('**/api/projects/1/assistant/status**', route => route.fulfill({ json: { available: true, reason: null, purpose: PURPOSE } }));
  await page.route('**/api/projects/1/assistant/ask', route => {
    s.asks.push(route.request().postDataJSON());
    return route.fulfill({ json: { id: `run${s.asks.length}`, project_id: 1, state: 'thinking', steps: [], answer: null, error: null } });
  });
  await page.route('**/api/projects/1/assistant/runs/*/cancel', route => {
    s.cancels += 1;
    return route.fulfill({ json: { id: 'run1', project_id: 1, steps: STEP, answer: null, ...s.cancelView } });
  });
  await page.route('**/api/projects/1/assistant/runs/*', route => {
    const id = new URL(route.request().url()).pathname.split('/').pop();
    if (s.hold) return route.fulfill({ json: { id, project_id: 1, state: 'thinking', steps: STEP, answer: null, error: null } });
    return route.fulfill({ json: { id, project_id: 1, steps: STEP, answer: null, error: null, ...s.final } });
  });
  await page.waitForSelector('.slicer-step.active[data-step="plates"]', { timeout: 10000 });
  await page.locator('#btn-assistant').click();
  const panel = page.locator('#assistant-panel');
  await panel.locator('.assistant-empty').waitFor();
  return { page, context, panel, s };
}

async function ask(panel, text) {
  await panel.locator('.assistant-input').fill(text);
  await panel.getByRole('button', { name: 'Send' }).click();
}

async function cancelFlow(browser, baseUrl) {
  const { context, panel, s } = await open(browser, baseUrl);
  try {
    await ask(panel, 'Why is this plate slow?');
    const cancel = panel.getByRole('button', { name: 'Cancel' });
    await cancel.waitFor();
    assert.match(await panel.locator('.assistant-notice').innerText(), /Stops by itself after 60 s/);
    s.cancelView = { state: 'cancelled', error: 'Cancelled; nothing was changed' };
    await cancel.click();
    const notice = panel.locator('[data-end="cancelled"]');
    await notice.waitFor();
    assert.match(await notice.innerText(), /Cancelled; nothing was changed/);
    assert.equal(s.cancels, 1);
    assert.equal(await panel.locator('.assistant-spin').count(), 0, 'no spinner after Cancel');
    assert.equal(await panel.getByRole('button', { name: 'Cancel' }).count(), 0);
    // Try again asks the same question once more, without the cancelled attempt in the thread or the history
    s.hold = false;
    s.final = { state: 'answer', answer: 'Because of supports.', error: null };
    await notice.getByRole('button', { name: 'Try again' }).click();
    await panel.locator('.assistant-msg.answer').waitFor();
    assert.deepEqual(s.asks[1], { message: 'Why is this plate slow?', history: [] });
    assert.equal(await panel.locator('.assistant-msg.me').count(), 1, 'one copy of the question');
    assert.equal(await panel.locator('[data-end]').count(), 0);
    console.log('PASS: Cancel while thinking, notice, Try again');
  } finally { await context.close(); }
}

async function endStates(browser, baseUrl) {
  const { context, panel, s } = await open(browser, baseUrl);
  try {
    s.hold = false;
    const cases = [
      ['timeout', 'No answer in 60 s; nothing was changed', /No answer in 60 s; nothing was changed/],
      ['usage_limit', 'AI unavailable: usage limit reached', /AI unavailable: usage limit reached/],
      ['error', 'The assistant stopped unexpectedly (exit code 3).', /stopped unexpectedly[\s\S]*Nothing was changed/],
    ];
    for (const [state, error, pattern] of cases) {
      s.final = { state, answer: null, error };
      await ask(panel, `question for ${state}`);
      const notice = panel.locator(`[data-end="${state}"]`);
      await notice.waitFor();
      assert.match(await notice.innerText(), pattern);
      assert.equal(await notice.getByRole('button', { name: 'Try again' }).count(), 1, `${state} has Try again`);
      assert.equal(await panel.locator('.assistant-spin').count(), 0, `${state}: no spinner`);
      await panel.getByRole('button', { name: 'Clear' }).click();
    }
    console.log('PASS: timeout, usage limit and error notices with Try again');
  } finally { await context.close(); }
}

async function neverASpinnerForever(browser, baseUrl) {
  const { page, context, panel, s } = await open(browser, baseUrl);
  try {
    await page.route('**/api/projects/1/assistant/runs/*', route => route.abort());      // the connection drops mid-run
    await ask(panel, 'hello');
    const notice = panel.locator('[data-end="error"]');
    await notice.waitFor({ timeout: 40000 });
    assert.match(await notice.innerText(), /Lost contact/);
    assert.equal(await panel.locator('.assistant-spin').count(), 0);
    assert.equal(s.asks.length, 1);
    console.log('PASS: a dropped connection ends as an error, not a spinner');
  } finally { await context.close(); }
}

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    await cancelFlow(browser, server.baseUrl);
    await endStates(browser, server.baseUrl);
    await neverASpinnerForever(browser, server.baseUrl);
  } finally { await browser.close(); await server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
