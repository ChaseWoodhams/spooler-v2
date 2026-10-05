/* Spooler – job review: toolpath preview and preflight checks, the only way a queued job is started.
   Loaded before app.js; uses app.js/jobs.js globals (printers, jobsApi, jobPrinterOptions, toast, …) at call time. */
"use strict";

const REVIEW_ICON = { pass: "✓", info: "i", warn: "!", fail: "✕" };
const REVIEW_STATUS = {
  checking: ["idle", "Checking…"], pass: ["complete", "Ready"], warn: ["paused", "Warnings"], fail: ["offline", "Blocked"],
};
const FEATURE_COLORS = {
  "Outer wall": "#ff8a3d", "Inner wall": "#ffc66d", "Overhang wall": "#ff5d5d", "Sparse infill": "#6c8cff",
  "Internal solid infill": "#a66cff", "Solid infill": "#a66cff", "Top surface": "#ff5da2", "Bottom surface": "#4fd1c5",
  "Bridge": "#5fb3ff", "Internal Bridge": "#5fb3ff", "Gap infill": "#e5e5e5", "Skirt": "#8a8a8a", "Brim": "#3ccf6e",
  "Support": "#9aa0a6", "Support interface": "#c7ccd1", "Support transition": "#c7ccd1", "Prime tower": "#b58a5a",
  "Ironing": "#ffd84d",
};
const TOOLPATH_SCALE  = 100; // must match gcode_analysis.POINT_SCALE / POINT_OFFSET
const TOOLPATH_OFFSET = 50;
const BED_MM = 256;
const PURGE_ZONE = [246, 0, 256, 20];
const ISO_POINT_BUDGET_BYTES = 1_600_000; // ~400k points drawn per frame in the 3D view

let _review = null; // { job, report, toolpath: DataView | null, layer, view }
let _reviewFrame = 0;

// ─── Open / check / start ─────────────────────────────────────────────────────
async function openReview(job) {
  _review = { job, report: null, toolpath: null, layer: 0, view: "3d" };
  document.getElementById("review-title").textContent = job.name;
  document.getElementById("review-subtitle").textContent = jobMetaText(job);
  const select = document.getElementById("review-printer");
  select.innerHTML = jobPrinterOptions(job.printer_id);
  select.disabled = job.source === "printer";
  document.getElementById("review-ack").checked = false;
  _setReviewView("3d");
  document.getElementById("modal-review").classList.add("open");
  await runReview();
}

function closeReview() {
  document.getElementById("modal-review").classList.remove("open");
  _review = null;
}

async function runReview() {
  const review = _review;
  if (!review) return;
  const printerId = document.getElementById("review-printer").value;
  _setReviewStatus("checking");
  document.getElementById("review-check-list").innerHTML = '<li class="queue-empty">Running preflight checks…</li>';
  document.getElementById("btn-review-start").disabled = true;
  try {
    const query = printerId ? `?printer_id=${encodeURIComponent(printerId)}` : "";
    const report = await jobsApi("GET", `/api/jobs/${review.job.id}/preflight${query}`);
    if (_review !== review) return;
    review.report = report;
    if (report.preview && !review.toolpath) {
      const resp = await fetch(`/api/jobs/${review.job.id}/toolpath`);
      if (resp.ok) review.toolpath = new DataView(await resp.arrayBuffer());
      if (_review !== review) return;
      review.layer = report.preview.layers.length - 1;
    }
    _renderReviewReport();
    drawReview();
  } catch (err) {
    if (_review !== review) return;
    _setReviewStatus("fail");
    document.getElementById("review-check-list").innerHTML =
      `<li class="review-check fail"><span class="review-check-icon">✕</span><div class="review-check-title">${escHtml(err.message)}</div></li>`;
  }
}

