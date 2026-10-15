// Settings > Assistant > Tuning > Model: a short text field drawn from the server's description; blank is allowed and means the CLI default;
// Save sends only a changed name; a refused name shows the server's message; Reset model restores blank. The route is stubbed.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const { chromium } = require('playwright');

const view = name => ({
  cli_path: '', jev_url: 'http://127.0.0.1:3455', defaults: { cli_path: '', jev_url: 'http://127.0.0.1:3455' },
  tuning: { version: 1, values: { model: { name } }, notes: [], groups: { model: { label: 'Model', hint: 'Which model a run uses.', fields: {
    name: { label: 'Model name', type: 'str', unit: 'text', default: '', min: 0, max: 100, hint: 'One word, no spaces.' } } } } },
});

async function run(browser, baseUrl) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' });
  const page = await context.newPage();
  const s = { name: 'claude-old', saved: [] };
  await page.routeWebSocket(/.*/, () => {});
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/assistant/settings') {
      if (route.request().method() === 'GET') return route.fulfill({ json: view(s.name) });
      const body = route.request().postDataJSON();
      s.saved.push(body);
      if (body.reset_tuning) { s.name = ''; return route.fulfill({ json: view(s.name) }); }
      const name = body.tuning?.model?.name;
      if (/\s/.test(name)) return route.fulfill({ status: 400, json: { error: 'Model name must be one word without spaces' } });
      if (name !== undefined) s.name = name.trim();
      return route.fulfill({ json: view(s.name) });
    }
    return route.fulfill({ json: url.pathname === '/api/printers' ? [] : {} });
  });
  try {
    await page.goto(baseUrl);
    await page.locator('#btn-app-settings').click();
    await page.locator('#btn-settings-goto-assistant').click();
    const input = page.locator('#assistant-tuning-model-name');
    await input.waitFor();
    assert.equal(await input.getAttribute('type'), 'text');
    assert.equal(await input.inputValue(), 'claude-old', 'the saved name is shown');
    assert.equal(await page.locator('#assistant-tuning-dirty').isHidden(), true);
    await input.fill('two words');
    assert.equal(await page.locator('#assistant-tuning-dirty').isVisible(), true, 'an unsaved edit is flagged');
    await page.locator('#btn-assistant-save').click();
    await page.locator('#settings-feedback, .settings-feedback', { hasText: 'one word' }).first().waitFor();
    assert.equal(await input.inputValue(), 'two words', 'a refused name stays as typed');
    await input.fill('');
    await page.locator('#btn-assistant-save').click();
    await page.waitForFunction(() => document.getElementById('settings-assistant').style.display === 'none');
    assert.deepEqual(s.saved.at(-1).tuning, { model: { name: '' } }, 'blank is sent as an empty name');
    await page.locator('#btn-settings-goto-assistant').click();
    await input.waitFor();
    s.name = 'm2';
    await page.getByRole('button', { name: 'Reset model' }).click();
    await page.waitForFunction(() => document.getElementById('assistant-tuning-model-name').value === '');
    assert.deepEqual(s.saved.at(-1), { reset_tuning: 'model' });
    console.log('PASS: Assistant tuning Model field');
  } finally { await context.close(); }
}

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try { await run(browser, server.baseUrl); } finally { await browser.close(); await server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
