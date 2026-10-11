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
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, serviceWorkers: 'block' });
    await page.routeWebSocket(/.*/, ws => ws.close());
    page.on('pageerror', e => console.error('Page error:', e.message));
    await page.route('http://spooler.test/**', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/config.js') return route.fulfill({ body: 'window.SPOOLER_WS_PORT = 1;', contentType: 'text/javascript' });
      const file = path.join(root, url.pathname === '/' ? 'index.html' : url.pathname);
      if (!file.startsWith(root) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404 });
      return route.fulfill({ body: fs.readFileSync(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.html') ? 'text/html' : 'application/octet-stream' });
    });
    const uploads = [];
    await page.route('**/api/**', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/projects/upload') { uploads.push(Object.fromEntries(url.searchParams)); return route.fulfill({ status: 500, json: { error: 'test' } }); }
      return route.fulfill({ json: url.pathname === '/api/projects' ? { projects: [] } : {} });
    });
    await page.goto('http://spooler.test/');

    const header = page.locator('.header-actions');
    assert.equal(await header.getByRole('button', { name: 'New project', exact: true }).isVisible(), true, 'header offers New project');
    assert.equal(await header.getByRole('button', { name: 'Quick print', exact: true }).isVisible(), true, 'header keeps Quick print');
    assert.equal(await page.locator('#btn-import, #btn-import-folder').count(), 0, 'header Import and Folder are gone');
    assert.equal(await header.getByRole('button', { name: 'Add Printer', exact: true }).count() > 0, true);
    assert.equal(await header.getByRole('button', { name: 'Discover', exact: true }).count() > 0, true);
    console.log('PASS: Header has New project and Quick print, not Import and Folder');

    await header.getByRole('button', { name: 'New project', exact: true }).click();
    await page.waitForFunction(() => location.hash === '#projects=new');
    await page.waitForFunction(() => document.getElementById('projects-frame').getAttribute('src') === '/slicer.html?new=1');
    console.log('PASS: New project opens the New project page');

    await page.goto('http://spooler.test/');
    await page.evaluate(() => openQueue());
    const queue = page.locator('#panel-queue');
    const text = await queue.innerText();
    assert.match(text, /New project/, 'the Queue panel points to New project');
    assert.doesNotMatch(text, /import/i, 'the Queue panel no longer talks about importing');
    await queue.getByRole('button', { name: 'New project', exact: true }).click();
    await page.waitForFunction(() => location.hash === '#projects=new');
    assert.equal(await queue.getAttribute('aria-hidden'), 'true', 'the Queue panel closes');
    console.log('PASS: Queue panel points to New project');

    await page.goto('http://spooler.test/');
    await page.evaluate(() => {
      const dt = new DataTransfer();
      dt.items.add(new File(['solid x\nendsolid x\n'], 'bracket.stl'));
      document.dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
    });
    await page.waitForFunction(() => document.readyState === 'complete');
    await new Promise(r => setTimeout(r, 500));
    assert.equal(uploads.length, 1, 'one upload for one dropped file');
    assert.equal(uploads[0].quick, '1', 'the drop is a Quick print upload');
    assert.notEqual(await page.evaluate(() => location.hash), '#projects=new');
    console.log('PASS: A file dropped on the dashboard still opens Quick print');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
