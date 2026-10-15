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

async function open(browser, { tooClose = false, view = null, slices = {}, bundle = null } = {}) {
  const page = await browser.newPage({ viewport: view ? view : process.env.VIEW ? { width: +process.env.VIEW.split('x')[0], height: +process.env.VIEW.split('x')[1] } : { width: 1400, height: 900 } });
  const server = { patches: [], packs: [], queued: [], revision: 1,
    data: { plates: [{ id: 1, name: 'Plate 1' }, { id: 2, name: 'Plate 2' }], bed: 'textured', profile: { bundle_id: bundle, process: {}, filament: {} },
            objects: [object(1, 1, 50, 50), object(2, 1, 120, 50), object(3, 1, 200, 150), object(4, 2, 128, 128)] } };
  const project = () => ({ id: 70, name: 'Plates', revision: server.revision, split_id: null, split_ids: [], models: [1, 2, 3, 4].map(model), slices,
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
    if (p === '/api/projects/70/queue' && method === 'POST') { server.queued.push(JSON.parse(body)); return route.fulfill({ json: { jobs: [] } }); }
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
    if (/^\/api\/projects\/70\/plates\/\d+\/(analysis|toolpath)$/.test(p)) return route.fulfill({ status: 500, json: { error: 'no preview in this fixture' } });   // the preview is not under test
    if (p.startsWith("/api/slicer/profiles/") && p.endsWith("/settings")) return route.fulfill({ json: { machine: { printable_area: ["0x0", "256x0", "256x256", "0x256"] }, process: { layer_height: "0.2" }, filament: {}, machine_name: "M" } });
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
  if (bundle) { await page.waitForTimeout(600); return { page, server }; }   // lands on Print when every Plate is sliced
  await page.locator('[data-step="plates"]').click();
  await page.locator('#object-list [data-object]').first().waitFor();
  await page.waitForTimeout(400);   // geometry loads, then the first measure
  return { page, server };
}


const check=(name,ok,detail)=>{ assert.ok(ok,name+(detail?': '+detail:'')); console.log('PASS: '+name); };
const css=(page,sel,prop)=>page.locator(sel).first().evaluate((el,p)=>getComputedStyle(el)[p],prop);

(async () => {
  const browser = await chromium.launch();
  try {
    // PS-46 Select all and Orient all are on the bar; the More panel scrolls instead of clipping
    {
      const { page } = await open(browser, { view: { width: 1400, height: 700 } });
      assert.equal(await page.locator('#btn-select-all').isVisible(), true, 'Select all is visible without opening More');
      assert.equal(await page.locator('#btn-orient-all').isVisible(), true, 'Orient all is visible without opening More');
      assert.equal(await page.locator('#btn-select-all').evaluate(el => !el.closest('.tool-more-panel')), true, 'Select all is outside the More panel');
      await page.locator('#prepare-tools .tool-more > summary').click();
      assert.equal(await css(page, '#prepare-tools .tool-more-panel', 'overflowY'), 'auto');
      assert.notEqual(await css(page, '#prepare-tools .tool-more-panel', 'maxHeight'), 'none');
      const box = await page.locator('#prepare-tools .tool-more-panel').boundingBox();
      check('More panel stays inside the window', box.y + box.height <= 700 + 1, JSON.stringify(box));
      await page.close();
    }

    // PS-45 an open Print settings fold grows to its content
    {
      const { page } = await open(browser);
      await page.locator('#fold-settings > summary').click();
      assert.equal(await css(page, '#fold-settings', 'flexGrow'), '0');
      assert.equal(await css(page, '#fold-settings', 'flexShrink'), '0');
      check('Open Print settings fold is not stretched', true);
      await page.close();
    }

    // PS-47 the object panel is not pushed away by a long object list; Duplicate is apart from the orientation actions; filaments grouped by brand
    {
      const { page } = await open(browser, { view: { width: 1400, height: 800 } });
      assert.ok(await page.locator('#object-list').evaluate(el => el.getBoundingClientRect().height <= innerHeight * 0.3 + 1), 'object list is capped');
      await page.locator('#object-list [data-object="1"] .slicer-object-select').click();
      const order = await page.evaluate(() => document.getElementById('object-list').compareDocumentPosition(document.getElementById('object-panel')));
      assert.ok(order & 4, 'panel follows the list');
      const rows = await page.locator('#object-panel .slicer-actions-row').count();
      assert.ok(rows >= 2, 'object actions are in more than one group, got ' + rows);
      assert.equal(await page.locator('#object-panel [data-object-action="duplicate"]').evaluate(el => !el.parentElement.querySelector('[data-object-action="turn-left"]')), true, 'Duplicate is not beside Turn');
      check('Object panel and list sizing, grouped actions', true);
      await page.close();
    }

    // PS-44 the stepper stays put between steps at 1000 px
    {
      const { page } = await open(browser, { view: { width: 1000, height: 800 } });
      const pos = async () => page.locator('#step-bar').evaluate(el => { const r = el.getBoundingClientRect(); return [Math.round(r.top), Math.round(r.height)]; });
      const seen = {};
      for (const step of ['plates', 'slice', 'print']) { await page.locator('[data-step="' + step + '"]').click(); await page.waitForTimeout(250); seen[step] = await pos(); }
      assert.deepEqual(seen.plates, seen.slice, 'stepper moved between Plates and Slice: ' + JSON.stringify(seen));
      assert.deepEqual(seen.plates, seen.print, 'stepper moved between Plates and Print: ' + JSON.stringify(seen));
      check('Stepper does not jump between steps at 1000 px', true);
      await page.close();
    }

    // PS-42 and PS-43 phone: compact header, canvas gets at least half the screen, no plate add/remove while editing is deferred
    {
      const { page } = await open(browser, { view: { width: 390, height: 844 } });
      assert.equal(await page.evaluate(() => document.body.classList.contains('defer-edit')), true);
      assert.equal(await page.locator('[data-plate="new"]').isVisible(), false, '+ Plate is hidden in defer mode');
      assert.equal(await page.locator('[data-plate="remove"]').count() === 0 || await page.locator('[data-plate="remove"]').isVisible() === false, true, '- Remove plate is hidden in defer mode');
      assert.equal(await page.locator('#btn-fit').isVisible(), true, 'Fit view stays');
      assert.equal(await page.locator('#btn-undo').isVisible(), true, 'Undo stays');
      const header = await page.locator('.slicer-top').evaluate(el => el.getBoundingClientRect().height);
      const canvas = await page.locator('#viewport').evaluate(el => el.getBoundingClientRect().height);
      check('Phone header is at most 30% and the canvas at least 50% of the screen', header <= 844 * 0.3 && canvas >= 844 * 0.5, 'header ' + header + ', canvas ' + canvas);
      await page.locator('#btn-mobile-edit').click();
      assert.equal(await page.locator('[data-plate="new"]').isVisible(), true, '+ Plate returns with editing controls');
      await page.close();
    }


    // PS-48 a finished Slice step reads as a status, Slice again is a normal button, and the header button names its effect
    {
      const done = { state: 'sliced', outdated: false, layers: 10, estimate_s: 600, estimate_g: 5 };
      const { page, server } = await open(browser, { slices: { 1: done, 2: done }, bundle: 'b1' });
      await page.locator('[data-step="slice"]').click();
      await page.locator('#slicing-list [data-slice-plate]').first().waitFor();
      assert.equal(await page.locator('#slicing-actions [data-slice-all]').count(), 0, 'no disabled Slice all button');
      assert.match(await page.locator('#slicing-actions').innerText(), /All plates sliced/);
      assert.equal(await page.locator('#slicing-list [data-slice-plate="1"]').isEnabled(), true, 'Slice again is enabled');
      assert.match(await page.locator('#slicing-list [data-slice-plate="1"]').innerText(), /Slice again/);
      assert.match(await page.locator('[data-send-this-plate]').innerText(), /^Send Plate 1 to /);
      check('Slice step: status line, Slice again enabled, named header action', true);
      // SWU-08 the forward action is "Print →" and sends nothing; the direct send is secondary and confirms
      assert.equal(await page.locator('#step-actions .btn-primary').innerText(), 'Print →');
      assert.equal(await page.locator('[data-send-this-plate]').evaluate(b => b.classList.contains('btn-secondary') && !b.classList.contains('btn-primary')), true, 'direct send is secondary');
      await page.locator('#step-actions [data-go="print"]').click();
      await page.waitForSelector('.slicer-step.active[data-step="print"]');
      await page.waitForTimeout(300);
      assert.deepEqual(server.queued, [], 'Print → sent no Job');
      await page.locator('[data-step="slice"]').click();
      await page.locator('[data-send-this-plate]').click();
      const dialog = page.locator('.confirm-dialog');
      await dialog.waitFor();
      assert.match(await dialog.innerText(), /Plate 1/);
      assert.match(await dialog.innerText(), /next free printer/);
      await dialog.getByRole('button', { name: /cancel/i }).click();
      await page.waitForTimeout(200);
      assert.deepEqual(server.queued, [], 'cancelling the confirmation sent nothing');
      await page.locator('[data-send-this-plate]').click();
      await dialog.waitFor();
      await dialog.locator('.btn-primary').click();
      await page.waitForTimeout(400);
      assert.equal(server.queued.length, 1, 'confirming sends the Plate');
      check('Print → sends nothing; direct send confirms naming Plate and printer', true);
      // Send Plates says why it is disabled, tied to the button
      await page.locator('[data-step="print"]').click();
      await page.waitForSelector('#pp-send');
      assert.equal(await page.locator('#pp-send').getAttribute('aria-disabled'), 'true');
      const why = await page.locator('#pp-send').getAttribute('aria-describedby');
      assert.match(await page.locator('#' + why).innerText(), /Tick the Plates to send/);
      check('Send Plates names why it is disabled', true);
      await page.close();
    }

    // T15 nothing that carries state is under 12 px in the editor
    {
      const { page } = await open(browser);
      await page.locator('#object-list [data-object="1"] .slicer-object-select').click();
      const small = await page.evaluate(() => [...document.querySelectorAll('body *')].filter(el => {
        if (!el.getClientRects().length) return false;
        const own = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
        return own && parseFloat(getComputedStyle(el).fontSize) < 12;
      }).map(el => el.tagName + '.' + el.className + ' ' + getComputedStyle(el).fontSize));
      assert.deepEqual(small, [], 'text under 12px: ' + [...new Set(small)].join(' | '));
      check('Editor text is at least 12 px', true);
      await page.close();
    }
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
