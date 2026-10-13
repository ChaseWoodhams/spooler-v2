const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const records = ['completed', 'cancelled', 'failed', 'unknown'].map((outcome, i) => ({
  id: i + 1, file_name: 'same-file.gcode', printer_name: `Printer ${i + 1}`, printer_id: `p${i+1}`,
  started_at: 1700000000 + i * 86400, ended_at: 1700000010 + i * 86400, outcome,
  outcome_unconfirmed: i === 3, duration_s: i === 0 ? 0 : null,
  filament_g: i === 0 ? 0 : null, filament_mm: null, cost: i === 0 ? 0 : null,
  estimate_s: i === 0 ? 0 : 120, estimate_g: 10, notes: '',
}));
// The detail is grouped, with the unrecorded rows folded away: open the unrecorded fold, then read the whole thing
const detailText = async page => { await page.locator('.history-detail details.history-missing').evaluateAll(ds => ds.forEach(d => { d.open = true; })); return page.locator('.history-detail').innerText(); };
(async () => {
  const server = http.createServer(async (req, res) => {
    try {
      const relative = new URL(req.url, 'http://localhost').pathname;
      const file = path.join(root, 'public', relative === '/' ? 'index.html' : relative);
      let content = await fs.readFile(file);
      if (file.endsWith("index.html")) content = content.toString().replace("</head>", '<link rel="stylesheet" href="history-quality.css"></head>');
      res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript; charset=utf-8' : file.endsWith('.css') ? 'text/css; charset=utf-8' : 'text/html; charset=utf-8');
      res.end(content);
    } catch { res.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch();
  try {
    for (const width of [320, 390, 768, 1440]) {
      const page = await browser.newPage({ viewport: { width, height: 900 } });
      await page.routeWebSocket(/.*/, ws => ws.onMessage(message => {
        if (JSON.parse(message).action === 'list_printers') ws.send(JSON.stringify({ type: 'printer_update', printer: {
          id: 'p1', name: 'Printer 1', connected: false, printer_type: 'cc1', status: {}, camera_url: null,
        } }));
      }));
      let saveAttempts = 0;
      let lastFilters = '';
      let failNextSearch = false;
      await page.route('**/api/**', route => {
        const url = new URL(route.request().url());
        if (url.pathname === '/api/failure-reasons') return route.fulfill({ json: { cancel_reasons: ['adhesion'], labels: { adhesion: 'Poor adhesion' } } });
        if (url.pathname === '/api/prints' && failNextSearch) { failNextSearch = false; return route.fulfill({ status: 503, json: { error: 'History unavailable' } }); }
        if (url.pathname === '/api/prints') lastFilters = url.search;
        if (/\/api\/prints\/\d+\/outcome$/.test(url.pathname)) return route.fulfill({ json: { ...records[0], outcome: 'failed', outcome_unconfirmed: false } });
        if (route.request().method() === 'PATCH') {
          saveAttempts++;
          return saveAttempts === 1 ? route.fulfill({ status: 503, json: { error: 'Fixture save unavailable' } }) : route.fulfill({ json: { ...records[0], notes: 'Retain my notes' } });
        }
        if (url.pathname === '/api/prints') return route.fulfill({ json: { prints: records, total: records.length, summary: { prints: 4, duration_s: 0, filament_g: 0, cost: 0 } } });
        if (/\/api\/prints\/\d+$/.test(url.pathname)) return route.fulfill({ json: { ...records[0], job_events: [{ at: 1700000000, event: 'Started' }] } });
        return route.fulfill({ json: {} });
      });
      await page.goto(`http://127.0.0.1:${server.address().port}`);
      await page.locator('#card-p1').waitFor();
      await page.evaluate(() => openHistory());
      await page.locator('.history-row').first().waitFor();
      assert.equal(await page.locator('.history-date').first().isVisible(), true, `Date visible at ${width}`);
      assert.equal(await page.locator('.history-printer').first().isVisible(), true, `Printer visible at ${width}`);
      for (const label of ['Completed', 'Cancelled', 'Failed', 'Outcome unknown', 'Unconfirmed']) assert.equal(await page.locator('.history-row .status-badge').filter({ hasText: label }).count() > 0, true);
      await page.locator('.history-row').first().focus();
      await page.keyboard.press('Enter');
      await page.locator('.history-events').waitFor();
      const facts = await detailText(page);
      assert.match(facts, /0 g/);
      const summaryText = (await page.locator('#history-filter-summary').textContent()).trim();
      assert.equal(summaryText, 'All prints · all time', `Filter summary text at ${width}`);
      assert.match(facts, /Slicer estimate · duration/);
      assert.ok(!facts.includes('�'), `No replacement characters at ${width}`);
      assert.match(facts, /Observed duration\s+0s/);
      assert.match(facts, /Slicer estimate[^\n]*duration\s+0s/);
      assert.match(await page.locator('.history-row').first().innerText(), /0s observed/);
      assert.match(facts, /Length not recorded/);
      assert.match(facts, /Slicer estimate/);
      assert.equal(await page.locator('.history-row').first().evaluate(el => el === document.activeElement), true);
      await page.getByText('Job timeline (1 events)', { exact: true }).click();
      assert.match(await page.locator('.history-events time').innerText(), /2023/);

      assert.equal(await page.locator('#panel-history').evaluate(el => el.scrollWidth <= el.clientWidth), true);
      await page.getByRole('textbox', { name: 'Notes', exact: true }).fill('Retain my notes');
      await page.getByRole('button', { name: 'Save notes', exact: true }).click();
      await page.locator('[role=status]:not(#toast-area)').filter({ hasText: 'Fixture save unavailable' }).waitFor();
      assert.equal(await page.getByRole('textbox', { name: 'Notes', exact: true }).inputValue(), 'Retain my notes');
      await page.getByRole('button', { name: 'Save notes', exact: true }).click();
      await page.locator('[role=status]:not(#toast-area)').filter({ hasText: 'Notes saved.' }).waitFor();
      assert.equal(await page.getByRole('button', { name: 'Save notes', exact: true }).evaluate(el => el === document.activeElement), true);
      await page.locator('[data-history-outcome="1"]').selectOption('failed');
      await page.getByLabel('Poor adhesion').check();
      await page.getByRole('button', { name: 'Save outcome', exact: true }).click();
      await page.locator('[role=status]:not(#toast-area)').filter({ hasText: 'Outcome saved and confirmed' }).waitFor();
      assert.equal(await page.getByRole('button', { name: 'Save outcome', exact: true }).evaluate(el => el === document.activeElement), true);
      if (width <= 640) {   // phones start with the filters collapsed
        assert.equal(await page.getByRole('searchbox', { name: 'Search history' }).isVisible(), false, 'Filters start collapsed on a phone');
        await page.getByRole('button', { name: 'Show filters' }).click();
      }
      await page.getByRole('searchbox', { name: 'Search history' }).fill('same-file');
      await page.locator('#history-printer').selectOption('p1');
      await page.locator('#history-outcome').selectOption('unconfirmed');
      await page.waitForFunction(() => document.getElementById('history-filter-summary')?.textContent.includes('same-file'));
      await page.getByRole('button', { name: 'Hide filters' }).click();
      assert.equal(await page.getByRole('searchbox', { name: 'Search history' }).isVisible(), false);
      assert.match(await page.locator('#history-filter-summary').innerText(), /same-file.*Unconfirmed outcome/);
      assert.match(await page.locator('#history-result-count').innerText(), /4/);
      assert.match(lastFilters, /q=same-file/);
      assert.match(lastFilters, /unconfirmed=1/);
      assert.match(lastFilters, /printer_id=p1/);
      await page.getByRole('button', { name: 'Show filters' }).focus();
      await page.keyboard.press('Enter');
      assert.equal(await page.getByRole('searchbox', { name: 'Search history' }).isVisible(), true);
      failNextSearch = true;
      await page.locator('#history-range').selectOption('7');
      await page.locator('[role=status]:not(#toast-area)').filter({ hasText: 'History unavailable' }).waitFor();
      await page.getByRole('button', { name: 'Retry history' }).click();
      await page.waitForFunction(() => document.getElementById('history-result-count').textContent === '4 results');
      assert.match(lastFilters, /since=/);
      assert.match(lastFilters, /q=same-file/);
      await page.locator('.history-row').nth(1).click();
      const older = await detailText(page);   // the unrecorded rows sit in a fold, which detailText opens
      assert.match(older, /Not recorded/);
      assert.match(older, /End/);
      await page.close();
    }
    console.log('History context and telemetry PASS');
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(err => { console.error(err); process.exitCode = 1; });

