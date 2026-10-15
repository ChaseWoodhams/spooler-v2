// Assistant panel shell: opens on the project page, shows AI unavailable with Check again, the empty thread
// (capability line, Purpose summary, example chips), goes full width when narrow, and never gets in the way of
// the page. Also the Assistant page in Settings. Fixtures only: nothing real is called.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = require('playwright');

const PURPOSE = { default: { value: 'general', note: '' }, models: [
  { id: 11, name: 'Bracket part 1', value: 'functional', note: 'carries motor thrust', source: 'model' },
  { id: 12, name: 'Bracket part 2', value: 'general', note: '', source: 'default' } ] };

// Serves assistant/status from `mode`: 'down' (unavailable), 'up', or 'error' (HTTP 500)
async function stubStatus(page, state) {
  state.calls = [];
  await page.route('**/api/projects/1/assistant/status**', route => {
    state.calls.push(new URL(route.request().url()).search);
    if (state.mode === 'error') return route.fulfill({ status: 500, json: { error: 'boom' } });
    if (state.mode === 'up') return route.fulfill({ json: { available: true, reason: null, purpose: PURPOSE } });
    return route.fulfill({ json: { available: false, reason: 'No claude CLI found on PATH. Set the CLI path in Settings.', purpose: PURPOSE } });
  });
}

async function panelStates(browser, baseUrl) {
  const { page, context } = await sf.openSlicer(browser, baseUrl, { step: 'plates' });
  const state = { mode: 'down' };
  await stubStatus(page, state);
  try {
    await page.waitForSelector('.slicer-step.active[data-step="plates"]', { timeout: 10000 });
    assert.equal(await page.locator('#assistant-panel').isVisible(), false, 'the panel is closed until asked for');
    assert.equal(state.calls.length, 0, 'loading the page never calls the assistant');
    assert.equal(await page.locator('#btn-assistant').getAttribute('aria-expanded'), 'false');

    // unavailable: says why, offers Check again, the rest of the page still works
    await page.locator('#btn-assistant').click();
    assert.equal(await page.locator('#btn-assistant').getAttribute('aria-expanded'), 'true');
    const panel = page.locator('#assistant-panel');
    await panel.waitFor();
    await panel.getByText('AI unavailable').waitFor();
    assert.match(await panel.innerText(), /No claude CLI found on PATH/);
    assert.match(await panel.innerText(), /Everything else works as normal/);
    assert.equal(await panel.locator('.assistant-input').isDisabled(), true, 'nothing to type into while unavailable');
    assert.equal(await page.locator('[data-object="1"]').count() > 0, true, 'the Objects list still renders');

    // Check again asks for a fresh check, and an available answer shows the empty thread
    state.mode = 'up';
    await panel.getByRole('button', { name: 'Check again' }).click();
    await panel.locator('.assistant-empty').waitFor();
    assert.ok(state.calls.some(search => /fresh=1/.test(search)), 'Check again rechecks instead of reading the cache');
    const text = await panel.innerText();
    assert.match(text, /propose changes/i);
    assert.match(text, /can't touch printers/i);
    assert.match(text, /Functional/, 'the Purpose summary names the Purpose');
    assert.match(text, /carries motor thrust/);
    assert.match(text, /Bracket part 1/);
    assert.equal(await panel.locator('.assistant-chip').count(), 3, 'a few example prompts');
    assert.equal(await panel.getByText('AI unavailable').count(), 0);

    // a chip fills the composer for editing
    const chip = await panel.locator('.assistant-chip').first().textContent();
    await panel.locator('.assistant-chip').first().click();
    assert.equal(await panel.locator('.assistant-input').inputValue(), chip);

    // wide: docked on the right of the main column
    const box = await panel.boundingBox();
    const main = await page.locator('#slicer-main').boundingBox();
    assert.ok(box.x >= main.x + main.width - 1, 'docked to the right of the page');
    assert.ok(box.width < 480, `docked panel is a column, not the page (${Math.round(box.width)}px)`);

    // Close puts the page back
    await panel.getByRole('button', { name: 'Close assistant' }).click();
    assert.equal(await panel.isVisible(), false);
    console.log('PASS: assistant panel unavailable, empty thread and docking');
  } finally { await context.close(); }
}

async function narrowAndFailure(browser, baseUrl) {
  const { page, context } = await sf.openSlicer(browser, baseUrl, { step: 'plates', width: 800, height: 900 });
  const state = { mode: 'error' };
  await stubStatus(page, state);
  try {
    await page.waitForSelector('.slicer-step.active[data-step="plates"]', { timeout: 10000 });
    await page.locator('#btn-assistant').click();
    const panel = page.locator('#assistant-panel');
    await panel.getByText('AI unavailable').waitFor();
    assert.match(await panel.innerText(), /boom|could not/i, 'a failing status route reads as unavailable with a reason');
    const box = await panel.boundingBox();
    assert.ok(Math.abs(box.width - 800) <= 1, `full width under 900px (${box.width})`);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'no horizontal scroll');
    await panel.getByRole('button', { name: 'Close assistant' }).click();
    assert.equal(await panel.isVisible(), false);
    console.log('PASS: narrow panel is full width and a failing status stays contained');
  } finally { await context.close(); }
}

