// The Split step's blocked actions give a reason (tied by aria-describedby) and bring it into view when pressed; a failed
// Auto-fix is a note beside the cut list, not a failed split; a Split that stops on a Part marks that Part and names the cut.
// The server is stubbed (reads only, plus the few writes a test names); no printer is ever contacted.
const assert = require('node:assert/strict');
const fx = require('./fixtures.cjs');
const sf = require('./slicer-fixtures.cjs');
const { chromium } = require('playwright');

const { cut, piece } = sf;
const checkOf = (...pieces) => ({ cuts: [cut(1, 100)], pieces });
const readySplit = (check = checkOf(piece(), piece()), extra = {}) => ({ state: 'ready', result: null, stage: null, cuts: { cuts: [cut(1, 100)] }, check, ...extra });

(async () => {
  const server = await fx.startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    const open = (splitOverride, extra = {}) => sf.openStep(browser, server.baseUrl, { step: 'split', ready: '[data-action="split"]', splitOverride, ...extra });
    const reasonOf = async (page, id = 'split-why') => (await page.locator(`#${id}`).innerText()).replace(/\s+/g, ' ').trim();
    const tied = async (page, button, id = 'split-why') => assert.equal(await page.locator(button).getAttribute('aria-describedby'), id, `${button} is tied to #${id}`);

    // A stage is running
    let t = await open(readySplit(undefined, { state: 'planning', stage: 'plan', progress: { message: 'Choosing cut 2', percent: null } }));
    try {
      assert.equal(await t.page.locator('[data-action="split"]').getAttribute('aria-disabled'), 'true');
      assert.equal(await reasonOf(t.page), "Can't split while the split is planning");
      await tied(t.page, '[data-action="split"]');
      assert.ok(await t.page.locator('#split-why').isVisible(), 'the reason is visible text');
      console.log('PASS: Split says why while a stage is running');
    } finally { await t.context.close(); }

    // Nothing planned yet
    t = await open(readySplit({ cuts: [], pieces: [] }, { cuts: { cuts: [] } }));
    try {
      assert.equal(await reasonOf(t.page), 'Plan the cuts first: there are no parts to split yet');
      console.log('PASS: Split says to plan the cuts first');
    } finally { await t.context.close(); }

    // Cuts edited and not yet checked
    t = await open(readySplit(), { onWrite: ({ path, split }) => { if (path.endsWith('/cuts')) { split.state = 'checking'; return split; } } });
    try {
      await t.page.locator('[data-cut-offset="1"]').fill('120');
      await t.page.locator('[data-cut-offset="1"]').dispatchEvent('change');
      assert.equal(await reasonOf(t.page), "Can't split until the cuts are checked");
      await tied(t.page, '[data-action="split"]');
      console.log('PASS: Split says why while the cuts are unchecked');
    } finally { await t.context.close(); }

    // A Part that is too big
    t = await open(readySplit(checkOf(piece(), piece({ fits: false, cuts: [1, 2], made_by_cut: 2 }))));
    try {
      assert.equal(await reasonOf(t.page), 'Part 2 is too big for the printer, move Cuts 1, 2 or use Auto-fix');
      assert.equal(await t.page.locator('[data-action="split"]').getAttribute('aria-disabled'), 'true');
      await tied(t.page, '[data-action="split"]');
      console.log('PASS: Split names a Part that is too big and the cuts that shaped it');
    } finally { await t.context.close(); }

    // A too-small Part with no creating cut names the cuts that apply to it; with none, the scale
    t = await open(readySplit(checkOf(piece({ tiny: true, made_by_cut: null, cuts: [3, 5] }), piece())));
    try {
      assert.equal(await reasonOf(t.page), 'Part 1 is too small to print, move or remove Cuts 3, 5');
      console.log('PASS: A too-small Part with no creating cut still names the cuts to change');
    } finally { await t.context.close(); }
    t = await open(readySplit(checkOf(piece({ tiny: true, made_by_cut: null, cuts: [] })), { cuts: { cuts: [] } }));
    try {
      assert.match(await reasonOf(t.page), /Part 1 is too small to print and no cut made it, so check the model's scale and units/);
      console.log('PASS: A too-small uncut model points at scale and units, not at fewer cuts');
    } finally { await t.context.close(); }

    // Pressing a blocked action shows and emphasises its reason
    t = await open(readySplit(undefined, { state: 'checking', stage: 'check' }));
    try {
      await t.page.locator('[data-action="plan"]').click({ force: true });
      await t.page.waitForSelector('#split-plan-why.reason-flash');
      await t.page.locator('[data-action="split"]').click({ force: true });
      await t.page.waitForSelector('#split-why.reason-flash');
      assert.ok(await t.page.locator('#split-why').isVisible());
      await t.page.waitForFunction(() => !document.querySelector('#split-why.reason-flash'), null, { timeout: 5000 });
      console.log('PASS: Pressing a blocked action emphasises its reason for a moment');
    } finally { await t.context.close(); }

    // Model step: "Split it anyway" while the model is being worked on
    t = await sf.openStep(browser, server.baseUrl, { step: 'model', ready: '[data-action="go-split"]',
      mutate: (p, s) => { s.analysis.fits_whole = true; }, splitOverride: { state: 'planning', stage: 'plan', progress: { message: 'Planning', percent: null } } });
    try {
      const go = t.page.locator('[data-action="go-split"]');
      assert.equal(await go.getAttribute('aria-disabled'), 'true');
      assert.equal(await go.getAttribute('aria-describedby'), 'go-split-why');
      assert.equal(await reasonOf(t.page, 'go-split-why'), "Can't split while the split is planning");
      console.log('PASS: Split it anyway says why it is blocked');
    } finally { await t.context.close(); }

    // Editing a cut while Plan again is running does not leave a false "same plan" note
    t = await open(readySplit(), { onWrite: ({ path, split }) => {
      if (path.endsWith('/plan')) { split.state = 'planning'; split.stage = 'plan'; return split; }
      if (path.endsWith('/cuts')) return split;
    } });
    try {
      await t.page.locator('[data-action="plan"]').click();
      await t.page.waitForFunction(() => document.querySelector('[data-action="plan"]')?.getAttribute('aria-disabled') === 'true');
      await t.page.locator('[data-cut-offset="1"]').fill('130');
      await t.page.locator('[data-cut-offset="1"]').dispatchEvent('change');
      t.split.state = 'ready'; t.split.stage = null;     // the planner finishes with the same cuts
      await t.page.waitForFunction(() => document.querySelector('[data-action="plan"]')?.getAttribute('aria-disabled') !== 'true', null, { timeout: 10000 });
      assert.equal(await t.page.locator('#plan-result').count(), 0, 'no "same plan" note after the cuts were edited');
      console.log('PASS: Editing a cut during a running plan leaves no false same-plan note');
    } finally { await t.context.close(); }

    // A failed Auto-fix is a note next to the cut list; the split stays usable
    const autofixNote = "Auto-fix couldn't remove the parts that are too small to print. The cuts are unchanged: move or remove the cut that makes them.";
    t = await open(readySplit(checkOf(piece(), piece({ tiny: true, made_by_cut: 2, cuts: [1, 2] }))), { onWrite: ({ path, split }) => {
      if (path.endsWith('/plan')) { split.state = 'planning'; split.stage = 'plan'; return split; }
    } });
    try {
      await t.page.locator('[data-action="autofix"]').click();
      await t.page.waitForFunction(() => document.querySelector('[data-action="autofix"]')?.getAttribute('aria-disabled') === 'true');
      t.split.state = 'ready'; t.split.stage = null; t.split.plan_note = autofixNote;   // as the server leaves it
      await t.page.waitForSelector('#plan-result', { timeout: 10000 });
      assert.equal((await t.page.locator('#plan-result').innerText()).trim(), autofixNote);
      assert.equal(await t.page.locator('#plan-result').getAttribute('role'), 'status');
      assert.equal(await t.page.locator('.split-failed').count(), 0, 'not shown as a failed split');
      assert.ok(await t.page.locator('[data-cut-offset="1"]').isVisible(), 'the cut list is still there');
      assert.ok(await t.page.locator('#split-why [data-action="select-cut"]').isVisible(), 'the cut to change can be selected');
      assert.doesNotMatch(await t.page.locator('#plan-result').innerText(), /found nothing to change|same plan/i);
      console.log('PASS: A failed Auto-fix is a note beside the cut list, with the cuts untouched');
    } finally { await t.context.close(); }

    // A Split that stopped on a Part marks it, names the cut and offers to select it
    const stopped = 'Part 2 is too small to print — move or remove Cut 2';
    t = await open(readySplit(checkOf(piece(), piece({ tiny: false, made_by_cut: 2, cuts: [1, 2] })), { state: 'failed', stage: 'split', error: stopped, error_parts: [2], cuts: { cuts: [cut(1, 100), cut(2, 150)] } }));
    try {
      assert.match(await t.page.locator('.split-failed [role="alert"]').innerText(), /Part 2 is too small to print — move or remove Cut 2/);
      const row = t.page.locator('[data-check-part="2"][data-failed]');
      assert.match(await row.innerText(), /the split stopped on this part · change Cut 2/);
      assert.equal(await t.page.locator('[data-check-part="1"][data-failed]').count(), 0);
      assert.equal(await t.page.locator('#split-viewer canvas').evaluate(c => c.dataset.failedParts), '1', 'the Part is outlined in the preview');
      await row.locator('[data-action="select-cut"]').click();
      assert.equal(await t.page.locator('.split-cut.selected').getAttribute('data-cut'), '2');
      assert.equal(await t.page.locator('[data-action="split"]').getAttribute('aria-disabled'), 'true', 'Split stays blocked after a failure');
      console.log('PASS: A Split that stopped on a Part highlights it, names the cut and offers to select it');
    } finally { await t.context.close(); }
  } finally { await browser.close(); await server.close(); }
})().catch(error => { console.error(error); process.exit(1); });
