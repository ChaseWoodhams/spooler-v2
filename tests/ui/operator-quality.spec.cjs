// Safe operator UI coverage: static assets plus deterministic HTTP/WebSocket fixtures only.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const publicDir = process.env.UI_PUBLIC_DIR || path.join(root, 'public'); // override to run the guard against another checkout
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
  const readOnly = new Set(['list_printers', 'get_printer_files', 'list_files']);
  assert.deepEqual(fixture.commands.filter(command => !readOnly.has(command.action)), [], 'All printer messages must be read-only');
}
async function fixturePage(browser, baseUrl, width, height = 900) {
  const context = await browser.newContext({ viewport: { width, height }, serviceWorkers: 'block' });
  const page = await context.newPage();
  page.on("pageerror", error => console.error("Operator browser error:", error.message));
  const fixture = { page, context, commands: [], sockets: [], inventory: [], inventoryMode: 'ready', assignmentFail: true,
    catalogueMode: 'ready', notificationLoadFail: false, notificationSaved: {}, notificationSaveOk: false,
    notificationPosts: 0, notificationSavedBody: null, inventoryRelease: null, catalogueRelease: null, patches: [] };
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
    if (url.pathname === '/api/outcome-prompts') return route.fulfill({ json: { awaiting: [{ printer_id: 'operator-fixture',
      printer_name: 'Operator fixture printer', print_id: 7, file_name: 'fixture.gcode', reported_outcome: 'completed', duration_s: 3600, quick: true }] } });
    if (url.pathname === '/api/failure-reasons') return route.fulfill({ json: { failure_reasons: ['spaghetti', 'adhesion'], labels: { spaghetti: 'Spaghetti', adhesion: 'Lifted off the bed' } } });
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
      fixture.notificationSavedBody = request.postDataJSON();
      if (fixture.notificationSaveOk) return route.fulfill({ json: { ok: true } });
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
  await nozzle.fill('301');
  await page.getByRole('button', { name: 'Apply nozzle target', exact: true }).click();
  assert.equal(await nozzle.evaluate(el => el.validity.rangeOverflow), true);
  await nozzle.fill('0');
  await temperatures.focus();
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
  const fanSlider = page.getByRole('slider', { name: 'Model fan', exact: true });
  await fanSlider.evaluate(el => { el.value = '40'; el.dispatchEvent(new Event('input', { bubbles: true })); el.focus(); });
  await fixture.updatePrinter({ status: { ...printer.status, CurrentFanSpeed: { ModelFan: 85 }, PrintInfo: { Status: 3, PrintSpeedPct: 130 } } });
  await page.waitForFunction(() => document.querySelector('#detail-speed-slider')?.value === '130');
  assert.equal(await fanSlider.evaluate(el => document.activeElement === el), true);
  assert.equal(await fanSlider.inputValue(), '40');
  assert.equal(await page.locator('#detail-model-value').innerText(), '40%');
  assert.equal(await page.evaluate(() => typeof window.protoTab), 'undefined');
  assert.equal(await page.evaluate(() => typeof window.selectPrinterTab), 'function');
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
// Fan / jog / light feedback. Sends go only to the fixture socket (never a real printer); counts commands per interaction.
async function hardwareFeedback(fixture) {
  const { page } = fixture;
  const count = action => fixture.commands.filter(command => command.action === action).length;
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const fb = key => page.locator(`[data-fb="${key}"]`);
  const expectFb = async (locator, pattern) => { const deadline = Date.now() + 5000; let text = ""; while (Date.now() < deadline) { text = await locator.innerText(); if (pattern.test(text)) return; await sleep(30); } assert.match(text, pattern); };
  await page.evaluate(() => { _HW.debounce = 250; _HW.confirm = 700; _HW.jog = 600; _HW.flash = 400; });
  await page.locator('#card-operator-fixture').focus();
  await page.keyboard.press('Enter');
  await page.getByRole('tab', { name: 'Air & light', exact: true }).click();

  // Fan: three arrow presses settle into ONE command; pending, then confirmed from the printer report
  const fan = page.getByRole('slider', { name: 'Model fan', exact: true });
  await fan.focus();
  for (let i = 0; i < 3; i++) await page.keyboard.press('ArrowRight');
  await expectFb(fb('fan_model'), /Setting 15%/);
  assert.equal(await page.locator('#detail-model-value').innerText(), '15%', 'requested value is kept while pending');
  assert.equal(count('set_fan'), 0, 'nothing is sent while the slider is still being adjusted');
  await waitForFixture(() => count('set_fan') > 0, 'set_fan');
  await sleep(400);
  assert.equal(count('set_fan'), 1, 'one command per settle');
  assert.equal(fixture.commands.find(command => command.action === 'set_fan').speed, 15);
  assert.equal(await fan.evaluate(el => document.activeElement === el), true, 'focus stays on the slider');
  await fixture.updatePrinter({ status: { ...printer.status, CurrentFanSpeed: { ModelFan: 15 } } });
  await expectFb(fb('fan_model'), /Set to 15%/);
  await page.waitForFunction(() => !document.querySelector('[data-fb="fan_model"]').textContent);

  // Fan: a refused command shows an error beside the slider and in the error toast
  await page.keyboard.press('ArrowRight');
  await waitForFixture(() => count('set_fan') === 2, 'second set_fan');
  for (const socket of fixture.sockets) socket.send(JSON.stringify({ type: 'error', message: 'Printer did not accept the fan command' }));
  await expectFb(fb('fan_model'), /Fan did not change/);
  assert.equal(await page.locator('.toast.error').count(), 1);

  // Fan: never confirmed -> error after the timeout
  await page.keyboard.press('ArrowRight');
  await expectFb(fb('fan_model'), /did not report the change/);

  // Light: one command per tap, pending until the printer reports it
  const light = page.getByRole('button', { name: 'Chamber lighting', exact: true });
  await light.click();
  await light.click();
  assert.equal(count('light_on'), 1, 'a second tap while pending sends nothing');
  assert.equal(await light.getAttribute('aria-busy'), 'true');
  assert.equal(await light.getAttribute('aria-pressed'), 'false', 'state follows the printer report, not the tap');
  await expectFb(fb('light'), /Turning on/);
  await fixture.updatePrinter({ status: { ...printer.status, LightStatus: { SecondLight: true } } });
  await expectFb(fb('light'), /Light on/);
  assert.equal(await light.getAttribute('aria-pressed'), 'true');
  await sleep(3200);
  assert.equal(await light.getAttribute('aria-pressed'), 'true', 'no 3 s revert while the printer still reports on');
  await page.getByRole('button', { name: 'Chamber lighting', exact: true }).click();
  await expectFb(fb('light'), /did not report the change/);

  // Jog: a second tap while the move is in flight does not move again; focus stays; confirmed from coordinates
  await page.getByRole('tab', { name: 'Motion', exact: true }).click();
  const xPlus = page.getByRole('button', { name: 'X+', exact: true });
  await xPlus.focus();
  await xPlus.click();
  await xPlus.click({ force: true });
  await page.keyboard.press('Enter');
  assert.equal(count('move_axis'), 1, 'taps during a move are ignored');
  assert.equal(await xPlus.getAttribute('aria-disabled'), 'true');
  await expectFb(fb('jog'), /Moving/);
  assert.equal(await xPlus.evaluate(el => document.activeElement === el), true);
  await fixture.updatePrinter({ status: { ...printer.status, CurrentCoord: '1.00,0.00,0.00' } });
  await expectFb(fb('jog'), /Moved/);
  assert.equal(await xPlus.getAttribute('aria-disabled'), null);
  await xPlus.click();
  assert.equal(count('move_axis'), 2, 'next move works once the first settled');
  // A refused jog reports the error and frees the buttons
  for (const socket of fixture.sockets) socket.send(JSON.stringify({ type: 'error', message: 'Printer did not accept the motion command' }));
  await expectFb(fb('jog'), /Move not confirmed/);
  assert.equal(await xPlus.getAttribute('aria-disabled'), null);
  console.log(`Hardware feedback PASS ${page.viewportSize().width}x${page.viewportSize().height}`);
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
  fixture.inventoryMode = 'loading';fixture.inventoryRelease = null;
  for (const socket of fixture.sockets) socket.close();
  await waitForFixture(() => typeof fixture.inventoryRelease === 'function', 'reconnected inventory refresh');
  assert.equal(await spoolButton.isEnabled(), false, 'Open picker disables stale assignment rows as soon as inventory refresh starts');
  await spoolButton.evaluate(button => button.dispatchEvent(new MouseEvent('click', { bubbles: true })));
  assert.equal(fixture.patches.length, 0, 'Loading inventory cannot be assigned even by an already-dispatched click');
  fixture.inventoryMode = 'ready';fixture.inventoryRelease();
  await waitState(page, 'spools-status', 'ready');
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
  await page.locator('#settings-printer-list').getByRole('button', { name: /^Edit / }).click();
  await page.locator('#settings-edit-ip').fill('');
  await page.locator('#btn-settings-printer-save').click();
  assert.equal(await page.locator('#settings-edit-ip').evaluate(el => document.activeElement === el), true);
  assert.equal(fixture.commands.some(command => ['add_printer', 'update_printer'].includes(command.action)), false);
  await page.locator('#btn-settings-back-printer-edit').click();
  await page.locator('#settings-printer-list').getByRole('button', { name: /^Edit / }).click();
  await page.locator('#btn-settings-printer-remove').click();
  const removal = page.getByRole('dialog', { name: 'Remove Operator fixture printer?' });
  await removal.waitFor();
  await expectText(removal, /saved printer settings.*Jobs and Prints are kept/s);
  assert.equal(await page.evaluate(() => document.activeElement.textContent), 'Cancel', 'focus starts on Cancel');
  await removal.getByRole('button', { name: 'Cancel' }).click();
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

const healthStatus = (over = {}) => {
  const now = Math.floor(Date.now() / 1000);
  return { generated_at: now,
    spooler: { started_at: now - 3600, loop_lag_ms: 5, browser_clients: 1 },
    spoolman: { ok: true, version: '0.22', latency_ms: 12, pending_deductions: 0 },
    supervisor: { updated_at: now, started_at: now - 3600, stale: false, services: [{ name: 'spooler', restarts: 0 }] },
    printers: [{ id: printer.id, name: printer.name, connected: true, connected_since: now - 600 }],
    backups: { running: false, overdue: false, last_error: null, spoolman_db: true, interval_hours: 24, keep: 14, count: 3, dir: 'C:\\backups',
      latest: { name: 'spooler-backup-1.zip', size: 2048, created_at: now - 60 } },
    ...over };
};
async function healthNextLines(fixture) {
  const { page } = fixture;
  const open = page.viewportSize().width < 641 ? null : page.getByRole('button', { name: /System health/ });
  const send = status => fixture.sockets[0].send(JSON.stringify({ type: 'system_status', status }));
  const rows = () => page.locator('.health-row');
  const rowNamed = title => rows().filter({ has: page.locator('.health-row-title', { hasText: new RegExp('^' + title + '$') }) });
  await page.waitForFunction(() => typeof applySystemStatus === 'function');
  await page.evaluate(() => document.getElementById('modal-system-health').classList.add('open'));
  void open;
  // ok + informational: no Next line anywhere
  send(healthStatus());
  await page.waitForFunction(() => document.querySelectorAll('.health-row').length > 5);
  assert.equal(await page.locator('.health-row-next').count(), 0, 'healthy and informational rows have no Next line');
  assert.ok(await rowNamed('Location').count() === 1 && await rowNamed('Recovery').count() === 1);
  // warn: backup stale points at Back up now
  const now = Math.floor(Date.now() / 1000);
  send(healthStatus({ backups: { ...healthStatus().backups, overdue: true } }));
  await page.waitForFunction(() => document.querySelectorAll('.health-row-next').length > 0);
  await expectText(rowNamed('Latest backup').locator('.health-row-next'), /^Next: Press Back up now./);
  assert.equal(await page.locator('.health-row-next').count(), 1, 'only the stale backup row has a Next line');
  // down: Spoolman unreachable names how to start it
  send(healthStatus({ spoolman: { ok: false, down_since: now - 30, error: 'refused', latency_ms: 0, pending_deductions: 0 } }));
  await page.waitForFunction(() => /Start Spoolman/.test(document.getElementById('system-health-body').innerText));
  await expectText(rowNamed('Spoolman').locator('.health-row-next'), /Start Spoolman .*start-local/);
  // unknown: supervisor missing gives its own reason; Spoolman not checked yet gives another
  send(healthStatus({ supervisor: null, spoolman: { ok: null, pending_deductions: 0 } }));
  await page.waitForFunction(() => /start the supervisor/.test(document.getElementById('system-health-body').innerText));
  await expectText(rowNamed('Supervisor').locator('.health-row-next'), /start the supervisor/);
  await expectText(rowNamed('Spoolman').locator('.health-row-next'), /first Spoolman check/);
  assert.doesNotMatch(await page.locator('#system-health-body').innerText(), /No action needed|follow the recovery steps|Wait for the next health update/);
  console.log(`Health Next lines only on actionable rows PASS ${page.viewportSize().width}`);
}
async function pushDeliveryWarning(fixture) {
  const { page } = fixture;
  fixture.notificationSaveOk = true;
  await page.addInitScript(() => {
    Object.defineProperty(window, 'Notification', { configurable: true, value: { permission: 'granted', requestPermission: async () => 'granted' } });
    Object.defineProperty(window, 'PushManager', { configurable: true, value: function PushManager() {} });
    Object.defineProperty(navigator, 'serviceWorker', { configurable: true, value: { register: async () => ({ pushManager: { subscribe: async () => { throw new Error('Fixture push failure'); } } }) } });
  });
  await page.reload();
  if (page.viewportSize().width < 641) { await page.locator('#btn-mobile-more').click(); await page.locator('#btn-mobile-settings').click(); }
  else await page.locator('#btn-app-settings').click();
  await page.locator('#btn-settings-goto-notifications').click();
  await page.locator('#notif-layer-on').check();
  await page.locator('#btn-notif-save').click();
  await page.waitForFunction(() => document.getElementById('settings-feedback').textContent.includes('push delivery is not registered'));
  assert.equal(await page.locator('#settings-notifications').isVisible(), true);
  assert.match(await page.locator('#settings-feedback').innerText(), /browser notification permission.*service worker/i);
  assert.equal(fixture.notificationSavedBody?.layer?.enabled, true);
  assert.equal(fixture.notificationPosts, 1);
  fixture.notificationSaveOk = false;
  console.log(`Push warning remains persistent after settings save PASS ${page.viewportSize().width}`);
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
  await page.waitForFunction(() => /Could not load notification settings/.test(document.getElementById('notif-state').textContent), null, { timeout: 5000 });
  assert.equal(await page.locator('#notif-state .state-block[data-state="error"]').isVisible(), true);
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
// Dialog foundation: every modal has dialog semantics, a name, a focus trap, topmost-only Escape, focus restore and a reachable primary action.
const dialogJob = { id: 'job-1', name: 'Fixture job', file_name: 'fixture.gcode', source: 'upload', printer_id: null, state: 'queued', print_opts: {} };
const dialogOpeners = {
  'modal-files': "openFileBrowser('operator-fixture')",
  'modal-print-opts': "openPrintOpts('/fixture/a.gcode', 'a.gcode')",
  'modal-scanner': 'openScanner()',
  'modal-changelog': 'openChangelog()',
  'modal-system-health': 'openSystemHealth()',
  'modal-job': `openJobEditor({ mode: 'edit', job: ${JSON.stringify(dialogJob)} })`,
  'modal-review': `openReview(${JSON.stringify(dialogJob)})`,
  'modal-spool-picker': "openModal(document.getElementById('modal-spool-picker'))",
  'modal-add-spool': "openModal(document.getElementById('modal-add-spool'))",
  'modal-app-settings': "openModal(document.getElementById('modal-app-settings'))",
  'outcome-prompt': "OutcomePrompt.open('operator-fixture')",
};
async function dialogs(fixture) {
  const { page } = fixture;
  const size = page.viewportSize();
  await page.evaluate(() => {
    const b = document.createElement('button'); b.id = 't-opener'; b.textContent = 'opener';
    b.style.cssText = 'position:fixed;left:0;bottom:0;z-index:1'; document.body.appendChild(b);
  });
  const opener = page.locator('#t-opener');
  const state = id => page.evaluate(id => {
    const el = id === 'outcome-prompt' ? document.querySelector('.outcome-prompt') : document.getElementById(id);
    return el && { open: el.classList.contains('open'), role: el.getAttribute('role'), modal: el.getAttribute('aria-modal'),
      name: (el.getAttribute('aria-labelledby') ? document.getElementById(el.getAttribute('aria-labelledby'))?.textContent : el.getAttribute('aria-label')) || '',
      inside: el.contains(document.activeElement), onOpener: document.activeElement?.id === 't-opener' };
  }, id);
  const selector = id => id === 'outcome-prompt' ? '.outcome-prompt' : '#' + id;
  for (const [id, call] of Object.entries(dialogOpeners)) {
    await opener.focus();
    await page.evaluate(call);
    await page.waitForFunction(sel => document.querySelector(sel)?.classList.contains('open'), selector(id));
    const opened = await state(id);
    assert.equal(opened.role, 'dialog', id + ' role');
    assert.equal(opened.modal, 'true', id + ' aria-modal');
    assert.ok(opened.name.trim(), id + ' has an accessible name');
    assert.equal(opened.inside, true, id + ' moves focus inside');
    for (let i = 0; i < 25; i++) {
      await page.keyboard.press(i % 5 === 4 ? 'Shift+Tab' : 'Tab');
      assert.equal((await state(id)).inside, true, id + ' Tab stays inside (' + i + ')');
    }
    // the primary action must be reachable without scrolling the page
    const reach = await page.evaluate(sel => {
      const el = document.querySelector(sel);
      const bar = [...el.querySelectorAll('.modal-actions')].find(a => a.getClientRects().length);
      const btn = (bar && ([...bar.querySelectorAll('.btn-primary')].find(b => b.getClientRects().length) || bar.querySelector('button')))
        || el.querySelector('[data-op="ok"]');
      const r = btn.getBoundingClientRect();
      return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, w: innerWidth, h: innerHeight, box: el.querySelector('.modal-box').getBoundingClientRect().toJSON() };
    }, selector(id));
    assert.ok(reach.top >= 0 && reach.bottom <= reach.h + 0.5 && reach.left >= 0 && reach.right <= reach.w + 0.5,
      `${id} primary action inside ${size.width}x${size.height}: ${JSON.stringify(reach)}`);
    assert.ok(reach.box.left >= 0 && reach.box.right <= reach.w + 0.5, id + ' box fits the width');
    await page.keyboard.press('Escape');
    await page.waitForFunction(sel => !document.querySelector(sel)?.classList.contains('open'), selector(id));
    assert.equal((await page.evaluate(() => document.activeElement.id)), 't-opener', id + ' restores focus to the opener');
  }
  // Escape closes the topmost layer only: Print options over Files, then Files
  await opener.focus();
  await page.evaluate("openFileBrowser('operator-fixture')");
  await page.evaluate("openPrintOpts('/fixture/a.gcode', 'a.gcode')");
  await page.keyboard.press('Escape');
  assert.deepEqual(await page.evaluate(() => [...document.querySelectorAll('.modal.open')].map(m => m.id)), ['modal-files']);
  await page.keyboard.press('Escape');
  assert.deepEqual(await page.evaluate(() => [...document.querySelectorAll('.modal.open')].map(m => m.id)), []);
  // a scanner over the Spools panel closes first; the panel stays
  await page.evaluate('openSpools()');
  await page.evaluate('openScanner()');
  await page.keyboard.press('Escape');
  assert.equal(await page.evaluate(() => document.getElementById('modal-scanner').classList.contains('open')), false);
  assert.equal(await page.evaluate(() => document.getElementById('panel-spools').classList.contains('open')), true, 'Spools panel stays open');
  await page.keyboard.press('Escape');
  assert.equal(await page.evaluate(() => document.getElementById('panel-spools').classList.contains('open')), false);
  // plate buttons expose the selection
  await page.evaluate("_setPrintPlate(1)");
  assert.equal(await page.locator('#print-plate-smooth').getAttribute('aria-pressed'), 'true');
  assert.equal(await page.locator('#print-plate-textured').getAttribute('aria-pressed'), 'false');
  await page.evaluate("setJobPlate(false)");
  assert.equal(await page.locator('#job-plate-textured').getAttribute('aria-pressed'), 'true');
  assert.equal(await page.locator('#job-plate-smooth').getAttribute('aria-pressed'), 'false');
  console.log(`Dialog foundation PASS ${size.width}x${size.height}`);
}
// Bounded waits and real loading, error and empty states: delayed and failing responses never leave a blank panel, a false
// "No printers connected", or a form that looks saved while it is not loaded.
async function loadingStates(browser, baseUrl, width, height) {
  const shots = process.env.SHOT_DIR;
  const shot = async (page, name) => { if (shots) await page.screenshot({ path: path.join(shots, `${name}-${width}.png`) }); };
  const open = async (setup) => {
    const context = await browser.newContext({ viewport: { width, height }, serviceWorkers: 'block' });
    const page = await context.newPage();
    const fx = { page, context, sockets: [], wsMode: 'silent', dashboard: 'ok', dashboardGate: null, notifications: 'ok', notificationsGate: null };
    await page.routeWebSocket(/.*/, socket => {
      fx.sockets.push(socket);
      if (fx.wsMode === 'close-first' && fx.sockets.length === 1) socket.close();
    });
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/dashboard') {
        if (fx.dashboardGate) await fx.dashboardGate;
        return fx.dashboard === 'fail' ? route.fulfill({ status: 503, json: { error: 'Fixture dashboard unavailable' } })
          : route.fulfill({ json: { needs_you: { printers: [], prints: [] }, projects: [{ id: 3, name: 'Fixture project', percent: 50, printed_ok: 1, total: 2 }] } });
      }
      if (url.pathname === '/api/notification-settings' && route.request().method() === 'GET') {
        if (fx.notificationsGate) await fx.notificationsGate;
        return fx.notifications === 'fail' ? route.fulfill({ status: 503, json: { error: 'Fixture notification settings unavailable' } }) : route.fulfill({ json: {} });
      }
      return route.fulfill({ json: {} });
    });
    setup?.(fx);
    await page.goto(baseUrl);
    return fx;
  };

  // Printers: nothing is claimed until the first list arrives (even an empty one)
  {
    const fx = await open();
    const { page } = fx;
    await page.locator('#printers-state .state-block[data-state="loading"]').waitFor();
    assert.equal(await page.locator('#empty-state').isVisible(), false, 'No printers connected must not flash before the list arrives');
    assert.equal(await page.locator('#printer-grid').isVisible(), false);
    await shot(page, 'printers-loading');
    for (const socket of fx.sockets) socket.send(JSON.stringify({ type: 'printers_listed' }));
    await page.locator('#empty-state').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#printers-state .state-block').count(), 0);
    await fx.context.close();
  }
  // Printers: a connection that closes before the list shows an error, not "No printers connected"
  {
    const fx = await open(f => { f.wsMode = 'close-first'; });
    const { page } = fx;
    await page.locator('#printers-state .state-block[data-state="error"]').waitFor();
    assert.equal(await page.locator('#empty-state').isVisible(), false, 'a failure is not an empty list');
    await shot(page, 'printers-error');
    await page.waitForFunction(() => document.querySelector('#printers-state .state-block[data-state="loading"]'));   // the automatic reconnect is under way
    for (const socket of fx.sockets) socket.send(JSON.stringify({ type: 'printers_listed' }));
    await page.locator('#empty-state').waitFor({ state: 'visible' });
    await fx.context.close();
  }
  // Needs you and Projects: loading while the dashboard is slow, an error with Retry when it fails, then the real data
  {
    let release;
    const fx = await open(f => { f.dashboard = 'fail'; f.dashboardGate = new Promise(resolve => { release = resolve; }); });
    const { page } = fx;
    await page.locator('#needs-you .state-block[data-state="loading"]').waitFor();
    await page.locator('#dash-projects .state-block[data-state="loading"]').waitFor();
    await shot(page, 'dashboard-loading');
    release();
    const error = page.locator('#needs-you .state-block[data-state="error"]');
    await error.waitFor();
    await page.locator('#dash-projects .state-block[data-state="error"]').waitFor();
    await shot(page, 'dashboard-error');
    fx.dashboard = 'ok'; fx.dashboardGate = null;
    await error.getByRole('button', { name: 'Retry' }).click();
    await page.locator('#dash-projects').getByText('Fixture project').waitFor();
    assert.match(await page.locator('#needs-you').innerText(), /Nothing needs you/, 'nothing needs you: a slim line says so instead of the section vanishing');
    assert.equal(await page.locator('#needs-you-status').innerText(), 'Nothing needs you', 'the live region stays in the DOM');
    await fx.context.close();
  }
  // Notification settings: dimmed and inert while loading, the error and Retry on failure, Save stays disabled
  {
    let release;
    const fx = await open(f => { f.notifications = 'fail'; f.notificationsGate = new Promise(resolve => { release = resolve; }); });
    const { page } = fx;
    if (width < 641) { await page.locator('#btn-mobile-more').click(); await page.locator('#btn-mobile-settings').click(); }
    else await page.locator('#btn-app-settings').click();
    await page.locator('#btn-settings-goto-notifications').click();
    await page.locator('#notif-state .state-block[data-state="loading"]').waitFor();
    assert.equal(await page.locator('#notif-list').getAttribute('data-gated'), 'true');
    assert.equal(await page.locator('#notif-list').evaluate(el => getComputedStyle(el).pointerEvents), 'none', 'the form is inert while loading');
    assert.equal(await page.locator('#notif-finished-on').isDisabled(), true);
    assert.equal(await page.locator('#btn-notif-save').isDisabled(), true);
    await shot(page, 'notifications-loading');
    release();
    await page.locator('#notif-state .state-block[data-state="error"]').waitFor();
    assert.equal(await page.locator('#notif-list').getAttribute('data-gated'), 'true', 'unloaded values are never shown as real');
    assert.equal(await page.locator('#btn-notif-save').isDisabled(), true);
    assert.equal(await page.getByRole('button', { name: 'Retry notification settings' }).isVisible(), true);
    await shot(page, 'notifications-error');
    await fx.context.close();
  }
  console.log(`Loading, error and empty states PASS ${width}`);
}

(async () => {
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
  const browser = await chromium.launch();
  try {
    for (const width of [320, 390, 430, 768, 1440]) {
      for (const scenario of [cockpit, inventory, settings, notificationRecovery, pushDeliveryWarning, healthNextLines]) {
        const fixture = await fixturePage(browser, `http://127.0.0.1:${server.address().port}`, width);
        try { await scenario(fixture); assertReadOnlyCommands(fixture); } finally { await fixture.context.close(); }
      }
    }
    for (const [width, height] of [[390, 844], [1098, 507]]) await loadingStates(browser, `http://127.0.0.1:${server.address().port}`, width, height);
    for (const [width, height] of [[390, 844], [1098, 507]]) {
      const fixture = await fixturePage(browser, `http://127.0.0.1:${server.address().port}`, width, height);
      try { await dialogs(fixture); assertReadOnlyCommands(fixture); } finally { await fixture.context.close(); }
      const hardware = await fixturePage(browser, `http://127.0.0.1:${server.address().port}`, width, height);
      try { await hardwareFeedback(hardware); } finally { await hardware.context.close(); }
    }
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; });


