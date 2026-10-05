/* Spooler – slicing models with OrcaSlicer: upload dialog, locked profiles and slice status.
   Loaded before app.js; uses app.js/jobs.js globals (printers, jobsApi, jobPrinterOptions, toast, …) at call time. */
"use strict";

const SLICE_MODEL_RE    = /\.(stl|3mf|obj|step|stp)$/i;
const SLICE_STATE_LABEL = { queued: "Waiting to slice", slicing: "Slicing…", done: "Sliced", failed: "Slicing failed" };
const SLICE_STATE_CLASS = { queued: "idle", slicing: "warmingup", done: "complete", failed: "offline" };
const SLICE_DONE_VISIBLE_S = 3600; // finished slices stay listed in the queue panel for an hour

let sliceRecords  = [];
let slicerInfo    = null; // GET /api/slicer: availability and locked profiles
let slicerPresets = null; // GET /api/slicer/presets, fetched when locking a new profile
let _sliceFile    = null;

// ─── Data ─────────────────────────────────────────────────────────────────────
async function loadSlicer() {
  try {
    const [info, list] = await Promise.all([
      fetch("/api/slicer").then(r => (r.ok ? r.json() : null)),
      fetch("/api/slices").then(r => (r.ok ? r.json() : null)),
    ]);
    if (info) slicerInfo = info;
    if (list) applySlices(list.slices);
  } catch (_) { /* Spooler may be restarting */ }
}

function applySlices(list) {
  sliceRecords = list || [];
  renderSlices();
}

function uploadFileWithProgress(url, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total * 100); };
    xhr.onload = () => {
      if (xhr.status === 401) { location.replace("/login"); return; }
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (_) {}
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error(data.error || `Upload failed (HTTP ${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error("Upload failed — the connection to Spooler was lost"));
    xhr.send(file);
  });
}

// ─── Slice cards in the queue panel ───────────────────────────────────────────
function renderSlices() {
  const now = Date.now() / 1000;
  const visible = sliceRecords.filter(s => s.state !== "done" || now - (s.finished_at || 0) < SLICE_DONE_VISIBLE_S);
  document.getElementById("slice-body").innerHTML = visible.length
    ? `<section class="queue-section"><h4>Slicing</h4>${visible.map(sliceCard).join("")}</section>`
    : "";
}

function sliceCard(s) {
  const meta = [s.bundle_label, s.bed_type === "smooth" ? "Smooth plate" : "Textured plate"];
  if (s.state === "done") {
    if (s.estimate_s) meta.push(formatTime(s.estimate_s));
    if (s.estimate_g) meta.push(`${Math.round(s.estimate_g)} g`);
    meta.push(s.job_ids.length === 1 ? `queued as job #${s.job_ids[0]}` : `queued as ${s.job_ids.length} jobs`);
  }
  const actions = [];
  if (s.state === "failed") {
    actions.push(`<button class="btn btn-secondary btn-sm" data-slice-action="retry" data-slice-id="${s.id}">Retry</button>`);
  }
  if (s.state === "failed" || s.state === "queued") {
    actions.push(`<button class="btn btn-danger btn-sm" data-slice-action="delete" data-slice-id="${s.id}">Remove</button>`);
  }
  return `<div class="job-card${s.state === "slicing" ? " job-card-active" : ""}"><div class="job-main">
    <div class="job-title-row">
      <span class="job-name" title="${escAttr(escHtml(s.source_name))}">${escHtml(s.source_name)}</span>
      <span class="status-badge ${SLICE_STATE_CLASS[s.state]}">${SLICE_STATE_LABEL[s.state]}</span>
    </div>
    <div class="job-meta">${escHtml(meta.filter(Boolean).join(" · "))}</div>
    ${s.state === "slicing" ? '<div class="job-progress job-progress-busy"><div class="job-progress-bar"></div></div>' : ""}
    ${s.error ? `<div class="job-error">${escHtml(s.error)}</div>` : ""}
    ${actions.length ? `<div class="job-controls">${actions.join("")}</div>` : ""}
  </div></div>`;
}

