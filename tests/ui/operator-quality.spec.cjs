// Safe operator UI coverage: static assets plus deterministic HTTP/WebSocket fixtures only.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const printer = { id: 'operator-fixture', name: 'Operator fixture printer', ip: '192.0.2.10',
  printer_type: 'cc1', connected: true, camera_url: null,
  status: { PrintInfo: { Status: 0 }, TempOfNozzle: 22, TempOfHotbed: 21, TempTargetNozzle: 0, TempTargetHotbed: 0 } };
const waitState = (page, id, state) => page.waitForFunction(({ id, state }) => document.getElementById(id).dataset.state === state, { id, state }, { timeout: 5000 });
const expectText = async (locator, pattern) => assert.match(await locator.innerText(), pattern);
async function waitForFixture(predicate, name) {
  const deadline = Date.now() + 5000;
  while (!predicate() && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(predicate(), `${name} was requested within 5 seconds`);
}
function assertReadOnlyCommands(fixture) {
  const readOnly = new Set(['list_printers', 'get_printer_files']);
  assert.deepEqual(fixture.commands.filter(command => !readOnly.has(command.action)), [], 'All printer messages must be read-only');
}
async function fixturePage(browser, baseUrl, width) {
  const context = await browser.newContext({ viewport: { width, height: 900 }, serviceWorkers: 'block' });
  const page = await context.newPage();
  page.on("pageerror", error => console.error("Operator browser error:", error.message));
  const fixture = { page, context, commands: [], sockets: [], inventory: [], inventoryMode: 'ready', assignmentFail: true,
    catalogueMode: 'ready', notificationLoadFail: false, notificationSaved: {}, inventoryRelease: null, catalogueRelease: null, patches: [], notificationPosts: 0 };
  await page.routeWebSocket(/.*/, socket => {
    fixture.sockets.push(socket);
    socket.onMessage(message => {
      const request = JSON.parse(message);
      fixture.commands.push(request);
      if (request.action === 'list_printers') socket.send(JSON.stringify({ type: 'printer_update', printer }));
    });
  });
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === '/api/spoolman/api/v1/spool' && request.method() === 'GET') {
      if (fixture.inventoryMode === 'loading') await new Promise(resolve => { fixture.inventoryRelease = resolve; });
      if (fixture.inventoryMode === 'error') return route.fulfill({ status: 503, json: { error: 'Fixture inventory unavailable' } });
      return route.fulfill({ json: fixture.inventory });
    }
    if (/\/api\/spoolman\/api\/v1\/spool\/\d+$/.test(url.pathname) && request.method() === 'PATCH') {
      fixture.patches.push({ path: url.pathname, body: request.postDataJSON() });
      if (fixture.assignmentFail) return route.fulfill({ status: 503, json: { error: 'Fixture assignment unavailable' } });
      fixture.inventory[0].location = request.postDataJSON().location;
      return route.fulfill({ json: fixture.inventory[0] });
    }
    if (url.pathname === '/api/filament-meta') {
      if (fixture.catalogueMode === 'loading') await new Promise(resolve => { fixture.catalogueRelease = resolve; });
      if (fixture.catalogueMode === 'error') return route.fulfill({ status: 503, json: { error: 'Fixture catalogue unavailable' } });
      return route.fulfill({ json: fixture.catalogueMode === 'empty' ? { brands: [], materials: [] }
        : { brands: ['Fixture brand'], materials: [{ name: 'PLA', density: 1.24 }] } });
    }
    if (url.pathname === '/api/spoolman/api/v1/filament' && request.method() === 'POST') return route.fulfill({ status: 503, body: 'Fixture filament save unavailable' });
    if (url.pathname === '/api/notification-settings' && request.method() === 'GET') return fixture.notificationLoadFail
      ? route.fulfill({ status: 503, json: { error: 'Fixture notification settings unavailable' } })
      : route.fulfill({ json: fixture.notificationSaved });
    if (url.pathname === '/api/notification-settings' && request.method() === 'POST') {
      fixture.notificationPosts++;
      return route.fulfill({ status: 503, json: { error: 'Fixture notification save unavailable' } });
    }
    return route.fulfill({ json: {} });
  });
  await page.goto(baseUrl);
  await page.locator('#card-operator-fixture').waitFor();
  fixture.updatePrinter = async data => {
    for (const socket of fixture.sockets) socket.send(JSON.stringify({ type: 'printer_update', printer: { ...printer, ...data } }));
  };
  return fixture;
}
async function cockpit(fixture) {
  const { page } = fixture;
  await page.locator('#card-operator-fixture').focus();
  await page.keyboard.press('Enter');
  await page.getByRole('tab', { name: 'Motion', exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: 'Home XY', exact: true }).isEnabled(), true);
  await page.getByRole('tab', { name: 'Motion', exact: true }).focus();
  await page.keyboard.press('ArrowRight');
  const temperatures = page.getByRole('tab', { name: 'Temperatures', exact: true });
  assert.equal(await temperatures.getAttribute('aria-selected'), 'true');
  assert.equal(await temperatures.evaluate(el => document.activeElement === el), true);
  const nozzle = page.getByRole('spinbutton', { name: 'Nozzle target temperature (°C)' });
  assert.equal(await nozzle.getAttribute('min'), '0');
  assert.equal(await nozzle.getAttribute('max'), '300');
  assert.equal(await page.getByRole('spinbutton', { name: 'Bed target temperature (°C)' }).getAttribute('max'), '110');
  await expectText(page.getByRole('tabpanel'), /Zero turns heating off/);
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.getByRole('slider', { name: 'Print speed', exact: true }).isDisabled(), true);
  await page.keyboard.press('Home');
  await fixture.updatePrinter({ status: { ...printer.status, PrintInfo: { Status: 3, Filename: 'fixture.gcode', CurrentLayer: 25, TotalLayer: 100, RemainTime: 60 } } });
  await page.getByRole('button', { name: 'Pause', exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: 'Home XY', exact: true }).isDisabled(), true);
  await expectText(page.locator('.pv-hudjob'), /fixture.gcode/);
  await page.getByRole('tab', { name: 'Motion', exact: true }).focus();
  await page.keyboard.press('End');
  assert.equal(await page.getByRole('tab', { name: 'Files', exact: true }).getAttribute('aria-selected'), 'true');
  await page.keyboard.press('ArrowLeft');
  assert.equal(await page.getByRole('slider', { name: 'Print speed', exact: true }).isEnabled(), true);
  await fixture.updatePrinter({ connected: false });
  await expectText(page.getByRole('tabpanel'), /Printer offline/);
  assert.equal(await page.getByRole('slider', { name: 'Model fan', exact: true }).isDisabled(), true);
  await page.getByRole('tab', { name: 'Air & light', exact: true }).focus();
  await page.keyboard.press('ArrowLeft');
  assert.equal(await nozzle.isDisabled(), true);
  await page.getByRole('button', { name: /All printers/ }).click();
  assertReadOnlyCommands(fixture);
  console.log(`Cockpit idle/printing/offline PASS ${page.viewportSize().width}`);
}
async function inventory(fixture) {
  const { page } = fixture;
  fixture.inventoryMode = 'loading';
  await page.locator('#btn-spools').click();
  await waitState(page, 'spools-status', 'loading');
  assert.equal(await page.locator('#spools-empty').isVisible(), false);
  await page.waitForFunction(() => document.getElementById('spools-status').textContent.includes('Loading'));
  await waitForFixture(() => fixture.inventoryRelease, 'Inventory loading fixture');
  fixture.inventoryMode = 'ready'; fixture.inventoryRelease();
  await waitState(page, 'spools-status', 'ready');
  assert.equal(await page.locator('#spools-empty').isVisible(), true);
  await page.locator('#btn-spools-close').click();
  fixture.inventoryMode = 'error';
  await page.locator('#btn-spools').click();
  await waitState(page, 'spools-status', 'error');
  assert.equal(await page.locator('#spools-empty').isVisible(), false);
  fixture.inventoryMode = 'ready';
  fixture.inventory = [{ id: 17, location: '', initial_weight: 1000, remaining_weight: 750,
    filament: { id: 18, material: 'PLA', name: 'Blue', color_hex: '0000FF', vendor: { name: 'Fixture brand' } } }];
  await page.getByRole('button', { name: 'Retry inventory', exact: true }).click();
  await waitState(page, 'spools-status', 'ready');
  await expectText(page.locator('#spools-list'), /750 g remaining/);
  fixture.catalogueMode = 'loading';
  await page.locator('#btn-add-spool').click();
  await waitState(page, 'spool-catalogue-status', 'loading');
  await page.locator('#spool-input-weight').fill('750');
  await page.locator('#spool-input-color').fill('Entered blue');
  await page.locator('#spool-input-material').selectOption('__other__');
  await page.getByRole('textbox', { name: 'Material name', exact: true }).fill('Manual PLA');
  await waitForFixture(() => fixture.catalogueRelease, 'Catalogue loading fixture');
  fixture.catalogueMode = 'empty'; fixture.catalogueRelease();
  await waitState(page, 'spool-catalogue-status', 'empty');
  await expectText(page.locator('#spool-catalogue-status'), /Other/);
  assert.equal(await page.locator('#spool-input-weight').inputValue(), '750');
  assert.equal(await page.locator('#spool-input-color').inputValue(), 'Entered blue');
  assert.equal(await page.getByRole('textbox', { name: 'Material name', exact: true }).inputValue(), 'Manual PLA', 'Manual selection made during loading is preserved');
  assert.equal(await page.locator('#spool-input-material').inputValue(), '__other__', 'Manual material remains selected when catalogue finishes');
  await page.locator('#spool-input-material').selectOption('__other__');
  await page.getByRole('textbox', { name: 'Material name', exact: true }).fill('Manual PLA');
  await page.locator('#btn-add-spool-confirm').click();
  await waitState(page, 'spool-form-feedback', 'error');
  await expectText(page.locator('#spool-form-feedback'), /Fixture filament save unavailable/);
  assert.equal(await page.locator('#modal-add-spool').isVisible(), true);
  assert.equal(await page.getByRole('textbox', { name: 'Material name', exact: true }).inputValue(), 'Manual PLA');
  assert.equal(await page.locator('#spool-input-weight').inputValue(), '750');
  await page.locator('#btn-add-spool-cancel').click();
  assert.equal(await page.locator('#btn-add-spool').evaluate(el => document.activeElement === el), true);
  fixture.catalogueMode = 'error';
  await page.locator('#btn-add-spool').click();
  await waitState(page, 'spool-catalogue-status', 'error');
  fixture.catalogueMode = 'ready';
  await page.locator('#btn-spool-catalogue-retry').click();
  await waitState(page, 'spool-catalogue-status', 'ok');
  assert.equal(await page.getByRole('textbox', { name: 'Material name', exact: true }).inputValue(), 'Manual PLA');
  assert.equal(await page.locator('#spool-input-weight').inputValue(), '750');
  await page.locator('#btn-add-spool-cancel').click();
  await page.locator('#btn-spools-close').click();
  const pickerOpener = page.locator('#card-operator-fixture .card-spool button');
  await pickerOpener.click();
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#modal-spool-picker').isVisible(), false);
  assert.equal(await pickerOpener.evaluate(el => document.activeElement === el), true);
  await pickerOpener.click();
  const spoolButton = page.getByRole('button', { name: /Fixture brand PLA Blue/ });
  await spoolButton.focus();
  await page.keyboard.press('Enter');
  await waitState(page, 'spool-picker-feedback', 'error');
  await expectText(page.locator('#spool-picker-feedback'), /Could not assign.*retrying/s);
  assert.equal(await page.locator('#modal-spool-picker').isVisible(), true);
  assert.equal(await spoolButton.isEnabled(), true);
  fixture.assignmentFail = false;
  await spoolButton.focus(); await page.keyboard.press('Enter');
  await page.locator('#modal-spool-picker').waitFor({ state: 'hidden' });
  await page.waitForFunction(() => document.querySelector('#card-operator-fixture .spool-card-name')?.textContent.includes('Blue'));
  assert.equal(fixture.patches.length, 2);
  assert.equal(fixture.patches[1].body.location, 'Operator fixture printer');
  console.log(`Spool dependency/catalogue/save/assignment PASS ${page.viewportSize().width}`);
}
async function settings(fixture) {
  const { page } = fixture;
  if (page.viewportSize().width < 641) {
    await page.locator('#btn-mobile-more').click();
    await page.locator('#btn-mobile-settings').click();
  } else await page.locator('#btn-app-settings').click();
  await page.locator('#btn-settings-goto-printers').click();
  await page.locator('#btn-settings-add-printer').click();
  await page.locator('#btn-settings-printer-save').click();
  assert.equal(await page.locator('#settings-edit-ip').getAttribute('aria-invalid'), 'true');
  assert.equal(await page.locator('#settings-edit-ip').evaluate(el => document.activeElement === el), true);
  await expectText(page.locator('#settings-edit-ip-error'), /IP address/);
  await page.locator('#settings-edit-ip').fill('192.0.2.20');
  await page.locator('#settings-edit-type').selectOption('cc2');
  await page.locator('#btn-settings-printer-save').click();
  assert.equal(await page.locator('#settings-edit-access-code').getAttribute('aria-invalid'), 'true');
  assert.equal(await page.locator('#settings-edit-access-code').evaluate(el => document.activeElement === el), true);
  await page.locator('#settings-edit-type').selectOption('prusa');
  await expectText(page.locator('#settings-printer-edit'), /experimental.*API address.*credentials/s);
  assert.equal(fixture.commands.some(command => ['add_printer', 'update_printer'].includes(command.action)), false);
  await page.locator('#btn-settings-back-printer-edit').click();
  await page.locator('#settings-printer-list').getByRole('button', { name: 'Edit', exact: true }).click();
  let removalText = '';
  page.once('dialog', async dialog => { removalText = dialog.message(); await dialog.dismiss(); });
  await page.locator('#btn-settings-printer-remove').click();
  assert.match(removalText, /Remove Operator fixture printer.*saved printer settings.*Jobs and Prints are kept/s);
  assert.equal(fixture.commands.some(command => command.action === 'remove_printer'), false);
  assert.equal(await page.locator('#settings-printer-edit').isVisible(), true);
  await page.locator('#btn-settings-back-printer-edit').click();
  await page.locator('#btn-settings-back-printers').click();
  const notificationRead = page.waitForResponse(response => new URL(response.url()).pathname === '/api/notification-settings');
  await page.locator('#btn-settings-goto-notifications').click();
  await (await notificationRead).finished();
  await expectText(page.locator('#settings-notifications'), /browser permission.*push subscription.*reachable Spooler server/s);
  await page.locator('#notif-layer-on').focus();
  await page.keyboard.press('Space');
  assert.equal(await page.locator('#notif-layer-on').isChecked(), true);
  await page.locator('#notif-layer-number').fill('0');
  await page.locator('#btn-notif-save').click();
  await expectText(page.locator('#settings-feedback'), /outside its allowed range/);
  assert.equal(await page.locator('#notif-layer-number').evaluate(el => document.activeElement === el), true);
  assert.equal(fixture.notificationPosts, 0);
  await page.locator('#notif-layer-on').focus();
  await page.keyboard.press('Space');
  assert.equal(await page.locator('#notif-layer-on').isChecked(), false);
  await page.locator('#btn-notif-save').click();
  await page.waitForFunction(() => document.getElementById('settings-feedback').textContent.includes('Fixture notification save unavailable'), null, { timeout: 5000 });
  assert.equal(fixture.notificationPosts, 1);
  assert.equal(await page.locator('#settings-notifications').isVisible(), true);
  assert.equal(await page.locator('#btn-notif-save').isEnabled(), true);
  assert.equal(await page.locator('#settings-feedback').getAttribute('aria-live'), 'polite');
  await page.locator('#btn-notif-test').click();
  await page.waitForFunction(() => /Allow notifications|does not support notifications/.test(document.getElementById('settings-feedback').textContent), null, { timeout: 5000 });
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#modal-app-settings').isVisible(), false);
  assert.equal(await page.evaluate(() => document.activeElement.id), page.viewportSize().width < 641 ? 'btn-mobile-more' : 'btn-app-settings');
  console.log(`Settings printer/notification validation PASS ${page.viewportSize().width}`);
}
async function notificationRecovery(fixture) {
  const { page } = fixture;
  fixture.notificationLoadFail = true;
  if (page.viewportSize().width < 641) {
    await page.locator('#btn-mobile-more').click(); await page.locator('#btn-mobile-settings').click();
  } else await page.locator('#btn-app-settings').click();
  const notificationRead = page.waitForResponse(response => new URL(response.url()).pathname === '/api/notification-settings');
  await page.locator('#btn-settings-goto-notifications').click();
  await (await notificationRead).finished();
  await page.waitForFunction(() => /Could not load notification settings/.test(document.getElementById('settings-feedback').textContent), null, { timeout: 5000 });
  assert.equal(await page.locator('#settings-feedback').isVisible(), true);
  assert.equal(await page.locator('#btn-notif-save').isDisabled(), true);
  const retry = page.getByRole('button', { name: 'Retry notification settings', exact: true });
  assert.equal(await retry.isVisible(), true);
  fixture.notificationLoadFail = false;
  fixture.notificationSaved = { layer: { enabled: true, layer: 7 } };
  await retry.click();
  await page.waitForFunction(() => document.getElementById('notif-layer-number').value === '7');
  assert.equal(await page.locator('#notif-layer-on').isChecked(), true);
  assert.equal(await page.locator('#btn-notif-save').isEnabled(), true);
  assert.equal(await retry.isVisible(), false);
  assert.equal(fixture.notificationPosts, 0);
  console.log(`Notification read failure/retry preserves saved rules PASS ${page.viewportSize().width}`);
}
(async () => {
  const server = http.createServer(async (request, response) => {
    try {
      const pathname = new URL(request.url, 'http://localhost').pathname;
      const filename = path.join(root, 'public', pathname === '/' ? 'index.html' : pathname);
      const content = await fs.readFile(filename);
      response.setHeader('Content-Type', filename.endsWith('.js') ? 'text/javascript' : filename.endsWith('.css') ? 'text/css' : 'text/html');
      response.end(content);
    } catch { response.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch();
  try {
    for (const width of [390, 1440]) {
      for (const scenario of [cockpit, inventory, settings, notificationRecovery]) {
        const fixture = await fixturePage(browser, `http://127.0.0.1:${server.address().port}`, width);
        try { await scenario(fixture); assertReadOnlyCommands(fixture); } finally { await fixture.context.close(); }
      }
    }
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; });


