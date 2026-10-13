// Ticket 22: Projects list and the Project Print step (PS-36 to PS-41)
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
let playwright;
try { playwright = require(process.env.PLAYWRIGHT_MODULE || 'playwright'); }
catch (error) {
  // Managed sibling worktrees share the integration checkout's installed dependencies.
  playwright = require(path.resolve(__dirname, '../../../../spooler-v2/node_modules/playwright'));
}
const { chromium } = playwright;
const root = path.resolve(__dirname, '../../public');
const now = Date.now() / 1000;

const listed = [
  { id: 7, name: 'Bracket', plates: 2, objects: 3, updated_at: now - 3 * 3600, split_id: null, sliced: 2, slicing: 0, queued: 0, printing: 0, printed_ok: 1, failed: 0, outcome_unknown: 1, busy: false },
  { id: 8, name: 'Empty one', plates: 1, objects: 0, updated_at: now - 40 * 86400, split_id: null, sliced: 0, slicing: 0, queued: 0, printing: 0, printed_ok: 0, failed: 0, outcome_unknown: 0, busy: false },
];
const project = (id = 1) => ({ id, name: 'Fixture project', revision: 1, split_id: null, models: [{ id: 1, name: 'Cube', state: 'failed', error: 'Fixture preview unavailable', size: [20, 20, 20] }], slices: {}, server_time: now,
  data: { plates: [{ id: 1, name: 'Plate 1', printer_id: 'p1' }, { id: 2, name: 'Plate 2', printer_id: 'p2' }], objects: [{ id: 1, name: 'Cube A', model: 1, plate: 1, position: [20, 20, 0], rotation: [0, 0, 0], scale: [1, 1, 1] }, { id: 2, name: 'Cube B', model: 1, plate: 2, position: [30, 30, 0], rotation: [0, 0, 0], scale: [1, 1, 1] }], bed: 'textured', profile: { bundle_id: null, process: {}, filament: {} } } });
const split = { id: 1, source_name: 'fixture-small.stl', state: 'ready', settings: { scale: 1, up: 'z', printer: { bed_x: 240, bed_y: 240, height: 250 }, planner: { mode: 'smart', balanced: false }, connectors: { kind: 'none' } },
  cuts: { cuts: [] }, analysis: { raw_size: [0.1, 0.2, 0.3], size: [0.1, 0.2, 0.3], volume_cm3: 0.001, source_triangles: 12, triangles: 12, repairs: [], mean_wall_mm: 0.1, fits_whole: true, estimated_parts: 1 } };
const status = (over = {}) => ({ plates: [{ id: 1, name: 'Plate 1', status: 'sliced', parts: 1, printer_id: 'p1', estimate_s: 600, estimate_g: 5 },
  { id: 2, name: 'Plate 2', status: 'sliced', parts: 1, printer_id: 'p2', estimate_s: 1200, estimate_g: 10 }], print_order: [1, 2],
  progress: { total: 2, printed_ok: 0, outcome_unknown: 0, counts: { unsliced: 0, sliced: 2, waiting: 0, queued: 0, printing: 0, printed_ok: 0, failed: 0 }, filament_g: { total: 15, used: 0 } }, ...over });
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==', 'base64');
const printers = [{ id: 'p1', name: 'Printer One', printer_type: 'cc1', connected: true }, { id: 'p2', name: 'Printer Two', printer_type: 'cc1', connected: true }];

