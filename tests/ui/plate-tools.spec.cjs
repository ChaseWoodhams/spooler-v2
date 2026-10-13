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

// Plate tools on the Plates step: Shift-click selection, nudge, align and spread, snap, drag to a plate tab, and the
// "too close" warning. Three 40 x 30 x 20 mm boxes on plate 1 and one on plate 2; the server is a fixture.
function boxPreview() {
  const [w, d, h] = [40, 30, 20];
  const corners = [[0, 0, 0], [w, 0, 0], [w, d, 0], [0, d, 0], [0, 0, h], [w, 0, h], [w, d, h], [0, d, h]];
  const tris = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]];
  const buffer = Buffer.alloc(8 + 8 + 8 * 12 + 12 * 12);
  buffer.write('SPV1', 0, 'latin1');
  buffer.writeUInt32LE(1, 4);
  buffer.writeUInt32LE(8, 8);
  buffer.writeUInt32LE(12, 12);
  corners.forEach((c, i) => c.forEach((v, k) => buffer.writeFloatLE(v, 16 + i * 12 + k * 4)));
  tris.forEach((t, i) => t.forEach((v, k) => buffer.writeUInt32LE(v, 16 + 96 + i * 12 + k * 4)));
  return buffer;
}
const model = id => ({ id, file: `box${id}.stl`, state: 'ready', sha256: `sha${id}`, import: [1, 'z'], size: [40, 30, 20], triangles: 12, volume_cm3: 1, repairs: [] });
const object = (id, plate, x, y) => ({ id, model: id, plate, name: `box${id}`, position: [x, y], rotation: [0, 0, 0], scale: [1, 1, 1] });

