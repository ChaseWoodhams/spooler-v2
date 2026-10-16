// Settings > Assistant > Tuning > Limits: the fields, their bounds and defaults all come from the server's `tuning` section; unsaved edits are
// flagged; Save sends only what changed; a refused value shows the server's message and keeps what was typed; Reset limits restores the
// defaults; a note about a damaged file is shown. The route is stubbed (nothing real is called), like the other Assistant specs.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const { chromium } = require('playwright');

const FIELDS = {
  deadline_seconds: { label: 'Run deadline', unit: 'seconds', default: 60, min: 5, max: 600, hint: 'An Assistant run that has not answered by then is stopped.' },
  max_turns: { label: 'CLI turn cap', unit: 'turns', default: 12, min: 1, max: 50, hint: 'The most tool-using turns in one run.' },
};
const view = (values, notes = []) => ({
  cli_path: '', jev_url: 'http://127.0.0.1:3455', defaults: { cli_path: '', jev_url: 'http://127.0.0.1:3455' },
  tuning: { version: 1, values: { limits: values }, notes, groups: { limits: { label: 'Limits', hint: 'How long and how deep.', fields: FIELDS } } },
});

async function open(browser, baseUrl, s) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' });
  const page = await context.newPage();
  s.saved = [];
  await page.routeWebSocket(/.*/, () => {});
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/assistant/settings') {
      if (route.request().method() === 'GET') return route.fulfill({ json: view(s.values, s.notes) });
      const body = route.request().postDataJSON();
      s.saved.push(body);
      if (body.reset_tuning) { s.values = { deadline_seconds: 60, max_turns: 12 }; return route.fulfill({ json: view(s.values) }); }
      const limits = body.tuning?.limits || {};
      if (limits.deadline_seconds < 5) return route.fulfill({ status: 400, json: { error: 'Run deadline cannot be below 5 seconds (got 2)' } });
      s.values = { ...s.values, ...limits };
      return route.fulfill({ json: view(s.values) });
    }
    return route.fulfill({ json: url.pathname === '/api/printers' ? [] : {} });
  });
  await page.goto(baseUrl);
  await page.locator('#btn-app-settings').click();
  await page.locator('#btn-settings-goto-assistant').click();
  await page.locator('#assistant-tuning-limits-max_turns').waitFor();
  return { page, context };
}

async function limits(browser, baseUrl) {
  const s = { values: { deadline_seconds: 90, max_turns: 12 }, notes: ['Limits, CLI turn cap: the saved value is not allowed, so the default (12) is in use.'] };
  const { page, context } = await open(browser, baseUrl, s);
  try {
    const deadline = page.locator('#assistant-tuning-limits-deadline_seconds');
    assert.equal(await deadline.inputValue(), '90', 'the saved value is shown');
    assert.equal(await deadline.getAttribute('min'), '5');
    assert.equal(await deadline.getAttribute('max'), '600');
    assert.match(await page.locator('#assistant-tuning-limits-deadline_seconds-hint').innerText(), /Allowed 5 to 600; default 60/, 'bounds and default come from the server');
    assert.match(await page.locator('#assistant-tuning-notes').innerText(), /CLI turn cap: the saved value is not allowed/, 'a damaged-file note is shown');
    assert.equal(await page.locator('#assistant-tuning-dirty').isHidden(), true, 'nothing is flagged before an edit');

    await page.fill('#assistant-tuning-limits-max_turns', '20');
    assert.equal(await page.locator('#assistant-tuning-dirty').isVisible(), true, 'an unsaved edit is flagged');
    await page.fill('#assistant-tuning-limits-max_turns', '12');
    assert.equal(await page.locator('#assistant-tuning-dirty').isHidden(), true, 'putting the saved value back clears the flag');

    await page.fill('#assistant-tuning-limits-deadline_seconds', '2');
    await page.locator('#btn-assistant-save').click();
    await page.locator('#settings-feedback, .settings-feedback', { hasText: 'cannot be below 5' }).first().waitFor();
    assert.equal(await deadline.inputValue(), '2', 'a refused value stays as typed');
    assert.equal(await page.locator('#assistant-tuning-dirty').isVisible(), true);

    await page.fill('#assistant-tuning-limits-deadline_seconds', '120');
    await page.locator('#btn-assistant-save').click();
    await page.waitForFunction(() => document.getElementById('settings-assistant').style.display === 'none');
    assert.deepEqual(s.saved.at(-1).tuning, { limits: { deadline_seconds: 120 } }, 'only the changed value is sent');

    await page.locator('#btn-settings-goto-assistant').click();
    await page.locator('#assistant-tuning-limits-max_turns').waitFor();
    assert.equal(await deadline.inputValue(), '120');
    await page.getByRole('button', { name: 'Reset limits' }).click();
    await page.waitForFunction(() => document.getElementById('assistant-tuning-limits-deadline_seconds').value === '60');
    assert.deepEqual(s.saved.at(-1), { reset_tuning: 'limits' });
    assert.equal(await page.locator('#assistant-tuning-dirty').isHidden(), true);
    console.log('PASS: Assistant tuning Limits section');
  } finally { await context.close(); }
}

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    await limits(browser, server.baseUrl);
  } finally {
    await browser.close();
    await server.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
