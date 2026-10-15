// Ticket 20 · History P2: unconfirmed chip and nav count (PS-30), outcome correction (PS-31), row fit (PS-32),
// phone filters and rows (PS-33), Export CSV by fetch (PS-34).
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const now = Math.floor(Date.now() / 1000);
const records = [
  { id: 1, file_name: 'a-rather-long-file-name-for-a-benchy-in-two-colours.gcode', printer_name: 'Printer One', printer_id: 'p1', started_at: now - 3600, ended_at: now - 60,
    outcome: 'completed', outcome_unconfirmed: true, duration_s: 3500, filament_g: 12, filament_mm: null, cost: 0.42, estimate_s: 3400, estimate_g: 11, notes: '' },
  { id: 2, file_name: 'older.gcode', printer_name: 'Printer One', printer_id: 'p1', started_at: now - 40 * 86400, ended_at: now - 40 * 86400 + 600,
    outcome: 'failed', outcome_unconfirmed: false, duration_s: 600, filament_g: 2, filament_mm: null, cost: null, estimate_s: 900, estimate_g: 3, notes: '', failure_reasons: ['adhesion'] },
];

(async () => {
  const server = http.createServer(async (req, res) => {
    try {
      const relative = new URL(req.url, 'http://localhost').pathname;
      const file = path.join(root, 'public', relative === '/' ? 'index.html' : relative);
      const content = await fs.readFile(file);
      res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript; charset=utf-8' : file.endsWith('.css') ? 'text/css; charset=utf-8' : 'text/html; charset=utf-8');
      res.end(content);
    } catch { res.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch();
  const open = async (width, { total = records.length, exportStatus = 200, unconfirmed = 1 } = {}) => {
    const page = await browser.newPage({ viewport: { width, height: 900 }, acceptDownloads: true });
    const seen = { prints: [], posts: [], exports: 0 };
    page.on('pageerror', e => console.error('Page error:', e.message));
    await page.routeWebSocket(/.*/, ws => ws.onMessage(message => {
      if (JSON.parse(message).action === 'list_printers') ws.send(JSON.stringify({ type: 'printer_update', printer: { id: 'p1', name: 'Printer One', connected: false, printer_type: 'cc1', status: {}, camera_url: null } }));
    }));
    await page.route('**/api/**', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/failure-reasons') return route.fulfill({ json: { cancel_reasons: ['adhesion', 'spaghetti'], labels: { adhesion: 'Poor adhesion', spaghetti: 'Spaghetti' } } });
      if (url.pathname === '/api/prints/export.csv') {
        seen.exports++;
        return exportStatus === 200
          ? route.fulfill({ status: 200, headers: { 'Content-Type': 'text/csv', 'Content-Disposition': 'attachment; filename="spooler-prints.csv"' }, body: 'id,file\n1,a\n' })
          : route.fulfill({ status: exportStatus, json: { error: 'Export unavailable' } });
      }
      if (/\/api\/prints\/\d+\/outcome$/.test(url.pathname)) { seen.posts.push(JSON.parse(route.request().postData() || '{}')); return route.fulfill({ json: { ...records[0], outcome: 'failed', outcome_unconfirmed: false, failure_reasons: seen.posts.at(-1).failure_reasons } }); }
      if (url.pathname === '/api/prints') {
        seen.prints.push(url.search);
        const onlyUnconfirmed = url.searchParams.get('unconfirmed') === '1';
        if (url.searchParams.get('limit') === '1' && onlyUnconfirmed) return route.fulfill({ json: { prints: [], total: unconfirmed } });
        return route.fulfill({ json: { prints: total ? records : [], total, summary: { prints: total, duration_s: 0, filament_g: 0, cost: 0 } } });
      }
      if (/\/api\/prints\/\d+$/.test(url.pathname)) return route.fulfill({ json: { ...records[0], job_events: [] } });
      return route.fulfill({ json: {} });
    });
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.locator('#card-p1').waitFor();
    return { page, seen };
  };
  try {
    // PS-30: chip and nav count
    {
      const { page, seen } = await open(1098);
      await page.locator('#history-unconfirmed-count').waitFor({ state: 'visible' });
      assert.equal((await page.locator('#history-unconfirmed-count').textContent()).replace(/\s+/g, ' ').trim(), '1 unconfirmed');
      await page.evaluate(() => openHistory());
      await page.locator('.history-row').first().waitFor();
      const chip = page.locator('#btn-history-unconfirmed');
      assert.equal((await chip.innerText()).trim(), '1 unconfirmed', 'the totals line carries the count');
      await chip.click();
      await page.waitForFunction(() => document.getElementById('history-outcome').value === 'unconfirmed');
      assert.ok(seen.prints.some(q => q.includes('unconfirmed=1') && q.includes('limit=50')), 'the chip sets the filter');
      await page.close();
      const none = await open(1098, { unconfirmed: 0 });
      await none.page.evaluate(() => openHistory());
      await none.page.locator('.history-row').first().waitFor();
      assert.equal(await none.page.locator('#btn-history-unconfirmed').count(), 0, 'no chip when nothing is unconfirmed');
      assert.equal(await none.page.locator('#history-unconfirmed-count').isVisible(), false);
      await none.page.close();
      console.log('PASS: unconfirmed chip and nav count (PS-30)');
    }

    // PS-31: outcome correction needs a reason for Failed, reads back the change
    {
      const { page, seen } = await open(1098);
      await page.evaluate(() => openHistory());
      const detailLoaded = page.waitForResponse(r => /\/api\/prints\/1$/.test(r.url()));
      await page.locator('.history-row').first().click();
      await detailLoaded;
      await page.waitForTimeout(100);   // the form re-renders once when the details arrive
      const readback = page.locator('[data-history-readback="1"]');
      assert.match(await readback.innerText(), /stays Completed and is marked confirmed/);
      await page.locator('[data-history-outcome="1"]').selectOption('failed');
      assert.match(await readback.innerText(), /changes from Completed to Failed and is marked confirmed/);
      assert.ok(await page.getByRole('group', { name: /What went wrong/ }).isVisible(), 'reasons sit in a labelled fieldset');
      await page.getByRole('button', { name: 'Confirm outcome', exact: true }).click();
      await page.locator('[data-history-feedback="1"]').filter({ hasText: 'Pick at least one failure reason' }).waitFor();
      assert.equal(seen.posts.length, 0, 'nothing is sent without a reason');
      await page.getByLabel('Poor adhesion').check();
      assert.match(await readback.innerText(), /Reasons: Poor adhesion/);
      await page.getByRole('button', { name: 'Confirm outcome', exact: true }).click();
      await page.locator('[data-history-feedback="1"]').filter({ hasText: 'Outcome saved and confirmed' }).waitFor();
      assert.deepEqual(seen.posts, [{ outcome: 'failed', failure_reasons: ['adhesion'] }]);
      await page.close();
      console.log('PASS: outcome correction rule and read-back (PS-31)');
    }

    // PS-32 desktop row fit; PS-33 phone rows, collapsed filters and chips
    {
      const { page } = await open(1098);
      await page.evaluate(() => openHistory());
      await page.locator('.history-row').first().waitFor();
      const geometry = await page.locator('.history-row').first().evaluate(row => {
        const top = el => Math.round(el.getBoundingClientRect().top);
        const badges = [...row.querySelectorAll('.status-badge')];
        return { rowH: row.getBoundingClientRect().height, tops: badges.map(top), dateTop: top(row.querySelector('.history-date')),
          statsClipped: row.querySelector('.history-stats').scrollWidth > row.querySelector('.history-stats').clientWidth + 1,
          dateText: row.querySelector('.history-date').textContent };
      });
      assert.equal(new Set(geometry.tops).size, 1, 'both badges share a line');
      assert.ok(geometry.rowH < 60, `desktop row stays one line (${geometry.rowH}px)`);
      assert.equal(geometry.statsClipped, false, 'the stats column has room');
      assert.ok(!/\b20\d\d\b/.test(geometry.dateText), 'this year needs no year in the date');
      await page.close();

      const phone = await open(390);
      await phone.page.evaluate(() => openHistory());
      await phone.page.locator('.history-row').first().waitFor();
      assert.equal(await phone.page.getByRole('searchbox', { name: 'Search history' }).isVisible(), false, 'filters start collapsed');
      const rowH = await phone.page.locator('.history-row').first().evaluate(row => row.getBoundingClientRect().height);
      assert.ok(rowH <= 125, `phone row is 3-4 lines (${rowH}px)`);
      await phone.page.getByRole('button', { name: 'Show filters' }).click();
      await phone.page.locator('#history-outcome').selectOption('failed');
      await phone.page.getByRole('button', { name: 'Hide filters' }).click();
      const chip = phone.page.getByRole('button', { name: 'Clear filter: Failed' });
      await chip.waitFor();
      await chip.click();
      await phone.page.waitForFunction(() => document.getElementById('history-outcome').value === '');
      await phone.page.close();
      console.log('PASS: row fit, phone rows, collapsed filters with chips (PS-32, PS-33)');
    }

    // PS-34: Export CSV is fetched; failure leaves the page, success downloads, zero results disables it
    {
      const { page, seen } = await open(1098);
      await page.evaluate(() => openHistory());
      await page.locator('.history-row').first().waitFor();
      const [download] = await Promise.all([page.waitForEvent('download'), page.locator('#btn-history-export').click()]);
      assert.equal(download.suggestedFilename(), 'spooler-prints.csv');
      assert.equal(seen.exports, 1);
      await page.close();

      const failing = await open(1098, { exportStatus: 503 });
      await failing.page.evaluate(() => openHistory());
      await failing.page.locator('.history-row').first().waitFor();
      const before = failing.page.url();
      await failing.page.locator('#btn-history-export').click();
      await failing.page.locator('#toast-area').getByText(/Couldn.t export history: Export unavailable/).waitFor();
      assert.equal(failing.page.url(), before, 'the page did not navigate away');
      assert.equal(await failing.page.locator('#card-p1').count(), 1, 'the dashboard is still there');
      await failing.page.close();

      const empty = await open(1098, { total: 0 });
      await empty.page.evaluate(() => openHistory());
      await empty.page.locator('#history-empty').waitFor({ state: 'visible' });
      assert.equal(await empty.page.locator('#btn-history-export').isDisabled(), true, 'Export is off with 0 results');
      await empty.page.close();
      console.log('PASS: Export CSV by fetch (PS-34)');
    }
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(err => { console.error(err); process.exitCode = 1; });
