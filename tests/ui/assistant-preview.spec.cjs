// Settings > Assistant > Preview: choose a project, type a sample question, see the instructions and the prompt as text.
// The routes are stubbed (nothing real is called), like the other Assistant specs.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const { chromium } = require('playwright');

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' });
    const page = await context.newPage();
    const asked = [];
    await page.routeWebSocket(/.*/, () => {});
    await page.route('**/api/**', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/assistant/settings') {
        return route.fulfill({ json: { cli_path: '', jev_url: 'http://127.0.0.1:3455', defaults: { cli_path: '', jev_url: 'http://127.0.0.1:3455' }, tuning: { version: 1, values: {}, notes: [], groups: {} } } });
      }
      if (url.pathname === '/api/assistant/run-log') return route.fulfill({ json: { entries: [], cap: 50 } });
      if (url.pathname === '/api/projects') return route.fulfill({ json: [{ id: 7, name: 'Brackets' }, { id: 8, name: 'Shelf' }] });
      if (url.pathname === '/api/projects/8/assistant/preview') {
        asked.push(route.request().postDataJSON());
        return route.fulfill({ json: { instructions: 'INSTRUCTIONS <b>x</b>', prompt: 'PROMPT <i>y</i>' } });
      }
      return route.fulfill({ json: url.pathname === '/api/printers' ? [] : {} });
    });
    await page.goto(server.baseUrl);
    await page.locator('#btn-app-settings').click();
    await page.locator('#btn-settings-goto-assistant').click();
    await page.locator('#assistant-preview-project option').first().waitFor({ state: 'attached' });
    await page.locator('#assistant-preview-project').selectOption({ label: 'Shelf' });
    await page.locator('#assistant-preview-question').fill('Why two plates?');
    await page.locator('#btn-assistant-preview').click();
    await page.waitForFunction(() => document.getElementById('assistant-preview-prompt').textContent.length > 0);
    assert.equal(await page.locator('#assistant-preview-instructions').innerText(), 'INSTRUCTIONS <b>x</b>', 'shown as text, never markup');
    assert.equal(await page.locator('#assistant-preview-prompt').innerText(), 'PROMPT <i>y</i>');
    assert.equal(asked.length, 1);
    assert.equal(asked[0].message, 'Why two plates?');
    assert.match(await page.locator('#assistant-preview-status').innerText(), /Nothing was started/);
    await context.close();
    console.log('PASS: Assistant preview section');
  } finally {
    await browser.close();
    await server.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
