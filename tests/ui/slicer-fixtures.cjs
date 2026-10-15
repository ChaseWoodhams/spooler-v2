// Slicer workspace fixture: a Project with a Split (two Parts), models, two Plates and a current Slice on Plate 1.
// openSlicer(browser, baseUrl, { width, height, theme, step }) opens slicer.html?project=1 on that step with /api/** stubbed
// (reads only; any write fails the test) and never reaches a real printer. The 3D viewport, toolpath and layer slider
// render from stubbed data and are not judged by anything here.
const fx = require('./fixtures.cjs');

const SPLIT_ID = 1;
const part = (id, n) => ({ id, name: `Bracket part ${n}`, file: `bracket-part-${n}.stl`, state: 'ready', size: [180, 60, 30], split: { id: SPLIT_ID, part: n, supports: n === 2 } });
const makeProject = () => ({
  id: 1, name: 'Bracket assembly', revision: 3, split_id: SPLIT_ID, split_ids: [SPLIT_ID], server_time: Date.now() / 1000,
  models: [part(11, 1), part(12, 2)],
  slices: {
    1: { state: 'sliced', outdated: false, sliced_at: 1700000000, estimate_s: 7260, estimate_g: 42.4, layers: 150, observed: { state: 'match' } },
  },
  data: {
    plates: [{ id: 1, name: 'Plate 1' }, { id: 2, name: 'Plate 2' }],
    objects: [
      { id: 1, model: 11, name: 'Bracket part 1', plate: 1, position: [120, 120], rotation: [0, 0, 0], scale: [1, 1, 1], overrides: {} },
      { id: 2, model: 12, name: 'Bracket part 2', plate: 2, position: [120, 120], rotation: [0, 0, 0], scale: [1, 1, 1], overrides: {} },
    ],
    bed: 'textured', profile: { bundle_id: null, process: {}, filament: {} },
  },
});
const makeSplit = () => ({
  id: SPLIT_ID, source_name: 'bracket.stl', state: 'split',
  settings: { scale: 1, up: 'z', printer: { bed_x: 240, bed_y: 240, height: 250 }, planner: { mode: 'smart', balanced: false }, connectors: { kind: 'none' } },
  cuts: { cuts: [{ id: 1, normal: [1, 0, 0], offset: 0 }] },
  analysis: { raw_size: [360, 60, 30], size: [360, 60, 30], volume_cm3: 120, source_triangles: 4800, triangles: 4800, repairs: [], mean_wall_mm: 2.4, fits_whole: false, estimated_parts: 2 },
  check: { pieces: [{ fits: true }, { fits: true }] },
  result: { summary: { parts: 2, cuts: 1, rods: {}, rod_lengths_mm: {}, holes_not_shared_by_two: 0, joints: 1, joints_without_rods: 1, parts_needing_supports: 1 },
    parts: [{ number: 1, print_size: [180, 60, 30], holes: 0 }, { number: 2, print_size: [180, 60, 30], holes: 0 }], assembly: [{ part: 1 }, { part: 2, joins: [1], rods: 0 }] },
});
// SPV1 preview mesh: one box 180 x 60 x 30 mm (the format viewport.js parses)
function boxMesh() {
  const [x, y, z] = [180, 60, 30];
  const v = [[0, 0, 0], [x, 0, 0], [x, y, 0], [0, y, 0], [0, 0, z], [x, 0, z], [x, y, z], [0, y, z]];
  const t = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]];
  const b = Buffer.alloc(16 + 96 + 144);
  b.write('SPV1', 0); b.writeUInt32LE(1, 4); b.writeUInt32LE(8, 8); b.writeUInt32LE(12, 12);
  v.flat().forEach((n, i) => b.writeFloatLE(n, 16 + i * 4));
  t.flat().forEach((n, i) => b.writeUInt32LE(n, 112 + i * 4));
  return b;
}
const analysis = () => ({ layers: Array.from({ length: 150 }, (_, i) => ({ z: 0.2 * (i + 1), height: 0.2, offset: 0, length: 0 })), features: ['Outer wall', 'Infill'],
  feature_stats: { 'Outer wall': { extruded_mm: 3000, time_s: 2400 }, Infill: { extruded_mm: 2000, time_s: 3000 } }, temperatures: {} });

