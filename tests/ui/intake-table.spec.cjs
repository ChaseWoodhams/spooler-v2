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

// A stateful fake of the draft + intake-files endpoints. Files named by what they are: small fits, big is bigger than the bed,
// metres is a unit mix-up, bad is refused at upload.
// A model reads as analysing for 1.2 s after its upload (the page polls every 1.5 s); a unit fix is answered at once.
const SHAPES = {
  'seat.stl': { size: [0.65, 0.974, 0.617], status: 'check_units', fits: true, after: 'split', fix: { factor: 1000, scale: 1000, label: "It's in metres: scale x1000" } },
  // the believable unit guesses, most common first; three of them make a row offer three choices
  'three.stl': { size: [0.974, 0.65, 0.617], status: 'check_units', fits: true, units: [[1000, 'metres'], [25.4, 'inches'], [100, 'decimetres']] },
  'small.stl': { size: [40, 30, 20], status: 'fits', fits: true },
  'big.stl': { size: [650, 400, 300], status: 'split', fits: false },
  'metres.stl': { size: [0.2, 0.1, 0.05], status: 'check_units', fits: true, fix: { factor: 1000, scale: 1000, label: "It's in metres: scale x1000" } },
};
const guesses = s => s.units || (s.fix ? [[s.fix.factor, 'metres']] : []);
const shell = id => ({ id, name: 'Untitled project', revision: 1, split_id: null, draft: true, models: [], slices: {}, server_time: Date.now() / 1000,
  data: { plates: [{ id: 1, name: 'Plate 1' }], objects: [], bed: 'textured', profile: { bundle_id: null, process: {}, filament: {} } } });

async function open(browser, viewport) {
  const page = await browser.newPage({ viewport });
  const server = { calls: [], models: [], nextModel: 1, closed: false };
  await page.route('http://spooler.test/**', route => {
    const file = path.join(root, new URL(route.request().url()).pathname);
    if (!file.startsWith(root) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404 });
    return route.fulfill({ body: fs.readFileSync(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.html') ? 'text/html' : 'application/octet-stream' });
  });
  page.on('pageerror', e => console.error('Page error:', e.message));
  const fixed = (s, m) => guesses(s).some(([factor]) => factor === m.scale);
  const rows = () => {
        const out = server.models.map(m => {
      const s = SHAPES[m.file], done = m.analysed || Date.now() - m.at > 1200;
      return { model: m.id, object: m.id + 100, file: m.file, kind: 'model', state: done ? 'ready' : 'analysing', error: null, size_bytes: 1000,
        size: done ? s.size.map(v => v * (m.scale || 1)) : null, scale: m.scale || 1, up: m.up || 'z',
        status: !done ? null : (fixed(s, m) ? (s.after || 'fits') : s.status), fits: done ? (fixed(s, m) ? !s.after : s.fits) : null,
        scale_ok: done ? s.status !== 'check_units' || fixed(s, m) : null, scale_fix: done && s.fix && !m.scale ? s.fix : null,
        scale_fixes: done && !m.scale ? guesses(s).map(([factor, unit]) => ({ factor, unit, scale: factor, size: s.size.map(v => Math.round(v * factor * 10) / 10) })) : [],
        reason: null, repairs: [], triangles: 12, volume_cm3: 1, copies: m.copies || 1, estimate: null };
    });
    const count = status => out.filter(r => r.status === status).length;
    const analysing = out.filter(r => r.state === 'analysing').length;
    const summary = { files: out.length, items: out.reduce((a, r) => a + r.copies, 0), split: count('split'), fits: count('fits'), check_units: count('check_units'),
      analysing, failed: 0, steps: ['plates'] };
    summary.can_create = out.length > 0 && !summary.check_units && !analysing;
    return { rows: out, summary };
  };
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url()), method = request.method(), p = url.pathname;
    const body = request.postData();
    if (method !== 'GET') server.calls.push({ method, path: p, params: Object.fromEntries(url.searchParams), body: body && p.includes('/models/upload') ? null : body });
    let m;
    if (p === '/api/projects' && method === 'GET') return route.fulfill({ json: { projects: [] } });
    if (p === '/api/projects' && method === 'POST') return route.fulfill({ json: shell(50) });
    if (p === '/api/projects/50/models/upload') {
      const file = url.searchParams.get('filename');
      if (file === 'bad.stl') return route.fulfill({ status: 400, json: { error: 'Not a valid STL file' } });
      const model = { id: server.nextModel++, file, copies: 1, at: Date.now() };
      server.models.push(model);
      return route.fulfill({ json: { ...shell(50), models: server.models.map(x => ({ id: x.id, file: x.file, state: 'importing' })) } });
    }
    if (p === '/api/projects/50/intake-files') { return route.fulfill({ json: rows() }); }
    if ((m = p.match(/^\/api\/projects\/50\/models\/(\d+)$/)) && method === 'DELETE') {
      server.models = server.models.filter(x => x.id !== Number(m[1]));
      return route.fulfill({ json: shell(50) });
    }
    if ((m = p.match(/^\/api\/projects\/50\/models\/(\d+)\/settings$/))) {
      Object.assign(server.models.find(x => x.id === Number(m[1])), { ...JSON.parse(body), analysed: true });
      return route.fulfill({ json: shell(50) });
    }
    if (p === '/api/projects/50/copies') {
      const change = JSON.parse(body);
      server.models.find(x => x.id + 100 === change.object).copies = change.copies;
      return route.fulfill({ json: shell(50) });
    }
    if (p === '/api/projects/50/create') {
      server.created = true;
      return route.fulfill({ json: { project: { ...shell(50), draft: false, name: JSON.parse(body).name || 'Untitled project' }, step: 'plates' } });
    }
    if (p === '/api/projects/50/discard-draft') { server.discarded = true; return route.fulfill({ json: { ok: true } }); }
    if (p === '/api/projects/50') return route.fulfill({ json: { ...shell(50), draft: false } });
    return route.fulfill({ json: p === '/api/splits' ? { splits: [] }
      : p.endsWith('/status') ? { plates: [], progress: { counts: {}, filament_g: {} } }
      : p.endsWith('/jobs') ? { jobs: [] } : p === '/api/printers' ? []
      : p === '/api/defaults' ? { defaults: { options: {} } } : p === '/api/slicer/spools' ? { spools: [] }
      : p === '/api/slicer/presets' ? { machines: [], processes: {}, filaments: {} } : {} });
  });
  await page.goto('http://spooler.test/slicer.html');
  await page.locator('#dlg-projects').waitFor({ state: 'visible' });
  return { page, server };
}

