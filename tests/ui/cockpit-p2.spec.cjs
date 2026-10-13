// Ticket 19: cockpit P2 batch (temperature pills, compact camera, units, home labels, focus on re-render)
// and the Files browser (search, sort, folders, delete waits for the printer) and the Job review modal.
// Fixtures only: every printer message is recorded and none is sent anywhere.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const publicDir = process.env.UI_PUBLIC_DIR || path.join(root, 'public'); // point at another checkout to see a guard fail there

const basePrinter = { id: 'p1', name: 'Cockpit fixture', ip: '192.0.2.20', printer_type: 'cc1', connected: true, camera_url: null,
  attrs: { FirmwareVersion: '01.04.00' },
  status: { PrintInfo: { Status: 0 }, TempOfNozzle: 22, TempOfHotbed: 21, TempOfBox: 24, TempTargetNozzle: 0, TempTargetHotbed: 0,
    CurrentCoord: '202.00,150.00,10.00' } };
const printingStatus = { ...basePrinter.status, PrintInfo: { Status: 3, Filename: 'benchy.gcode', Progress: 40, RemainTime: 1200, TotalLayer: 100, CurrentLayer: 40 } };
const files = [
  { name: 'zebra.gcode', path: 'zebra.gcode', size: 5000 },
  { name: 'alpha.gcode', path: 'alpha.gcode', size: 90000 },
  { name: 'benchy.gcode', path: 'models/benchy.gcode', size: 40000 },
  { name: 'cube.gcode', path: 'models/small/cube.gcode', size: 1000 },
];
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function open(browser, base, { width = 1098, height = 507, camera = false, status } = {}) {
  const context = await browser.newContext({ viewport: { width, height }, serviceWorkers: 'block' });
  const page = await context.newPage();
  page.on('pageerror', e => console.error('Browser error:', e.message));
  const fx = { page, context, commands: [], sockets: [], files: files.slice(), deleteWorks: true,
    printer: { ...basePrinter, camera_url: camera ? '/cam' : null, status: status || basePrinter.status } };
  await page.routeWebSocket(/.*/, socket => {
    fx.sockets.push(socket);
    socket.onMessage(raw => {
      const msg = JSON.parse(raw);
      fx.commands.push(msg);
      if (msg.action === 'list_printers') socket.send(JSON.stringify({ type: 'printer_update', printer: fx.printer }));
      if (msg.action === 'list_files') socket.send(JSON.stringify({ type: 'file_list', printer_id: 'p1', files: fx.files }));
      if (msg.action === 'delete_file' && fx.deleteWorks) fx.files = fx.files.filter(f => f.path !== msg.filename);
    });
  });
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith('/api/camera/')) return route.abort();
    if (url.pathname === '/api/jobs/1/preflight') return route.fulfill({ json: fx.report });
    if (url.pathname === '/api/outcome-prompts') return route.fulfill({ json: { awaiting: [] } });
    return route.fulfill({ json: {} });
  });
  fx.update = data => { fx.printer = { ...fx.printer, ...data }; for (const s of fx.sockets) s.send(JSON.stringify({ type: 'printer_update', printer: fx.printer })); };
  await page.goto(`${base}/#printer=p1`);
  await page.getByRole('tab', { name: 'Motion', exact: true }).waitFor();
  return fx;
}