// GET /api/projects/:id/status, shaped as projects.plate_status returns it
const projectStatus = () => ({ project_id: 1, print_order: [1, 2], plates: [
  { id: 1, name: 'Plate 1', status: 'sliced', outcome_unknown: false, outdated: false, mixed: null, filament: null, parts: 1, layout: 'fx', job_id: null, printer_id: null, problems: [], problem_reason: '', warning_reason: '', estimate_s: 7260, estimate_g: 42.4, time_estimate: null },
  { id: 2, name: 'Plate 2', status: 'unsliced', outcome_unknown: false, outdated: false, mixed: null, filament: null, parts: 1, layout: 'fx', job_id: null, printer_id: null, problems: [], problem_reason: '', warning_reason: '', estimate_s: null, estimate_g: null, time_estimate: null }],
  progress: { total: 2, counts: { unsliced: 1, sliced: 1, queued: 0, waiting: 0, printing: 0, printed_ok: 0, failed: 0 }, printed_ok: 0, outcome_unknown: 0, percent: 0,
    filament_g: { used: 0, total: 42.4, unknown_plates: 1, spool: null, by_filament: [] } } });

// Text each step must show (independent literals from the data above)
const EXPECT = {
  model: ['bracket.stl', '360 × 60 × 30'],
  split: ['180 × 60 × 30', '2 from 1 cut'],
  plates: ['Plate 1', 'Plate 2', 'Bracket part 1', '180 × 60 × 30 mm'],
  slice: ['Current Slice', '150 layers'],
  print: ['0 of 2 plates completed', 'needs slicing'],
};

async function openSlicer(browser, baseUrl, { width = 1280, height = 900, theme = 'dark', step = 'model', plateFit = null, mutate = null, slices = null, writes = null } = {}) {
  const project = makeProject(), split = makeSplit();
  if (slices) project.slices = slices;
  if (mutate) mutate(project);
  const context = await browser.newContext({ viewport: { width, height }, serviceWorkers: 'block' });
  await context.addInitScript(([t, s]) => { try { localStorage.setItem('theme', t); localStorage.setItem('slicer.step.1', s); } catch (_) { /* ignore */ } }, [theme, step]);
  const page = await context.newPage();
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) console.error('console:', m.text()); });
  await page.routeWebSocket(/.*/, () => {});
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url());
    if (route.request().method() !== 'GET' && writes) { const out = writes(route.request().method(), new URL(route.request().url()).pathname, project); if (out !== undefined) return route.fulfill({ json: out }); }
    if (route.request().method() !== 'GET') return route.fulfill({ status: 500, json: { detail: 'fixture is read-only' } });
    const p = url.pathname;
    const json = p === '/api/projects/1' ? project : p === `/api/splits/${SPLIT_ID}` ? split
      : /\/plates\/\d+\/analysis$/.test(p) ? analysis()
      : /\/toolpath$/.test(p) ? null
      : /\/models\/\d+\/preview$/.test(p) ? 'mesh'
      : p === '/api/jobs' ? { jobs: [] } : p === '/api/outcome-prompts' ? { awaiting: [] }
      : /\/plates\/\d+\/fit$/.test(p) ? (plateFit ? plateFit(url) : { printer_id: null, warnings: [] })
      : p.endsWith('/status') ? projectStatus()
      : p.endsWith('/jobs') ? { jobs: [] } : p === '/api/printers' ? fx.printers(2)
      : p === '/api/defaults' ? { defaults: { options: {} } } : p === '/api/slicer/spools' ? { spools: [] }
      : p === '/api/slicer/presets' ? { machines: [], processes: {}, filaments: {} } : {};
    if (json === 'mesh') return route.fulfill({ body: boxMesh(), contentType: 'application/octet-stream' });
    if (json === null) return route.fulfill({ body: Buffer.alloc(8), contentType: 'application/octet-stream' });
    return route.fulfill({ json });
  });
  await page.goto(`${baseUrl}/slicer.html?project=1`);
  return { page, context };
}

module.exports = { openSlicer, makeProject, makeSplit, EXPECT };