const drop = (page, names) => page.evaluate(names => {
  const dt = new DataTransfer();
  for (const name of names) dt.items.add(new File(['solid x\nendsolid x\n'], name));
  document.getElementById('dlg-projects').dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
}, names);
const noPageScroll = page => page.evaluate(() => ({ doc: document.documentElement.scrollWidth <= innerWidth,
  dialog: document.getElementById('dlg-projects').scrollWidth <= document.getElementById('dlg-projects').clientWidth }));

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const { page, server } = await open(browser, { width: 1280, height: 800 });
    const table = page.locator('#new-intake');
    await drop(page, ['small.stl', 'big.stl', 'metres.stl', 'bad.stl']);

    // Rows appear at once, before the server has analysed anything
    await table.getByText('small.stl', { exact: true }).waitFor({ timeout: 5000 });
    assert.equal(await table.locator('[data-intake-row]').count(), 4, 'one row per dropped file');
    const create = server.calls.find(c => c.path === '/api/projects');
    assert.deepEqual(JSON.parse(create.body).draft, true, 'the first drop makes a draft project');
    assert.equal(await page.evaluate(() => localStorage.getItem('slicer.project')), null, 'a draft is never remembered as the last-open project');
    await table.getByText('Checking…').first().waitFor({ timeout: 5000 });
    console.log('PASS: First drop creates a draft and shows a row per file, pending while analysed');

    // Rows fill in as the server analyses; a failed upload shows its error and does not stop the rest
    const row = name => table.locator('[data-intake-row]', { hasText: name });
    await row('big.stl').getByText('Too big, will be split').waitFor({ timeout: 8000 });
    assert.match(await row('big.stl').innerText(), /650 × 400 × 300 mm/);
    assert.match(await row('small.stl').innerText(), /Fits the bed/);
    assert.match(await row('small.stl').innerText(), /40 × 30 × 20 mm/);
    assert.match(await row('metres.stl').innerText(), /Check units/);
    assert.match(await row('bad.stl').innerText(), /Not a valid STL file/);
    assert.equal(server.models.length, 3, 'the other files were still uploaded');
    assert.equal(server.calls.filter(c => c.path === '/api/projects/50/models/upload').length, 4);
    console.log('PASS: Rows fill in size and status; a failed file shows its error and the rest continue');

    // Auto-name until the user types
    const name = page.locator('#new-project-name');
    assert.equal(await name.inputValue(), 'small + 2 more');
    await name.fill('Chair');
    await row('bad.stl').getByRole('button', { name: /Remove/ }).click();
    await row('big.stl').getByRole('button', { name: /Remove big\.stl/ }).click();
    await page.waitForFunction(() => document.querySelectorAll('[data-intake-row]').length === 2);
    assert.equal(await name.inputValue(), 'Chair', 'typing a name stops auto-naming');
    assert.ok(server.calls.some(c => c.method === 'DELETE' && c.path === '/api/projects/50/models/2'), 'removing a row removes the model from the project');
    console.log('PASS: Auto-name from the files until a name is typed; remove deletes the model');

    // Copies
    const copies = row('small.stl').getByLabel(/Copies/);
    assert.equal(await copies.inputValue(), '1');
    await copies.fill('3');
    await copies.press('Tab');
    await page.waitForFunction(() => true);
    const call = await (async () => { for (let i = 0; i < 50; i++) { const c = server.calls.find(x => x.path === '/api/projects/50/copies'); if (c) return c; await page.waitForTimeout(100); } })();
    assert.deepEqual({ object: JSON.parse(call.body).object, copies: JSON.parse(call.body).copies }, { object: 101, copies: 3 });
    console.log('PASS: Copies writes the existing per-object Copies');

    // Check units blocks Create; the one-click fix unblocks it
    const createButton = page.getByRole('button', { name: 'Create project', exact: true });
    assert.equal(await createButton.isDisabled(), true, 'Create is blocked while a row says Check units');
    assert.deepEqual(await noPageScroll(page), { doc: true, dialog: true }, 'no horizontal scroll at 1280');
    await row('metres.stl').getByRole('button', { name: /^Metres/ }).click();
    await row('metres.stl').getByText('Fits the bed').waitFor({ timeout: 8000 });
    assert.deepEqual(JSON.parse(server.calls.find(c => c.path === '/api/projects/50/models/3/settings').body), { scale: 1000 });
    await page.waitForFunction(() => !document.getElementById('btn-new-create').disabled);
    console.log('PASS: Check units blocks Create until the one-click fix is applied');

    // Set units and up axis: per file, offered on every ready row, and no units question on a plausible model
    assert.equal(await row('small.stl').getByText(/Check units|Looks wrong/).count(), 0, 'a plausible model shows no units question');
    assert.equal(await table.locator('.intake-units-panel').count(), 0, 'the choice stays closed until asked for');
    await row('small.stl').getByRole('button', { name: /units and up axis/i }).click();
    const panel = row('small.stl').locator('.intake-units-panel');
    assert.deepEqual(await panel.getByLabel('Units').locator('option').allInnerTexts(), ['Millimetres', 'Centimetres', 'Inches', 'Metres']);
    assert.deepEqual(await panel.getByLabel('Up axis').locator('option').allInnerTexts(), ['Z', 'Y']);
    assert.equal(await panel.getByLabel('Units').inputValue(), '1');
    await panel.getByLabel('Units').selectOption({ label: 'Inches' });
    await panel.getByLabel('Up axis').selectOption({ label: 'Y' });
    await panel.getByRole('button', { name: 'Apply' }).click();
    await page.waitForFunction(() => document.querySelector('[data-intake-row] .intake-units-panel') === null);
    const unitCall = server.calls.filter(c => c.path === '/api/projects/50/models/1/settings').map(c => JSON.parse(c.body));
    assert.deepEqual(unitCall, [{ scale: 25.4, up: 'y' }], 'only that file is changed');
    assert.equal(server.calls.filter(c => c.path.endsWith('/settings')).length, 2, 'the other rows are untouched (this one plus the earlier fix)');
    console.log('PASS: Set units and up axis changes one file only');

    // Create promotes the draft and opens the returned step; closing afterwards does not discard it
    await createButton.click();
    await page.waitForFunction(() => !document.getElementById('dlg-projects').open);
    assert.equal(JSON.parse(server.calls.find(c => c.path === '/api/projects/50/create').body).name, 'Chair');
    await page.waitForFunction(() => location.search.includes('project=50'));
    assert.equal(server.discarded, undefined, 'a created project is not discarded');
    assert.equal(await page.evaluate(() => localStorage.getItem('slicer.project')), '50', 'a promoted project is remembered');
    console.log('PASS: Create promotes the draft, names it and opens the project');
    await page.close();

    // Closing the page without creating discards the draft with a beacon
    const second = await open(browser, { width: 1280, height: 800 });
    await drop(second.page, ['small.stl']);
    await second.page.locator('#new-intake').getByText('small.stl', { exact: true }).waitFor();
    assert.equal(await second.page.locator('#new-project-name').inputValue(), 'small', 'one file names the project after it');
    await second.page.locator('#dlg-projects').getByRole('button', { name: 'Close', exact: true }).click();
    for (let i = 0; i < 50 && !second.server.discarded; i++) await second.page.waitForTimeout(100);
    assert.equal(second.server.discarded, true, 'closing the dialog discards the draft');
    assert.equal(await second.page.evaluate(() => localStorage.getItem('slicer.project')), null);
    console.log('PASS: Closing without Create discards the draft');
    await second.page.close();

    // SEAT: 0.65 x 0.974 x 0.617 read as metres; after x1000 it is 650 x 974 x 617 mm, too big for the bed, not Check units
    const seat = await open(browser, { width: 1280, height: 800 });
    await drop(seat.page, ['seat.stl']);
    const seatRow = seat.page.locator('#new-intake [data-intake-row]');
    await seatRow.getByRole('button', { name: /^Metres/ }).click({ timeout: 8000 });
    await seatRow.getByText('Too big, will be split').waitFor({ timeout: 8000 });
    assert.doesNotMatch(await seatRow.innerText(), /Check units|Looks wrong/);
    assert.match(await seatRow.innerText(), /650 × 974 × 617 mm/);
    console.log('PASS: SEAT at x1000 is Too big, will be split');
    await seat.page.close();

    // Several believable guesses: up to three choices, each with its resulting size; the second one is applied alone
    const choices = await open(browser, { width: 1280, height: 800 });
    await drop(choices.page, ['three.stl']);
    const choiceRow = choices.page.locator('#new-intake [data-intake-row]');
    await choiceRow.locator('[data-fix]').first().waitFor({ timeout: 8000 });
    assert.deepEqual(await choiceRow.locator('[data-fix]').allInnerTexts(),
      ['Metres: 974 × 650 × 617 mm', 'Inches: 24.7 × 16.5 × 15.7 mm', 'Decimetres: 97.4 × 65 × 61.7 mm']);
    assert.equal(await choiceRow.getByRole('button', { name: 'Units and up axis' }).count(), 1, 'the units link stays');
    await choiceRow.getByRole('button', { name: /^Inches/ }).click();
    await choiceRow.getByText('Fits the bed').waitFor({ timeout: 8000 });
    assert.deepEqual(choices.server.calls.filter(c => c.path.endsWith('/settings')).map(c => JSON.parse(c.body)), [{ scale: 25.4 }]);
    console.log('PASS: A row offers up to three unit choices with resulting sizes and applies the one picked');
    await choices.page.close();

    // Narrow screen
    const narrow = await open(browser, { width: 390, height: 844 });
    await drop(narrow.page, ['small.stl', 'big.stl', 'metres.stl']);
    await narrow.page.locator('#new-intake').getByText('Too big, will be split').waitFor({ timeout: 8000 });
    assert.deepEqual(await noPageScroll(narrow.page), { doc: true, dialog: true }, 'no horizontal scroll at 390');
    console.log('PASS: The table stays usable at 390 px without horizontal scroll');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
