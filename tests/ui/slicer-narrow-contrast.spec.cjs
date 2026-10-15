// Slicer steps (Model, Split, Plates, Slice, Print): no horizontal scroll at 320 px, and AA text contrast in both
// themes. Contrast is computed from computed styles over the composited ancestor backgrounds. Not measured: text over
// images/canvas (skipped, counted; gradients are judged against their worst colour stop), non-text contrast (borders, icons), hover/focus states.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = require('playwright');
const STEPS = ['model', 'split', 'plates', 'slice', 'print'];

function overflow() {
  const vw = document.documentElement.clientWidth, bad = [];
  const name = el => `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}${typeof el.className === 'string' && el.className.trim() ? '.' + el.className.trim().split(/\s+/).join('.') : ''}`;
  const doc = document.documentElement;
  if (doc.scrollWidth > vw) bad.push(`document scrollWidth ${doc.scrollWidth} > ${vw}`);
  if (document.body.scrollWidth > vw) bad.push(`body scrollWidth ${document.body.scrollWidth} > ${vw}`);
  const clipped = el => { for (let n = el.parentElement; n; n = n.parentElement) { const o = getComputedStyle(n).overflowX; if (o !== 'visible') return n; } return null; };
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    if (r.width <= 1 || s.display === 'none' || s.visibility === 'hidden' || el.closest('[hidden], canvas, dialog:not([open])')) continue;
    if (el.scrollWidth > el.clientWidth + 1 && el.clientWidth > 0 && ['visible', 'hidden'].includes(s.overflowX) && s.display !== 'inline' && s.textOverflow !== 'ellipsis') bad.push(`${name(el)} content ${el.scrollWidth} > box ${el.clientWidth}`);
    if (r.right > vw + 1 && !clipped(el)) bad.push(`${name(el)} right edge ${Math.round(r.right)} > ${vw}`);
  }
  return [...new Set(bad)].slice(0, 15);
}

function contrast() {
  const parse = c => { const p = c.match(/[\d.]+/g)?.map(Number) || [0, 0, 0, 0]; return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; };
  const over = (t, b) => [0, 1, 2].map(i => t[i] * t[3] + b[i] * (1 - t[3])).concat(1);
  const lum = c => { const [r, g, b] = c.slice(0, 3).map(v => { v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }); return r * .2126 + g * .7152 + b * .0722; };
  const name = el => `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}${typeof el.className === 'string' && el.className.trim() ? '.' + el.className.trim().split(/\s+/).join('.') : ''}`;
  const fails = [], seen = new Set(); let checked = 0, skipped = 0;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n; (n = walker.nextNode());) {
    const el = n.parentElement;
    if (!n.nodeValue.trim() || !el || seen.has(el) || ['SCRIPT', 'STYLE', 'OPTION', 'NOSCRIPT'].includes(el.tagName)) continue;
    seen.add(el);
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    if (!r.width || !r.height || s.visibility === 'hidden' || s.display === 'none' || el.closest('[hidden], canvas, dialog:not([open])') || el.closest(':disabled, [aria-disabled=true]')) continue;
    const layers = []; let stops = null;
    for (let a = el; a; a = a.parentElement) { const cs = getComputedStyle(a), own = parse(cs.backgroundColor); if (cs.backgroundImage !== 'none' && !stops) stops = cs.backgroundImage.startsWith('url(') ? [] : [...cs.backgroundImage.matchAll(/rgba?([^)]*)/g)].map(m => parse(m[0])); layers.unshift(own); if (own[3] === 1) break; }
    if (stops && !stops.length) { skipped++; continue; }
    const base = layers.reduce((b, t) => over(t, b), [255, 255, 255, 1]);
    // a gradient/image behind the text: judge against its worst colour stop (an image is skipped)
    const bgs = stops ? stops.map(t => over(t, base)) : [base];
    let op = 1; for (let a = el; a; a = a.parentElement) op *= parseFloat(getComputedStyle(a).opacity);
    let ratio = Infinity;
    for (const bg of bgs) { const t = parse(s.color), fgc = over([t[0], t[1], t[2], t[3] * op], bg); const [x, y] = [lum(fgc), lum(bg)]; ratio = Math.min(ratio, (Math.max(x, y) + .05) / (Math.min(x, y) + .05)); }
    const px = parseFloat(s.fontSize), large = px >= 24 || (px >= 18.66 && parseInt(s.fontWeight) >= 700);
    checked++;
    if (ratio < (large ? 3 : 4.5)) fails.push(`${ratio.toFixed(2)}:1 ${name(el)} "${n.nodeValue.trim().slice(0, 24)}"`);
  }
  return { fails, checked, skipped };
}

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  const summary = [];
  try {
    for (const theme of ['dark', 'light']) for (const step of STEPS) {
      for (const width of [320, 1280]) {
        let ctx;
        for (let attempt = 0; attempt < 3; attempt++) {
          try {
            ctx = await sf.openSlicer(browser, server.baseUrl, { width, height: width === 320 ? 640 : 900, theme, step });
            await ctx.page.waitForSelector(`.slicer-step.active[data-step="${step}"]`, { timeout: 10000 });
            break;
          } catch (e) { await ctx?.context.close(); ctx = null; if (attempt === 2) throw e; }
        }
        const { page, context } = ctx;
        try {
          await page.waitForTimeout(600);
          if (step === 'print') { await page.locator('[data-tick]').first().check(); await page.evaluate(() => document.querySelectorAll('details:not(.tool-more)').forEach(d => { d.open = true; })); await page.waitForTimeout(700); }
          if (width === 320) assert.deepEqual(await page.evaluate(overflow), [], `${theme}/${step}: horizontal overflow at 320 px`);
          {
            const c = await page.evaluate(contrast);
            summary.push(`${theme}/${step}/${width}: ${c.checked} checked, ${c.skipped} skipped`);
            assert.deepEqual(c.fails, [], `${theme}/${step}/${width}: text under AA`);
          }
        } finally { await context.close(); }
      }
    }
    console.log('PASS: no horizontal scroll at 320 px; text contrast AA in both themes\n' + summary.join('\n'));
  } finally { await browser.close(); await server.close(); }
})().catch(e => { console.error(e.message || e); process.exit(1); });
