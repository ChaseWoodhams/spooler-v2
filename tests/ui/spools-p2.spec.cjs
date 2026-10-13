// Ticket 20 · Spools P2: scanner errors by cause (PS-26), EAN lookup that reports inline and keeps typed fields (PS-27),
// Add Spool form (PS-28), Spool picker with an empty inventory or nothing assigned (PS-29).
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');

(async () => {
  const server = http.createServer(async (req, res) => {
    try {
      const relative = new URL(req.url, 'http://localhost').pathname;
      const content = await fs.readFile(path.join(root, 'public', relative === '/' ? 'index.html' : relative));
      res.setHeader('Content-Type', relative.endsWith('.js') ? 'text/javascript; charset=utf-8' : relative.endsWith('.css') ? 'text/css; charset=utf-8' : 'text/html; charset=utf-8');
      res.end(content);
    } catch { res.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch();
  const open = async (width, { inventory = [], lookup = null, cameraError = null, blockLibrary = false } = {}) => {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    const seen = { filaments: [], spools: [], lookups: [] };
    page.on('pageerror', e => console.error('Page error:', e.message));
    if (cameraError) await page.addInitScript(name => {
      window.Html5Qrcode = class { start() { return Promise.reject(Object.assign(new Error('camera'), { name })); } stop() {} clear() {} };
    }, cameraError);
    if (blockLibrary) await page.route(/unpkg\.com/, route => route.abort());
    await page.routeWebSocket(/.*/, ws => ws.onMessage(message => {
      if (JSON.parse(message).action === 'list_printers') ws.send(JSON.stringify({ type: 'printer_update', printer: { id: 'p1', name: 'Printer One', connected: false, printer_type: 'cc1', status: {}, camera_url: null } }));
    }));
    await page.route('**/api/**', route => {
      const url = new URL(route.request().url());
      const method = route.request().method();
      if (url.pathname === '/api/spoolman/api/v1/spool' && method === 'GET') return route.fulfill({ json: inventory });
      if (url.pathname === '/api/spoolman/api/v1/spool' && method === 'POST') { seen.spools.push(route.request().postDataJSON()); return route.fulfill({ json: { id: 99 } }); }
      if (url.pathname === '/api/spoolman/api/v1/filament' && method === 'POST') { seen.filaments.push(route.request().postDataJSON()); return route.fulfill({ json: { id: 5 } }); }
      if (url.pathname === '/api/filament-meta') return route.fulfill({ json: { brands: ['Acme'], materials: [{ name: 'PLA', density: 1.24 }] } });
      if (url.pathname === '/api/lookup-ean') { seen.lookups.push(url.search); return lookup ? route.fulfill(lookup) : route.fulfill({ status: 404, json: {} }); }
      return route.fulfill({ json: {} });
    });
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.locator('#card-p1').waitFor();
    await page.waitForFunction(() => document.getElementById('spools-list') !== null);
    return { page, seen };
  };
  const openAdd = async page => { await page.evaluate(() => document.getElementById('btn-add-spool').click()); await page.locator('#modal-add-spool').waitFor({ state: 'visible' }); };

  try {
    // PS-26: one message per cause, manual entry one tap away
    for (const [cameraError, expected] of [
      ['NotAllowedError', /Camera permission was denied/], ['NotFoundError', /No camera found/], ['NotReadableError', /in use by another app/]]) {
      const { page } = await open(390, { cameraError });
      await openAdd(page);
      await page.locator('#btn-scan-ean').click();
      await page.locator('#scanner-error-msg').waitFor({ state: 'visible' });
      assert.match(await page.locator('#scanner-error-msg').innerText(), expected, cameraError);
      assert.match(await page.locator('#scanner-error-sub').innerText(), /EAN/, 'every cause offers manual entry');
      await page.locator('#btn-scanner-manual').click();
      await page.locator('#modal-scanner').waitFor({ state: 'hidden' });
      assert.equal(await page.locator('#spool-input-ean').evaluate(el => el === document.activeElement), true, 'focus lands in the EAN field');
      await page.close();
    }
    {
      const { page } = await open(390, { blockLibrary: true });
      await openAdd(page);
      await page.locator('#btn-scan-ean').click();
      await page.locator('#scanner-error-msg').waitFor({ state: 'visible' });
      assert.match(await page.locator('#scanner-error-msg').innerText(), /Barcode scanning isn.t available/);
      assert.match(await page.locator('#scanner-error-sub').innerText(), /library/);
      await page.close();
    }
    console.log('PASS: scanner errors by cause (PS-26)');

    // PS-27: inline lookup status; typed fields are kept and named
    {
      const { page } = await open(390, { lookup: { json: { manufacturer: 'Acme', material: 'PLA', color_name: 'Red', color_hex: 'ff0000', weight: 750, name: 'Acme PLA Red' } } });
      await openAdd(page);
      const placeholder = await page.locator('#spool-input-ean').getAttribute('placeholder');
      assert.ok(placeholder.length <= 16, `placeholder fits a phone (${placeholder})`);
      await page.locator('#spool-input-color').fill('Mine');
      await page.locator('#spool-input-ean').fill('4006381333931');
      await page.locator('#spool-input-ean').press('Enter');
      const status = page.locator('#spool-ean-status');
      await page.waitForFunction(() => /Found/.test(document.getElementById('spool-ean-status').textContent));
      assert.match(await status.innerText(), /Filled in: brand, material, colour, weight\./);
      assert.match(await status.innerText(), /Kept what you typed: colour name\./);
      assert.equal(await page.locator('#spool-input-color').inputValue(), 'Mine', 'the typed colour name is not overwritten');
      assert.equal(await page.locator('#spool-input-weight').inputValue(), '750');
      assert.equal(await page.locator('#spool-input-brand').inputValue(), 'Acme');
      await page.close();
      const miss = await open(390);
      await openAdd(miss.page);
      await miss.page.locator('#spool-input-ean').fill('00000000');
      await miss.page.locator('#spool-input-ean').press('Enter');
      await miss.page.waitForFunction(() => /database/.test(document.getElementById('spool-ean-status').textContent));
      assert.equal(await miss.page.locator('#spool-ean-status').getAttribute('data-state'), 'error');
      assert.equal(await miss.page.locator('#spool-input-weight').inputValue(), '1000', 'a miss changes nothing');
      await miss.page.close();
      console.log('PASS: EAN lookup reports inline and keeps typed fields (PS-27)');
    }

    // PS-28: a real form, inline errors, optional colour is not saved as grey
    {
      const { page, seen } = await open(390);
      await openAdd(page);
      assert.equal(await page.locator('form#form-add-spool #btn-add-spool-confirm[type="submit"]').count(), 1, 'a real form');
      assert.equal(await page.locator('#spool-input-diameter').isVisible(), false, 'diameter and density are under Advanced');
      await page.locator('#spool-input-material').selectOption('');
      await page.locator('#spool-input-weight').fill('');
      await page.locator('#btn-add-spool-confirm').click();
      assert.equal(await page.locator('#spool-input-material').getAttribute('aria-invalid'), 'true');
      assert.equal(await page.locator('#spool-input-weight').getAttribute('aria-invalid'), 'true');
      assert.match(await page.locator('#spool-input-material-error').innerText(), /Choose a material/);
      assert.match(await page.locator('#spool-form-feedback').innerText(), /2 fields need attention/);
      assert.match(await page.locator('#spool-input-material').getAttribute('aria-describedby'), /spool-input-material-error/);
      assert.equal(await page.locator('#spool-input-material').evaluate(el => el === document.activeElement), true, 'focus goes to the first problem');
      await page.locator('#spool-input-diameter').evaluate(el => { el.closest('details').open = true; });
      await page.locator('#spool-input-diameter').fill('0');
      await page.locator('#spool-input-material').selectOption('__other__');
      await page.locator('#spool-input-material-other').fill('PLA');
      await page.locator('#spool-input-weight').fill('900');
      await page.locator('#btn-add-spool-confirm').click();
      assert.match(await page.locator('#spool-input-diameter-error').innerText(), /Diameter must be greater than zero/);
      assert.equal(await page.locator('#spool-input-material').getAttribute('aria-invalid'), null, 'a fixed field loses its error');
      await page.locator('#spool-input-diameter').fill('1.75');
      await page.locator('#spool-input-weight').press('Enter');   // Enter submits a real form
      await page.waitForFunction(() => document.getElementById('modal-add-spool').classList.contains('open') === false);
      assert.equal(seen.filaments.length, 1);
      assert.equal('color_hex' in seen.filaments[0], false, 'no colour chosen: none is saved');
      assert.equal(seen.filaments[0].material, 'PLA');
      await openAdd(page);
      await page.locator('#spool-input-material').selectOption('PLA');
      await page.locator('#spool-input-hex').fill('#ff0000');
      assert.match(await page.locator('#spool-color-hex-value').innerText(), /#FF0000/);
      assert.equal(await page.locator('#spool-colour-pick').getAttribute('data-set'), 'true');
      await page.locator('#btn-add-spool-confirm').click();
      await page.waitForFunction(() => document.getElementById('modal-add-spool').classList.contains('open') === false);
      assert.equal(seen.filaments[1].color_hex, 'ff0000');
      await page.close();
      console.log('PASS: Add Spool form (PS-28)');
    }

    // PS-29: picker with no inventory, and nothing to unassign
    {
      const { page } = await open(390);
      await page.evaluate(() => openSpoolPicker('p1'));
      await page.locator('#modal-spool-picker').waitFor({ state: 'visible' });
      await page.getByText('No spools in inventory').waitFor();
      assert.equal(await page.getByRole('button', { name: /None/ }).count(), 0, 'nothing to unassign');
      await page.locator('#btn-picker-add-spool').click();
      await page.locator('#modal-add-spool').waitFor({ state: 'visible' });
      assert.equal(await page.locator('#modal-spool-picker').isVisible(), false, 'the picker steps aside for the form');
      await page.close();

      const stocked = [{ id: 17, location: '', initial_weight: 1000, remaining_weight: 750, filament: { id: 18, material: 'PLA', name: 'Blue', color_hex: '0000FF', vendor: { name: 'Acme' } } }];
      const some = await open(390, { inventory: stocked });
      await some.page.evaluate(() => fetchSpools());
      await some.page.waitForFunction(() => spools.length === 1);
      await some.page.evaluate(() => openSpoolPicker('p1'));
      await some.page.locator('.spool-pick-item').first().waitFor();
      assert.equal(await some.page.getByRole('button', { name: /None/ }).count(), 0, 'no None row when nothing is assigned');
      assert.equal(await some.page.locator('.spool-pick-item').count(), 1);
      await some.page.evaluate(() => { spools[0].location = 'Printer One'; renderPickerList('p1'); });
      assert.equal(await some.page.getByRole('button', { name: /None/ }).count(), 1, 'None returns once a Spool is assigned');
      await some.page.close();
      console.log('PASS: Spool picker empty and unassigned states (PS-29)');
    }
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(err => { console.error(err); process.exitCode = 1; });
