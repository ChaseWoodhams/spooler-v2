// In-app confirm dialog (confirm-dialog.js): no native confirm()/alert() in public/, every dialog names its target and the
// consequence, focus starts on Cancel, and destructive buttons sit apart from the primary row. Fixture HTTP/WebSocket only.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const publicDir = process.env.UI_PUBLIC_DIR || path.join(root, 'public');
const shots = process.env.UI_SHOTS || '';

async function walk(dir) {
  const out = [];
  for (const entry of await fs.readdir(dir, { withFileTypes: true })) {
    if (entry.name === 'vendor' || entry.name === 'node_modules') continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...await walk(full));
    else if (/\.(js|html)$/.test(entry.name)) out.push(full);
  }
  return out;
}

// Guard: the browser's own confirm()/alert() boxes are gone from every non-vendor script and inline script
async function noNativeDialogs() {
  const hits = [];
  for (const file of await walk(publicDir)) {
    (await fs.readFile(file, 'utf8')).split(/\r?\n/).forEach((line, i) => {
      if (/\b(confirm|alert)\(/.test(line)) hits.push(`${path.relative(publicDir, file)}:${i + 1}: ${line.trim().slice(0, 80)}`);
    });
  }
  assert.deepEqual(hits, [], 'native confirm()/alert() calls left in public/');
  console.log('PASS: no native confirm() or alert() in public/');
}

const idle = { id: 'p1', name: 'Voxel', ip: '192.0.2.10', printer_type: 'cc1', connected: true, camera_url: null,
  status: { PrintInfo: { Status: 0 }, TempOfNozzle: 22, TempOfHotbed: 21, TempTargetNozzle: 0, TempTargetHotbed: 0 } };
const printingStatus = { ...idle.status,
  PrintInfo: { Status: 13, Filename: '/usb/benchy.gcode', CurrentLayer: 42, TotalLayer: 100, RemainTime: 600 } };

async function open(browser, baseUrl, { width = 1098, height = 700 } = {}) {
  const context = await browser.newContext({ viewport: { width, height }, serviceWorkers: 'block' });
  const page = await context.newPage();
  const fx = { page, context, commands: [], sockets: [], deletes: [], printer: idle,
    inventory: [{ id: 17, location: '', initial_weight: 1000, remaining_weight: 750,
      filament: { id: 18, material: 'PLA', name: 'Blue', color_hex: '0000FF', vendor: { id: 5, name: 'Fixture brand' } } }],
    jobs: [{ id: 3, name: 'Bracket plate', file_name: 'bracket.gcode', source: 'upload', printer_id: null, state: 'queued', print_opts: {}, position: 1 }],
    importReply: { created: 4, total: 4, skipped: 0 } };
  page.on('pageerror', error => console.error('Browser error:', error.message));
  await page.routeWebSocket(/.*/, socket => {
    fx.sockets.push(socket);
    socket.onMessage(message => {
      const request = JSON.parse(message);
      fx.commands.push(request);
      if (request.action === 'list_printers') socket.send(JSON.stringify({ type: 'printer_update', printer: fx.printer }));
    });
  });
  await page.route('**/api/**', route => {
    const request = route.request(), url = new URL(request.url());
    if (request.method() === 'DELETE') { fx.deletes.push(url.pathname); return route.fulfill({ json: {} }); }
    if (url.pathname === '/api/spoolman/api/v1/spool') return route.fulfill({ json: fx.inventory });
    if (url.pathname === '/api/jobs') return route.fulfill({ json: { jobs: fx.jobs } });
    if (url.pathname === '/api/import-filaments') return route.fulfill({ json: fx.importReply });
    if (url.pathname === '/api/outcome-prompts') return route.fulfill({ json: { awaiting: [] } });
    return route.fulfill({ json: {} });
  });
  await page.goto(baseUrl);
  await page.locator('#card-p1').waitFor();
  fx.update = async data => { for (const s of fx.sockets) s.send(JSON.stringify({ type: 'printer_update', printer: { ...fx.printer, ...data } })); };
  return fx;
}
const stray = fx => fx.commands.filter(c => !['list_printers', 'get_printer_files', 'list_files'].includes(c.action));
const dialogOf = (page, name) => page.getByRole('dialog', { name });
const focused = page => page.evaluate(() => document.activeElement && document.activeElement.textContent.trim());
const shot = async (page, name) => { if (shots) await page.screenshot({ path: path.join(shots, name + '.png') }); };

// Asks, names the target and consequence, starts on Cancel; Escape and Cancel change nothing, the verb button does the action
async function askThenDo(fx, trigger, { name, body, verb, command }) {
  const { page } = fx;
  const before = stray(fx).length;
  await trigger();
  const box = dialogOf(page, name);
  await box.waitFor();
  assert.match(await box.innerText(), body, `${verb}: the dialog says what happens`);
  assert.equal(await focused(page), 'Cancel', `${verb}: focus starts on Cancel`);
  assert.equal(await box.getByRole('button', { name: 'OK', exact: true }).count(), 0, 'no bare OK button');
  await shot(page, `${verb.replace(/\W+/g, '-')}-${page.viewportSize().width}`);
  await page.keyboard.press('Escape');
  await box.waitFor({ state: 'detached' });
  assert.equal(stray(fx).length, before, `${verb}: Escape does nothing`);
  await trigger();
  await box.waitFor();
  await box.getByRole('button', { name: 'Cancel', exact: true }).click();
  await box.waitFor({ state: 'detached' });
  assert.equal(stray(fx).length, before, `${verb}: Cancel does nothing`);
  await trigger();
  await box.waitFor();
  await box.getByRole('button', { name: verb, exact: true }).click();
  await box.waitFor({ state: 'detached' });
  if (command) assert.ok(stray(fx).some(c => c.action === command), `${verb}: sends ${command}`);
}

async function hardware(fx) {
  const { page } = fx;
  await page.locator('#card-p1').focus();
  await page.keyboard.press('Enter');
  await page.getByRole('tab', { name: 'Motion', exact: true }).waitFor();
  await askThenDo(fx, () => page.getByRole('button', { name: 'Home XY', exact: true }).click(),
    { name: 'Home the X and Y axes on Voxel?', body: /chamber is clear/, verb: 'Home XY', command: 'home_axes' });
  await page.getByRole('tab', { name: 'Temperatures', exact: true }).click();
  await page.getByRole('spinbutton', { name: 'Nozzle target temperature (°C)' }).fill('210');
  await askThenDo(fx, () => page.getByRole('button', { name: 'Apply nozzle target', exact: true }).click(),
    { name: 'Set the nozzle target to 210 °C on Voxel?', body: /heating straight away/, verb: 'Heat nozzle', command: 'set_temperature' });
  console.log(`PASS: Home and set temperature name the printer and ask in-app ${page.viewportSize().width}`);
}

async function stopPrint(fx) {
  const { page } = fx;
  fx.printer = { ...idle, status: printingStatus };
  await fx.update({});
  await page.locator('#card-p1').focus();
  await page.keyboard.press('Enter');
  const pause = page.locator('.pv-btns').getByRole('button', { name: 'Pause', exact: true });
  const stop = page.locator('.pv-btns').getByRole('button', { name: 'Stop', exact: true });
  await stop.waitFor();
  const [a, b] = await Promise.all([pause.boundingBox(), stop.boundingBox()]);
  const gap = Math.max(b.x - (a.x + a.width), b.y - (a.y + a.height));
  assert.ok(gap >= 16, `Stop is held apart from Pause in the HUD (gap ${Math.round(gap)}px)`);
  await askThenDo(fx, () => stop.click(),
    { name: 'Stop the print on Voxel?', body: /benchy\.gcode \(42% done\).*cannot be resumed/s, verb: 'Stop print', command: 'stop' });
  console.log(`PASS: Stop names the printer and progress, and sits apart from Pause ${page.viewportSize().width}`);
}

async function editPrinter(fx) {
  const { page } = fx;
  if (page.viewportSize().width < 641) {
    await page.locator('#btn-mobile-more').click();
    await page.locator('#btn-mobile-settings').click();
  } else await page.locator('#btn-app-settings').click();
  await page.locator('#btn-settings-goto-printers').click();
  await page.locator('#settings-printer-list').getByRole('button', { name: 'Edit', exact: true }).click();
  const remove = page.locator('#btn-settings-printer-remove');
  await remove.waitFor();
  assert.equal(await remove.evaluate(el => !el.closest('.modal-actions')), true, 'Remove is outside the Save row');
  assert.equal(await page.locator('#btn-settings-printer-save').isVisible(), true);
  await askThenDo(fx, () => remove.click(),
    { name: 'Remove Voxel?', body: /saved printer settings.*Jobs and Prints are kept/s, verb: 'Remove printer', command: 'remove_printer' });
  console.log(`PASS: Remove printer is outside the Save row and names the printer ${page.viewportSize().width}`);
}

async function filesAndSpools(fx) {
  const { page } = fx;
  await page.evaluate(() => openFileBrowser('p1'));
  await page.waitForTimeout(200);
  await page.evaluate(() => renderFileList([{ name: 'cube.gcode', path: '/usb/cube.gcode', size: 2048, is_dir: false }]));
  const del = page.locator('#files-list').getByRole('button', { name: 'Delete cube.gcode', exact: true });
  await del.waitFor();
  const [print, d] = await Promise.all([page.locator('#files-list').getByRole('button', { name: 'Print', exact: true }).boundingBox(), del.boundingBox()]);
  assert.ok(d.x - (print.x + print.width) >= 16, 'Delete is held apart from Print/Queue in the Files modal');
  await askThenDo(fx, () => del.click(),
    { name: 'Delete cube.gcode?', body: /Voxel's storage.*cannot be undone/s, verb: 'Delete file', command: 'delete_file' });
  await page.keyboard.press('Escape');
  console.log(`PASS: Delete file names the file and printer ${page.viewportSize().width}`);

  await page.locator('#btn-spools').click();
  const removeSpool = page.locator('#spools-list').getByRole('button', { name: 'Remove', exact: true });
  await removeSpool.waitFor();
  await removeSpool.click();
  const box = dialogOf(page, 'Remove Fixture brand Blue PLA?');
  await box.waitFor();
  assert.match(await box.innerText(), /filament entry in Spoolman.*brand/s, 'the spool dialog says it also removes the filament and brand');
  assert.equal(await focused(page), 'Cancel');
  await box.getByRole('button', { name: 'Cancel', exact: true }).click();
  assert.deepEqual(fx.deletes, [], 'Cancel removes nothing');
  await removeSpool.click();
  await box.getByRole('button', { name: 'Remove spool', exact: true }).click();
  await page.waitForTimeout(300);
  assert.ok(fx.deletes.includes('/api/spoolman/api/v1/spool/17'), 'the spool is deleted after confirming');
  console.log(`PASS: Remove spool names the spool and the cascade ${page.viewportSize().width}`);

  await page.locator('#btn-import-filaments').click();
  await page.locator('#toast-area .toast', { hasText: 'Imported 4 ELEGOO filaments' }).waitFor();
  fx.importReply = { created: 0, total: 4, skipped: 4 };
  await page.locator('#btn-import-filaments').click();
  await page.locator('#toast-area .toast', { hasText: 'already in Spoolman' }).waitFor();
  console.log(`PASS: Import Elegoo reports through toasts ${page.viewportSize().width}`);
}

async function jobs(fx) {
  const { page } = fx;
  await page.evaluate(() => { openQueue(); });
  const del = page.locator('[data-job-action="delete"]');
  await del.waitFor();
  await del.click();
  const box = dialogOf(page, 'Delete Bracket plate?');
  await box.waitFor();
  assert.match(await box.innerText(), /leaves the queue/);
  assert.equal(await focused(page), 'Cancel');
  await box.getByRole('button', { name: 'Cancel', exact: true }).click();
  assert.deepEqual(fx.deletes, []);
  await del.click();
  await box.getByRole('button', { name: 'Delete Job', exact: true }).click();
  await page.waitForTimeout(300);
  assert.ok(fx.deletes.includes('/api/jobs/3'));
  console.log(`PASS: Delete Job names the Job ${page.viewportSize().width}`);
}

(async () => {
  await noNativeDialogs();
  const server = http.createServer(async (request, response) => {
    try {
      const pathname = new URL(request.url, 'http://localhost').pathname;
      const filename = path.join(publicDir, pathname === '/' ? 'index.html' : pathname);
      const content = await fs.readFile(filename);
      response.setHeader('Content-Type', filename.endsWith('.js') ? 'text/javascript' : filename.endsWith('.css') ? 'text/css' : 'text/html');
      response.end(content);
    } catch { response.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch();
  try {
    for (const [width, height] of [[1098, 507], [390, 844]]) {
      for (const scenario of [hardware, stopPrint, editPrinter, filesAndSpools, jobs]) {
        const fx = await open(browser, base, { width, height });
        try { await scenario(fx); } finally { await fx.context.close(); }
      }
    }
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; });