document.getElementById("slice-body").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-slice-action]");
  if (!btn) return;
  const record = sliceRecords.find(s => s.id === Number(btn.dataset.sliceId));
  if (!record) return;
  try {
    if (btn.dataset.sliceAction === "retry") {
      await jobsApi("POST", `/api/slices/${record.id}/retry`, {});
    } else if (btn.dataset.sliceAction === "delete") {
      if (!confirm(`Remove ${record.source_name}? The uploaded model is deleted.`)) return;
      await jobsApi("DELETE", `/api/slices/${record.id}`);
    }
  } catch (err) {
    toast(err.message, true);
  }
  loadSlicer();
});

// ─── Slice dialog ─────────────────────────────────────────────────────────────
function setSlicePlate(smooth) {
  document.getElementById("slice-plate-textured").className = `btn print-plate-btn ${smooth ? "btn-secondary" : "btn-primary"}`;
  document.getElementById("slice-plate-smooth").className = `btn print-plate-btn ${smooth ? "btn-primary" : "btn-secondary"}`;
  document.getElementById("slice-plate-smooth").dataset.selected = smooth ? "1" : "";
}

function _renderProfileOptions(selectedId) {
  const bundles = slicerInfo?.bundles || [];
  document.getElementById("slice-input-profile").innerHTML = bundles.length
    ? bundles.map(b => `<option value="${escAttr(escHtml(b.id))}"${b.id === selectedId ? " selected" : ""}>${escHtml(b.label)}</option>`).join("")
    : '<option value="">No locked profiles yet</option>';
}

function _refreshSliceAvailability() {
  const available = !!slicerInfo?.available;
  const ready = available && (slicerInfo.bundles || []).length > 0;
  const notice = document.getElementById("slice-unavailable");
  notice.hidden = ready;
  notice.textContent = available
    ? "No locked profile yet — lock one below."
    : "OrcaSlicer isn't set up on the Spooler computer (set ORCA_SLICER_PATH).";
  document.getElementById("btn-slice-new-profile").hidden = !available;
  document.getElementById("btn-slice-save").disabled = !ready;
}

async function openSliceEditor(file) {
  _sliceFile = file;
  document.getElementById("slice-modal-file").textContent = `${file.name} · ${formatFileSize(file.size)}`;
  document.getElementById("slice-profile-form").hidden = true;
  document.getElementById("slice-upload-progress").hidden = true;
  document.getElementById("slice-opt-timelapse").checked = false;
  document.getElementById("slice-opt-leveling").checked = false;
  document.getElementById("slice-input-notes").value = "";
  document.getElementById("slice-input-printer").innerHTML = jobPrinterOptions(_rememberedPrinter());
  setSlicePlate(false);
  document.getElementById("modal-slice").classList.add("open");

  await loadSlicer();
  let remembered = "";
  try { remembered = localStorage.getItem("slice.lastProfile") || ""; } catch (_) {}
  _renderProfileOptions(remembered);
  _refreshSliceAvailability();
}

function closeSliceEditor() {
  document.getElementById("modal-slice").classList.remove("open");
  _sliceFile = null;
}

function _fillPresetChoices() {
  const machine = document.getElementById("slice-preset-machine").value;
  const fill = (id, names, preferred) => {
    document.getElementById(id).innerHTML = (names || [])
      .map(n => `<option${n === preferred ? " selected" : ""}>${escHtml(n)}</option>`).join("");
  };
  fill("slice-preset-process", slicerPresets.processes[machine], slicerPresets.defaults.process);
  fill("slice-preset-filament", slicerPresets.filaments[machine], slicerPresets.defaults.filament);
}

