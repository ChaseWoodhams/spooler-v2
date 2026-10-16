// Settings > Assistant > Run log: entries newest first with outcome and reason, the privacy note, details with the prompt, and Clear.
// The routes are stubbed (nothing real is called), like the other Assistant specs.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const { chromium } = require('playwright');

const entry = (n, outcome, reason) => ({
  time: `2026-10-15T10:0${n}:00`, project_id: 1, project_name: 'Brackets', question: `Question ${n}`, outcome, reason, duration_seconds: 1.5,
  model: 'fake-model', tool_calls: 1, steps: ['Reading project'], proposals: [], prompt: `PROMPT <b>${n}</b>`, system_prompt: `SYSTEM <i>${n}</i>`, tuning: { limits: { deadline_seconds: 60 } },
});

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' });
    const page = await context.newPage();
    let entries = [entry(2, 'failed', 'The assistant stopped unexpectedly (exit code 3).'), entry(1, 'answered', null)];
    let cleared = 0;
    await page.routeWebSocket(/.*/, () => {});
    await page.route('**/api/**', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/assistant/run-log') return route.fulfill({ json: { entries, cap: 50 } });
      if (url.pathname === '/api/assistant/run-log/clear') { cleared++; entries = []; return route.fulfill({ json: { entries, cap: 50 } }); }
      if (url.pathname === '/api/assistant/settings') {
        return route.fulfill({ json: { cli_path: '', jev_url: 'http://127.0.0.1:3455', defaults: { cli_path: '', jev_url: 'http://127.0.0.1:3455' }, tuning: { version: 1, values: {}, notes: [], groups: {} } } });
      }
      return route.fulfill({ json: url.pathname === '/api/printers' ? [] : {} });
    });
    await page.goto(server.baseUrl);
    await page.locator('#btn-app-settings').click();
    await page.locator('#btn-settings-goto-assistant').click();
    await page.locator('#assistant-runlog-list li').first().waitFor();

    assert.match(await page.locator('#assistant-runlog').innerText(), /project name, your question and the exact prompt[\s\S]*never sent to any service/, 'the privacy note is shown');
    const items = await page.locator('#assistant-runlog-list li summary').allInnerTexts();
    assert.equal(items.length, 2);
    assert.match(items[0], /failed.*Question 2/, 'newest first, with its outcome');
    await page.locator('#assistant-runlog-list li').first().locator('summary').click();
    assert.match(await page.locator('#assistant-runlog-list li').first().innerText(), /exit code 3/, 'the reason is shown');
    assert.equal(await page.locator('#assistant-runlog-list pre').first().innerText(), 'PROMPT <b>2</b>', 'the prompt is shown as text, never as markup');
    assert.equal(await page.locator('#assistant-runlog-list pre').nth(1).innerText(), 'SYSTEM <i>2</i>', 'the system prompt sent is shown too, as text');

    await page.locator('#btn-assistant-runlog-clear').click();
    await page.waitForFunction(() => document.querySelectorAll('#assistant-runlog-list li').length === 0);
    assert.equal(cleared, 1);
    assert.match(await page.locator('#assistant-runlog-status').innerText(), /No Assistant runs logged yet/);
    await context.close();
    console.log('PASS: Assistant run log section');
  } finally {
    await browser.close();
    await server.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