async function settingsPage(browser, baseUrl) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' });
  const page = await context.newPage();
  const saved = [];
  await page.routeWebSocket(/.*/, () => {});
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/assistant/settings') {
      if (route.request().method() === 'POST') {
        saved.push(route.request().postDataJSON());
        return route.fulfill({ json: { cli_path: saved.at(-1).cli_path, jev_url: saved.at(-1).jev_url || 'http://127.0.0.1:3455', defaults: { cli_path: '', jev_url: 'http://127.0.0.1:3455' } } });
      }
      return route.fulfill({ json: { cli_path: '', jev_url: 'http://127.0.0.1:3455', defaults: { cli_path: '', jev_url: 'http://127.0.0.1:3455' } } });
    }
    if (url.pathname === '/api/assistant/status') return route.fulfill({ json: { available: false, reason: 'No claude CLI found on PATH.' } });
    return route.fulfill({ json: url.pathname === '/api/printers' ? [] : {} });
  });
  try {
    await page.goto(baseUrl);
    await page.locator('#btn-app-settings').click();
    await page.locator('#btn-settings-goto-assistant').click();
    assert.equal(await page.locator('#assistant-cli-path').inputValue(), '');
    assert.equal(await page.locator('#assistant-jev-url').inputValue(), '', 'the default shows as the placeholder');
    assert.equal(await page.locator('#assistant-jev-url').getAttribute('placeholder'), 'http://127.0.0.1:3455');
    assert.equal(await page.locator('#settings-assistant input[type=password]').count(), 0, 'no API key field');
    assert.doesNotMatch(await page.locator('#settings-assistant').innerText(), /api key field|enter.*api key/i);
    await page.fill('#assistant-cli-path', 'C:\\tools\\claude.exe');
    await page.fill('#assistant-jev-url', 'http://127.0.0.1:4000');
    await page.locator('#btn-assistant-check').click();
    await page.getByText(/AI unavailable: No claude CLI/).waitFor();
    assert.deepEqual(saved.at(-1), { cli_path: 'C:\\tools\\claude.exe', jev_url: 'http://127.0.0.1:4000' }, 'Check saves what is typed first');
    await page.locator('#btn-assistant-save').click();
    assert.equal(saved.length, 2);
    console.log('PASS: Assistant settings page');
  } finally { await context.close(); }
}

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    await panelStates(browser, server.baseUrl);
    await narrowAndFailure(browser, server.baseUrl);
    await settingsPage(browser, server.baseUrl);
  } finally { await browser.close(); await server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
