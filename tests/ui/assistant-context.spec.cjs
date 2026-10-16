// Settings > Assistant > Tuning > Prompt context: switches are checkboxes drawn from the server's field list, Save sends only the changed
// ones as booleans, the number fields keep their bounds, and Reset prompt context restores the defaults. The route is stubbed.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const { chromium } = require('playwright');

const FIELDS = {
  send_purposes: { label: 'Model Purposes', type: 'bool', default: true, hint: 'Send each Model Purpose.' },
  history_enabled: { label: 'Chat history', type: 'bool', default: true, hint: 'Send earlier turns.' },
  history_turns: { label: 'History turns kept', type: 'int', unit: 'turns', default: 12, min: 1, max: 50, hint: 'Oldest dropped first.' },
};
const DEFAULTS = { send_purposes: true, history_enabled: true, history_turns: 12 };
const view = values => ({
  cli_path: '', jev_url: 'http://127.0.0.1:3455', defaults: { cli_path: '', jev_url: 'http://127.0.0.1:3455' },
  tuning: { version: 1, values: { prompt_context: values }, notes: [], groups: { prompt_context: { label: 'Prompt context', hint: 'What each question sends.', fields: FIELDS } } },
});

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  const s = { values: { ...DEFAULTS, send_purposes: false }, saved: [] };
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' });
    const page = await context.newPage();
    await page.routeWebSocket(/.*/, () => {});
    await page.route('**/api/**', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/assistant/settings') {
        if (route.request().method() === 'GET') return route.fulfill({ json: view(s.values) });
        const body = route.request().postDataJSON();
        s.saved.push(body);
        s.values = body.reset_tuning ? { ...DEFAULTS } : { ...s.values, ...(body.tuning?.prompt_context || {}) };
        return route.fulfill({ json: view(s.values) });
      }
      return route.fulfill({ json: url.pathname === '/api/printers' ? [] : {} });
    });
    await page.goto(server.baseUrl);
    await page.locator('#btn-app-settings').click();
    await page.locator('#btn-settings-goto-assistant').click();
    await page.locator('#assistant-tuning-prompt_context-history_turns').waitFor();
    const purposes = page.locator('#assistant-tuning-prompt_context-send_purposes');
    assert.equal(await purposes.getAttribute('type'), 'checkbox');
    assert.equal(await purposes.isChecked(), false, 'the saved value is shown');
    assert.equal(await page.locator('#assistant-tuning-prompt_context-history_enabled').isChecked(), true);
    assert.match(await page.locator('#assistant-tuning-prompt_context-send_purposes-hint').innerText(), /Default on/);
    assert.equal(await page.locator('#assistant-tuning-dirty').isHidden(), true);
    await purposes.check();
    assert.equal(await page.locator('#assistant-tuning-dirty').isVisible(), true, 'a toggle is flagged as unsaved');
    await purposes.uncheck();
    assert.equal(await page.locator('#assistant-tuning-dirty').isHidden(), true);
    await page.locator('#assistant-tuning-prompt_context-history_enabled').uncheck();
    await page.locator('#btn-assistant-save').click();
    await page.waitForFunction(() => document.getElementById('settings-assistant').style.display === 'none');
    assert.deepEqual(s.saved.at(-1).tuning, { prompt_context: { history_enabled: false } }, 'only the changed toggle is sent, as a boolean');
    await page.locator('#btn-settings-goto-assistant').click();
    await page.locator('#assistant-tuning-prompt_context-history_turns').waitFor();
    await page.getByRole('button', { name: 'Reset prompt context' }).click();
    await page.waitForFunction(() => document.getElementById('assistant-tuning-prompt_context-send_purposes').checked === true);
    assert.deepEqual(s.saved.at(-1), { reset_tuning: 'prompt_context' });
    console.log('PASS: Assistant tuning Prompt context section');
    await context.close();
  } finally {
    await browser.close();
    await server.close();
  }
})().catch(e => { console.error(e); process.exit(1); });
