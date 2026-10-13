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

// The Projects page: where Spooler lands with no project open. Rows are the old Projects dialog's, with where each project has got to.
const PROJECTS = [
  { id: 7, name: 'Garden chair', plates: 3, objects: 16, models: 4, sliced: 3, slicing: 0, queued: 0, printing: 0, busy: false, split_id: null, updated_at: 1_800_000_300, created_at: 1 },
  { id: 8, name: 'Lamp shade', plates: 2, objects: 5, models: 5, sliced: 1, slicing: 0, queued: 1, printing: 1, busy: false, split_id: 3, updated_at: 1_800_000_200, created_at: 1 },
  { id: 9, name: 'Bracket set', plates: 1, objects: 12, models: 3, sliced: 0, slicing: 1, queued: 0, printing: 0, busy: true, split_id: null, updated_at: 1_800_000_100, created_at: 1 },
  { id: 10, name: 'Fresh start', plates: 1, objects: 0, models: 0, sliced: 0, slicing: 0, queued: 0, printing: 0, busy: false, split_id: null, updated_at: 1_800_000_000, created_at: 1 },
];
const project = (id, models = []) => ({ id, name: PROJECTS.find(p => p.id === id).name, revision: 1, split_id: null, split_ids: [], models, slices: {}, server_time: Date.now() / 1000,
  data: { plates: [{ id: 1, name: 'Plate 1' }], objects: [], bed: 'textured', profile: { bundle_id: null, process: {}, filament: {} } } });