(async () => {
  const server = http.createServer(async (req, res) => {
    try {
      const p = new URL(req.url, 'http://localhost').pathname;
      const file = path.join(publicDir, p === '/' ? 'index.html' : p);
      res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
      res.end(await fs.readFile(file));
    } catch { res.writeHead(404).end(); }
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch();
  try {
    // PS-15 / PS-20: labelled temperature pills, units, firmware, distinguishable Home buttons, one verb
    {
      const fx = await open(browser, base);
      const { page } = fx;
      const pills = await page.locator('.pv-pill').evaluateAll(els => els.map(e => ({ text: e.innerText.replace(/\s+/g, ' ').trim(), label: e.getAttribute('aria-label'), icon: !!e.querySelector('svg'), size: parseFloat(getComputedStyle(e.querySelector('.pv-pill-label')).fontSize) })));
      assert.deepEqual(pills.map(p => p.text.split(' ')[0]), ['Nozzle', 'Bed', 'Chamber']);
      assert.ok(pills.every(p => p.icon && p.size >= 12 && /degrees/.test(p.label)), 'icon, >=12px label and an aria-label on every pill');
      assert.match(pills[0].text, /\/0$/, 'target shows as /0');
      assert.doesNotMatch(await page.locator('.pv-pills').innerText(), /[🔥▦▢]/u, 'no emoji/glyph icons');
      assert.match(await page.locator('.pv-xyz').innerText(), /X 202\.00 mm · Y 150\.00 mm · Z 10\.00 mm/);
      assert.match(await page.locator('.pv-title small').innerText(), /firmware 01\.04\.00/);
      const homes = await page.locator('.pv-jog .home').evaluateAll(els => els.map(e => e.innerText.replace(/\s+/g, '')));
      assert.deepEqual(homes, ['⌂XY', '⌂Z'], 'the two Home buttons read differently');
      await page.getByRole('button', { name: 'Home all', exact: true }).waitFor();
      await page.getByRole('tab', { name: 'Temperatures', exact: true }).click();
      await page.getByRole('button', { name: 'Set nozzle target', exact: true }).waitFor();
      assert.equal(await page.getByRole('button', { name: 'Set bed target', exact: true }).innerText(), 'Set');
      const small = await page.locator('.pv-tabbody small').first().evaluate(e => parseFloat(getComputedStyle(e).fontSize));
      assert.ok(small >= 12, `helper text ${small}px >= 12px`);
      await fx.context.close();
      console.log('PASS: Pills, units, firmware line, Home labels and one verb');
    }

    // PS-17: the hero no longer pushes the controls below the fold on a short screen; compact mode and no-feed collapse
    {
      const feed = await open(browser, base, { camera: true });
      const bar = async () => (await feed.page.locator('.pv-tabs').boundingBox());
      const before = await bar();
      assert.ok(before.y + before.height <= 507, `tab bar fully above the fold at 507px (y=${before.y})`);
      await feed.page.locator('[data-focus-key=compact]').click();
      const after = await bar();
      assert.ok(after.y < before.y - 20, 'compact mode pulls the tab bar up');
      assert.equal(await feed.page.locator('[data-focus-key=compact]').getAttribute('aria-pressed'), 'true');
      assert.equal(await feed.page.locator('[data-focus-key=compact]').evaluate(e => document.activeElement === e), true, 'focus stays on the toggle');
      await feed.page.reload();
      await feed.page.locator('.pv-hero.compact').waitFor();
      await feed.context.close();
      const none = await open(browser, base, { camera: false });
      const hero = await none.page.locator('.pv-hero').evaluate(e => ({ nofeed: e.classList.contains('nofeed'), h: e.getBoundingClientRect().height }));
      assert.ok(hero.nofeed && hero.h <= 200, `no feed collapses the hero (${hero.h}px)`);
      assert.equal(await none.page.locator('[data-focus-key=compact]').count(), 0, 'no compact toggle without a feed');
      await none.context.close();
      console.log('PASS: Compact camera mode and collapsed hero without a feed');
    }

    // T8: a live status update must not drop focus from an id-less cockpit button
    {
      const fx = await open(browser, base, { status: printingStatus });
      const { page } = fx;
      for (const key of ['pause', 'stop', 'step-1', 'back']) {
        await page.locator(`[data-focus-key=${key}]`).focus();
        fx.update({ status: { ...printingStatus, TempOfNozzle: 200 + Math.random() } });
        await page.waitForFunction(() => /^2\d\d\.\d°/.test(document.querySelector('.pv-pill')?.innerText.replace(/^\S+\s+/, '') || ''));
        assert.equal(await page.evaluate(() => document.activeElement?.dataset.focusKey), key, `focus stays on ${key} after a status update`);
      }
      await fx.context.close();
      console.log('PASS: Focus survives cockpit re-renders (data-focus-key)');
    }

    // PS-18: search, sort, folders, and a delete that waits for the printer's own list
    {
      const fx = await open(browser, base);
      const { page } = fx;
      await page.getByRole('tab', { name: 'Files', exact: true }).click();
      await page.getByRole('tabpanel').getByRole('button', { name: 'Browse & start print', exact: true }).click();
      const names = () => page.locator('#files-list .file-item').evaluateAll(els => els.map(e => e.querySelector('.file-name').innerText.replace('📁 ', '').trim()));
      await page.locator('#files-list .file-item').first().waitFor();
      assert.deepEqual(await names(), ['models', 'alpha.gcode', 'zebra.gcode'], 'folders first, then files by name');
      await page.locator('#files-sort').selectOption('size');
      assert.deepEqual(await names(), ['models', 'alpha.gcode', 'zebra.gcode'], 'size sort: largest first');
      await page.locator('#files-sort').selectOption('name');
      await page.locator('#files-search').fill('cube');
      assert.deepEqual(await names(), ['cube.gcode'], 'search reaches into folders');
      assert.equal(await page.locator('#files-search').evaluate(e => document.activeElement === e), true, 'typing keeps focus in the search box');
      await page.locator('#files-search').fill('');
      await page.getByRole('button', { name: 'Open folder models', exact: true }).click();
      assert.deepEqual(await names(), ['small', 'benchy.gcode']);
      await page.getByRole('button', { name: 'Open folder small', exact: true }).click();
      assert.deepEqual(await names(), ['cube.gcode']);
      await page.locator('.file-up').click(); await page.locator('.file-up').click();
      assert.deepEqual(await names(), ['models', 'alpha.gcode', 'zebra.gcode']);

      // Delete: confirm, row says Deleting…, then "Deleted" once the printer's list no longer has it
      fx.deleteWorks = false;
      await page.getByRole('button', { name: 'Delete zebra.gcode', exact: true }).click();
      await page.getByRole('button', { name: 'Delete file', exact: true }).click();
      await page.locator('.file-deleting').waitFor();
      assert.equal(await page.locator('.file-deleting .file-delete').innerText(), 'Deleting…');
      assert.equal(await page.locator('.file-deleting .file-delete').isDisabled(), true);
      assert.equal(fx.commands.filter(c => c.action === 'delete_file').length, 1);
      await sleep(1500);
      assert.equal(await page.locator('.file-deleting').count(), 1, 'still waiting while the printer lists the file');
      fx.files = fx.files.filter(f => f.path !== 'zebra.gcode');
      await page.locator('.toast', { hasText: 'Deleted zebra.gcode' }).waitFor({ timeout: 4000 });
      assert.equal(await page.locator('.file-deleting').count(), 0);
      assert.deepEqual(await names(), ['models', 'alpha.gcode']);
      await fx.context.close();
      console.log('PASS: Files: search, sort, folders and a delete that waits for the printer');
    }

    // PS-18: a delete the printer never performs ends in an error, not a silent success
    {
      const fx = await open(browser, base);
      const { page } = fx;
      await page.clock.install();
      fx.deleteWorks = false;
      await page.getByRole('tab', { name: 'Files', exact: true }).click();
      await page.getByRole('tabpanel').getByRole('button', { name: 'Browse & start print', exact: true }).click();
      await page.getByRole('button', { name: 'Delete alpha.gcode', exact: true }).click();
      await page.getByRole('button', { name: 'Delete file', exact: true }).click();
      await page.locator('.file-deleting').waitFor();
      for (let i = 0; i < 12; i++) { await page.clock.runFor(1000); await sleep(30); }
      await page.locator('.toast.error', { hasText: 'alpha.gcode is still on the printer' }).waitFor();
      assert.equal(await page.locator('.file-deleting').count(), 0);
      await fx.context.close();
      console.log('PASS: A delete that never happens reports an error');
    }

    // PS-19: separate warnings and bed ticks, one wording, a text alternative and a named nozzle Save
    {
      const fx = await open(browser, base);
      const { page } = fx;
      fx.report = { status: 'warn', can_start: true, needs_acknowledgement: true, printer_id: 'p1',
        checks: [{ id: 'nozzle', level: 'info', title: 'Nozzle', detail: 'Unknown' }, { id: 'x', level: 'warn', title: 'Thin walls', detail: 'Check them' }], preview: null };
      await page.evaluate(() => openReview({ id: 1, name: 'cube.gcode', file_name: 'cube.gcode', printer_id: 'p1', source: 'upload' }));
      await page.locator('#review-check-list .review-check.warn').waitFor();
      assert.equal(await page.locator('#modal-review input[type=checkbox]:visible').count(), 2, 'warnings and bed are separate ticks');
      assert.match(await page.locator('#review-warnings-label').innerText(), /does not confirm the bed is clear/);
      await page.locator('#review-ack').check();
      assert.equal(await page.locator('#btn-review-start').isDisabled(), true, 'the bed tick alone does not cover the warnings');
      await page.locator('#review-warnings-ack').check();
      assert.equal(await page.locator('#btn-review-start').isDisabled(), false, 'both ticks enable Start');
      const names = await page.locator('#modal-review button:visible').evaluateAll(els => els.map(e => e.getAttribute('aria-label') || e.innerText.trim()));
      assert.ok(names.includes('Check again') && !names.some(n => /^Recheck$/.test(n)), `one wording: ${names}`);
      assert.ok(names.includes('Save installed nozzle size'));
      assert.equal(await page.locator('#review-canvas').getAttribute('role'), 'img');
      assert.match(await page.locator('#review-canvas').getAttribute('aria-label'), /Toolpath preview/);
      assert.equal(await page.locator('label[for=review-nozzle-size]').count(), 1);
      // Accepting warnings for later is a separate, confirmed step that never claims the bed is clear
      await page.locator('#btn-review-accept').click();
      assert.match(await page.locator('.modal.open', { hasText: 'Accept these warnings for later?' }).innerText(), /does not confirm the bed is clear/);
      await fx.context.close();
      console.log('PASS: Job review: separate warnings and bed ticks, one wording, named controls');
    }

    // T15: one placeholder token that passes contrast on the input fills
    {
      const fx = await open(browser, base);
      const { page } = fx;
      await page.getByRole('tab', { name: 'Files', exact: true }).click();
      await page.getByRole('tabpanel').getByRole('button', { name: 'Browse & start print', exact: true }).click();
      const color = await page.locator('#files-search').evaluate(e => getComputedStyle(e, '::placeholder').color);
      const bg = await page.locator('#files-search').evaluate(e => getComputedStyle(e).backgroundColor);
      const lum = c => { const [r, g, b] = c.match(/[\d.]+/g).slice(0, 3).map(Number).map(v => (v /= 255) <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4); return .2126 * r + .7152 * g + .0722 * b; };
      const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + .05) / (y + .05); };
      assert.ok(ratio(color, bg) >= 4.5, `placeholder ${color} on ${bg}: ${ratio(color, bg).toFixed(2)}`);
      await fx.context.close();
      console.log('PASS: Placeholder token contrast');
    }
  } finally { await browser.close(); server.close(); }
})().catch(e => { console.error(e); process.exit(1); });