async function startFromReview() {
  const review = _review;
  if (!review?.report) return;
  const btn = document.getElementById("btn-review-start");
  const printerId = document.getElementById("review-printer").value || null;
  btn.disabled = true;
  try {
    await jobsApi("POST", `/api/jobs/${review.job.id}/start`, {
      printer_id: printerId,
      acknowledge_warnings: review.report.needs_acknowledgement,
    });
    toast(`Starting ${review.job.name} on ${jobPrinterName(printerId || review.job.printer_id)}`);
    closeReview();
  } catch (err) {
    toast(err.message, true);
    runReview();
  }
  loadJobs();
}

async function acceptWarningsForLater() {
  const review = _review;
  if (!review?.report) return;
  const btn = document.getElementById("btn-review-accept");
  btn.disabled = true;
  try {
    const printerId = document.getElementById("review-printer").value || null;
    const result = await jobsApi("POST", `/api/jobs/${review.job.id}/acknowledge-warnings`, printerId ? { printer_id: printerId } : {});
    toast(`Accepted ${result.acknowledged.length} warning${result.acknowledged.length === 1 ? "" : "s"}: Bed cleared can start ${review.job.name} on them`);
  } catch (err) {
    toast(err.message, true);
  }
  btn.disabled = false;
}

function _updateReviewStart() {
  const report = _review?.report;
  document.getElementById("btn-review-accept").hidden = !(report && report.can_start && report.needs_acknowledgement);
  document.getElementById("btn-review-start").disabled =
    !report || !report.can_start || !document.getElementById("review-ack").checked;
}

// ─── Checks panel ─────────────────────────────────────────────────────────────
function _setReviewStatus(status) {
  const [cls, label] = REVIEW_STATUS[status];
  const badge = document.getElementById("review-status");
  badge.className = `status-badge ${cls}`;
  badge.textContent = label;
}

function _renderReviewReport() {
  const { report } = _review;
  _setReviewStatus(report.status);

  document.getElementById("review-check-list").innerHTML = report.checks.map(c =>
    `<li class="review-check ${c.level}"><span class="review-check-icon">${REVIEW_ICON[c.level]}</span><div>` +
    `<div class="review-check-title">${escHtml(c.title)}</div>` +
    (c.detail ? `<div class="review-check-detail">${escHtml(c.detail)}</div>` : "") +
    "</div></li>").join("");

  const preview = report.preview;
  const summary = [];
  if (preview) {
    const est = preview.estimates || {};
    const model = preview.bounds.model;
    if (est.estimate_s) summary.push(`<span><strong>${escHtml(formatTime(est.estimate_s))}</strong> print</span>`);
    if (est.estimate_g) summary.push(`<span><strong>${Math.round(est.estimate_g)} g</strong> filament</span>`);
    summary.push(`<span><strong>${preview.layers.length}</strong> layers</span>`);
    if (model) {
      const dims = [0, 1, 2].map(i => (i === 2 ? model.max[2] : model.max[i] - model.min[i]).toFixed(1));
      summary.push(`<span><strong>${dims.join(" × ")}</strong> mm</span>`);
    }
    const settings = [preview.config.print_settings_id, preview.config.filament_settings_id].filter(Boolean);
    if (settings.length) summary.push(`<span>${escHtml(settings.join(" · "))}</span>`);
  }
  document.getElementById("review-summary").innerHTML = summary.join("");

  const nozzleCheck = report.checks.find(c => c.id === "nozzle");
  document.getElementById("review-nozzle").hidden = !(report.printer_id && nozzleCheck && nozzleCheck.level === "info");
  document.getElementById("review-ack-text").textContent = report.needs_acknowledgement
    ? "I've read the warnings above, and the build plate is clear"
    : "The build plate is clear and the right filament is loaded";
  _updateReviewStart();
}

// ─── Toolpath drawing ─────────────────────────────────────────────────────────
function _eachPath(view, layer, fn) {
  let pos = layer.offset;
  const end = layer.offset + layer.length;
  while (pos < end) {
    const count = view.getUint16(pos + 1, true);
    fn(view.getUint8(pos), count, pos + 3);
    pos += 3 + count * 4;
  }
}

function _point(view, base, i) {
  return [view.getUint16(base + i * 4, true) / TOOLPATH_SCALE - TOOLPATH_OFFSET,
          view.getUint16(base + i * 4 + 2, true) / TOOLPATH_SCALE - TOOLPATH_OFFSET];
}