async function open(browser, { projects = PROJECTS, search = '', setup } = {}) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const server = { calls: [], projects: [...projects], listFail: false, projectDelay: 0, previewGate: null, models: [] };
  await page.addInitScript(() => { try { localStorage.setItem('slicer.project', '7'); } catch (_) { /* storage blocked */ } });   // a last-open project must not be reopened by itself
  await page.route('http://spooler.test/**', route => {
    const file = path.join(root, new URL(route.request().url()).pathname);
    if (!file.startsWith(root) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404 });
    return route.fulfill({ body: fs.readFileSync(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.html') ? 'text/html' : 'application/octet-stream' });
  });
  page.on('pageerror', e => console.error('Page error:', e.message));
  await page.route('**/api/**', async route => {
    const request = route.request(), method = request.method(), p = new URL(request.url()).pathname;
    if (method !== 'GET') server.calls.push({ method, path: p });
    let m;
    if (p === '/api/projects' && method === 'GET') return server.listFail ? route.fulfill({ status: 503, json: { error: 'Fixture list unavailable' } }) : route.fulfill({ json: { projects: server.projects } });
    if (/\/models\/\d+\/preview$/.test(p)) {   // a model's mesh: held back until the test releases it; the fixture has no real mesh
      if (server.previewGate) await server.previewGate;
      return route.fulfill({ status: 404, json: { error: 'Fixture has no mesh' } });
    }
    if (/^\/api\/projects\/\d+$/.test(p) && method === 'GET' && server.projectDelay) await new Promise(resolve => setTimeout(resolve, server.projectDelay));
    if ((m = p.match(/^\/api\/projects\/(\d+)$/)) && method === 'DELETE') {
      server.projects = server.projects.filter(x => x.id !== Number(m[1]));
      return route.fulfill({ json: { ok: true } });
    }
    if ((m = p.match(/^\/api\/projects\/(\d+)$/))) return route.fulfill({ json: project(Number(m[1]), server.models) });
    return route.fulfill({ json: p === '/api/splits' ? { splits: [{ id: 3, project_id: 8 }, { id: 4, project_id: null, source_name: 'loose.stl', parts: 6, state: 'split' }] }
      : p.endsWith('/status') ? { plates: [], progress: { counts: {}, filament_g: {} } }
      : p.endsWith('/jobs') ? { jobs: [] } : p === '/api/printers' ? []
      : p === '/api/defaults' ? { defaults: { options: {} } } : p === '/api/slicer/spools' ? { spools: [] }
      : p === '/api/slicer/presets' ? { machines: [], processes: {}, filaments: {} } : {} });
  });
  setup?.(server);
  await page.goto(`http://spooler.test/slicer.html${search}`);
  return { page, server };
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    // With no project in the address the page lists the projects instead of reopening the last one or opening a dialog
    {
      const { page } = await open(browser);
      const list = page.locator('#projects-dashboard #project-list');
      await list.getByText('Garden chair').waitFor({ timeout: 8000 });
      assert.equal(await page.locator('#dlg-projects').evaluate(d => d.open), false, 'no dialog over the page');
      assert.equal(await page.locator('.slicer-body').isVisible(), false, 'the workspace is not shown');
      assert.equal(await page.locator('.pd-head h2').innerText(), 'Projects');
      assert.equal(await list.locator('.slicer-project-row[data-name]').count(), 4);
      const row = name => list.locator('.slicer-project-row', { hasText: name });
      assert.match(await row('Garden chair').innerText(), /3 plates · 16 objects/);
      assert.match(await row('Garden chair').innerText(), /All plates sliced/);
      assert.match(await row('Lamp shade').innerText(), /1 in the queue/);
      assert.match(await row('Lamp shade').innerText(), /Printing/);
      assert.match(await row('Lamp shade').innerText(), /from a model upload/);
      assert.match(await row('Bracket set').innerText(), /Slicing a plate/);
      assert.match(await row('Fresh start').innerText(), /Empty/);
      assert.match(await list.innerText(), /split models without a project[\s\S]*loose\.stl/i);
      if (process.env.SHOT) await page.screenshot({ path: process.env.SHOT });
      console.log('PASS: The Projects page lists every project with where it has got to');

      // The search box narrows the list
      await page.locator('#pd-search').fill('lamp');
      assert.equal(await list.locator('.slicer-project-row[data-name]:not([hidden])').count(), 1);
      await page.locator('#pd-search').fill('');
      assert.equal(await list.locator('.slicer-project-row[data-name]:not([hidden])').count(), 4);
      console.log('PASS: Search narrows the projects');

      // New project opens the form as a dialog over the page
      await page.locator('#btn-pd-new').click();
      await page.locator('#dlg-projects').waitFor({ state: 'visible' });
      assert.equal(await page.locator('#dlg-projects h3').innerText(), 'New project');
      assert.equal(await page.evaluate(() => document.activeElement?.id), 'new-project-name');
      await page.locator('#dlg-projects').getByRole('button', { name: 'Close', exact: true }).click();
      assert.equal(await page.locator('#projects-dashboard').isVisible(), true, 'closing it leaves the page');
      console.log('PASS: New project opens its form over the page');

      // Open goes into the project and the page gives way to the workspace; the Projects button comes back
      await row('Garden chair').getByRole('button', { name: 'Open' }).click();
      await page.waitForFunction(() => location.search === '?project=7');
      await page.locator('.slicer-body').waitFor({ state: 'visible' });
      assert.equal(await page.locator('#projects-dashboard').isVisible(), false);
      await page.locator('#btn-projects').click();
      await page.locator('#projects-dashboard').waitFor({ state: 'visible' });
      assert.equal(await page.evaluate(() => location.search), '', 'the address no longer names a project');
      assert.equal(await page.locator('.slicer-body').isVisible(), false);
      console.log('PASS: Open enters a project and the Projects button comes back to the page');
      await page.close();
    }

    // Delete asks first and refreshes the list
    {
      const { page, server } = await open(browser);
      const list = page.locator('#project-list');
      await list.getByText('Bracket set').waitFor({ timeout: 8000 });
      page.once('dialog', dialog => dialog.accept());
      await list.locator('.slicer-project-row', { hasText: 'Bracket set' }).getByRole('button', { name: 'Delete' }).click();
      await page.waitForFunction(() => !document.getElementById('project-list').innerText.includes('Bracket set'));
      assert.deepEqual(server.calls.filter(c => c.method === 'DELETE').map(c => c.path), ['/api/projects/9']);
      console.log('PASS: Delete removes the project from the page');
      await page.close();
    }

    // No projects: one sentence and one button
    {
      const { page } = await open(browser, { projects: [] });
      const empty = page.locator('#project-list .pd-empty');
      await empty.waitFor({ timeout: 8000 });
      assert.match(await empty.innerText(), /No projects yet/);
      await empty.getByRole('button', { name: 'New project' }).click();
      await page.locator('#dlg-projects').waitFor({ state: 'visible' });
      console.log('PASS: An empty Projects page says what to do next with one button');
      await page.close();
    }

    // Loading, failing and recovering: a loading block, then an error with an in-place Retry, never a blank or a bare error line
    {
      const { page, server } = await open(browser, { setup: s => { s.listFail = true; } });
      const error = page.locator('#project-list .state-block[data-state="error"]');
      await error.waitFor({ timeout: 8000 });
      assert.match(await error.innerText(), /Couldn't load your projects: Fixture list unavailable/);
      server.listFail = false;
      await error.getByRole('button', { name: 'Retry projects' }).click();
      await page.locator('#project-list').getByText('Garden chair').waitFor({ timeout: 8000 });
      assert.equal(await page.locator('#project-list .state-block').count(), 0, 'the error is gone after Retry');
      console.log('PASS: A failed project list shows an error with Retry, and Retry recovers');
      await page.close();
    }

    // Opening a project shows the steps and sidebar at once; the 3D models load behind a loading block, with Retry when they fail
    {
      let release;
      const { page } = await open(browser, { search: '?project=7', setup: s => {
        s.models = [{ id: 1, state: 'ready', sha256: 'a', name: 'chair.stl' }];
        s.previewGate = new Promise(resolve => { release = resolve; });
      } });
      await page.locator('#step-bar button, #step-bar a').first().waitFor({ timeout: 8000 });
      assert.equal(await page.locator('#project-sidebar').isVisible(), true, 'sidebar shows while the models still load');
      assert.equal(await page.locator('#project-opening').isVisible(), false, 'the opening notice is gone once the project is in');
      const viewport = await page.evaluate(() => !!document.getElementById('viewport')?.querySelector('canvas'));
      if (viewport) {
        const loading = page.locator('#viewport-message .state-block[data-state="loading"]');
        await loading.waitFor({ timeout: 8000 });
        release();
        const error = page.locator('#viewport-message .state-block[data-state="error"]');
        await error.waitFor({ timeout: 8000 });
        assert.match(await error.innerText(), /1 model didn't load/);
        await error.getByRole('button', { name: 'Retry models' }).waitFor();
        console.log('PASS: Models load behind a loading block, a failure offers Retry');
      } else {
        release();
        console.log('SKIP: no WebGL here, so the model loading block is not exercised');
      }
      await page.close();
    }

    // A slow project fetch shows "Opening project…", a failed one an error with Retry in place
    {
      const { page, server } = await open(browser, { search: '?project=7', setup: s => { s.projectDelay = 600; } });
      await page.locator('#project-opening .state-block[data-state="loading"]').waitFor({ timeout: 8000 });
      assert.match(await page.locator('#project-opening').innerText(), /Opening project/);
      await page.locator('#step-bar button, #step-bar a').first().waitFor({ timeout: 8000 });
      assert.equal(await page.locator('#project-opening').isVisible(), false);
      console.log('PASS: Opening a project shows a bounded loading notice');
      await page.close();
    }

    // The header's New project (?new=1) shows the form over the page
    {
      const { page } = await open(browser, { search: '?new=1' });
      await page.locator('#dlg-projects').waitFor({ state: 'visible' });
      assert.equal(await page.locator('#projects-dashboard').isVisible(), true, 'the page is behind the form');
      console.log('PASS: ?new=1 opens the form over the Projects page');
      await page.close();
    }
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