async function open(browser, { width, height, url, autoStart = { p1: false, p2: true }, statusBody = status() }) {
  const page = await browser.newPage({ viewport: { width, height: height || (width < 700 ? 844 : 800) } });
  const calls = [];
  await page.route('http://spooler.test/**', route => {
    const file = path.join(root, new URL(route.request().url()).pathname);
    if (!file.startsWith(root) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404 });
    return route.fulfill({ body: fs.readFileSync(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.html') ? 'text/html' : 'application/octet-stream' });
  });
  page.on('pageerror', e => console.error('Page error:', e.message));
  await page.route('**/api/**', route => {
    const u = new URL(route.request().url()), method = route.request().method();
    if (method !== 'GET') calls.push({ method, path: u.pathname, body: route.request().postData() });
    if (u.pathname === '/api/projects/7/plates/1/preview.png') return route.fulfill({ body: png, contentType: 'image/png' });
    const bed = u.pathname.match(/^\/api\/printers\/(p\d)\/bed-cleared$/);
    const data = u.pathname === '/api/projects' && method === 'GET' ? { projects: listed }
      : u.pathname === '/api/splits' ? { splits: [] }
      : u.pathname === '/api/projects/7' ? project(7)
      : u.pathname === '/api/projects/1' ? project(1)
      : u.pathname === '/api/splits/1' ? split
      : u.pathname.endsWith('/status') ? statusBody
      : u.pathname.endsWith('/jobs') ? { jobs: [] }
      : u.pathname === '/api/printers' ? printers
      : bed ? { auto_start: autoStart[bed[1]] }
      : u.pathname === '/api/outcome-prompts' ? { awaiting: [] }
      : u.pathname === '/api/defaults' ? { defaults: { options: {} } }
      : u.pathname === '/api/slicer/spools' ? { spools: [] }
      : u.pathname === '/api/slicer/presets' ? { machines: [], processes: {}, filaments: {} }
      : u.pathname.endsWith('/preview.png') ? null : {};
    if (data === null) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ json: data });
  });
  await page.goto(url);
  return { page, calls };
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    // PS-40: the whole row opens the Project, by pointer and by keyboard; chips say what needs doing; dates are relative
    {
      const { page } = await open(browser, { width: 1280, url: 'http://spooler.test/slicer.html' });
      const row = page.locator('.slicer-project-row[data-name="bracket"]');
      await row.waitFor();
      const text = await row.innerText();
      assert.match(text, /1 completed|Printed OK/i, 'a completed chip');
      assert.match(text, /1 needs outcome|Needs outcome/i, 'an Outcome-to-confirm chip');
      assert.match(text, /3 hours? ago|3 h ago/i, 'a relative date');
      assert.doesNotMatch(text, /\d{1,2}\/\d{1,2}\/\d{2,4}/, 'no raw locale timestamp');
      assert.match(await page.locator('.slicer-project-row[data-name="empty one"]').innerText(), /last month|40 days ago|1 month ago/i);
      assert.equal(await row.locator('img.pd-thumb').count(), 1, 'a thumbnail when the Project has objects');
      assert.equal(await page.locator('.slicer-project-row[data-name="empty one"] img.pd-thumb').count(), 0, 'none when it is empty');
      const openBtn = row.getByRole('button', { name: 'Open Bracket' });
      await openBtn.focus();
      assert.equal(await page.evaluate(() => document.activeElement.matches('[data-open-project]')), true, 'the open control is a real, focusable button');
      const box = await row.boundingBox();
      assert.ok((await openBtn.boundingBox()).width > box.width * 0.6 || await page.evaluate(() => {
        const el = document.querySelector('.slicer-project-row[data-name="bracket"] [data-open-project]');
        const after = getComputedStyle(el, '::after');
        return after.position === 'absolute' && after.content !== 'none';
      }), 'the open control covers the row');
      await page.mouse.click(box.x + box.width / 2, box.y + box.height - 6);   // on the row, not on a button
      await page.waitForFunction(() => /project=7/.test(location.search) || document.title.includes('Fixture project') || !!document.querySelector('#step-bar .slicer-step'), null, { timeout: 8000 });
      console.log('PASS: Projects list row opens by click and keyboard, with Outcome chip, relative date and thumbnail');
      await page.close();
    }

    // PS-41: the New project dialog
    {
      const { page } = await open(browser, { width: 1280, url: 'http://spooler.test/slicer.html' });
      await page.locator('#btn-pd-new').click();
      const create = page.locator('#btn-new-create');
      assert.equal(await create.isVisible(), true, 'Create project is visible before any model is added');
      assert.equal(await create.isDisabled(), true);
      assert.match(await page.locator('#new-create-reason').innerText(), /add (a )?model/i, 'and says why');
      assert.equal(await create.getAttribute('aria-describedby'), 'new-create-reason');
      const drop = page.locator('#new-drop');
      assert.equal(await drop.getAttribute('tabindex'), '0');
      assert.ok(await drop.getAttribute('role'), 'the dropzone has a role');
      const types = await page.locator('#new-drop-types').innerText();
      assert.match(types, /STL/); assert.match(types, /OBJ/); assert.match(types, /3MF/); assert.match(types, /G-code/i);
      assert.equal(await page.locator('#new-drop-types').evaluate(el => (el.textContent.match(/\./g) || []).length), 1, 'one sentence');
      assert.equal(await drop.getAttribute('aria-describedby'), 'new-drop-types');
      await drop.focus();
      const chooser = page.waitForEvent('filechooser', { timeout: 4000 });
      await page.keyboard.press('Enter');
      await chooser;
      const chooser2 = page.waitForEvent('filechooser', { timeout: 4000 });
      await drop.focus();
      await page.keyboard.press('Space');
      await chooser2;
      console.log('PASS: New project dialog: visible disabled Create with a reason, one accepted-types sentence, keyboard dropzone');
      await page.close();
    }

    // PS-36, PS-37, PS-38, PS-39 on desktop and phone
    for (const width of [1280, 390]) {
      const { page, calls } = await open(browser, { width, url: 'http://spooler.test/slicer.html?project=1&step=print' });
      await page.locator('#project-page .pp-progress').waitFor({ timeout: 8000 });
      // PS-36 / PS-37: no sidebar on Print, so no toggle and no editing note
      assert.equal(await page.locator('#btn-sidebar').isVisible(), false, `${width}: no Hide sidebar on Print`);
      assert.equal(await page.locator('.slicer-small-screen-note').isVisible(), false, `${width}: no editing note on Print`);
      // PS-38: chips for non-zero counts only; the empty case says so
      const head = await page.locator('.pp-progress').innerText();
      assert.match(head, /2 sliced/);
      assert.doesNotMatch(head, /0 unsliced|0 queued|0 held|0 printing|0 failed|0 completed/);
      assert.match(head, /No outcomes to confirm/);
      assert.doesNotMatch(head, /0 latest Outcome/);
      const label = await page.locator('.pp-progress .pp-bar[role="img"]').first().getAttribute('aria-label');
      assert.match(label, /2 sliced/, 'the status bar names its parts');
      // PS-39: one explanation, real switches, 44 px, confirm on enable
      assert.equal(await page.locator('.pp-autostart-note').count(), 1, 'one shared explanation');
      const switches = page.getByRole('switch', { name: /Auto-start/ });
      assert.equal(await switches.count(), 2);
      for (let i = 0; i < 2; i++) {
        assert.equal(await switches.nth(i).getAttribute('aria-describedby'), 'pp-autostart-note');
        const b = await page.locator('.pp-autostart').nth(i).boundingBox();
        assert.ok(b.height >= 43.5 && b.width >= 43.5, `${width}: switch hit area ${b.width}x${b.height}`);
      }
      const first = switches.nth(0);   // p1 starts off
      assert.equal(await first.isChecked(), false);
      await first.click();
      const dlg = page.getByRole('dialog', { name: /Auto-start/ });
      await dlg.waitFor({ timeout: 4000 });
      await dlg.getByRole('button', { name: 'Cancel' }).click();
      await page.waitForTimeout(200);
      assert.equal(await first.isChecked(), false, 'Cancel leaves it off');
      assert.equal(calls.filter(c => /auto-start/.test(c.path)).length, 0, 'and sends nothing');
      await first.click();
      await dlg.waitFor({ timeout: 4000 });
      await dlg.getByRole('button', { name: /Turn on/ }).click();
      await page.waitForFunction(() => document.querySelector('[data-autostart="p1"]').checked);
      assert.equal(calls.filter(c => /auto-start/.test(c.path)).length, 1);
      const second = switches.nth(1);   // p2 starts on; turning off needs no question
      await second.click();
      await page.waitForFunction(() => !document.querySelector('[data-autostart="p2"]').checked);
      assert.equal(await page.getByRole('dialog').count(), 0, 'turning it off does not ask');
      assert.equal(calls.filter(c => /auto-start/.test(c.path)).length, 2);
      console.log(`PASS: Print step at ${width}px: no sidebar toggle or editing note, count chips, auto-start switches`);
      await page.close();
    }

    // PS-38: an Outcome to confirm is a chip with the count
    {
      const body = status();
      body.progress.outcome_unknown = 2; body.progress.counts.printing = 1; body.progress.counts.sliced = 1;
      const { page } = await open(browser, { width: 1280, url: 'http://spooler.test/slicer.html?project=1&step=print', statusBody: body });
      await page.locator('.pp-progress').waitFor({ timeout: 8000 });
      const head = await page.locator('.pp-progress').innerText();
      assert.match(head, /1 printing/);
      assert.match(head, /2 latest Outcomes? unknown/);
      assert.doesNotMatch(head, /No outcomes to confirm/);
      console.log('PASS: Progress header keeps the unknown-Outcome count when there is one');
      await page.close();
    }

    // PS-37: on a short phone the Print step has one scroll surface under the head, not a page scroll plus an inner one
    {
      const { page } = await open(browser, { width: 390, height: 480, url: 'http://spooler.test/slicer.html?project=1&step=print' });
      await page.locator('#project-page .pp-progress').waitFor({ timeout: 8000 });
      const r = await page.evaluate(() => {
        const surfaces = [];
        for (const el of document.querySelectorAll('body *')) {
          const cs = getComputedStyle(el);
          if (/(auto|scroll)/.test(cs.overflowY) && el.scrollHeight > el.clientHeight + 1 && el.getClientRects().length) surfaces.push(el.id || el.className || el.tagName);
        }
        const over = [...document.querySelectorAll('body > *, body > * > *')].filter(el => el.getBoundingClientRect().bottom > innerHeight + 1).map(el => (el.id || el.className || el.tagName) + ' ' + Math.round(el.getBoundingClientRect().bottom));
        return { surfaces, over, doc: document.scrollingElement.scrollHeight - innerHeight };
      });
      assert.ok(r.surfaces.length + (r.doc > 1 ? 1 : 0) <= 1, 'one scroll surface on Print at 390x480: ' + JSON.stringify(r));
      console.log('PASS: Print step on a short phone has one scroll surface');
      await page.close();
    }

    // PS-37: the editing note shows on an editing step on a phone
    {
      const { page } = await open(browser, { width: 390, url: 'http://spooler.test/slicer.html?project=1&step=print' });
      await page.locator('#project-page .pp-progress').waitFor({ timeout: 8000 });
      const noteOnPlates = await page.evaluate(async () => {
        document.querySelector('[data-step="plates"]')?.click();
        await new Promise(r => setTimeout(r, 600));
        return { step: document.body.dataset.workflowStep, shown: getComputedStyle(document.querySelector('.slicer-small-screen-note')).display !== 'none' };
      });
      assert.equal(noteOnPlates.step, 'plates');
      assert.equal(noteOnPlates.shown, true, 'the note stays on the Plates step');
      console.log('PASS: Editing note stays on editing steps');
      await page.close();
    }
  } finally {
    await browser.close();
  }
})().catch(e => { console.error(e); process.exit(1); });