function _featureColor(name) {
  return FEATURE_COLORS[name] || "#9fb3c8";
}

function _setReviewView(view) {
  if (_review) _review.view = view;
  document.getElementById("review-view-3d").classList.toggle("active", view === "3d");
  document.getElementById("review-view-layer").classList.toggle("active", view === "layer");
}

function drawReview() {
  cancelAnimationFrame(_reviewFrame);
  _reviewFrame = requestAnimationFrame(_drawReviewNow);
}

function _drawReviewNow() {
  const review = _review;
  const preview = review?.report?.preview;
  const canvas = document.getElementById("review-canvas");
  const ready = !!(preview && review.toolpath && preview.layers.length);
  document.getElementById("review-no-preview").hidden = ready || !review?.report;
  for (const id of ["review-canvas", "review-layer-row", "review-legend", "review-view-toggle"]) {
    document.getElementById(id).hidden = !ready;
  }
  if (!ready) return;

  const layers = preview.layers;
  review.layer = Math.max(0, Math.min(review.layer, layers.length - 1));
  const slider = document.getElementById("review-layer");
  slider.max = layers.length - 1;
  slider.value = review.layer;
  document.getElementById("review-layer-label").textContent =
    `Layer ${review.layer + 1} of ${layers.length} · Z ${layers[review.layer].z} mm`;

  const size = Math.max(200, Math.min(canvas.parentElement.clientWidth, 560));
  const dpr = window.devicePixelRatio || 1;
  canvas.width = canvas.height = Math.round(size * dpr);
  canvas.style.width = canvas.style.height = `${size}px`;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, size, size);
  ctx.lineCap = ctx.lineJoin = "round";
  if (review.view === "layer") _drawTopView(ctx, size, preview, review);
  else _drawIsoView(ctx, size, preview, review);

  const used = new Set();
  for (let i = 0; i <= review.layer; i++) _eachPath(review.toolpath, layers[i], f => used.add(f));
  document.getElementById("review-legend").innerHTML = [...used]
    .map(i => preview.features[i]).filter(name => name && name !== "Unknown" && name !== "Custom")
    .map(name => `<span style="--swatch:${_featureColor(name)}">${escHtml(name)}</span>`).join("");
}

function _strokeLayer(ctx, view, layer, features, project, colorFor) {
  _eachPath(view, layer, (feature, count, base) => {
    ctx.strokeStyle = colorFor(features[feature]);
    ctx.beginPath();
    for (let i = 0; i < count; i++) {
      const [sx, sy] = project(..._point(view, base, i));
      if (i) ctx.lineTo(sx, sy); else ctx.moveTo(sx, sy);
    }
    ctx.stroke();
  });
}

function _drawTopView(ctx, size, preview, review) {
  const margin = 12;
  const scale = (size - margin * 2) / BED_MM;
  const X = x => margin + x * scale;
  const Y = y => size - margin - y * scale; // bed front (Y 0) at the bottom

  ctx.fillStyle = "rgba(127,127,127,.08)";
  ctx.fillRect(X(0), Y(BED_MM), BED_MM * scale, BED_MM * scale);
  ctx.strokeStyle = "rgba(127,127,127,.22)";
  ctx.lineWidth = 1;
  for (let mm = 0; mm <= BED_MM; mm += 32) {
    ctx.beginPath(); ctx.moveTo(X(mm), Y(0)); ctx.lineTo(X(mm), Y(BED_MM)); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(X(0), Y(mm)); ctx.lineTo(X(BED_MM), Y(mm)); ctx.stroke();
  }
  const [px0, py0, px1, py1] = PURGE_ZONE;
  ctx.fillStyle = "rgba(239,68,68,.22)";
  ctx.fillRect(X(px0), Y(py1), (px1 - px0) * scale, (py1 - py0) * scale);

  const project = (x, y) => [X(x), Y(y)];
  const layers = preview.layers;
  if (review.layer > 0) {
    ctx.lineWidth = Math.max(1, 0.4 * scale);
    _strokeLayer(ctx, review.toolpath, layers[review.layer - 1], preview.features, project, () => "rgba(127,127,127,.35)");
  }
  ctx.lineWidth = Math.max(1, 0.45 * scale);
  _strokeLayer(ctx, review.toolpath, layers[review.layer], preview.features, project, _featureColor);
}

