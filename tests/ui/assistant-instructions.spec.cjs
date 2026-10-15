// Settings > Assistant > Tuning > Instructions: the editable box with a counter against the server's cap, the built-in text beside it, Save
// sends the text, an over-length refusal keeps what was typed, and Reset instructions empties the box. The route is stubbed.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const { chromium } = require('playwright');

const BUILTIN = 'You are the Spooler Assistant. Built-in wording.';
const view = text => ({
  cli_path: '', jev_url: 'http://127.0.0.1:3455', defaults: { cli_path: '', jev_url: 'http://127.0.0.1:3455' },
  tuning: { version: 1, values: { instructions: { text } }, notes: [], groups: { instructions: { label: 'Instructions', hint: 'Whole text.', fields: {
    text: { label: 'Instruction text', type: 'text', unit: 'characters', default: '', min: 0, max: 50, hint: 'A fixed block follows.', builtin: BUILTIN } } } } },
});

async function run(browser, baseUrl) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' });
  const page = await context.newPage();
  const s = { text: '', saved: [] };
  await page.routeWebSocket(/.*/, () => {});
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/assistant/settings') {
      if (route.request().method() === 'GET') return route.fulfill({ json: view(s.text) });
      const body = route.request().postDataJSON();
      s.saved.push(body);
      if (body.reset_tuning) { s.text = ''; return route.fulfill({ json: view('') }); }
      const text = body.tuning?.instructions?.text;
      if (text !== undefined && text.length > 50) return route.fulfill({ status: 400, json: { error: 'Instruction text cannot be longer than 50 characters' } });
      if (text !== undefined) s.text = text;
      return route.fulfill({ json: view(s.text) });
    }
    return route.fulfill({ json: url.pathname === '/api/printers' ? [] : {} });
  });
  try {
    await page.goto(baseUrl);
    await page.locator('#btn-app-settings').click();
    await page.locator('#btn-settings-goto-assistant').click();
    const box = page.locator('#assistant-tuning-instructions-text');
    await box.waitFor();
    assert.equal(await box.inputValue(), '', 'empty means the built-in text');
    assert.equal(await page.locator('#assistant-tuning-instructions-text-builtin').inputValue(), BUILTIN, 'the built-in text is shown beside it');
    assert.equal(await page.locator('#assistant-tuning-instructions-text-builtin').isEditable(), false);
    assert.match(await page.locator('#assistant-tuning-instructions-text-count').innerText(), /^0 of 50 characters/, 'the counter shows the cap');
    await box.fill('Be brief.');
    assert.match(await page.locator('#assistant-tuning-instructions-text-count').innerText(), /^9 of 50/);
    assert.equal(await page.locator('#assistant-tuning-dirty').isVisible(), true);
    await page.locator('#btn-assistant-save').click();
    await page.waitForFunction(() => document.getElementById('settings-assistant').style.display === 'none');
    assert.deepEqual(s.saved.at(-1).tuning, { instructions: { text: 'Be brief.' } });

    await page.locator('#btn-settings-goto-assistant').click();
    await box.waitFor();
    assert.equal(await box.inputValue(), 'Be brief.');
    await page.getByRole('button', { name: 'Reset instructions' }).click();
    await page.waitForFunction(() => document.getElementById('assistant-tuning-instructions-text').value === '');
    assert.deepEqual(s.saved.at(-1), { reset_tuning: 'instructions' });
    console.log('PASS: Assistant tuning Instructions section');
  } finally { await context.close(); }
}

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    await run(browser, server.baseUrl);
  } finally {
    await browser.close();
    await server.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
