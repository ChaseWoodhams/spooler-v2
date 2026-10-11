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
const project = id => ({ id, name: PROJECTS.find(p => p.id === id).name, revision: 1, split_id: null, split_ids: [], models: [], slices: {}, server_time: Date.now() / 1000,
  data: { plates: [{ id: 1, name: 'Plate 1' }], objects: [], bed: 'textured', profile: { bundle_id: null, process: {}, filament: {} } } });

async function open(browser, { projects = PROJECTS, search = '' } = {}) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const server = { calls: [], projects: [...projects] };
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
    if (p === '/api/projects' && method === 'GET') return route.fulfill({ json: { projects: server.projects } });
    if ((m = p.match(/^\/api\/projects\/(\d+)$/)) && method === 'DELETE') {
      server.projects = server.projects.filter(x => x.id !== Number(m[1]));
      return route.fulfill({ json: { ok: true } });
    }
    if ((m = p.match(/^\/api\/projects\/(\d+)$/))) return route.fulfill({ json: project(Number(m[1])) });
    return route.fulfill({ json: p === '/api/splits' ? { splits: [{ id: 3, project_id: 8 }, { id: 4, project_id: null, source_name: 'loose.stl', parts: 6, state: 'split' }] }
      : p.endsWith('/status') ? { plates: [], progress: { counts: {}, filament_g: {} } }
      : p.endsWith('/jobs') ? { jobs: [] } : p === '/api/printers' ? []
      : p === '/api/defaults' ? { defaults: { options: {} } } : p === '/api/slicer/spools' ? { spools: [] }
      : p === '/api/slicer/presets' ? { machines: [], processes: {}, filaments: {} } : {} });
  });
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
