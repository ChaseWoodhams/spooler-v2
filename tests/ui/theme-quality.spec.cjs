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
   for(const item of measurements.motion) if(item.animation > .01 || item.transition > .01 || (item.animation > 0 && item.iterations==='infinite')) failures.push({theme,state,motion:item});
   await context.close();
  }
  if(failures.length) console.error('Measured failures:',JSON.stringify(failures,null,2));
  assert.equal(failures.length,0,'Rendered colors, focus and motion must meet this targeted regression threshold');
  console.log('PASS: dark/light empty and connected dashboard contrast, focus-visible, reduced motion');
 } finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
