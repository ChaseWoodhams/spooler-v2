// Settings > Assistant, every section on one page: edit and save across groups, preview shows the saved text, the run log stays, and
// Reset all tuning restores every group and the page shows it (values, no unsaved notice, feedback). The routes are stubbed.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const { chromium } = require('playwright');

const GROUPS = {
  limits: { label: 'Limits', hint: 'Caps.', fields: { max_turns: { label: 'Turns', type: 'int', unit: 'turns', default: 12, min: 1, max: 50, hint: 'Per run.' } } },
  instructions: { label: 'Instructions', hint: 'Standing text.', fields: { text: { label: 'Instructions', type: 'text', unit: 'characters', default: '', max: 8000, hint: 'Empty means built-in.', builtin: 'BUILT-IN' } } },
  model: { label: 'Model', hint: 'Model name.', fields: { name: { label: 'Model name', type: 'str', default: '', max: 100, hint: 'Blank for CLI default.' } } },
  prompt_context: { label: 'Prompt context', hint: 'What is sent.', fields: { send_purposes: { label: 'Send Purposes', type: 'bool', default: true, hint: 'Purposes.' } } },
  proposal_kinds: { label: 'Proposal kinds', hint: 'Kinds.', fields: { arrange: { label: 'Arrange', type: 'bool', default: true, hint: 'Arrange.' } } },
};
const DEFAULTS = { limits: { max_turns: 12 }, instructions: { text: '' }, model: { name: '' }, prompt_context: { send_purposes: true }, proposal_kinds: { arrange: true } };
const clone = x => JSON.parse(JSON.stringify(x));

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' });
    const page = await context.newPage();
    let values = clone(DEFAULTS);
    const entries = [{ time: '2026-10-15T10:01:00', project_id: 1, project_name: 'Brackets', question: 'Kept question', outcome: 'answered', reason: null, duration_seconds: 1, model: 'm', tool_calls: 0, steps: [], proposals: [], prompt: 'P', tuning: {} }];
    const posts = [];
    const view = () => ({ cli_path: '', jev_url: 'http://127.0.0.1:3455', defaults: { cli_path: '', jev_url: 'http://127.0.0.1:3455' }, tuning: { version: 1, values: clone(values), notes: [], groups: GROUPS } });
    await page.routeWebSocket(/.*/, () => {});
    await page.route('**/api/**', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/assistant/settings') {
        if (route.request().method() === 'GET') return route.fulfill({ json: view() });
        const body = route.request().postDataJSON();
        posts.push(body);
        if (body.reset_tuning === 'all') values = clone(DEFAULTS);
        for (const [g, kv] of Object.entries(body.tuning || {})) Object.assign(values[g], kv);
        return route.fulfill({ json: view() });
      }
      if (url.pathname === '/api/assistant/run-log') return route.fulfill({ json: { entries, cap: 50 } });
      if (url.pathname === '/api/projects') return route.fulfill({ json: [{ id: 7, name: 'Brackets' }] });
      if (url.pathname === '/api/projects/7/assistant/preview') return route.fulfill({ json: { instructions: values.instructions.text || 'BUILT-IN', prompt: `model=${values.model.name || 'default'}` } });
      return route.fulfill({ json: url.pathname === '/api/printers' ? [] : {} });
    });
    const open = async () => {
      await page.locator('#btn-settings-goto-assistant').click();
      await page.locator('#assistant-tuning-proposal_kinds-arrange').waitFor();
    };
    await page.goto(server.baseUrl);
    await page.locator('#btn-app-settings').click();
    await open();

    // Every section is on the one page.
    for (const sel of ['#assistant-tuning', '#assistant-runlog', '#assistant-preview', '#btn-assistant-tuning-reset-all'])
      assert.ok(await page.locator(sel).isVisible(), `${sel} is shown`);
    assert.equal(await page.locator('#assistant-tuning-groups fieldset').count(), 5);
    await page.locator('#assistant-runlog-list li').first().waitFor();

    // Edit across groups and save once.
    await page.locator('#assistant-tuning-limits-max_turns').fill('7');
    await page.locator('#assistant-tuning-instructions-text').fill('Be brief.');
    await page.locator('#assistant-tuning-model-name').fill('some-model');
    await page.locator('#assistant-tuning-prompt_context-send_purposes').uncheck();
    await page.locator('#assistant-tuning-proposal_kinds-arrange').uncheck();
    await page.locator('#btn-assistant-save').click();
    await page.waitForFunction(() => document.getElementById('settings-assistant').style.display === 'none');
    assert.deepEqual(posts.at(-1).tuning, { limits: { max_turns: 7 }, instructions: { text: 'Be brief.' }, model: { name: 'some-model' }, prompt_context: { send_purposes: false }, proposal_kinds: { arrange: false } });
    await open();

    // Preview shows the saved values.
    await page.locator('#assistant-preview-project option').first().waitFor({ state: 'attached' });
    await page.locator('#btn-assistant-preview').click();
    await page.waitForFunction(() => document.getElementById('assistant-preview-prompt').textContent.length > 0);
    assert.equal(await page.locator('#assistant-preview-instructions').innerText(), 'Be brief.');
    assert.equal(await page.locator('#assistant-preview-prompt').innerText(), 'model=some-model');

    // Reset all: every group back, the page shows it, the run log is untouched.
    await page.locator('#btn-assistant-tuning-reset-all').click();
    await page.waitForFunction(() => document.getElementById('assistant-tuning-limits-max_turns').value === '12');
    assert.deepEqual(posts.at(-1), { reset_tuning: 'all' });
    assert.equal(await page.locator('#assistant-tuning-instructions-text').inputValue(), '');
    assert.equal(await page.locator('#assistant-tuning-model-name').inputValue(), '');
    assert.equal(await page.locator('#assistant-tuning-prompt_context-send_purposes').isChecked(), true);
    assert.equal(await page.locator('#assistant-tuning-proposal_kinds-arrange').isChecked(), true);
    assert.equal(await page.locator('#assistant-tuning-dirty').isHidden(), true, 'nothing is left unsaved');
    assert.match(await page.locator('body').innerText(), /All tuning reset to the defaults/);
    assert.equal(await page.locator('#assistant-runlog-list li').count(), 1, 'the run log is kept');
    await context.close();
    console.log('PASS: Assistant settings sections together');
  } finally {
    await browser.close();
    await server.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
