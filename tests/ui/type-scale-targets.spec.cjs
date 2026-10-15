// Slicer steps (Model, Split, Plates, Slice, Print) at 1280 px in both themes: no text under 12 px, sizes from type
// tokens, every visible control has a hit area of at least 24 x 24 px, keyboard focus is visible, status is never
// colour alone, and the per-Plate send checkbox's hit area is the whole Plate row.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '../..');
const STEPS = ['model', 'split', 'plates', 'slice', 'print'];
const CONTROLS = 'button, input:not([type=hidden]), select, textarea, a[href], summary, [role=button], [role=tab], [role=switch]';

// Static: every font-size in these steps' CSS is a token (or relative), and every size token is >= 12 px.
for (const file of ['slicer.css', 'split.css']) {
  const css = fs.readFileSync(path.join(root, 'public', file), 'utf8');
  const bad = [...css.matchAll(/font-size:\s*([^;}]+)/g)].map(m => m[1].trim()).filter(v => !/^(var\(--fs-[a-z-]+\)|inherit|[\d.]+(em|rem|%))$/.test(v));
  assert.deepEqual([...new Set(bad)], [], `${file} has one-off font sizes`);
}
const tokens = JSON.parse(fs.readFileSync(path.join(root, 'tokens/spooler.tokens.json'), 'utf8')).base.dimension;
const sizes = Object.entries(tokens).filter(([k]) => k.startsWith('fs-'));
assert.ok(tokens['fs-body'] && tokens['fs-small'], 'fs-body and fs-small tokens exist');
assert.equal(tokens['fs-body'].$value, '14px', 'body text is 14 px');
for (const [k, v] of sizes) assert.ok(parseFloat(v.$value) >= 12, `${k} is under 12 px`);
console.log('PASS: font sizes come from type tokens, none under 12 px');

function measure(controlsSelector) {
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0' && !el.closest('[hidden], canvas, dialog:not([open])');
  };
  const small = [], tiny = [];
  const label = el => `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}${el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/).join('.') : ''} "${(el.innerText || el.getAttribute('aria-label') || el.value || '').trim().slice(0, 30)}"`;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n; (n = walker.nextNode());) {
    if (!n.nodeValue.trim()) continue;
    const el = n.parentElement;
    if (!el || ['SCRIPT', 'STYLE', 'OPTION'].includes(el.tagName) || !visible(el)) continue;
    const px = parseFloat(getComputedStyle(el).fontSize);
    if (px < 12) tiny.push(`${px}px ${label(el)}`);
  }
  for (const el of document.querySelectorAll(controlsSelector)) {
    if (!visible(el)) continue;
    const box = el.matches('input[type=checkbox], input[type=radio]') && el.closest('label') ? el.closest('label') : el;
    const r = box.getBoundingClientRect();
    if (r.width < 24 || r.height < 24) small.push(`${Math.round(r.width)}x${Math.round(r.height)} ${label(el)}`);
  }
  return { small, tiny };
}

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    for (const theme of ['dark', 'light']) for (const step of STEPS) {
      const { page, context } = await sf.openSlicer(browser, server.baseUrl, { width: 1280, height: 900, theme, step });
      try {
        await page.waitForSelector(`.slicer-step.active[data-step="${step}"]`, { timeout: 10000 });
        await page.waitForTimeout(600);
        if (step === 'print') { await page.locator('[data-tick]').first().check(); await page.evaluate(() => document.querySelectorAll('details').forEach(d => { d.open = true; })); }
        const { small, tiny } = await page.evaluate(measure, CONTROLS);
        assert.deepEqual(tiny, [], `${theme}/${step}: text under 12 px`);
        assert.deepEqual(small, [], `${theme}/${step}: controls under 24x24`);

        // focus ring: every control shows an outline or box-shadow when focused from the keyboard
        await page.keyboard.press('Tab');
        const handles = await page.locator(CONTROLS).elementHandles();
        const missing = [];
        for (const h of handles) {
          const info = await h.evaluate(el => {
            const r = el.getBoundingClientRect(), s = getComputedStyle(el);
            if (!(r.width > 0 && r.height > 0) || s.visibility === 'hidden' || s.display === 'none' || el.closest('[hidden], canvas, dialog:not([open])') || el.disabled) return null;
            el.focus({ focusVisible: true });
            if (document.activeElement !== el) return null;
            const f = getComputedStyle(el);
            const ring = (f.outlineStyle !== 'none' && parseFloat(f.outlineWidth) > 0) || (f.boxShadow !== 'none');
            return { ring, name: `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}.${String(el.className).trim().split(/\s+/).join('.')}` };
          });
          if (info && !info.ring) missing.push(info.name);
        }
        assert.deepEqual(missing, [], `${theme}/${step}: no visible focus ring`);
      } finally { await context.close(); }
    }
    console.log('PASS: no text under 12 px, controls >= 24x24, focus rings visible (1280 px, both themes, five steps)');

    // Print: the whole Plate row is the send checkbox's label
    for (const theme of ['dark', 'light']) {
      const { page, context } = await sf.openSlicer(browser, server.baseUrl, { theme, step: 'print' });
      try {
        await page.locator('[data-tick]').first().waitFor();
        const row = page.locator('[data-plate-card]:has([data-tick])').first();
        const rowBox = await row.boundingBox();
        const labelBox = await page.locator('[data-tick]').first().evaluate(el => { const l = el.closest('label'); if (!l) return null; const r = l.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height }; });
        assert.ok(labelBox, 'the send checkbox sits in a label');
        for (const k of ['x', 'y', 'width', 'height']) assert.ok(Math.abs(labelBox[k] - rowBox[k]) <= 1.5, `label ${k} ${labelBox[k]} matches the row ${rowBox[k]}`);
        const tick = page.locator('[data-tick]').first();
        await page.mouse.click(rowBox.x + rowBox.width - 6, rowBox.y + 4);
        assert.equal(await tick.isChecked(), true, 'clicking empty space in the row ticks the Plate');
        await page.mouse.click(rowBox.x + rowBox.width - 6, rowBox.y + 4);
        assert.equal(await tick.isChecked(), false, 'clicking it again unticks');
      } finally { await context.close(); }
    }
    console.log('PASS: the send checkbox hit area is the whole Plate row');

    // Status is never colour alone: each Plate row carries its status in words
    for (const theme of ['dark', 'light']) {
      const { page, context } = await sf.openSlicer(browser, server.baseUrl, { theme, step: 'print' });
      try {
        await page.locator('[data-plate-card]').first().waitFor();
        const rows = await page.locator('[data-plate-card]').evaluateAll(list => list.map(li => ({ cls: li.className, text: li.querySelector('.pp-state')?.innerText.trim() || li.closest('.pp-group')?.querySelector('h3')?.innerText.trim() || '' })));
        assert.ok(rows.length >= 2);
        for (const r of rows) assert.ok(r.text.length >= 3, `Plate row (${r.cls}) states its status in words (own label or its group heading)`);
        const empties = await page.evaluate(() => [...document.querySelectorAll('.pp-state, .pp-chip, .slicer-tag, .pd-chip')].filter(e => !e.innerText.trim() && !e.getAttribute('aria-label')).length);
        assert.equal(empties, 0, 'no status chip is empty (colour only)');
      } finally { await context.close(); }
    }
    console.log('PASS: status always carries text');
  } finally { await browser.close(); await server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
