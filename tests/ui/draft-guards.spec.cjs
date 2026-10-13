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

// The draft guards of the New project dialog: every draft call names its draft (draft_token = created_at), a draft that is gone
// stops the dialog with a plain message, a dialog closed while the draft is being made discards it, a dead server stops the
// polling with one message, and a G-code file in a multi-model drop is named rather than dropped silently.
const TOKEN = 1700000000.5;
const GONE = 'This new project was already created or cleared away. Start again.';
const shell = id => ({ id, name: 'Untitled project', revision: 1, split_id: null, split_ids: [], draft: true, created_at: TOKEN, models: [], slices: {},
  server_time: Date.now() / 1000, data: { plates: [{ id: 1, name: 'Plate 1' }], objects: [], bed: 'textured', profile: { bundle_id: null, process: {}, filament: {} } } });

async function open(browser) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const server = { calls: [], models: [], nextModel: 1, gone: false, down: false, analysing: false, polls: 0, release: null, holdDraft: false };
  await page.route('http://spooler.test/**', route => {
    const file = path.join(root, new URL(route.request().url()).pathname);
    if (!file.startsWith(root) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404 });
    return route.fulfill({ body: fs.readFileSync(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.html') ? 'text/html' : 'application/octet-stream' });
  });
  page.on('pageerror', e => console.error('Page error:', e.message));
  const rows = () => {
    const out = server.models.map(m => ({ model: m.id, object: m.id + 100, file: m.file, kind: 'model', state: server.analysing ? 'analysing' : 'ready', error: null, size_bytes: 1000,
      size: server.analysing ? null : [40, 30, 20], scale: 1, up: 'z', status: server.analysing ? null : 'fits', fits: !server.analysing, scale_ok: true, scale_fix: null,
      scale_fixes: [], reason: null, repairs: [], triangles: 12, volume_cm3: 1, copies: m.copies || 1, estimate: null }));
    const analysing = out.filter(r => r.state === 'analysing').length;
    return { rows: out, summary: { models: out.length, objects: out.length, split: 0, fits: out.length - analysing, check_units: 0, analysing, failed: 0, can_create: out.length > 0 && !analysing, steps: ['plates'] } };
  };
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url()), method = request.method(), p = url.pathname;
    const body = request.postData();
    server.calls.push({ method, path: p, params: Object.fromEntries(url.searchParams), body: p.includes('/models/upload') ? null : body });
    let m;
    if (p === '/api/projects' && method === 'GET') return route.fulfill({ json: { projects: [] } });
    if (p === '/api/projects' && method === 'POST') {
      if (server.holdDraft) await new Promise(resolve => { server.release = resolve; });
      return route.fulfill({ json: shell(50) });
    }
    if (p === '/api/projects/50/models/upload') {
      server.models.push({ id: server.nextModel++, file: url.searchParams.get('filename'), copies: 1 });
      return route.fulfill({ json: shell(50) });
    }
    if (p === '/api/projects/50/intake-files') {
      server.polls++;
      if (server.gone) return route.fulfill({ status: 409, json: { error: GONE } });
      if (server.down) return route.fulfill({ status: 500, json: { error: 'boom' } });
      return route.fulfill({ json: rows() });
    }
    if ((m = p.match(/^\/api\/projects\/50\/models\/(\d+)$/)) && method === 'DELETE') {
      server.models = server.models.filter(x => x.id !== Number(m[1]));
      return route.fulfill({ json: shell(50) });
    }
    if (p === '/api/projects/50/copies') {
      if (server.gone) return route.fulfill({ status: 409, json: { error: GONE } });
      server.models.find(x => x.id + 100 === JSON.parse(body).object).copies = JSON.parse(body).copies;
      return route.fulfill({ json: shell(50) });
    }
    if (p === '/api/projects/50/create') return route.fulfill({ json: { project: { ...shell(50), draft: false }, step: 'plates' } });
    if (p === '/api/projects/50/discard-draft') { server.discarded = true; return route.fulfill({ json: { ok: true } }); }
    if (p === '/api/projects/50') return route.fulfill({ json: { ...shell(50), draft: false } });
    return route.fulfill({ json: p === '/api/splits' ? { splits: [] }
      : p.endsWith('/status') ? { plates: [], progress: { counts: {}, filament_g: {} } }
      : p.endsWith('/jobs') ? { jobs: [] } : p === '/api/printers' ? []
      : p === '/api/defaults' ? { defaults: { options: {} } } : p === '/api/slicer/spools' ? { spools: [] }
      : p === '/api/slicer/presets' ? { machines: [], processes: {}, filaments: {} } : {} });
  });
  await page.goto('http://spooler.test/slicer.html?new=1');
  await page.locator('#dlg-projects').waitFor({ state: 'visible' });
  return { page, server };
}