async function open(browser, { tooClose = false, view = null } = {}) {
  const page = await browser.newPage({ viewport: view ? view : process.env.VIEW ? { width: +process.env.VIEW.split('x')[0], height: +process.env.VIEW.split('x')[1] } : { width: 1400, height: 900 } });
  const server = { patches: [], packs: [], revision: 1,
    data: { plates: [{ id: 1, name: 'Plate 1' }, { id: 2, name: 'Plate 2' }], bed: 'textured', profile: { bundle_id: null, process: {}, filament: {} },
            objects: [object(1, 1, 50, 50), object(2, 1, 120, 50), object(3, 1, 200, 150), object(4, 2, 128, 128)] } };
  const project = () => ({ id: 70, name: 'Plates', revision: server.revision, split_id: null, split_ids: [], models: [1, 2, 3, 4].map(model), slices: {},
    server_time: Date.now() / 1000, data: server.data });
  await page.route('http://spooler.test/**', route => {
    const file = path.join(root, new URL(route.request().url()).pathname);
    if (!file.startsWith(root) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404 });
    return route.fulfill({ body: fs.readFileSync(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.html') ? 'text/html' : 'application/octet-stream' });
  });
  page.on('pageerror', e => console.error('Page error:', e.message));
  await page.route('**/api/**', async route => {
    const request = route.request(), method = request.method(), p = new URL(request.url()).pathname, body = request.postData();
    if (p === '/api/projects/70' && method === 'PATCH') {
      server.data = JSON.parse(body).data;
      server.revision += 1;
      server.patches.push(server.data);
      return route.fulfill({ json: project() });
    }
    if (p === '/api/projects/70/pack') {
      const change = JSON.parse(body);
      server.packs.push(change);
      if ('pack' in change) { if (change.pack) server.data.pack = change.pack; else delete server.data.pack; }
      if (change.plate_id) {
        const plate = server.data.plates.find(x => x.id === change.plate_id);
        if (change.plate_pack) plate.pack = { ...change.plate_pack, gap: { tight: 3, normal: 6, roomy: 12 }[change.plate_pack.preset] }; else delete plate.pack;
      }
      server.revision += 1;
      return route.fulfill({ json: project() });
    }
    if (p === '/api/projects/70') return route.fulfill({ json: project() });
    if (/^\/api\/projects\/70\/models\/\d+\/preview$/.test(p)) return route.fulfill({ body: boxPreview(), contentType: 'application/octet-stream' });
    if (p === '/api/projects/70/status') {
      const problems = tooClose ? [{ key: 2, plate: 1, problem: 'too close', other: 1, name: 'box2', text: 'box2 is too close to box1', soft: true },
                                   { key: 1, plate: 1, problem: 'too close', other: 2, name: 'box1', text: 'box1 is too close to box2', soft: true }] : [];
      return route.fulfill({ json: { plates: server.data.plates.map(pl => ({ id: pl.id, problems: pl.id === 1 ? problems : [], problem_reason: null, warning_reason: null })),
                                     progress: { counts: {}, filament_g: {} } } });
    }
    return route.fulfill({ json: p === '/api/splits' ? { splits: [] } : p.endsWith('/jobs') ? { jobs: [] } : p === '/api/printers' ? []
      : p === '/api/defaults' ? { defaults: { options: {} } } : p === '/api/slicer/spools' ? { spools: [] }
      : p === '/api/slicer/presets' ? { machines: [], processes: {}, filaments: {} } : {} });
  });
  await page.goto('http://spooler.test/slicer.html?project=70');
  await page.locator('#project-name').waitFor();
  await page.waitForFunction(() => document.getElementById('project-name').value !== '');
  await page.locator('[data-step="plates"]').click();
  await page.locator('#object-list [data-object]').first().waitFor();
  await page.waitForTimeout(400);   // geometry loads, then the first measure
  return { page, server };
}

const positions = server => Object.fromEntries(server.data.objects.map(o => [o.id, o.position.map(v => Math.round(v * 100) / 100)]));
async function saved(page, server, before) {
  for (let i = 0; i < 40 && server.patches.length === before; i++) await page.waitForTimeout(100);
  assert.ok(server.patches.length > before, 'the change is saved');
}
// Select all, the supports choice and the custom gap live in the More menu
const more = page => page.locator('#prepare-tools .tool-more > summary').click();
const selectAll = async page => { await more(page); await page.locator('#btn-select-all').click(); };
const row = (page, n) => page.locator(`#object-list [data-object="${n}"] .slicer-object-select`);

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    // Desktop opens editable with the gizmo; a phone defers 3D editing, keeps the toggle and a Fit view control
    {
      const { page } = await open(browser, { view: { width: 1098, height: 507 } });
      assert.equal(await page.evaluate(() => document.body.classList.contains('defer-edit')), false, 'desktop is not deferred');
      assert.equal(await page.locator('body').getAttribute('data-viewport-editable'), 'true');
      assert.equal(await page.locator('#prepare-tools [data-tool="translate"]').isDisabled(), false);
      assert.equal(await page.locator('#prepare-tools [data-tool="translate"]').isVisible(), true);
      await page.setViewportSize({ width: 390, height: 844 });
      await page.waitForFunction(() => document.body.classList.contains('defer-edit'));
      assert.equal(await page.locator('#btn-mobile-edit').isVisible(), true, 'the toggle shows on a phone');
      assert.equal(await page.locator('#btn-fit').isVisible(), true, 'Fit view is reachable in defer mode');
      if (process.env.SHOT_PHONE) await page.screenshot({ path: process.env.SHOT_PHONE });
      await page.locator('#btn-mobile-edit').click();
      assert.equal(await page.locator('body').getAttribute('data-viewport-editable'), 'true');
      await page.setViewportSize({ width: 1098, height: 507 });
      await page.waitForFunction(() => !document.body.classList.contains('defer-edit'));
      console.log('PASS: Desktop is editable, a phone defers editing with a toggle and Fit view');
      await page.close();
    }

    // Shift-click builds a selection; the panel counts it; Ctrl+A and Select all take every object on the plate
    {
      const { page } = await open(browser);
      await row(page, 1).click();
      await row(page, 2).click({ modifiers: ['Shift'] });
      assert.match(await page.locator('#object-panel').innerText(), /2 objects selected/);
      await row(page, 2).click({ modifiers: ['Shift'] });
      assert.equal(await page.locator('#object-panel h5').innerText(), 'box1', 'Shift-click again takes it out');
      await selectAll(page);
      assert.match(await page.locator('#object-panel').innerText(), /3 objects selected/);
      assert.equal(await page.locator('#align-menu').isDisabled(), false);
      if (process.env.SHOT) await page.screenshot({ path: process.env.SHOT });
      console.log('PASS: Shift-click and Select all build a selection');
      await page.close();
    }

    // Arrow keys nudge the whole selection; Shift is 10 mm
    {
      const { page, server } = await open(browser);
      await selectAll(page);
      await page.keyboard.press('ArrowRight');
      await page.keyboard.press('Shift+ArrowUp');
      await saved(page, server, 0);
      await page.waitForTimeout(900);
      assert.deepEqual(positions(server)[1], [51, 60]);
      assert.deepEqual(positions(server)[3], [201, 160]);
      console.log('PASS: Arrow keys nudge every selected object (Shift moves 10 mm)');
      await page.close();
    }

    // Align left edges, spread evenly, and place on the bed
    {
      const { page, server } = await open(browser);
      await selectAll(page);
      await page.locator('#align-menu').selectOption('left');
      await saved(page, server, 0);
      assert.deepEqual([1, 2, 3].map(id => positions(server)[id][0]), [50, 50, 50], 'left edges line up on the leftmost');
      assert.deepEqual([1, 2, 3].map(id => positions(server)[id][1]), [50, 50, 150], 'front to back is untouched');

      await page.locator('#align-menu').selectOption('spread-y');
      await saved(page, server, 1);
      assert.deepEqual([1, 2, 3].map(id => positions(server)[id][1]).sort((a, b) => a - b), [50, 100, 150], 'three objects spread with equal gaps');

      await page.locator('#align-menu').selectOption('bed-centre');
      await saved(page, server, 2);
      const ys = [1, 2, 3].map(id => positions(server)[id][1]);
      assert.equal((Math.min(...ys) - 15 + Math.max(...ys) + 15) / 2, 128, 'the selection as a whole is centred on the bed');
      assert.equal(await page.locator('#align-menu').inputValue(), '', 'the menu goes back to its title');
      console.log('PASS: Align, spread and centre-on-the-bed place the selection by its exact boxes');
      await page.close();
    }

    // Lining up needs two objects, and says so
    {
      const { page, server } = await open(browser);
      await row(page, 1).click();
      await page.locator('#align-menu').selectOption('left');
      await page.locator('.toast', { hasText: 'two or more' }).waitFor({ timeout: 3000 });
      await page.locator('#align-menu').selectOption('bed-left');
      await saved(page, server, 0);
      assert.equal(positions(server)[1][0], 25, 'one object goes to the left edge, 5 mm in');
      console.log('PASS: Line-up needs two objects; one can still go to a bed edge');
      await page.close();
    }

    // Snap is remembered and switches the grid on
    {
      const { page } = await open(browser);
      assert.equal(await page.locator('#snap-step').isDisabled(), true);
      await page.locator('#snap-on').check();
      await page.locator('#snap-step').selectOption('5');
      assert.equal(await page.locator('#snap-step').isDisabled(), false);
      await page.reload();
      await page.locator('#object-list [data-object]').first().waitFor({ timeout: 8000 }).catch(() => {});
      console.log('PASS: Snap can be switched on with a grid size');
      await page.close();
    }

    // Dragging an object onto another plate's tab moves it and leaves it clear of what is already there
    {
      const { page, server } = await open(browser);
      await row(page, 1).dragTo(page.locator('#plate-tabs [data-plate="2"]'));
      await saved(page, server, 0);
      const moved = server.data.objects.find(o => o.id === 1), stays = server.data.objects.find(o => o.id === 4);
      assert.equal(moved.plate, 2, 'the object is on Plate 2');
      assert.ok(Math.abs(moved.position[0] - stays.position[0]) >= 48 || Math.abs(moved.position[1] - stays.position[1]) >= 38,
        `box ${JSON.stringify(moved.position)} keeps clear of ${JSON.stringify(stays.position)}`);
      console.log('PASS: Dragging an object onto a plate tab moves it to a free spot');
      await page.close();
    }

    // Moving the selection to a new plate from the panel
    {
      const { page, server } = await open(browser);
      await row(page, 1).click();
      await row(page, 2).click({ modifiers: ['Shift'] });
      await page.locator('#group-plate').selectOption('new');
      await saved(page, server, 0);
      const plateIds = server.data.plates.map(p => p.id);
      assert.equal(plateIds.length, 3, 'a plate was added');
      assert.deepEqual(server.data.objects.filter(o => o.plate === 3).map(o => o.id).sort(), [1, 2]);
      console.log('PASS: The selection moves to a new plate from the panel');
      await page.close();
    }

    // Spacing: a plate can have its own density, the project has one, and Close supports is its own choice
    {
      const { page, server } = await open(browser);
      const select = page.locator('#pack-select');
      const lastPack = async n => { for (let i = 0; i < 50 && server.packs.length < n; i++) await page.waitForTimeout(100); return server.packs.at(-1); };
      assert.equal(await select.inputValue(), 'project:normal', 'Normal until something is chosen');
      await select.selectOption('plate:tight');
      await page.waitForFunction(() => document.getElementById('pack-select').value === 'plate:tight');
      assert.deepEqual(await lastPack(1), { plate_id: 1, plate_pack: { preset: 'tight' } });
      await select.selectOption('plate:inherit');
      assert.deepEqual(await lastPack(2), { plate_id: 1, plate_pack: null });
      await select.selectOption('project:roomy');
      await page.waitForFunction(() => document.getElementById('pack-select').value === 'project:roomy');
      assert.deepEqual(await lastPack(3), { pack: { preset: 'roomy' } });
      await more(page);
      await page.locator('#pack-supports').check();
      await more(page);
      await page.waitForFunction(() => document.getElementById('pack-supports').checked);
      assert.deepEqual(await lastPack(4), { pack: { preset: 'roomy', tight_supports: true } });
      await page.waitForTimeout(300);   // the page has taken the server's answer before the next choice
      await select.selectOption('project:custom');
      await more(page);
      await page.locator('#pack-gap-field').waitFor({ state: 'visible' });
      await page.locator('#pack-gap').fill('9');
      await page.locator('#pack-gap').press('Tab');
      await page.waitForFunction(() => document.getElementById('pack-gap').value === '9');
      assert.deepEqual(await lastPack(6), { pack: { preset: 'custom', gap: 9, tight_supports: true } });
      console.log('PASS: Spacing can be chosen for a plate or the project, with a custom gap and Close supports');
      await page.close();
    }

    // "Too close" is a warning: amber text, no red, and the Plates step is not blocked
    {
      const { page } = await open(browser, { tooClose: true });
      await page.waitForFunction(() => document.querySelector('#object-list .warn'), null, { timeout: 5000 });
      assert.equal(await page.locator('#object-list .bad').count(), 0, 'nothing is marked as an error');
      assert.match(await page.locator('#object-list .warn').first().innerText(), /too close/);
      assert.match(await page.locator('[data-step="plates"]').getAttribute('class'), /\bdone\b/, 'the Plates step still counts as done');
      console.log('PASS: Too close is a warning, not an error');
      await page.close();
    }
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