async function toggleProfileForm() {
  const form = document.getElementById("slice-profile-form");
  form.hidden = !form.hidden;
  if (form.hidden) return;
  if (!slicerPresets) {
    try {
      slicerPresets = await jobsApi("GET", "/api/slicer/presets");
    } catch (err) {
      toast(err.message, true);
      form.hidden = true;
      return;
    }
  }
  document.getElementById("slice-preset-machine").innerHTML = slicerPresets.machines
    .map(m => `<option${m === slicerPresets.defaults.machine ? " selected" : ""}>${escHtml(m)}</option>`).join("");
  _fillPresetChoices();
}

async function lockProfile(btn) {
  btn.disabled = true;
  try {
    const bundle = await jobsApi("POST", "/api/slicer/profiles", {
      machine:  document.getElementById("slice-preset-machine").value,
      process:  document.getElementById("slice-preset-process").value,
      filament: document.getElementById("slice-preset-filament").value,
    });
    await loadSlicer();
    _renderProfileOptions(bundle.id);
    _refreshSliceAvailability();
    document.getElementById("slice-profile-form").hidden = true;
    toast(`Locked profile: ${bundle.label}`);
  } catch (err) {
    toast(err.message, true);
  }
  btn.disabled = false;
}

async function saveSliceEditor() {
  if (!_sliceFile) return;
  const save = document.getElementById("btn-slice-save");
  const bundleId = document.getElementById("slice-input-profile").value;
  const printerId = document.getElementById("slice-input-printer").value;
  const params = new URLSearchParams({
    filename:  _sliceFile.name,
    bundle_id: bundleId,
    bed:       document.getElementById("slice-plate-smooth").dataset.selected === "1" ? "smooth" : "textured",
    leveling:  document.getElementById("slice-opt-leveling").checked ? "1" : "0",
    timelapse: document.getElementById("slice-opt-timelapse").checked ? "1" : "0",
    notes:     document.getElementById("slice-input-notes").value,
  });
  if (printerId) params.set("printer_id", printerId);

  save.disabled = true;
  const bar = document.querySelector("#slice-upload-progress .job-upload-bar");
  document.getElementById("slice-upload-progress").hidden = false;
  bar.style.width = "0%";
  try {
    const record = await uploadFileWithProgress(`/api/slices/upload?${params}`, _sliceFile,
      pct => { bar.style.width = `${pct}%`; });
    try {
      localStorage.setItem("slice.lastProfile", bundleId);
      localStorage.setItem("queue.lastPrinter", printerId || "");
    } catch (_) {}
    toast(`Uploaded ${record.source_name} — slicing…`);
    closeSliceEditor();
    loadSlicer();
  } catch (err) {
    toast(err.message, true);
    save.disabled = false;
  }
}

function addModelFile(file) {
  if (!file) return;
  if (!SLICE_MODEL_RE.test(file.name)) { toast("Choose an STL, 3MF, OBJ or STEP model", true); return; }
  openSliceEditor(file);
}

document.getElementById("slice-plate-textured").addEventListener("click", () => setSlicePlate(false));
document.getElementById("slice-plate-smooth").addEventListener("click", () => setSlicePlate(true));
document.getElementById("btn-slice-new-profile").addEventListener("click", toggleProfileForm);
document.getElementById("slice-preset-machine").addEventListener("change", _fillPresetChoices);
document.getElementById("btn-slice-lock-profile").addEventListener("click", (e) => lockProfile(e.currentTarget));
document.getElementById("btn-slice-cancel").addEventListener("click", closeSliceEditor);
document.getElementById("btn-slice-save").addEventListener("click", saveSliceEditor);
document.getElementById("modal-slice").addEventListener("click", (e) => {
  if (e.target.id === "modal-slice") closeSliceEditor();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && document.getElementById("modal-slice").classList.contains("open")) closeSliceEditor();
});

const _modelFileInput = document.getElementById("model-file-input");
document.getElementById("btn-queue-add-model").addEventListener("click", () => _modelFileInput.click());
_modelFileInput.addEventListener("change", () => {
  addModelFile(_modelFileInput.files[0]);
  _modelFileInput.value = "";
});
document.getElementById("btn-queue").addEventListener("click", loadSlicer);