const drop = (page, names) => page.evaluate(names => {
  const dt = new DataTransfer();
  for (const name of names) dt.items.add(new File(['solid x\nendsolid x\n'], name));
  document.getElementById('dlg-projects').dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
}, names);
const waitFor = async (fn, what) => { for (let i = 0; i < 80; i++) { if (fn()) return; await new Promise(r => setTimeout(r, 100)); } assert.fail(`timed out: ${what}`); };

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    // Every draft call carries the draft's created_at
    {
      const { page, server } = await open(browser);
      await drop(page, ['small.stl']);
      const row = page.locator('#new-intake [data-intake-row]', { hasText: 'small.stl' });
      await row.getByText('Fits the bed').waitFor({ timeout: 8000 });
      const copies = row.getByLabel(/Copies/);
      await copies.fill('2');
      await copies.press('Tab');
      await waitFor(() => server.calls.some(c => c.path === '/api/projects/50/copies'), 'copies');
      await page.waitForFunction(() => !document.getElementById('btn-new-create').disabled);
      await row.getByRole('button', { name: 'Remove small.stl' }).click();
      await waitFor(() => server.calls.some(c => c.method === 'DELETE'), 'remove');
      await drop(page, ['again.stl']);
      await page.waitForFunction(() => !document.getElementById('btn-new-create').disabled);
      await page.locator('#btn-new-create').click();
      await waitFor(() => server.calls.some(c => c.path === '/api/projects/50/create'), 'create');
      const draftCalls = server.calls.filter(c => c.path.startsWith('/api/projects/50/'));
      assert.ok(draftCalls.length >= 5);
      for (const c of draftCalls) assert.equal(c.params.draft_token, String(TOKEN), `${c.method} ${c.path} names its draft`);
      console.log('PASS: Every draft call carries the draft token');
      await page.close();
    }

    // A draft that is gone (swept; its id may belong to another project now) stops the dialog with a plain message
    {
      const { page, server } = await open(browser);
      await drop(page, ['small.stl']);
      const row = page.locator('#new-intake [data-intake-row]', { hasText: 'small.stl' });
      await row.getByText('Fits the bed').waitFor({ timeout: 8000 });
      server.gone = true;
      const copies = row.getByLabel(/Copies/);
      await copies.fill('3');
      await copies.press('Tab');
      await page.locator('#new-message').waitFor({ state: 'visible', timeout: 5000 });
      assert.match(await page.locator('#new-message').innerText(), /no longer there/i);
      assert.equal(await page.locator('#new-intake').isHidden(), true, 'the table is cleared');
      assert.equal(await page.locator('#btn-new-create').isDisabled(), true, 'Create stays on show but cannot be used');
      assert.match(await page.locator('#new-create-reason').innerText(), /add a model/i, 'and says what to do');
      assert.equal(server.discarded, undefined, 'nothing is discarded: the id is not ours any more');
      await page.close();
      console.log('PASS: A swept draft stops the dialog with a plain message');
    }

    // Closing the dialog while the draft is still being made discards it as soon as it exists
    {
      const { page, server } = await open(browser);
      server.holdDraft = true;
      await drop(page, ['small.stl']);
      await waitFor(() => server.release, 'the draft request');
      await page.evaluate(() => { window.__closed = false; const d = document.getElementById('dlg-projects'); d.addEventListener('close', () => { window.__closed = true; }); d.close(); });
      await page.waitForFunction(() => window.__closed);   // the page has seen the close before the draft request is answered
      server.release();
      await waitFor(() => server.discarded, 'the discard beacon');
      const discard = server.calls.find(c => c.path === '/api/projects/50/discard-draft');
      assert.equal(discard.params.draft_token, String(TOKEN));
      assert.equal(server.calls.some(c => c.path === '/api/projects/50/models/upload'), false, 'nothing is uploaded into the abandoned draft');
      await page.close();
      console.log('PASS: Closing while the draft is being made discards it once it exists');
    }

    // A dead server stops the polling after a few failures, with one message and no toast per poll
    {
      const { page, server } = await open(browser);
      server.analysing = true;
      await drop(page, ['small.stl']);
      await page.locator('#new-intake [data-intake-row]', { hasText: 'small.stl' }).getByText('Checking…').waitFor({ timeout: 8000 });
      server.down = true;
      await page.locator('#new-message').waitFor({ state: 'visible', timeout: 15000 });
      assert.match(await page.locator('#new-message').innerText(), /isn't answering/i);
      const polls = server.polls;
      await page.waitForTimeout(3500);
      assert.equal(server.polls, polls, 'the polling stopped');
      assert.ok(await page.locator('.toast').count() <= 1, 'at most one toast, not one per poll');
      assert.match(await page.locator('#new-create-reason').innerText(), /Reload/);
      await page.close();
      console.log('PASS: Repeated failed checks stop the polling with one message');
    }

    // A G-code file in a multi-model drop is named, and the models still go in
    {
      const { page, server } = await open(browser);
      await drop(page, ['small.stl', 'part.gcode']);
      await page.locator('#new-message').waitFor({ state: 'visible', timeout: 5000 });
      assert.match(await page.locator('#new-message').innerText(), /part\.gcode/);
      assert.match(await page.locator('#new-message').innerText(), /G-code/);
      await page.locator('#new-intake [data-intake-row]', { hasText: 'small.stl' }).waitFor({ timeout: 5000 });
      assert.equal(server.calls.filter(c => c.path === '/api/projects/50/models/upload').length, 1);
      await page.close();
      console.log('PASS: A G-code file in a multi-model drop gets a plain message');
    }
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