function _drawIsoView(ctx, size, preview, review) {
  const bounds = preview.bounds.model;
  if (!bounds) return;
  const cos = Math.cos(Math.PI / 6), sin = Math.sin(Math.PI / 6);
  const raw = (x, y, z) => [(x - y) * cos, -(x + y) * sin - z]; // back of the bed and higher layers go up

  const corners = [];
  for (const x of [bounds.min[0], bounds.max[0]])
    for (const y of [bounds.min[1], bounds.max[1]])
      for (const z of [0, bounds.max[2]]) corners.push(raw(x, y, z));
  const xs = corners.map(c => c[0]), ys = corners.map(c => c[1]);
  const margin = 20;
  const width = Math.max(...xs) - Math.min(...xs) || 1, height = Math.max(...ys) - Math.min(...ys) || 1;
  const scale = Math.min((size - 2 * margin) / width, (size - 2 * margin) / height);
  const ox = (size - width * scale) / 2 - Math.min(...xs) * scale;
  const oy = (size - height * scale) / 2 - Math.min(...ys) * scale;

  const layers = preview.layers;
  let bytes = 0;
  for (let i = 0; i <= review.layer; i++) bytes += layers[i].length;
  const step = Math.max(1, Math.ceil(bytes / ISO_POINT_BUDGET_BYTES));
  ctx.lineWidth = Math.max(0.6, 0.4 * scale);
  for (let i = 0; i <= review.layer; i++) {
    const current = i === review.layer;
    if (!current && i % step) continue;
    const z = layers[i].z;
    ctx.globalAlpha = current ? 1 : 0.35 + 0.45 * (i / Math.max(1, review.layer));
    const project = (x, y) => { const [sx, sy] = raw(x, y, z); return [ox + sx * scale, oy + sy * scale]; };
    _strokeLayer(ctx, review.toolpath, layers[i], preview.features, project, _featureColor);
  }
  ctx.globalAlpha = 1;
}

// ─── Events ───────────────────────────────────────────────────────────────────
document.getElementById("review-layer").addEventListener("input", (e) => {
  if (!_review) return;
  _review.layer = Number(e.target.value);
  drawReview();
});
document.getElementById("review-view-3d").addEventListener("click", () => { _setReviewView("3d"); drawReview(); });
document.getElementById("review-view-layer").addEventListener("click", () => { _setReviewView("layer"); drawReview(); });
document.getElementById("review-printer").addEventListener("change", runReview);
document.getElementById("review-ack").addEventListener("change", _updateReviewStart);
document.getElementById("btn-review-recheck").addEventListener("click", runReview);
document.getElementById("btn-review-start").addEventListener("click", startFromReview);
document.getElementById("btn-review-accept").addEventListener("click", acceptWarningsForLater);
document.getElementById("btn-review-close").addEventListener("click", closeReview);
document.getElementById("modal-review").addEventListener("click", (e) => {
  if (e.target.id === "modal-review") closeReview();
});
document.getElementById("btn-review-nozzle-save").addEventListener("click", async (e) => {
  const printerId = _review?.report?.printer_id;
  if (!printerId) return;
  e.currentTarget.disabled = true;
  try {
    await jobsApi("POST", `/api/printers/${encodeURIComponent(printerId)}/hardware`,
      { nozzle_diameter: Number(document.getElementById("review-nozzle-size").value) });
    toast("Nozzle size saved");
    runReview();
  } catch (err) {
    toast(err.message, true);
  }
  e.currentTarget.disabled = false;
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && document.getElementById("modal-review").classList.contains("open")) closeReview();
});
window.addEventListener("resize", () => { if (_review) drawReview(); });
