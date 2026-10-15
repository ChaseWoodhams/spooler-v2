// The Assembly guide panel tells "no guide made yet" (offer Generate) apart from "generation failed" (show the error and Retry).
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = require('playwright');

const emptyGuide = { state: 'none', stage: null, percent: null, error: null, has_pdf: false, paper: null, generated_at: null, plan_code: null, out_of_date: false, blocked: '' };

async function open(browser, baseUrl, guideReply) {
  const { page, context } = await sf.openSlicer(browser, baseUrl, { step: 'split' });
  const posts = [];
  await page.route('**/api/splits/1/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/guide') && route.request().method() === 'GET') return route.fulfill(guideReply());
    if (url.pathname.endsWith('/guide') && route.request().method() === 'POST') { posts.push(route.request().postDataJSON()); return route.fulfill({ json: { ...emptyGuide, state: 'generating', stage: 'Starting', percent: 0 } }); }
    if (url.pathname.endsWith('/assembly-plan')) return route.fulfill({ status: 404, json: { error: 'no plan' } });
    return route.fallback();
  });
  await page.route('**/api/splits/status', route => route.fulfill({ json: { guide: { available: true, default_paper: 'a4', papers: ['a4', 'letter'] } } }));
  await page.waitForSelector('[data-guide]', { timeout: 10000 });
  return { page, context, posts };
}

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    // Not found: no guide made yet, with Generate, never "Not found"
    let r = await open(browser, server.baseUrl, () => ({ status: 404, json: { error: 'Not found' } }));
    try {
      const panel = r.page.locator('[data-guide]');
      await panel.getByText('No guide has been made for this split yet').waitFor({ timeout: 5000 });
      assert.ok(!/not found/i.test(await panel.innerText()), 'the panel must not say "Not found"');
      assert.equal(await panel.locator('[role=alert]').count(), 0, 'no error is shown for a missing guide');
      await panel.getByRole('button', { name: 'Generate', exact: true }).click();
      await r.page.waitForTimeout(300);
      assert.equal(r.posts.length, 1, 'Generate starts the guide from the panel');
      console.log('PASS: A not-found guide shows "no guide made yet" with a working Generate');
    } finally { await r.context.close(); }

    // Real failure: the error and a Retry action
    r = await open(browser, server.baseUrl, () => ({ status: 500, json: { error: 'PDF library crashed' } }));
    try {
      const panel = r.page.locator('[data-guide]');
      await panel.getByText('PDF library crashed').waitFor({ timeout: 5000 });
      assert.ok(!/No guide has been made/.test(await panel.innerText()), 'a failure is not "no guide yet"');
      await panel.getByRole('button', { name: 'Retry', exact: true }).click();
      await r.page.waitForTimeout(300);
      assert.equal(r.posts.length, 1, 'Retry starts generation again');
      console.log('PASS: A failed guide shows the error and Retry');
    } finally { await r.context.close(); }

    // A failed generation job reported by the server also offers Retry
    r = await open(browser, server.baseUrl, () => ({ json: { ...emptyGuide, state: 'failed', error: 'Render failed on step 3' } }));
    try {
      const panel = r.page.locator('[data-guide]');
      await panel.getByText('Render failed on step 3').waitFor({ timeout: 5000 });
      await panel.getByRole('button', { name: 'Retry', exact: true }).waitFor({ timeout: 2000 });
      console.log('PASS: A failed generation job offers Retry');
    } finally { await r.context.close(); }
  } finally { await browser.close(); await server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
