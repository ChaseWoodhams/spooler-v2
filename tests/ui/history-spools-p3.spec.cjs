// Ticket 26 · History, Outcomes and Spools P3: Spools panel padding and per-spool menu (PS-59), History detail groups (PS-60),
// Load more focus and "Showing N of M" (PS-61), Failed sheet rows and Save reach (PS-62), no inline styling on the prompt (PS-63).
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const now = Math.floor(Date.now() / 1000);

const record = (id, extra = {}) => ({ id, file_name: `part-${id}.gcode`, printer_name: 'Printer One', printer_id: 'p1', started_at: now - id * 3600, ended_at: now - id * 3600 + 600,
  outcome: 'completed', outcome_unconfirmed: false, duration_s: 600, filament_g: 4, filament_mm: 1500, cost: 0.1, estimate_s: 620, estimate_g: 4, notes: '', ...extra });
// An old record: almost nothing was recorded for it
const OLD = record(1, { ended_at: null, duration_s: null, filament_g: null, filament_mm: null, cost: null, estimate_s: null, estimate_g: null,
  total_layers: null, spools: [{ spool_id: 17, vendor: 'Fixture brand', material: 'PLA', filament_name: 'Blue', color_hex: '0000ff', grams: 4, cost: null }] });
const MANY = Array.from({ length: 3 }, (_, i) => record(i + 2));
const SPOOL = { id: 17, location: '', initial_weight: 1000, remaining_weight: 750,
  filament: { id: 18, material: 'PLA', name: 'Blue', color_hex: '0000FF', vendor: { id: 5, name: 'Fixture brand' } } };
const AWAITING = { print_id: 7, printer_id: 'p1', printer_name: 'Printer One', file_name: 'bracket.gcode', reported_outcome: 'failed', outcome_unconfirmed: true };
const REASONS = ['adhesion', 'spaghetti', 'warping', 'clog', 'layer_shift', 'stringing', 'other'];

