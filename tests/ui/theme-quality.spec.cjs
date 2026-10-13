// This targeted regression measures rendered UI colors; it is not a WCAG conformance audit.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const fixtures = [0, 3, 15, 6, 9, 14, 0].map((code, i) => ({
  id: `theme-${i}`, name: `Contrast fixture ${i + 1}`, printer_type: 'cc1', connected: i !== 6,
  status: { CurrentStatus: [0], PrintInfo: { Status: code, Progress: 20, Filename: 'fixture.gcode' },
    TempOfNozzle: 210, TempOfHotbed: 60, TempOfBox: 22 }, camera_url: null,
}));
(async () => {
 // Static guard: every file that resets an outline must also define a :focus-visible ring built on --focus-ring.
 for (const file of ['style.css','slicer.css','history-quality.css','quick-print-quality.css','split.css','login.html']) {
  const text = require('node:fs').readFileSync(path.join(root, 'public', file), 'utf8');
  if (/outline:\s*(0|none)/.test(text)) assert.ok(/:focus-visible[^{]*\{[^}]*outline:\s*\d+px solid (var\(--focus-ring\)|#ff6b2b)/.test(text), file + ' removes an outline without a :focus-visible replacement');
  assert.ok(!/:focus-visible[^{]*\{[^}]*outline:[^;}]*var\(--accent\)/.test(text), file + ' focus ring must use var(--focus-ring), not --accent');
 }
 const server = http.createServer(async (req, res) => {
  try {
   const pathname = new URL(req.url, 'http://localhost').pathname;
   const file = path.join(root, 'public', pathname === '/' ? 'index.html' : pathname);
   res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
   res.end(await fs.readFile(file));
  } catch { res.writeHead(404).end(); }
 });
 await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
 const browser = await chromium.launch();
 const failures = [];
 try {
  for (const theme of ['dark', 'light']) for (const state of ['empty', 'connected']) {
   const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, serviceWorkers: 'block', reducedMotion: 'reduce' });
   const page = await context.newPage();
   await page.addInitScript(theme => localStorage.setItem('theme', theme), theme);
   await page.routeWebSocket(/.*/, ws => ws.onMessage(message => {
    if (JSON.parse(message).action === 'list_printers' && state === 'empty') ws.send(JSON.stringify({ type: 'printers_listed' }));
    if (JSON.parse(message).action === 'list_printers' && state === 'connected')
     fixtures.forEach(printer => ws.send(JSON.stringify({ type: 'printer_update', printer })));
   }));
   await page.route('**/api/**', route => route.fulfill({ json: {} }));
   await page.goto(`http://127.0.0.1:${server.address().port}`);
   if (state === 'connected') await page.locator('#card-theme-6').waitFor();
   else assert.equal(await page.locator('#empty-state').isVisible(), true);
   await page.locator('#btn-quick-print').focus();
   await page.keyboard.press('Tab');
   const measurements = await page.evaluate(() => {
    const parse = color => {
     const parts = color.match(/[\d.]+/g)?.map(Number) || [0, 0, 0, 0];
     return [parts[0], parts[1], parts[2], parts.length > 3 ? parts[3] : 1];
    };
    const over = (top, bottom) => [0,1,2].map(i => top[i] * top[3] + bottom[i] * (1 - top[3])).concat(1);
    const background = element => {
     const layers = [];
     for (let el = element; el; el = el.parentElement) layers.unshift(parse(getComputedStyle(el).backgroundColor));
     return layers.reduce((bottom, top) => over(top, bottom), [255,255,255,1]);
    };
    const luminance = color => {
     const channels = color.slice(0,3).map(c => { c /= 255; return c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4; });
     return channels[0] * .2126 + channels[1] * .7152 + channels[2] * .0722;
    };
    const contrast = (a,b) => { const x=luminance(a),y=luminance(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05); };
    const visible = element => element.getClientRects().length && getComputedStyle(element).visibility !== 'hidden' && !element.closest('[hidden]');
    const selectors = ['#empty-state h2','#empty-state p','header h1','header .btn-primary','header .btn-secondary','.card-name','.status-badge','.temp-label','.temp-value','.temp-target','.card-controls .btn-primary','.card-controls .btn-secondary','.card-controls .btn-danger','.camera-placeholder span','.progress-filename','.progress-pct'];
    const text = selectors.flatMap(selector => [...document.querySelectorAll(selector)].filter(el => visible(el) && !el.disabled).map(el => {
     const style = getComputedStyle(el), bg = background(el), color = over(parse(style.color), bg);
     return { selector, label: el.textContent.trim().slice(0,80), color: style.color, background: bg.slice(0,3).map(Math.round).join(','), ratio: contrast(color,bg), required: 4.5 };
    }));
    const active = document.activeElement, style = getComputedStyle(active), outline = parse(style.outlineColor);
    const focus = { selector: `#${active.id}`, focusVisible: active.matches(':focus-visible'), style: style.outlineStyle, width: parseFloat(style.outlineWidth),
      ratio: parseFloat(style.outlineOffset) > 0 ? contrast(outline, background(active.parentElement)) : Math.min(contrast(outline, background(active.parentElement)), contrast(outline, background(active))), required: 3 };
    const motion = [...document.querySelectorAll('.status-dot,.btn')].filter(visible).map(el => {
     const style=getComputedStyle(el);
     const seconds=value=>Math.max(...value.split(',').map(x=>parseFloat(x)*(x.trim().endsWith('ms')?.001:1)));
     return { selector: el.className, animation: seconds(style.animationDuration), iterations: style.animationIterationCount, transition: seconds(style.transitionDuration) };
    });
    return { text, focus, motion };
   });
   assert.ok(measurements.text.length >= (state === 'empty' ? 3 : 12),'Rendered contrast samples exist');
   if(state === 'connected')assert.ok(measurements.motion.some(item=>item.selector.includes('printing')),'Printing fixture exercises pulse animation');
   const combos = new Map(measurements.text.map(item => [`${item.selector}|${item.color}|${item.background}`,item]));
   for(const item of combos.values()) {
    console.log(`${theme}/${state} text ${item.selector} ${item.color} on rgb(${item.background}) = ${item.ratio.toFixed(2)}:1`);
    if(item.ratio < item.required) failures.push({theme,state,...item});
   }
   const primary = page.locator('header .btn-primary').first();
   await primary.hover();
   await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
   const hovered = await primary.evaluate(el => {
    const style=getComputedStyle(el);
    const luminance=color=>{const c=color.match(/[\d.]+/g).slice(0,3).map(Number).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;});return c[0]*.2126+c[1]*.7152+c[2]*.0722;};
    const a=luminance(style.color),b=luminance(style.backgroundColor);
    return {selector:'header .btn-primary:hover',color:style.color,background:style.backgroundColor,ratio:(Math.max(a,b)+.05)/(Math.min(a,b)+.05),required:4.5};
   });
   console.log(`${theme}/${state} primary hover ${JSON.stringify(hovered)}`);
   if(hovered.ratio < hovered.required)failures.push({theme,state,...hovered});
   console.log(`${theme}/${state} focus ${JSON.stringify(measurements.focus)}`);
   if(!measurements.focus.focusVisible || measurements.focus.style==='none' || measurements.focus.width < 2 || measurements.focus.ratio < 3) failures.push({theme,state,...measurements.focus});
   if (state === 'connected') {
    // Text painted on the accent: every site that fills with --accent-fill must keep white text at >= 4.5:1.
    await page.addStyleTag({ url: `http://127.0.0.1:${server.address().port}/slicer.css` });
    await page.evaluate(() => openPrinterDashboard('theme-1'));
    await page.waitForFunction(() => typeof window.cockpitPrinterHtml === 'function');
    await page.evaluate(() => renderPrinterDetail(printers['theme-1']));
    await page.locator('.pv-chip.on').waitFor();
    const accent = await page.evaluate(() => {
     const parse = color => color.match(/[\d.]+/g).slice(0, 3).map(Number);
     const luminance = color => { const c = parse(color).map(v => { v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }); return c[0] * .2126 + c[1] * .7152 + c[2] * .0722; };
     const contrast = (a, b) => { const x = luminance(a), y = luminance(b); return (Math.max(x, y) + .05) / (Math.min(x, y) + .05); };
     const probe = (html, selector) => { const host = document.createElement('div'); host.innerHTML = html; document.body.appendChild(host); return host.querySelector(selector); };
     const targets = {
      '--accent-fill token': probe('<span style="background:var(--accent-fill);color:#fff">x</span>', 'span'),
      '.toast-btn': probe('<button class="toast-btn">Undo</button>', 'button'),
      '.sidenav-badge': probe('<span class="sidenav-badge">3</span>', 'span'),
      '.needs-count': probe('<span class="needs-count">3</span>', 'span'),
      '.review-view-toggle .btn.active': probe('<div class="review-view-toggle"><button class="btn active">3D</button></div>', 'button'),
      '.slicer-step.active .num': probe('<div class="slicer-step active"><span class="num">1</span></div>', '.num'),
      '.slicer-mode .btn.active': probe('<div class="slicer-mode"><button class="btn active">Plan</button></div>', 'button'),
      '.slicer-tabs .btn.active': probe('<div class="slicer-tabs"><button class="btn active">Quality</button></div>', 'button'),
      '.slicer-toolbar .btn.active': probe('<div class="slicer-toolbar"><button class="btn active">Move</button></div>', 'button'),
      '.scope-toggle button.active': probe('<div class="scope-toggle"><button class="active">Plate</button></div>', 'button'),
      '.pv-chip.on': document.querySelector('.pv-chip.on'),
     };
     const rows = Object.entries(targets).map(([selector, el]) => { const style = getComputedStyle(el); if (!el || !style.backgroundColor.startsWith('rgb')) throw new Error(selector + ' bg=' + (el && style.backgroundColor));  if (!/^rgb/.test(style.color)) throw new Error(selector + ' color=' + style.color); return { selector, color: style.color, background: style.backgroundColor, ratio: contrast(style.color, style.backgroundColor), required: 4.5 }; });
     const chip = document.querySelector('.pv-chip.on'), idle = document.querySelector('.pv-chip:not(.on)');
     const cue = { weight: parseInt(getComputedStyle(chip).fontWeight, 10), idleWeight: parseInt(getComputedStyle(idle).fontWeight, 10), mark: getComputedStyle(chip, '::before').content };
     return { rows, cue };
    });
    for (const row of accent.rows) {
     console.log(`${theme} accent-fill ${row.selector} ${row.color} on ${row.background} = ${row.ratio.toFixed(2)}:1`);
     if (row.ratio < row.required) failures.push({ theme, state, ...row });
    }
    console.log(`${theme} selected jog chip cue ${JSON.stringify(accent.cue)}`);
    if (!(accent.cue.weight > accent.cue.idleWeight) || !/2713|✓/.test(accent.cue.mark)) failures.push({ theme, state, selector: '.pv-chip.on non-colour cue', ...accent.cue });
   }
   if (state === 'connected') {
    // Every input/select/textarea family and the card Files button must show the one --focus-ring ring (>= 2px, >= 3:1 vs its surround).
    await page.addStyleTag({ url: `http://127.0.0.1:${server.address().port}/split.css` });
    const rings = await page.evaluate(async () => {
     const parse = c => c.match(/[\d.]+/g).map(Number);
     const lum = c => { const v = parse(c).slice(0,3).map(x => { x /= 255; return x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4; }); return v[0]*.2126 + v[1]*.7152 + v[2]*.0722; };
     const contrast = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x,y)+.05)/(Math.min(x,y)+.05); };
     const surface = el => { for (let e = el; e; e = e.parentElement) { const c = getComputedStyle(e).backgroundColor; if (parse(c).length < 4 || parse(c)[3] > .9) return c; } return 'rgb(255,255,255)'; };
     const probes = { '.card-files-btn': '<button class="card-files-btn">f</button>', '.modal-box input': '<div class="modal-box"><input></div>', '.modal-box select': '<div class="modal-box"><select></select></div>', '.modal-box textarea': '<div class="modal-box"><textarea></textarea></div>', '.panel-form input': '<div class="panel-form"><input></div>', '.panel-form select': '<div class="panel-form"><select></select></div>', '.history-filters input': '<div class="history-filters"><input></div>', '.history-filters select': '<div class="history-filters"><select></select></div>', '.history-notes textarea': '<div class="history-notes"><textarea></textarea></div>', '.split-side input': '<div class="split-side"><input></div>' };
     const token = getComputedStyle(document.body).getPropertyValue('--focus-ring').trim();
     const frames = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 50))));
     const out = [];
     for (const [selector, html] of Object.entries(probes)) {
      const host = document.createElement('div'); host.innerHTML = html; document.body.appendChild(host);
      const el = host.querySelector('button,input,select,textarea'); el.focus(); await frames(); // outline colour transitions in; measure once settled
      const st = getComputedStyle(el), r = { selector, token, focusVisible: el.matches(':focus-visible'), style: st.outlineStyle, width: parseFloat(st.outlineWidth), color: st.outlineColor, ratio: contrast(st.outlineColor, surface(host)) };
      host.remove(); out.push(r);
     }
     return out;
    });
    for (const r of rings) {
     console.log(`${theme} ring ${r.selector} ${r.width}px ${r.color} = ${r.ratio.toFixed(2)}:1`);
     if (!r.token || !r.focusVisible || r.style === 'none' || r.width < 2 || r.ratio < 3) failures.push({ theme, state, ...r });
    }
   }
   for(const item of measurements.motion) if(item.animation > .01 || item.transition > .01 || (item.animation > 0 && item.iterations==='infinite')) failures.push({theme,state,motion:item});
   await context.close();
  }
  if(failures.length) console.error('Measured failures:',JSON.stringify(failures,null,2));
  assert.equal(failures.length,0,'Rendered colors, focus and motion must meet this targeted regression threshold');
  console.log('PASS: dark/light empty and connected dashboard contrast, focus-visible, reduced motion');
 } finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
