// Settings > Assistant > Tuning > Proposal kinds: switches are checkboxes drawn from the server's field list, Save sends only the changed
// ones as booleans, the number fields keep their bounds, and Reset prompt context restores the defaults. The route is stubbed.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const { chromium } = require('playwright');

const FIELDS = {
  orientation: { label: 'Orientation', type: 'bool', default: true, hint: 'Propose a better orientation.' },
  arrange: { label: 'Arrange', type: 'bool', default: true, hint: 'Propose an Arrange.' },
  plan: { label: 'Plan plates', type: 'bool', default: true, hint: 'Propose Plan plates.' },
  settings: { label: 'Settings', type: 'bool', default: true, hint: 'Propose setting changes.' },
};
const DEFAULTS = { orientation: true, arrange: true, plan: true, settings: true };
const view = values => ({
  cli_path: '', jev_url: 'http://127.0.0.1:3455', defaults: { cli_path: '', jev_url: 'http://127.0.0.1:3455' },
  tuning: { version: 1, values: { proposal_kinds: values }, notes: [], groups: { proposal_kinds: { label: 'Proposal kinds', hint: 'What each question sends.', fields: FIELDS } } },
});

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  const s = { values: { ...DEFAULTS, arrange: false }, saved: [] };
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
        s.values = body.reset_tuning ? { ...DEFAULTS } : { ...s.values, ...(body.tuning?.proposal_kinds || {}) };
        return route.fulfill({ json: view(s.values) });
      }
      return route.fulfill({ json: url.pathname === '/api/printers' ? [] : {} });
    });
    await page.goto(server.baseUrl);
    await page.locator('#btn-app-settings').click();
    await page.locator('#btn-settings-goto-assistant').click();
    await page.locator('#assistant-tuning-proposal_kinds-plan').waitFor();
    const arrange = page.locator('#assistant-tuning-proposal_kinds-arrange');
    assert.equal(await arrange.getAttribute('type'), 'checkbox');
    assert.equal(await arrange.isChecked(), false, 'the saved value is shown');
    assert.equal(await page.locator('#assistant-tuning-proposal_kinds-orientation').isChecked(), true);
    await page.locator('#assistant-tuning-proposal_kinds-plan').uncheck();
    await page.locator('#btn-assistant-save').click();
    await page.waitForFunction(() => document.getElementById('settings-assistant').style.display === 'none');
    assert.deepEqual(s.saved.at(-1).tuning, { proposal_kinds: { plan: false } }, 'only the changed switch is sent, as a boolean');
    await page.locator('#btn-settings-goto-assistant').click();
    await page.locator('#assistant-tuning-proposal_kinds-plan').waitFor();
    await page.getByRole('button', { name: 'Reset Proposal kinds' }).click();
    await page.waitForFunction(() => document.getElementById('assistant-tuning-proposal_kinds-arrange').checked === true);
    assert.deepEqual(s.saved.at(-1), { reset_tuning: 'proposal_kinds' });
    console.log('PASS: Assistant tuning Proposal kinds section');
    await context.close();
  } finally {
    await browser.close();
    await server.close();
  }
})().catch(e => { console.error(e); process.exit(1); });