(async () => {
  const server = http.createServer(async (req, res) => {
    try {
      const relative = new URL(req.url, 'http://localhost').pathname;
      const content = await fs.readFile(path.join(root, 'public', relative === '/' ? 'index.html' : relative));
      res.setHeader('Content-Type', relative.endsWith('.js') ? 'text/javascript; charset=utf-8' : relative.endsWith('.css') ? 'text/css; charset=utf-8' : 'text/html; charset=utf-8');
      res.end(content);
    } catch { res.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch();
  const open = async (width, height = 900, { theme } = {}) => {
    const context = await browser.newContext({ viewport: { width, height }, serviceWorkers: 'block' });
    const page = await context.newPage();
    const seen = { patches: [], prints: [], deletes: [] };
    if (theme) await page.addInitScript(t => localStorage.setItem('theme', t), theme);
    page.on('pageerror', e => console.error('Page error:', e.message));
    await page.routeWebSocket(/.*/, ws => ws.onMessage(message => {
      if (JSON.parse(message).action === 'list_printers') ws.send(JSON.stringify({ type: 'printer_update', printer: { id: 'p1', name: 'Printer One', connected: true, printer_type: 'cc1', status: { state: 'idle' }, files: [] } }));
    }));
    await page.route('**/api/**', route => {
      const url = new URL(route.request().url());
      const method = route.request().method();
      if (url.pathname === '/api/auth-status') return route.fulfill({ json: { spoolman_url: 'http://spoolman.test' } });
      if (url.pathname === '/api/failure-reasons') return route.fulfill({ json: { failure_reasons: REASONS, cancel_reasons: REASONS, labels: Object.fromEntries(REASONS.map(r => [r, r[0].toUpperCase() + r.slice(1).replace('_', ' ')])) } });
      if (url.pathname === '/api/outcome-prompts') return route.fulfill({ json: { awaiting: [AWAITING] } });
      if (url.pathname === '/api/spoolman/api/v1/spool' && method === 'GET') return route.fulfill({ json: [SPOOL] });
      if (/\/api\/spoolman\/api\/v1\/spool\/\d+$/.test(url.pathname) && method === 'PATCH') { seen.patches.push(route.request().postDataJSON()); return route.fulfill({ json: {} }); }
      if (url.pathname === '/api/prints') {
        seen.prints.push(url.search);
        if (url.searchParams.get('limit') === '1') return route.fulfill({ json: { prints: [], total: 0 } });
        const offset = Number(url.searchParams.get('offset') || 0);
        // Five records in all, served two at a time so "Load more" has something to load
        const all = [OLD, ...MANY, record(5)];
        return route.fulfill({ json: { prints: all.slice(offset, offset + 2), total: all.length, summary: { prints: all.length, duration_s: 0, filament_g: 0, cost: 0 } } });
      }
      if (/\/api\/prints\/\d+$/.test(url.pathname)) return route.fulfill({ json: { ...OLD, job_events: [] } });
      return route.fulfill({ json: {} });
    });
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.locator('#card-p1').waitFor();
    return { page, seen, context };
  };
  // The page serves 2 records per request here, so lower the page size the client asks for
  const openHistory = async (page) => { await page.locator('#btn-history').click(); await page.locator('#history-list .history-item').first().waitFor(); };

  try {
    for (const width of [1098, 390]) {
      // PS-60: groups, a spools heading, the empty rows folded away, readable values
      {
        const { page, context } = await open(width);
        await openHistory(page);
        await page.locator('[data-history-toggle="1"]').click();
        const detail = page.locator('#history-detail-1');
        await detail.waitFor();
        const headings = await detail.locator('h4').allTextContents();
        assert.ok(headings.includes('Timing') && headings.includes('Spools used'), `group headings, got ${JSON.stringify(headings)}`);
        const fold = detail.locator('details.history-missing');
        assert.equal(await fold.count(), 1, 'the unrecorded rows share one fold');
        assert.match(await fold.locator('summary').innerText(), /\d+ details? not recorded/);
        assert.equal(await fold.evaluate(d => d.open), false, 'the fold starts closed');
        const outside = await detail.locator('.history-group dd').allInnerTexts();
        assert.ok(!outside.some(t => /not recorded|not available/i.test(t)), `no "not recorded" row outside the fold: ${JSON.stringify(outside)}`);
        const size = await detail.locator('.history-group dd').first().evaluate(e => parseFloat(getComputedStyle(e).fontSize));
        assert.ok(size >= 13, `values are at least 13px, got ${size}`);
        console.log(`PASS: History detail groups its facts and folds the unrecorded ones ${width}`);
        await context.close();
      }

      // PS-61: Load more says how many are shown and puts focus on the first new row
      {
        const { page, context } = await open(width);
        await openHistory(page);
        await page.waitForFunction(t => document.getElementById('history-result-count').textContent.trim() === t, 'Showing 2 of 5');
        await page.locator('#btn-history-more').click();
        await page.waitForFunction(() => document.querySelectorAll('#history-list .history-item').length === 4);
        assert.equal(await page.evaluate(() => document.activeElement.getAttribute('data-history-toggle')), '3', 'focus is on the first new row');
        await page.waitForFunction(t => document.getElementById('history-result-count').textContent.trim() === t, 'Showing 4 of 5');
        await page.locator('#btn-history-more').click();
        await page.waitForFunction(() => document.querySelectorAll('#history-list .history-item').length === 5);
        assert.equal(await page.evaluate(() => document.activeElement.getAttribute('data-history-toggle')), '5', 'focus survives the button disappearing');
        await page.waitForFunction(t => document.getElementById('history-result-count').textContent.trim() === t, '5 results');
        console.log(`PASS: Load more shows Showing N of M and keeps focus on the first new row ${width}`);
        await context.close();
      }

      // PS-59: padded status line and a per-spool menu with Edit, Assign and Remove
      {
        const { page, seen, context } = await open(width);
        await page.locator('#btn-spools').click();
        const trigger = page.getByRole('button', { name: /^Actions for Fixture brand Blue PLA/ });
        await trigger.waitFor();
        await page.evaluate(() => { const s = document.getElementById('spools-status'); s.hidden = false; if (!s.textContent) s.textContent = 'Inventory loaded'; });
        const pad = await page.locator('#spools-status').evaluate(e => parseFloat(getComputedStyle(e).paddingLeft));
        assert.ok(pad >= 20, `status line has 20px side padding, got ${pad}`);
        const box = await trigger.boundingBox();
        assert.ok(box.width >= 44 && box.height >= 44, `menu button is a 44px target, got ${box.width}x${box.height}`);
        assert.equal(await trigger.getAttribute('aria-expanded'), 'false');
        await trigger.focus();
        await page.keyboard.press('Enter');
        const menu = page.getByRole('menu');
        await menu.waitFor();
        assert.equal(await trigger.getAttribute('aria-expanded'), 'true');
        const items = await menu.getByRole('menuitem').allInnerTexts();
        assert.deepEqual(items, ['Edit in Spoolman', 'Assign to Printer One', 'Remove']);
        assert.equal(await menu.getByRole('menuitem', { name: /Edit .* in Spoolman/ }).getAttribute('href'), 'http://spoolman.test/spool/edit/17');
        const inside = await menu.boundingBox();
        assert.ok(inside.x >= 0 && inside.x + inside.width <= width, 'the menu stays inside the screen');
        await page.keyboard.press('ArrowDown');
        assert.equal(await page.evaluate(() => document.activeElement.textContent), 'Assign to Printer One', 'arrow keys move through the items');
        await page.keyboard.press('Escape');
        assert.equal(await page.getByRole('menu').count(), 0, 'Escape closes the menu');
        assert.equal(await page.evaluate(() => document.activeElement.getAttribute('data-spool-menu')), '17', 'and focus returns to its button');
        await trigger.click();
        await page.getByRole('menuitem', { name: /^Assign .* to Printer One/ }).click();
        await page.waitForFunction(() => true);
        for (let i = 0; i < 30 && !seen.patches.length; i++) await page.waitForTimeout(100);
        assert.deepEqual(seen.patches[0], { location: 'Printer One' }, 'Assign sends the printer as the spool location');
        console.log(`PASS: Spools panel has a padded status line and an Edit/Assign/Remove menu ${width}`);
        await context.close();
      }
    }

    // PS-62 and PS-63: the Failed sheet on a short phone screen (the keyboard takes most of the height)
    for (const theme of ['dark', 'light']) {
      const { page, context } = await open(390, 420, { theme });
      await page.evaluate(() => OutcomePrompt.open('p1'));
      await page.getByRole('button', { name: 'Failed…' }).click();
      const box = page.locator('.outcome-prompt .modal-box');
      await box.locator('[data-reason]').first().waitFor();
      const heights = await box.locator('.outcome-check').evaluateAll(els => els.map(e => Math.round(e.getBoundingClientRect().height)));
      assert.ok(heights.length >= REASONS.length && heights.every(h => h >= 44), `reason rows are 44px tall, got ${JSON.stringify(heights)}`);
      const save = page.getByRole('button', { name: 'Save failure' });
      const r = await save.boundingBox();
      assert.ok(r.y >= 0 && r.y + r.height <= 420, `Save stays on screen on a 420px-tall viewport, got y=${r.y} h=${r.height}`);
      await box.locator('[data-reason]').last().scrollIntoViewIfNeeded();
      const after = await save.boundingBox();
      assert.ok(after.y + after.height <= 420, 'and still after scrolling the reasons');
      // PS-63: the prompt carries no inline styling; its colours and stacking come from the stylesheet and tokens
      assert.equal(await page.locator('.outcome-prompt [style], .outcome-prompt.modal[style*="z-index"]').count(), 0, 'no inline style attributes on the prompt');
      assert.equal(await page.locator('.outcome-prompt').evaluate(e => getComputedStyle(e).zIndex), '2000');
      const tick = await box.locator('[data-reason]').first().evaluate(e => ({ accent: getComputedStyle(e).accentColor, token: getComputedStyle(document.documentElement).getPropertyValue('--accent-fill').trim() }));
      assert.ok(tick.token && tick.accent !== 'auto', `ticks use the accent-fill token in ${theme}: ${JSON.stringify(tick)}`);
      const bg = await box.evaluate(e => getComputedStyle(e).backgroundColor);
      assert.equal(await page.evaluate(() => document.body.classList.contains('light-mode')), theme === 'light');
      assert.notEqual(bg, 'rgba(0, 0, 0, 0)', `the sheet has a themed background in ${theme}`);
      console.log(`PASS: Failed sheet has 44px rows, keeps Save in reach and uses no inline styling (${theme})`);
      await context.close();
    }
    const source = await fs.readFile(path.join(root, 'public/outcome-prompt.js'), 'utf8');
    assert.ok(!/style=|\.style\.|cssText|z-index/.test(source), 'outcome-prompt.js sets no styles itself');
    console.log('PASS: outcome-prompt.js sets no inline styles');
  } finally {
    await browser.close();
    server.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
