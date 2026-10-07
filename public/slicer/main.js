/* Spooler project workspace (slicer.html): one page from a model upload to the print queue.
   Model (analysis, units) → Split (cuts, parts) → Plates (parts packed onto plates; move, merge, settings for the
   project, a plate or an object) → Slice (every plate, layer preview) → Queue (send the plates as one batch, assign
   printers). Projects without an uploaded model start at Plates. Talks to /api/projects, /api/splits and /api/jobs. */
import * as orient from "./orient.js?v=2";
import { SettingsPanel } from "./settings.js?v=4";
import { splitStep } from "./split-step.js";
import { projectPage } from "./project-page.js";
import { Viewport, featureColor } from "./viewport.js?v=4";

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clone = v => JSON.parse(JSON.stringify(v));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const listNames = names => names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names.at(-1)}` : names[0] || "";

const STEPS = [["model", "Model"], ["split", "Split"], ["plates", "Plates"], ["slice", "Slice"], ["queue", "Queue"], ["print", "Print"]];
const JOB_STATES = {
  queued: ["Queued", "idle"], starting: ["Starting", "warmingup"], printing: ["Printing", "printing"], completed: ["Printed", "complete"],
  cancelled: ["Cancelled", "cancelled"], failed: ["Failed", "offline"], unknown: ["Outcome unknown", "paused"],
};
const OPEN_JOB_STATES = new Set(["queued", "starting", "printing"]);

const state = {
  project: null,
  plate: 1,
  selected: null,
  step: "plates",
  mode: "prepare",
  settingsTab: "process",
  schema: null,
  bundles: [],
  bundleSettings: new Map(),
  saveTimer: null,
  saving: false,
  pendingSave: false,
  poller: null,
  preview: null,        // { key, analysis, buffer }
  playing: null,        // { frame } while the preview plays the print
  hiddenFeatures: new Set(),
  problems: new Map(),
  importFiles: [],
  jobs: [],             // queue jobs sent from this project
  printers: [],
  jobsTimer: null,
  assigning: null,      // batch id whose printer chooser is open
};

// ── Helpers ─────────────────────────────────────────────────────────────────────
function toast(message, error = false) {
  const el = document.createElement("div");
  el.className = `toast${error ? " toast-error" : ""}`;
  el.textContent = message;
  $("toast-area").appendChild(el);
  setTimeout(() => el.remove(), error ? 6000 : 3500);
}

async function api(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 401) {
    location.replace("/login");
    throw new Error("Signed out");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (HTTP ${response.status})`);
    error.status = response.status;
    throw error;
  }
  return data;
}

async function binary(path) {
  const response = await fetch(path);
  if (response.status === 401) location.replace("/login");
  if (!response.ok) throw new Error(`Couldn't load ${path} (HTTP ${response.status})`);
  return response.arrayBuffer();
}

function upload(url, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = e => { if (e.lengthComputable) onProgress(e.loaded / e.total * 100); };
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

function duration(seconds) {
  if (!seconds) return "";
  const h = Math.floor(seconds / 3600), m = Math.round((seconds % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m} min`;
}

function remember(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch (_) { /* storage blocked */ }
  return null;
}

// What the user last picked lives on the server (printer, profile, spool, plate side, send options), so every
// browser starts a new project from it. Saving is best effort: a failure never interrupts the work.
function rememberDefaults(change) {
  api("PATCH", "/api/defaults", change).catch(() => {});
}

async function applySendDefaults() {
  try {
    const { options } = (await api("GET", "/api/defaults")).defaults;
    $("send-leveling").checked = Boolean(options.leveling);
    $("send-timelapse").checked = Boolean(options.timelapse);
  } catch (_) { /* the checkboxes stay as they are */ }
}

const data = () => state.project.data;
const plateById = id => data().plates.find(p => p.id === id) || null;
const currentPlate = () => plateById(state.plate);
const plateObjects = (plateId = state.plate) => data().objects.filter(o => o.plate === plateId);
const selectedObject = () => data().objects.find(o => o.id === state.selected) || null;
const sliceEntry = (plateId = state.plate) => state.project?.slices?.[String(plateId)] || null;
// A G-code import is a project whose plate has no objects, only an imported Slice: Model, Split, Plates and Slice don't apply.
const isImportedPlate = plateId => !!sliceEntry(plateId)?.imported;
const isGcodeProject = () => data().plates.some(p => isImportedPlate(p.id)) && !data().objects.length;
const platesWithObjects = () => data().plates.filter(p => plateObjects(p.id).length || isImportedPlate(p.id));
// Filament choice: a per-model preset plus colour label, absent meaning the project default (data().filaments["<model id>"])
const filamentChoice = modelId => data().filaments?.[String(modelId)] || null;
const choiceLabel = choice => choice ? `${choice.preset.replace(/ @.*$/, "")} ${choice.colour || ""}`.trim() : "the project default";
// "Plate 3 mixes PETG grey and PLA white" when the plate's objects hold more than one filament choice, else ""
function plateMix(plateId) {
  const seen = new Map();
  for (const o of plateObjects(plateId).sort((a, b) => a.id - b.id)) {
    const choice = filamentChoice(o.model);
    seen.set(choice ? `${choice.preset}\n${choice.colour || ""}` : "", choiceLabel(choice));
  }
  const labels = [...seen.values()];
  return labels.length < 2 ? "" : `${plateById(plateId)?.name || `Plate ${plateId}`} mixes ${labels.slice(0, -1).join(", ")} and ${labels.at(-1)}`;
}
function setFilamentChoice(modelId, choice) {
  const filaments = { ...(data().filaments || {}) };
  if (choice) filaments[String(modelId)] = choice; else delete filaments[String(modelId)];
  if (Object.keys(filaments).length) data().filaments = filaments; else delete data().filaments;
  changed();
  renderObjectPanel();
}
const isUpToDate = plateId =>sliceEntry(plateId)?.state === "sliced" && !sliceEntry(plateId).outdated;
const isBusy = plateId => ["queued", "slicing"].includes(sliceEntry(plateId)?.state);
const plateImporting = plateId => plateObjects(plateId).some(o => state.project.models.find(m => m.id === o.model)?.state === "importing");
const openJobPlates = () => new Set(state.jobs.filter(j => OPEN_JOB_STATES.has(j.state)).map(j => j.plate_id));

// ── Loading ─────────────────────────────────────────────────────────────────────
async function boot() {
  document.querySelectorAll("[data-steps], #prepare-tools").forEach(el => { el.hidden = true; });  // shown once a project is open
  const viewport = $("viewport");
  try {
    state.viewport = new Viewport(viewport, {
      onSelect: id => selectObject(id),
      onFace: face => faceClicked(face),
      onTransform: (id, change) => updateObject(id, change),
      onProblems: problems => { state.problems = problems; renderObjects(); renderSliceState(); },
    });
  } catch (err) {
    showMessage(`The 3D view isn't available in this browser: ${err.message}`);
  }
  const printer = printerPrefs();
  $("printer-shell").value = printer.shell;
  state.viewport?.setShell(printer.shell);
  showPrinter(printer.shown, { save: false });
  state.settings = new SettingsPanel({
    tabs: $("settings-tabs"), pages: $("settings-pages"), body: $("settings-body"), search: $("settings-search"),
    mode: $("settings-mode"), onChange: changeSetting, onReset: resetSettings, onTab: tab => { state.settingsTab = tab; renderSettings(); },
  });
  projectPage.attach({
    api, toast,
    projectId: () => state.project.id,
    sliceState: plateId => sliceEntry(plateId)?.state,
    onSlice: plateId => requestPlateSlice(plateId),
    onOpenPlate: plateId => { selectPlate(plateId); setStep("plates"); },
    hasAssembly: () => !!(state.project.split_id && splitStep.split?.result?.assembly),
    onOpenAssembly: async () => { await setStep("split"); document.querySelector(".split-assembly")?.scrollIntoView({ block: "center" }); },
    onSetPrinter: (plateId, printerId) => {
      const plate = data().plates.find(p => p.id === plateId);
      if (!plate) return;
      plate.printer_id = printerId;
      changed();
    },
  });
  splitStep.attach({
    api, toast,
    onStep: step => setStep(step),
    onLayout: layOutParts,
    onWhole: placeWhole,
    onChanged: () => { renderSteps(); renderStepActions(); },
  });
  $("settings-mode").value = remember("slicer.mode") || (remember("slicer.advanced") === "1" ? "advanced" : "simple");
  $("settings-mode").addEventListener("change", e => remember("slicer.mode", e.target.value));
  bindUi();
  try {
    state.schema = await (await fetch("/orca/settings.json")).json();
    state.settings.setSchema(state.schema);
  } catch (err) {
    toast("Couldn't load the slicer settings definitions", true);
  }
  await loadBundles();
  await loadPresets();
  loadSpools();
  loadPrinters();
  applySendDefaults();
  const params = new URLSearchParams(location.search);
  const asked = Number(params.get("project")) || 0;
  const id = asked || (params.has("new") ? 0 : Number(remember("slicer.project")) || 0);
  if (id) {
    try {
      await openProject(id, { toPrinter: !!params.get("printer"), restoring: !asked });
      return;
    } catch (_) { /* fall through to the project list */ }
  }
  openProjectsDialog();
}

async function loadBundles() {
  try {
    state.bundles = (await api("GET", "/api/slicer")).bundles || [];
  } catch (err) {
    toast(err.message, true);
  }
}

async function loadPresets() {
  try {
    state.presets = await api("GET", "/api/slicer/presets");
  } catch (err) {
    state.presets = null;
    toast(err.message, true);
  }
}

// ── Spools and printers ─────────────────────────────────────────────────────────
// A Spoolman spool sets these filament settings; clearing the spool puts them back to the preset's
const SPOOL_KEYS = ["filament_density", "filament_diameter", "nozzle_temperature", "nozzle_temperature_initial_layer", "textured_plate_temp",
  "textured_plate_temp_initial_layer", "hot_plate_temp", "hot_plate_temp_initial_layer", "filament_cost"];

async function loadSpools() {
  try {
    const result = await api("GET", "/api/slicer/spools");
    state.spools = result.spools;
    state.spoolError = null;
  } catch (err) {
    state.spools = state.spools || [];
    state.spoolError = err.message;
  }
  if (state.project) renderFarm();
}

const spoolLabel = s => `${s.name}${s.remaining_weight != null ? ` · ${Math.round(s.remaining_weight)} g` : ""}${s.location ? ` · on ${s.location}` : ""}`;

function projectNeedGrams() {
  return platesWithObjects().reduce((sum, p) => sum + (sliceEntry(p.id)?.estimate_g || 0), 0);
}

function renderFarm() {
  if (!state.project) return;
  const chosen = data().profile.spool_id;
  const spools = state.spools || [];
  const select = $("spool-select");
  if (state.spoolError && !spools.length) {
    select.innerHTML = '<option value="">Spoolman isn\'t reachable</option>';
    select.title = state.spoolError;
  } else {
    const known = spools.some(s => s.id === chosen);
    select.innerHTML = '<option value="">No spool — use the preset\'s values</option>'
      + spools.map(s => `<option value="${s.id}"${s.id === chosen ? " selected" : ""}>${esc(spoolLabel(s))}</option>`).join("")
      + (chosen && !known ? `<option value="${chosen}" selected>Spool #${chosen}</option>` : "");
  }
  const note = $("spool-note");
  const spool = spools.find(s => s.id === chosen);
  const need = projectNeedGrams();
  note.hidden = !spool;
  if (spool) {
    const short = need && spool.remaining_weight != null && spool.remaining_weight < need * 1.05;
    note.className = `slicer-muted${short ? " warn" : ""}`;
    note.textContent = `${Math.round(spool.remaining_weight ?? 0)} g left on this spool` + (need ? ` · the sliced plates need about ${Math.round(need)} g${short ? " — not enough" : ""}` : "");
  }

  const plate = currentPlate();
  const printer = plate?.printer_id || "";
  $("plate-printer-select").innerHTML = '<option value="">Any printer</option>'
    + state.printers.map(p => `<option value="${esc(p.id)}"${p.id === printer ? " selected" : ""}>${esc(p.name)}${p.connected ? "" : " (offline)"}</option>`).join("");
  const name = state.printers.find(p => p.id === printer)?.name;
  const button = $("btn-slice-send");
  button.textContent = name ? `Slice & send to ${name}` : "Slice & send";
  button.disabled = !printer || !plateObjects().length || !data().profile.bundle_id || isBusy(state.plate) || plateImporting(state.plate);
  refreshFit();
}

// Warnings about the plate's printer (offline, wrong nozzle, other material, too little filament); asked for when they may have changed
function refreshFit() {
  const plate = currentPlate();
  const box = $("plate-fit");
  if (!plate?.printer_id) { box.innerHTML = ""; state.fitKey = null; return; }
  const entry = sliceEntry(plate.id);
  const key = [state.project.id, plate.id, plate.printer_id, data().profile.bundle_id, data().profile.spool_id, entry?.state, entry?.estimate_g].join("|");
  if (key === state.fitKey) return;
  state.fitKey = key;
  api("GET", `/api/projects/${state.project.id}/plates/${plate.id}/fit?printer_id=${encodeURIComponent(plate.printer_id)}`).then(fit => {
    if (state.fitKey !== key) return;
    box.innerHTML = fit.warnings.map(w => `<div class="fit ${esc(w.level)}">${esc(w.text)}</div>`).join("");
  }).catch(() => { box.innerHTML = ""; });
}

async function chooseSpool(value) {
  if (!state.project) return;
  const id = value ? Number(value) : null;
  const profile = data().profile;
  if (!id) {
    for (const key of SPOOL_KEYS) delete profile.filament[key];
    profile.spool_id = null;
    rememberDefaults({ spool: null });
    changed();
    renderAll();
    return;
  }
  try {
    let match = await api("POST", "/api/slicer/spool-match", { spool_id: id, bundle_id: profile.bundle_id, bed: data().bed });
    if (match.preset && match.preset !== match.current_preset) {
      await choosePreset("filament", match.preset);
      if (currentPresets()?.filament !== match.preset) { renderFarm(); return; }   // switching was declined or failed
      match = await api("POST", "/api/slicer/spool-match", { spool_id: id, bundle_id: data().profile.bundle_id, bed: data().bed });
    }
    for (const key of SPOOL_KEYS) delete data().profile.filament[key];
    Object.assign(data().profile.filament, match.changes);
    data().profile.spool_id = id;
    rememberDefaults({ spool: id, profile: data().profile.bundle_id });
    changed();
    renderAll();
    toast(`Using ${match.spool.name}` + (match.preset ? "" : " — no stock preset for this material, so the preset's other settings are kept"));
  } catch (err) {
    toast(err.message, true);
    renderFarm();
  }
}

async function requestPlateSlice(plateId) {
  await flushSave();
  applyProject(await api("POST", `/api/projects/${state.project.id}/plates/${plateId}/slice`, {}));
  renderAll({ geometry: false });
  schedulePoll();
  toast("Slicing…");
}

async function sliceAndSend(plateIds) {
  await flushSave();
  const url = plateIds ? `/api/projects/${state.project.id}/plates/${plateIds[0]}/slice` : `/api/projects/${state.project.id}/slice`;
  const project = await api("POST", url, { send: true });
  applyProject(project);
  renderAll({ geometry: false });
  schedulePoll();
  toast("Slicing — it will go to the queue when it's done");
}

async function bundleSettings(bundleId) {
  if (!bundleId) return null;
  if (!state.bundleSettings.has(bundleId)) state.bundleSettings.set(bundleId, await api("GET", `/api/slicer/profiles/${bundleId}/settings`));
  return state.bundleSettings.get(bundleId);
}

async function openProject(id, { toPrinter = false, restoring = false } = {}) {
  const project = await api("GET", `/api/projects/${id}`);
  if (restoring && project.quick) throw new Error("A hidden quick project is never reopened on its own");   // the caller falls back to the list
  clearTimeout(state.jobsTimer);
  state.clockSkew = project.server_time - Date.now() / 1000;
  state.project = project;
  state.plate = project.data.plates[0].id;
  state.selected = null;
  state.preview = null;
  state.viewport?.clearGeometries();   // model ids restart at 1 in every project: never reuse another project's shapes
  state.loadedModels = new Map();
  resetEdits();
  state.jobs = [];
  state.assigning = null;
  if (!project.quick) remember("slicer.project", String(id));   // a hidden quick project is never restored as the last-open project
  history.replaceState(null, "", `?project=${id}`);
  await bundleSettings(project.data.profile.bundle_id).catch(err => toast(err.message, true));
  if (project.split_id) await splitStep.open(project.split_id).catch(err => toast(err.message, true));
  else splitStep.close();
  await loadJobs({ quiet: true });
  renderAll();
  await loadGeometry();
  state.viewport?.fit(state.viewport.objectsBox());
  const saved = remember(`slicer.step.${id}`);
  // A push deep link (?printer=) lands on the Print step, where that printer's card is.
  const landing = toPrinter && stepAvailable("print") ? "print" : saved && stepAvailable(saved) ? saved : defaultStep();
  await setStep(landing, { force: true });
  schedulePoll();
}

function applyProject(project, { keepLocalData = false } = {}) {
  const local = state.project;
  if (keepLocalData && local) {
    state.project = { ...project, data: local.data, revision: local.revision };
  } else {
    state.project = project;
  }
  if (!data().plates.some(p => p.id === state.plate)) state.plate = data().plates[0].id;
  if (state.selected && !data().objects.some(o => o.id === state.selected)) state.selected = null;
}

async function loadGeometry() {
  if (!state.viewport) return;
  state.loadedModels = state.loadedModels || new Map();
  for (const model of state.project.models) {   // a model id can come back with different contents after a delete
    if (state.loadedModels.has(model.id) && state.loadedModels.get(model.id) !== model.sha256) {
      state.viewport.removeGeometry(model.id);
      state.loadedModels.delete(model.id);
    }
  }
  const ready = state.project.models.filter(m => m.state === "ready" && !state.viewport.hasGeometry(m.id));
  for (const model of ready) {
    try {
      state.viewport.addModelGeometry(model.id, await binary(`/api/projects/${state.project.id}/models/${model.id}/preview`));
      state.loadedModels.set(model.id, model.sha256);
    } catch (err) {
      toast(err.message, true);
    }
  }
  renderViewport();
}

// ── Steps ───────────────────────────────────────────────────────────────────────
function stepAvailable(step) {
  const project = state.project;
  if (!project) return false;
  const split = project.split_id ? splitStep.split : null;
  if (isGcodeProject() && step !== "queue") return false;
  switch (step) {
    case "model":  return !!project.split_id;
    case "split":  return !!split?.analysis;
    case "plates": return !project.split_id || data().objects.length > 0;
    case "slice":  return data().objects.length > 0;
    case "queue":  return platesWithObjects().some(p => isUpToDate(p.id)) || state.jobs.length > 0;
    case "print":  return Object.values(project.slices).some(s => s.state === "sliced") || state.jobs.length > 0;
  }
  return false;
}

function stepDone(step) {
  const split = splitStep.split;
  switch (step) {
    case "model":  return !!split?.analysis;
    case "split":  return !!split?.result || data().objects.some(o => state.project.models.find(m => m.id === o.model)?.split?.part === 0);
    case "plates": return data().objects.length > 0;
    case "slice":  return platesWithObjects().length > 0 && platesWithObjects().every(p => isUpToDate(p.id));
    case "queue":  return state.jobs.length > 0;
  }
  return false;   // Print is never "done": the project page shows how far along it is
}

function defaultStep() {
  if (isGcodeProject()) return "queue";
  if (state.project.split_id && !data().objects.length) return splitStep.split?.result ? "split" : "model";
  if (!data().objects.length) return "plates";
  if (state.jobs.some(j => OPEN_JOB_STATES.has(j.state))) return "queue";
  return platesWithObjects().every(p => isUpToDate(p.id)) ? "slice" : "plates";
}

async function setStep(step, { force = false } = {}) {
  if (!state.project || (!force && !stepAvailable(step))) return;
  if (!stepAvailable(step)) step = isGcodeProject() ? "queue" : stepAvailable("plates") ? "plates" : "model";
  await flushSave();
  if (state.step !== step) document.querySelector(".slicer-sidebar").scrollTop = 0;
  state.step = step;
  remember(`slicer.step.${state.project.id}`, step);
  const splitView = step === "model" || step === "split";
  const pageView = step === "print";
  document.body.classList.toggle("print-step", pageView);
  $("project-page").hidden = !pageView;
  $("split-stage").hidden = !splitView;
  $("split-side").hidden = !splitView;
  $("viewport").hidden = splitView || pageView;
  $("plate-tabs").hidden = splitView || pageView;
  if (pageView) projectPage.show();
  else projectPage.hide();
  document.querySelectorAll("[data-steps]").forEach(el => { el.hidden = !el.dataset.steps.split(" ").includes(step); });
  if (splitView) splitStep.show(step);
  else splitStep.hide();
  if (step !== "plates") selectObject(null);
  clearTimeout(state.jobsTimer);
  if (step === "queue") {
    loadPrinters();
    loadJobs();
  }
  if (!splitView && !pageView) {
    state.viewport?.resize();
    await setMode(step === "slice" && sliceEntry()?.state === "sliced" ? "preview" : "prepare");
  }
  renderAll({ geometry: !splitView && !pageView });
}

function renderSteps() {
  if (!state.project) { $("step-bar").innerHTML = ""; return; }
  const steps = STEPS.filter(([id]) => isGcodeProject() ? id === "queue" : state.project.split_id || (id !== "model" && id !== "split"));
  $("step-bar").innerHTML = steps.map(([id, label], index) => {
    const skipped = id === "split" && splitSkipped();   // a model that fits the bed skips Split, but the step stays openable
    return `${index ? '<span class="slicer-step-sep"></span>' : ""}
    <button class="slicer-step${id === state.step ? " active" : ""}${skipped ? " skipped" : stepDone(id) ? " done" : ""}" type="button" data-step="${id}"
      ${stepAvailable(id) ? "" : " disabled"}${skipped ? ' title="This model fits the bed, so it was put on a plate whole. Open Split to cut it anyway."' : ""}><span class="num">${skipped ? "–" : stepDone(id) && id !== state.step ? "✓" : index + 1}</span>${label}${skipped ? "<small> not needed</small>" : ""}</button>`;
  }).join("");
}

function renderStepActions() {
  const el = $("step-actions");
  if (!state.project) { el.innerHTML = ""; return; }
  const next = (step, label) => `<button class="btn btn-primary" type="button" data-go="${step}"${stepAvailable(step) ? "" : " disabled"}>${label}</button>`;
  el.innerHTML = state.step === "plates" ? next("slice", "Slice →") : state.step === "slice" ? next("queue", "Send to the queue →") : "";
}

// ── Saving ──────────────────────────────────────────────────────────────────────
// Undo and redo keep snapshots of the project's data (objects, plates, profile changes). Rapid edits of the same kind
// (typing in a setting) share one step.
const editLog = { undo: [], redo: [], current: null, at: 0 };
const EDIT_LIMIT = 60;

function resetEdits() {
  editLog.undo = [];
  editLog.redo = [];
  editLog.current = state.project ? JSON.stringify(data()) : null;
  renderTools();
}

function recordEdit({ merge = false } = {}) {
  if (!state.project) return;
  const now = JSON.stringify(data());
  if (now === editLog.current) return;
  const together = merge && editLog.undo.length && Date.now() - editLog.at < 800;
  if (!together && editLog.current) editLog.undo.push(editLog.current);
  if (editLog.undo.length > EDIT_LIMIT) editLog.undo.shift();
  editLog.redo = [];
  editLog.current = now;
  editLog.at = Date.now();
}

function restoreEdit(snapshot) {
  const restored = JSON.parse(snapshot);
  const models = new Set(state.project.models.map(m => m.id));
  if (restored.objects.some(o => !models.has(o.model))) {
    toast("That step used a model that has since been removed", true);
    return false;
  }
  state.project.data = restored;
  editLog.current = snapshot;
  if (!data().plates.some(p => p.id === state.plate)) state.plate = data().plates[0].id;
  if (state.selected && !data().objects.some(o => o.id === state.selected)) state.selected = null;
  changed({ immediate: true });
  renderAll();
  return true;
}

function undo() {
  while (editLog.undo.length) {
    const before = editLog.current;
    if (restoreEdit(editLog.undo.pop())) { editLog.redo.push(before); renderTools(); return; }
  }
}

function redo() {
  while (editLog.redo.length) {
    const before = editLog.current;
    if (restoreEdit(editLog.redo.pop())) { editLog.undo.push(before); renderTools(); return; }
  }
}

function changed({ immediate = false, merge = false } = {}) {
  recordEdit({ merge });
  $("save-state").textContent = "Unsaved changes";
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(save, immediate ? 0 : 600);
  renderSliceState();
  renderPlates();
  renderTools();
}

async function save() {
  state.saveTimer = null;
  if (state.saving) { state.pendingSave = true; return; }
  state.saving = true;
  $("save-state").textContent = "Saving…";
  const sent = clone(data());
  try {
    const project = await api("PATCH", `/api/projects/${state.project.id}`, { data: sent, revision: state.project.revision });
    const localChangedMeanwhile = JSON.stringify(sent) !== JSON.stringify(data());
    applyProject(project, { keepLocalData: localChangedMeanwhile });
    if (localChangedMeanwhile) state.project.revision = project.revision;
    $("save-state").textContent = "Saved";
  } catch (err) {
    $("save-state").textContent = "Not saved";
    toast(err.message, true);
    if (err.status === 409 || err.status === 400) {
      state.project = await api("GET", `/api/projects/${state.project.id}`);
      renderAll();
    }
  } finally {
    state.saving = false;
    if (state.pendingSave) {
      state.pendingSave = false;
      await save();
    } else {
      renderAll({ geometry: false });
    }
  }
}

async function flushSave() {
  if (!state.project) return;
  if (state.saveTimer) {
    clearTimeout(state.saveTimer);
    await save();
  }
  while (state.saving) await new Promise(resolve => setTimeout(resolve, 50));
}

// The elapsed time of a running slice counts up between polls
setInterval(() => {
  if (!state.project || !Object.values(state.project.slices).some(s => s.state === "slicing")) return;
  renderSliceState();
  renderSlicing();
}, 1000);

function schedulePoll() {
  clearTimeout(state.poller);
  const busy = state.project && (state.project.models.some(m => m.state === "importing")
    || Object.values(state.project.slices).some(s => s.state === "queued" || s.state === "slicing"
      || (s.state === "sliced" && s.auto_send && !s.sent_job && !s.send_error)));
  if (!busy) return;
  state.poller = setTimeout(async () => {
    try {
      const fresh = await api("GET", `/api/projects/${state.project.id}`);
      state.clockSkew = fresh.server_time - Date.now() / 1000;
      const before = state.project;
      const newModels = fresh.models.filter(m => m.state === "ready" && before.models.find(b => b.id === m.id)?.state !== "ready");
      const sliceChanged = JSON.stringify(fresh.slices) !== JSON.stringify(before.slices);
      state.project = { ...before, models: fresh.models, slices: fresh.slices };
      if (!state.saving && !state.saveTimer && fresh.revision !== before.revision) applyProject(fresh);
      if (newModels.length) {
        await loadGeometry();
        // placeholders were fitted while importing; the real parts are usually far bigger
        const onThisPlate = newModels.some(m => plateObjects().some(o => o.model === m.id));
        if (onThisPlate && state.mode === "prepare" && (state.step === "plates" || state.step === "slice")) state.viewport?.fit(state.viewport.objectsBox());
      }
      if (sliceChanged) {
        const entry = sliceEntry(), was = before.slices[String(state.plate)];
        if (state.step === "slice" && entry?.state === "sliced" && !entry.outdated && was?.state !== "sliced") setMode("preview");
        for (const [plateId, now] of Object.entries(fresh.slices)) {
          const then = before.slices[plateId];
          if (now.state === "failed" && then?.state !== "failed") toast(`${plateById(Number(plateId))?.name || "A plate"}: slicing failed — ${now.error}`, true);
        }
        const plates = platesWithObjects();
        if (plates.length > 1 && plates.every(p => isUpToDate(p.id)) && Object.values(before.slices).some(s => s.state === "queued" || s.state === "slicing")) {
          toast(`All ${plates.length} plates are sliced`);
        }
      }
      renderAll({ geometry: false });
    } catch (_) { /* Spooler may be restarting */ }
    schedulePoll();
  }, 1500);
}

// ── Rendering ───────────────────────────────────────────────────────────────────
function renderAll({ geometry = true } = {}) {
  $("empty-state").hidden = !!state.project;
  if (!state.project) return;
  if (document.activeElement !== $("project-name")) $("project-name").value = state.project.name;
  $("project-name").title = state.project.name;
  document.title = `${state.project.name} – Spooler`;
  renderSteps();
  renderStepActions();
  renderProfile();
  renderPlates();
  renderObjects();
  renderSettings();
  renderSliceState();
  renderSlicing();
  renderBricklayers();
  renderSend();
  renderJobs();
  if (geometry) renderViewport();
  renderTools();
  renderObjectPanel();
}

const PRESET_KINDS = ["machine", "filament", "process"];
const PRESET_LISTS = { machine: "machines", filament: "filaments", process: "processes" };

// The presets a project is using, read from its locked profile
function currentPresets() {
  const bundle = state.bundles.find(b => b.id === data().profile.bundle_id);
  return bundle ? Object.fromEntries(PRESET_KINDS.map(kind => [kind, bundle.presets[kind].name])) : null;
}

function presetChanges(kind) {
  return kind === "machine" ? 0 : Object.keys(data().profile[kind] || {}).length;
}

function renderProfile() {
  const names = currentPresets();
  const presets = state.presets;
  for (const kind of PRESET_KINDS) {
    const select = $(`preset-${kind}-select`);
    const current = names?.[kind];
    const machine = names?.machine;
    let list = !presets ? [] : kind === "machine" ? presets.machines : presets[PRESET_LISTS[kind]][machine] || [];
    if (current && !list.includes(current)) list = [current, ...list];
    const dirty = presetChanges(kind) > 0;
    const user = new Set(presets?.user?.[kind] || []);
    select.innerHTML = list.length
      ? list.map(name => `<option value="${esc(name)}"${name === current ? " selected" : ""}>${esc(name.replace(/ @.*$/, ""))}${user.has(name) ? " (mine)" : ""}${name === current && dirty ? " *" : ""}</option>`).join("")
      : '<option value="">No presets available</option>';
    select.title = current || "";
    if (kind !== "machine") {
      $(`btn-save-${kind}`).hidden = !dirty;
      $(`btn-delete-${kind}`).hidden = !user.has(current);
    }
  }
  $("bed-select").value = data().bed;
  renderFarm();
}

// Pick another preset: the choice is locked in as a new profile; the other kinds keep their changes
async function choosePreset(kind, name) {
  const names = currentPresets();
  if (!names || names[kind] === name) return renderProfile();
  if (presetChanges(kind) && !confirm(`Switching ${kind} presets discards your unsaved ${kind} changes. Continue?`)) return renderProfile();
  const next = { ...names, [kind]: name };
  if (kind === "machine") {  // the process and filament must suit the new printer
    const fits = key => state.presets[PRESET_LISTS[key]][name] || [];
    for (const key of ["process", "filament"]) if (!fits(key).includes(next[key])) next[key] = fits(key)[0] || next[key];
  }
  try {
    const bundle = await api("POST", "/api/slicer/profiles", next);
    await loadBundles();
    await bundleSettings(bundle.id);
    const profile = data().profile;
    data().profile = { ...profile, bundle_id: bundle.id, [kind]: {}, ...(kind === "machine" ? { process: {}, filament: {} } : {}) };
    rememberDefaults({ profile: bundle.id });
    changed();
    renderAll();
    toast(`Using ${bundle.label}`);
  } catch (err) {
    toast(err.message, true);
    renderProfile();
  }
}

function openSavePreset(kind) {
  const names = currentPresets();
  state.savingPreset = kind;
  $("save-profile-title").textContent = `Save ${kind} preset`;
  $("save-profile-summary").textContent = `${plural(presetChanges(kind), "setting")} changed from “${names[kind]}”.`;
  $("save-profile-name").value = state.presets?.user?.[kind]?.includes(names[kind]) ? names[kind] : "";
  $("dlg-save-profile").showModal();
}

async function savePreset() {
  const kind = state.savingPreset;
  const names = currentPresets();
  try {
    const saved = await api("POST", "/api/slicer/presets", { kind, name: $("save-profile-name").value, base: names[kind], changes: data().profile[kind] });
    await loadPresets();
    const bundle = await api("POST", "/api/slicer/profiles", { ...names, [kind]: saved.preset });
    await loadBundles();
    await bundleSettings(bundle.id);
    data().profile = { ...data().profile, bundle_id: bundle.id, [kind]: {} };
    rememberDefaults({ profile: bundle.id });
    $("dlg-save-profile").close();
    changed();
    renderAll();
    toast(`Saved ${kind} preset: ${saved.preset}`);
  } catch (err) {
    toast(err.message, true);
  }
}

async function deletePreset(kind) {
  const names = currentPresets();
  if (!confirm(`Delete the saved ${kind} preset “${names[kind]}”? Projects already using it keep working.`)) return;
  try {
    await api("DELETE", `/api/slicer/presets/${kind}/${encodeURIComponent(names[kind])}`);
    await loadPresets();
    renderProfile();
    toast(`Deleted ${names[kind]}`);
  } catch (err) {
    toast(err.message, true);
  }
}

async function importPreset(file) {
  try {
    const raw = JSON.parse(await file.text());
    const result = await api("POST", "/api/slicer/presets/import", { preset: raw, name: raw.name });
    await loadPresets();
    renderProfile();
    toast(`Imported ${result.type} preset “${result.preset}”` + (result.dropped.length ? ` — ${plural(result.dropped.length, "setting")} not supported here were left out` : ""));
  } catch (err) {
    toast(err instanceof SyntaxError ? "That file isn't valid JSON" : err.message, true);
  }
}

function plateDot(plateId) {
  const entry = sliceEntry(plateId);
  return !entry ? "" : entry.state === "sliced" ? (entry.outdated ? "outdated" : "sliced") : entry.state === "failed" ? "failed" : "busy";
}

const STAGE_TEXT = { preparing: "Preparing the plate", slicing: "Slicing with OrcaSlicer", "post-processing": "Interlocking walls (BrickLayers)",
  analysing: "Checking the G-code" };
const clock = seconds => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;

// "Slicing with OrcaSlicer · 0:42 of about 1:20" — OrcaSlicer's command line reports no percentage, so this is elapsed time
// against how long the plate took last time
function slicingText(entry) {
  const now = Date.now() / 1000 + (state.clockSkew || 0);
  const elapsed = Math.max(0, now - (entry.started_at || now));
  const usual = entry.last_duration_ms ? ` of about ${clock(entry.last_duration_ms / 1000)}` : "";
  return `${STAGE_TEXT[entry.stage] || "Slicing"} · ${clock(elapsed)}${usual}`;
}

function plateStatus(plateId) {
  const entry = sliceEntry(plateId);
  if (!plateObjects(plateId).length) return { text: "Empty", cls: "" };
  if (plateImporting(plateId)) return { text: "Importing models…", cls: "" };
  if (!entry) return { text: "Not sliced yet", cls: "" };
  if (entry.state === "queued") return { text: "Waiting to slice…", cls: "" };
  if (entry.state === "slicing") return { text: slicingText(entry), cls: "" };
  if (entry.state === "failed") return { text: `Failed: ${entry.error}`, cls: "bad" };
  if (entry.outdated) return { text: "Changed since slicing — slice again", cls: "warn" };
  const text = [duration(entry.estimate_s), entry.estimate_g ? `${Math.round(entry.estimate_g)} g` : "", plural(entry.layers, "layer")].filter(Boolean).join(" · ");
  const printer = state.printers.find(p => p.id === entry.sent_to)?.name;
  if (entry.send_error) return { text: `${text} · not sent: ${entry.send_error}`, cls: "warn" };
  if (entry.sent_job) return { text: `${text} · in the queue${printer ? ` for ${printer}` : ""}`, cls: "good" };
  return { text: entry.auto_send ? `${text} · sending…` : text, cls: "good" };
}

function plateBadge(plate) {
  const keys = Object.keys(plate.overrides || {});
  const bricks = plateBricks(plate) ? '<span class="slicer-badge" title="BrickLayers interlocking walls are on for this plate">bricks</span>' : "";
  if (!keys.length) return bricks;
  const label = plate.overrides.enable_support === "1" ? "supports" : plural(keys.length, "setting");
  return `<span class="slicer-badge" title="Settings of its own: ${esc(keys.join(", "))}">${label}</span>${bricks}`;
}

// ── BrickLayers ─────────────────────────────────────────────────────────────────
// The project's setting lives in profile.bricklayers; a plate's own setting (or null to follow the project) in plate.bricklayers.
const BRICK_DEFAULTS = { enabled: false, extrusion_multiplier: 1.05, start_at_layer: 3 };

function projectBricks() {
  return { ...BRICK_DEFAULTS, ...(data().profile.bricklayers || {}) };
}

function plateBricks(plate) {
  const setting = plate?.bricklayers ?? data().profile.bricklayers;
  return setting?.enabled ? { ...BRICK_DEFAULTS, ...setting } : null;
}

function renderBricklayers() {
  if (!state.project || state.step !== "slice") return;
  const project = projectBricks();
  const plate = currentPlate();
  const mode = !plate || plate.bricklayers == null ? "project" : plate.bricklayers.enabled ? "on" : "off";
  $("bricklayers").innerHTML = `
    <p class="slicer-muted">Raises every other inner wall loop by half a layer so walls interlock like bricks — stronger across layers.
      While it's on, arc fitting is turned off and walls are classic, printed inner then outer.</p>
    <label class="slicer-check"><input type="checkbox" data-brick="enabled"${project.enabled ? " checked" : ""} /> On for the project</label>
    <div class="slicer-brick-numbers">
      <label class="slicer-field">Extrusion multiplier
        <input type="number" data-brick="extrusion_multiplier" min="0.8" max="1.5" step="0.01" value="${project.extrusion_multiplier}" /></label>
      <label class="slicer-field">Start at layer
        <input type="number" data-brick="start_at_layer" min="1" max="1000" step="1" value="${project.start_at_layer}" /></label>
    </div>
    ${plate ? `<label class="slicer-field">${esc(plate.name)}
      <select data-brick-plate>
        <option value="project"${mode === "project" ? " selected" : ""}>Use the project's setting (${project.enabled ? "on" : "off"})</option>
        <option value="on"${mode === "on" ? " selected" : ""}>On for this plate</option>
        <option value="off"${mode === "off" ? " selected" : ""}>Off for this plate</option>
      </select></label>` : ""}`;
}

function changeBricklayers(target) {
  if (target.matches("[data-brick-plate]")) {
    const plate = currentPlate();
    if (!plate) return;
    plate.bricklayers = target.value === "project" ? null : { ...projectBricks(), enabled: target.value === "on" };
  } else {
    const key = target.dataset.brick;
    const value = key === "enabled" ? target.checked : Number(target.value);
    if (key !== "enabled" && !Number.isFinite(value)) { renderBricklayers(); return; }
    data().profile.bricklayers = { ...projectBricks(), [key]: value };
    // plates with their own setting keep their on/off choice but follow the project's numbers
    data().plates.forEach(p => { if (p.bricklayers && key !== "enabled") p.bricklayers = { ...p.bricklayers, [key]: value }; });
  }
  changed();
  renderAll({ geometry: false });
}

function renderPlates() {
  const tabs = data().plates.map(plate => {
    const count = plateObjects(plate.id).length;
    return `<button class="slicer-plate-tab${plate.id === state.plate ? " active" : ""}" type="button" data-plate="${plate.id}"
      title="${esc(plate.name)} · ${plural(count, "object")}${plateMix(plate.id) ? ` · ${esc(plateMix(plate.id))}` : ""}"><span class="dot ${plateDot(plate.id)}"></span>${esc(plate.name)} <small>${count}</small>${plateBadge(plate)}</button>`;
  });
  if (state.step === "plates") {
    tabs.push('<button class="slicer-plate-tab" type="button" data-plate="new" title="Add a plate">+ Plate</button>');
    if (data().plates.length > 1) tabs.push('<button class="slicer-plate-tab" type="button" data-plate="remove" title="Remove this plate (only when empty)">− Remove plate</button>');
  }
  $("plate-tabs").innerHTML = tabs.join("");
}

function shortReason(error) {
  const text = String(error || "the file couldn't be read").replace(/\s+/g, " ").trim();
  return text.length > 60 ? `${text.slice(0, 57)}…` : text;
}

function renderObjects() {
  if (!state.project) return;
  const models = state.project.models;
  const rows = plateObjects().map(object => {
    const model = models.find(m => m.id === object.model);
    const issues = state.problems.get(object.id)?.issues || [];
    const status = model?.state === "importing" ? '<small>importing…</small>'
      : model?.state === "failed" ? `<small class="bad" title="${esc(model.error || "")}">Couldn't import — ${esc(shortReason(model.error))}</small>`
      : issues.length ? `<small class="bad">${esc(issues.join(", "))}</small>`
      : `<small>${(model?.size || []).map(v => Math.round(v * (object.scale[0] || 1))).join(" × ")} mm</small>`;
    const overrides = Object.keys(object.overrides || {}).length;
    return `<div class="slicer-object${object.id === state.selected ? " selected" : ""}${model?.state === "failed" ? " failed" : ""}" data-object="${object.id}">
      <div class="slicer-object-name" title="${esc(object.name)}">${esc(object.name)}<br>${status}</div>
      ${model?.state === "failed" ? `<button class="btn btn-secondary btn-sm slicer-retry-btn" type="button" data-retry-model="${model.id}" title="Try importing this model again">Retry</button>`
        : overrides ? `<span class="slicer-badge" title="Settings changed for this object">${overrides}</span>` : "<span></span>"}
      <button class="slicer-icon-btn" type="button" data-remove-object="${object.id}" title="Remove from the plate">✕</button>
    </div>`;
  });
  $("object-list").innerHTML = rows.join("") || '<p class="slicer-muted">No objects on this plate. Add a model or drop STL files on the bed.</p>';
}

function effectiveProcess(base) {
  return { ...(base?.process || {}), ...data().profile.process };
}

function renderSettings() {
  if (!state.project || !state.schema) return;
  const base = state.bundleSettings.get(data().profile.bundle_id);
  const plate = currentPlate();
  const object = state.step === "plates" ? selectedObject() : null;
  if (state.settingsTab === "object" && !object) state.settingsTab = "process";
  const objectsScope = state.settingsTab === "object";
  document.querySelectorAll("[data-scope-tab]").forEach(btn => {
    btn.classList.toggle("active", (btn.dataset.scopeTab === "objects") === objectsScope);
    btn.title = btn.dataset.scopeTab === "objects" ? (object ? `Settings for ${object.name} only` : "Select an object on the plate first")
      : "Settings for the whole project or a plate";
  });
  const tabs = objectsScope ? [{ id: "object", label: object.name }]
    : [{ id: "process", label: "Process" }, { id: "filament", label: "Filament" }, { id: "plate", label: plate.name }];
  renderScope();
  if (!base) {
    $("settings-body").innerHTML = '<p class="set-note">Choose or lock a profile to see its settings.</p>';
    $("settings-tabs").innerHTML = "";
    $("settings-pages").innerHTML = "";
    return;
  }
  if (state.settingsTab === "object") {
    const home = plateById(object.plate);
    state.settings.show({ tab: "object", tabs, layout: "process", base: { ...effectiveProcess(base), ...(home?.overrides || {}) },
      changes: object.overrides || {}, target: { kind: "object", id: object.id },
      note: `Changes here apply only to ${object.name}, on top of ${home?.name || "its plate"}'s settings.` });
  } else if (state.settingsTab === "plate") {
    state.settings.show({ tab: "plate", tabs, layout: "process", base: effectiveProcess(base), changes: plate.overrides || {},
      target: { kind: "plate", id: plate.id }, note: `Changes here apply only to ${plate.name}, on top of the project's settings.` });
  } else {
    const kind = state.settingsTab;
    const own = data().plates.filter(p => Object.keys(p.overrides || {}).length);
    const note = kind !== "process" ? "Filament settings apply to every plate."
      : own.length ? `Changes here apply to every plate. ${listNames(own.map(p => p.name))} ${own.length === 1 ? "has settings of its" : "have settings of their"} own on top.`
      : "Changes here apply to every plate.";
    state.settings.show({ tab: kind, tabs, layout: kind, base: base[kind], changes: data().profile[kind], target: { kind }, note });
  }
}

function renderScope() {
  const el = $("settings-scope");
  const plate = currentPlate();
  if (state.settingsTab !== "plate" || !plate) { el.hidden = true; el.innerHTML = ""; return; }
  const count = Object.keys(plate.overrides || {}).length;
  el.hidden = false;
  el.innerHTML = `<button class="btn btn-secondary btn-sm" type="button" data-scope="apply-all"${data().plates.length > 1 ? "" : " disabled"}
      title="Give every plate exactly these plate settings">Use on all plates</button>
    <button class="btn btn-secondary btn-sm" type="button" data-scope="clear"${count ? "" : " disabled"}>Clear ${esc(plate.name)}'s settings</button>`;
}

function renderSliceState() {
  if (!state.project) return;
  const el = $("slice-state");
  if (state.step !== "plates" && state.step !== "slice") { el.textContent = ""; return; }
  const status = plateStatus(state.plate);
  const blocked = [...state.problems.values()].some(p => p.issues.length);
  const text = blocked && status.cls !== "bad" ? "Fix the objects marked in red first" : `${currentPlate()?.name}: ${status.text}`;
  el.textContent = text;
  el.title = text;
  el.className = `slicer-slice-state ${blocked ? "bad" : status.cls}`;
}

function renderSlicing() {
  if (!state.project || state.step !== "slice") return;
  const plates = platesWithObjects();
  const todo = plates.filter(p => !isUpToDate(p.id) && !isBusy(p.id) && !plateImporting(p.id));
  const everyPlateHasPrinter = todo.length > 0 && todo.every(p => p.printer_id);
  $("slicing-actions").innerHTML = (everyPlateHasPrinter ? `<button class="btn btn-secondary btn-sm" type="button" data-slice-all-send title="Slice ${plural(todo.length, "plate")} and queue each for its own printer">Slice &amp; send</button>` : "") + `<button class="btn btn-primary btn-sm" type="button" data-slice-all${todo.length && data().profile.bundle_id ? "" : " disabled"}>
    ${todo.length === plates.length ? `Slice all ${plural(plates.length, "plate")}` : todo.length ? `Slice ${plural(todo.length, "plate")}` : "All sliced"}</button>`;
  $("slicing-list").innerHTML = plates.map(plate => {
    const status = plateStatus(plate.id);
    const again = sliceEntry(plate.id)?.state === "sliced" || sliceEntry(plate.id)?.state === "failed";
    return `<div class="slicer-plate-row${plate.id === state.plate ? " selected" : ""}" data-select-plate="${plate.id}">
      <span class="dot ${plateDot(plate.id)}"></span>
      <div><strong>${esc(plate.name)}</strong> ${plateBadge(plate)}<small class="${status.cls}">${plural(plateObjects(plate.id).length, "object")} · ${esc(status.text)}</small></div>
      <button class="btn btn-secondary btn-sm" type="button" data-slice-plate="${plate.id}"${isUpToDate(plate.id) || isBusy(plate.id) || plateImporting(plate.id) ? " disabled" : ""}>
        ${again ? "Slice again" : "Slice"}</button>
    </div>`;
  }).join("") || '<p class="slicer-muted">No plates with objects.</p>';
  const done = plates.filter(p => isUpToDate(p.id));
  const seconds = done.reduce((sum, p) => sum + (sliceEntry(p.id).estimate_s || 0), 0);
  const grams = done.reduce((sum, p) => sum + (sliceEntry(p.id).estimate_g || 0), 0);
  $("slicing-totals").textContent = done.length
    ? `${done.length} of ${plural(plates.length, "plate")} sliced · ${duration(seconds)} of printing · ${Math.round(grams)} g of filament`
    : "Click a sliced plate to see its layers.";
}

function renderSend() {
  if (!state.project || state.step !== "queue") return;
  const inQueue = openJobPlates();
  const previous = new Set([...document.querySelectorAll("[data-send-plate]")].filter(x => x.checked).map(x => Number(x.dataset.sendPlate)));
  const firstRender = !document.querySelector("[data-send-plate]");
  $("send-plates").innerHTML = platesWithObjects().map(plate => {
    const ready = isUpToDate(plate.id);
    const queued = inQueue.has(plate.id);
    const checked = firstRender ? ready && !queued : previous.has(plate.id) && ready;
    const status = queued ? "in the queue" : ready ? plateStatus(plate.id).text : "slice it first";
    return `<label class="slicer-plate-row">
      <input type="checkbox" data-send-plate="${plate.id}"${checked ? " checked" : ""}${ready ? "" : " disabled"}>
      <div><strong>${esc(plate.name)}</strong> ${plateBadge(plate)}<small class="${queued ? "warn" : ready ? "" : "bad"}">${esc(status)}</small></div><span></span>
    </label>`;
  }).join("") || '<p class="slicer-muted">No plates with objects.</p>';
  updateSendButton();
}

function updateSendButton() {
  const chosen = [...document.querySelectorAll("[data-send-plate]")].filter(x => x.checked);
  $("btn-send-batch").disabled = !chosen.length;
  $("btn-send-batch").textContent = chosen.length ? `Send ${plural(chosen.length, "plate")} to the queue` : "Send to the queue";
}

function renderJobs() {
  if (!state.project || state.step !== "queue") return;
  const focused = document.activeElement;
  if (focused?.tagName === "SELECT" && $("job-batches").contains(focused)) return;
  const batches = new Map();
  for (const job of [...state.jobs].reverse()) {
    const key = job.batch_id || `job-${job.id}`;
    if (!batches.has(key)) batches.set(key, []);
    batches.get(key).push(job);
  }
  const printerName = id => state.printers.find(p => p.id === id)?.name || (id ? "Unknown printer" : "No printer");
  const printerOptions = selected => '<option value="">No printer</option>' + state.printers.map(p =>
    `<option value="${esc(p.id)}"${p.id === selected ? " selected" : ""}>${esc(p.name)}${p.connected ? "" : " (offline)"}</option>`).join("");
  $("job-batches").innerHTML = [...batches.entries()].map(([key, jobs]) => {
    jobs.sort((a, b) => (a.plate_id || 0) - (b.plate_id || 0));
    const queued = jobs.filter(j => j.state === "queued");
    const sent = new Date(jobs[0].created_at * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" });
    const chooser = state.assigning === key ? `<div class="slicer-assign">
        ${state.printers.map(p => `<label class="slicer-check"><input type="checkbox" data-assign-printer="${esc(p.id)}"${p.connected ? " checked" : ""}> ${esc(p.name)}${p.connected ? "" : " (offline)"}</label>`).join("")
          || '<p class="slicer-muted">No printers that the queue can use yet.</p>'}
        <div class="slicer-actions-row">
          <button class="btn btn-primary btn-sm" type="button" data-assign-apply="${esc(key)}"${state.printers.length ? "" : " disabled"}>Spread ${plural(queued.length, "plate")} over these printers</button>
          <button class="btn btn-secondary btn-sm" type="button" data-assign-cancel>Cancel</button>
        </div>
      </div>` : "";
    return `<div class="slicer-batch">
      <div class="slicer-batch-head"><span>Sent ${esc(sent)} · ${plural(jobs.length, "plate")}</span>
        ${queued.length > 1 ? `<button class="btn btn-secondary btn-sm" type="button" data-assign="${esc(key)}">Assign printers</button>` : ""}</div>
      ${chooser}
      ${jobs.map(job => {
        const [label, cls] = JOB_STATES[job.state] || [job.state, "idle"];
        const plate = plateById(job.plate_id);
        return `<div class="slicer-job">
          <span class="slicer-job-name" title="${esc(job.notes || job.name)}">${esc(plate?.name || job.name)}</span>
          <span class="status-badge ${cls}">${label}</span>
          <div class="slicer-job-controls">
            ${job.state === "queued"
              ? `<select data-job-printer="${job.id}" aria-label="Printer for ${esc(plate?.name || job.name)}">${printerOptions(job.printer_id)}</select>
                 <a class="btn btn-primary btn-sm" href="/?review=${job.id}" title="Preview the toolpath, run the preflight checks and start">Review &amp; start</a>`
              : `<span class="slicer-muted">${esc(printerName(job.printer_id))}${job.error ? ` · ${esc(job.error)}` : ""}</span>`}
          </div>
        </div>`;
      }).join("")}
    </div>`;
  }).join("") || '<p class="slicer-muted">Nothing sent yet.</p>';
}

function renderViewport() {
  const viewport = state.viewport;
  if (!viewport || !state.project) return;
  const base = state.bundleSettings.get(data().profile.bundle_id);
  viewport.setBed(bedLimits(base?.machine), data().bed === "smooth");
  viewport.setObjects(plateObjects(), state.project.models, state.selected);
}

function renderTools() {
  if (!state.project) return;
  const preview = state.mode === "preview";
  $("prepare-tools").hidden = state.step !== "plates";
  $("view-tools").hidden = state.step !== "slice" && state.step !== "queue";
  $("mode-prepare").classList.toggle("active", !preview);
  $("mode-preview").classList.toggle("active", preview);
  $("mode-preview").disabled = sliceEntry()?.state !== "sliced";
  document.querySelectorAll("#prepare-tools [data-tool]").forEach(b => b.classList.toggle("active", b.dataset.tool === state.viewport?.tool));
  const others = data().plates.filter(p => p.id !== state.plate);
  $("merge-plate").innerHTML = `<option value="">Merge into…</option>${others.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join("")}`;
  $("merge-plate").disabled = !others.length;
  $("arrange-engine").value = data().arrange_engine === "orca" ? "orca" : "nester";
  $("btn-orient-all").disabled = !data().objects.length;
  $("btn-plan-plates").disabled = !data().objects.length;
  $("btn-undo").disabled = !editLog.undo.length;
  $("btn-redo").disabled = !editLog.redo.length;
}

function bedLimits(machine) {
  const points = value => (value || []).map(v => String(v).toLowerCase().split("x").map(Number)).filter(p => p.length === 2 && p.every(Number.isFinite));
  const area = points(machine?.printable_area);
  const exclude = points(machine?.bed_exclude_area);
  const box = list => [Math.min(...list.map(p => p[0])), Math.min(...list.map(p => p[1])), Math.max(...list.map(p => p[0])), Math.max(...list.map(p => p[1]))];
  const bed = area.length >= 3 ? box(area) : [0, 0, 256, 256];
  return { bed, exclude: exclude.length >= 3 ? box(exclude) : [246, 0, 256, 20], height: Number(machine?.printable_height) || 256 };
}

function renderObjectPanel() {
  const panel = $("object-panel");
  const object = selectedObject();
  if (!object || state.mode !== "prepare" || state.step !== "plates") { panel.hidden = true; return; }
  panel.hidden = false;
  const input = (field, index, value, step) =>
    `<input type="number" step="${step}" data-field="${field}" data-index="${index}" value="${Math.round(value * 1000) / 1000}">`;
  const uniform = object.scale.every(s => Math.abs(s - object.scale[0]) < 1e-9);
  const copies = data().objects.filter(o => o.model === object.model).length;
  const note = state.copiesNote?.model === object.model && state.copiesNote.count === copies ? state.copiesNote.text : "";
  panel.innerHTML = `
    <h5 title="${esc(object.name)}">${esc(object.name)}</h5>
    <div class="slicer-xyz"><span></span><span>X</span><span>Y</span><span>Z</span>
      <span>Position</span>${input("position", 0, object.position[0], 1)}${input("position", 1, object.position[1], 1)}<span class="slicer-muted">on bed</span>
      <span>Rotation°</span>${input("rotation", 0, object.rotation[0], 15)}${input("rotation", 1, object.rotation[1], 15)}${input("rotation", 2, object.rotation[2], 15)}
      <span>Scale</span>${input("scale", 0, object.scale[0], 0.05)}${input("scale", 1, object.scale[1], 0.05)}${input("scale", 2, object.scale[2], 0.05)}
    </div>
    <label class="slicer-check"><input type="checkbox" id="scale-uniform"${uniform ? " checked" : ""}> Keep proportions</label>
    <label class="slicer-field">Plate <select id="object-plate">${data().plates.map(p => `<option value="${p.id}"${p.id === object.plate ? " selected" : ""}>${esc(p.name)}</option>`).join("")}</select></label>
    <label class="slicer-field">Copies <input type="number" id="object-copies" min="1" max="500" step="1" value="${copies}" title="How many objects are made from this model, across all plates"></label>
    ${note ? `<div class="slicer-muted" id="object-copies-note">${esc(note)}</div>` : ""}
    ${filamentControls(object)}
    <div class="slicer-actions-row">
      <button class="btn btn-secondary btn-sm" type="button" data-object-action="flat" title="Rotate so the largest flat face is on the bed">Lay flat</button>
      <button class="btn btn-secondary btn-sm" type="button" data-object-action="orient" title="Suggest an orientation, to accept or ignore (O)">Suggest orientation</button>
      <button class="btn btn-secondary btn-sm" type="button" data-object-action="reset">Reset rotation</button>
      <button class="btn btn-secondary btn-sm" type="button" data-object-action="duplicate">Duplicate</button>
      <button class="btn btn-secondary btn-sm" type="button" data-object-action="settings">Settings</button>
    </div>${orientSuggestionHtml(object)}`;
}

// The inspector's filament choice (shared by every copy of the model), colour label, pin, and the mixed-plate warning
function filamentControls(object) {
  const choice = filamentChoice(object.model);
  const machine = currentPresets()?.machine;
  let list = state.presets?.filaments?.[machine] || [];
  if (choice && !list.includes(choice.preset)) list = [choice.preset, ...list];
  const base = currentPresets()?.filament;
  const mix = plateMix(object.plate);
  return `
    <label class="slicer-field">Filament <select id="object-filament">
      <option value="">Project default${base ? ` (${esc(base.replace(/ @.*$/, ""))})` : ""}</option>
      ${list.map(name => `<option value="${esc(name)}"${choice?.preset === name ? " selected" : ""}>${esc(name.replace(/ @.*$/, ""))}</option>`).join("")}
    </select></label>
    <label class="slicer-field"${choice ? "" : " hidden"}>Colour <input type="text" id="object-filament-colour" maxlength="${FILAMENT_COLOUR_MAX}" placeholder="e.g. grey" value="${esc(choice?.colour || "")}"></label>
    <label class="slicer-check" title="The planner, Arrange and Orient leave a pinned object where it is; you can still move it by hand"><input type="checkbox" id="object-pinned"${object.pinned ? " checked" : ""}> Pin</label>
    ${mix ? `<p class="slicer-warn" role="alert">${esc(mix)}. Slicing is refused until each plate holds one filament.</p>` : ""}`;
}
const FILAMENT_COLOUR_MAX = 40;

function showMessage(text) {
  $("viewport-message").hidden = !text;
  $("viewport-message").textContent = text || "";
}

// ── Editing ─────────────────────────────────────────────────────────────────────
function selectObject(id) {
  state.selected = id;
  if (!id && state.settingsTab === "object") state.settingsTab = "process";
  state.viewport?.select(id);
  renderObjects();
  renderObjectPanel();
  renderSettings();
}

async function selectPlate(plateId) {
  if (!plateById(plateId)) return;
  state.plate = plateId;
  state.selected = null;
  state.preview = null;
  await setMode(state.step === "slice" && sliceEntry()?.state === "sliced" ? "preview" : "prepare");
  renderAll();
  state.viewport?.fit(state.mode === "preview" && state.preview ? state.viewport.toolpathBox(state.preview.analysis) : state.viewport.objectsBox());
}

function updateObject(id, change) {
  const object = data().objects.find(o => o.id === id);
  if (!object) return;
  Object.assign(object, change);
  changed();
  renderViewport();
  renderObjectPanel();
}

function changesFor(target) {
  if (target.kind === "object") return data().objects.find(o => o.id === target.id) || null;
  if (target.kind === "plate") return plateById(target.id);
  return null;
}

function changeSetting(target, key, value) {
  const holder = changesFor(target);
  if (holder) {
    holder.overrides = { ...(holder.overrides || {}), [key]: Array.isArray(value) ? value[0] : value };
  } else {
    data().profile[target.kind][key] = value;
  }
  changed({ merge: true });
  renderProfile();
  renderSettings();
  renderObjects();
}

function resetSettings(target, keys) {
  const holder = changesFor(target);
  const changes = holder ? holder.overrides : data().profile[target.kind];
  if (!changes) return;
  keys.forEach(key => delete changes[key]);
  changed();
  renderProfile();
  renderSettings();
  renderObjects();
}

function usePlateSettingsEverywhere() {
  const source = currentPlate();
  const overrides = source.overrides || {};
  const differing = data().plates.filter(p => p.id !== source.id && Object.keys(p.overrides || {}).length
    && JSON.stringify(p.overrides) !== JSON.stringify(overrides));
  if (differing.length && !confirm(`${listNames(differing.map(p => p.name))} ${differing.length === 1 ? "has its" : "have their"} own settings. Replace them with ${source.name}'s?`)) return;
  data().plates.forEach(plate => { plate.overrides = clone(overrides); });
  changed();
  renderAll({ geometry: false });
  toast(Object.keys(overrides).length ? `Every plate now uses ${source.name}'s settings` : "Every plate now uses the project's settings");
}

function nextId(items) {
  return items.reduce((max, item) => Math.max(max, item.id), 0) + 1;
}

// F: the next click on a model lays the face under the pointer flat on the bed
function layOnFace(on) {
  state.layingOnFace = on;
  state.viewport?.setFaceMode(on);
  $("btn-face")?.classList.toggle("active", on);
  if (on) toast("Click the face that should sit on the bed");
}

function faceClicked({ objectId, normal }) {
  layOnFace(false);
  const object = data().objects.find(o => o.id === objectId);
  if (!object) return;
  state.selected = objectId;
  updateObject(objectId, { rotation: orient.layOnFace(normal, object.rotation) });
  renderAll({ geometry: false });
}

// O: ask the server for an orientation (nothing changes until it is accepted)
const ORIENT_MODES = { "least-support": "Least support", strength: "Strength (lie low)", looks: "Looks (front side)" };

async function suggestOrientation(object = selectedObject(), mode = state.orientMode || "least-support") {
  if (!object) return toast("Select an object first", true);
  state.orientMode = mode;
  await flushSave();
  // Looks favours the side facing the viewer (the front of the bed), as the model sees it
  const face = mode === "looks" ? `&face=${orient.toModelSpace([0, -1, 0], object.rotation).join(",")}` : "";
  try {
    const result = await api("GET", `/api/projects/${state.project.id}/objects/${object.id}/orientation?mode=${mode}${face}`);
    state.orientSuggestion = { objectId: object.id, mode, result };
  } catch (e) {
    return toast(e.message, true);
  }
  renderObjectPanel();
}

function orientSuggestionHtml(object) {
  const s = state.orientSuggestion;
  if (!s || s.objectId !== object.id) return "";
  const { current, suggested, changed } = s.result;
  const area = m => `${Math.round(m.overhang_area / 10) / 10} cm²`;
  const summary = !changed ? "Already the best of the orientations tried."
    : `Needs support on ${area(suggested)} instead of ${area(current)}, ${Math.round(suggested.height)} mm tall instead of ${Math.round(current.height)} mm.`;
  return `<div class="slicer-field" id="orient-suggestion">
    <select data-orient-mode aria-label="What to favour">${Object.entries(ORIENT_MODES).map(([value, label]) =>
      `<option value="${value}"${value === s.mode ? " selected" : ""}>${label}</option>`).join("")}</select>
    <span>${summary}</span>
    <div class="slicer-actions-row">
      <button class="btn btn-secondary btn-sm" type="button" data-orient-accept${changed ? "" : " disabled"}>Accept</button>
      <button class="btn btn-secondary btn-sm" type="button" data-orient-ignore>Ignore</button>
    </div>
  </div>`;
}

function acceptOrientation() {
  const s = state.orientSuggestion;
  state.orientSuggestion = null;
  if (s?.result.changed) updateObject(s.objectId, { rotation: s.result.rotation });
  else renderObjectPanel();
}

async function orientAll(wholeProject = false) {
  await flushSave();
  await withButton($("btn-orient-all"), async () => {
    const result = await api("POST", `/api/projects/${state.project.id}/orient-all`, wholeProject ? {} : { plate: state.plate });
    const { oriented, unchanged, pinned } = result.orient;
    state.orientSuggestion = null;
    applyProject(result);
    if (oriented.length) recordEdit();
    renderAll();
    toast(`Turned ${plural(oriented.length, "object")}, ${unchanged.length} already fine` + (pinned.length ? `, ${pinned.length} pinned left alone` : ""));
  });
}

function copyObject() {
  const object = selectedObject();
  if (!object) return;
  state.clipboard = clone(object);
  toast(`Copied ${object.name}`);
}

function pasteObject() {
  if (!state.clipboard) return;
  const source = state.clipboard;
  const copy = { ...clone(source), id: nextId(data().objects), plate: state.plate,
                 position: [source.position[0] + 15, source.position[1] + 15] };
  if (data().objects.length >= 500) return toast("A project holds at most 500 objects", true);
  data().objects.push(copy);
  state.clipboard = { ...clone(copy) };
  state.selected = copy.id;
  changed();
  renderAll();
}

// Copies: the number of objects made from the selected object's model, across all plates. The server adds or removes them.
async function setCopies(object, asked) {
  if (!Number.isInteger(asked) || asked < 1) { renderObjectPanel(); return toast("Copies must be a whole number, at least 1", true); }
  await flushSave();
  try {
    const result = await api("POST", `/api/projects/${state.project.id}/copies`, { object: object.id, copies: asked });
    applyProject(result);
    const { model, count, pinned } = result.copies;
    state.copiesNote = count > asked
      ? { model, count, text: `Still ${count}: ${plural(pinned, "pinned object")} can't be removed. Unpin ${pinned === 1 ? "it" : "them"} to lower this.` }
      : null;
    recordEdit();
    renderAll();
  } catch (err) {
    toast(err.message, true);
    renderObjectPanel();
  }
}

function removeSelected() {
  const object = selectedObject();
  if (!object) return;
  data().objects = data().objects.filter(o => o.id !== object.id);
  state.selected = null;
  changed();
  renderAll();
}

function zoomToSelection() {
  state.viewport?.fit(state.viewport.objectBox(state.selected));
}

async function layFlat(object) {
  // Lay flat: pick the rotation among the axis-aligned ones (and the current one) with the largest area touching the bed,
  // measured from the preview mesh's triangles.
  const geometry = state.viewport?.geometries.get(object.model);
  if (!geometry) return;
  const THREE = await import("three");
  const positions = geometry.getAttribute("position");
  const index = geometry.getIndex();
  const vertex = t => index ? index.getX(t) : t;
  const count = index ? index.count : positions.count;
  const candidates = [[0, 0, 0], [90, 0, 0], [-90, 0, 0], [180, 0, 0], [0, 90, 0], [0, -90, 0]];
  const a = new THREE.Vector3(), b = new THREE.Vector3(), c = new THREE.Vector3();
  let best = null;
  for (const [rx, ry] of candidates) {
    const euler = new THREE.Euler(THREE.MathUtils.degToRad(rx), THREE.MathUtils.degToRad(ry), THREE.MathUtils.degToRad(object.rotation[2]), "ZYX");
    const matrix = new THREE.Matrix4().makeRotationFromEuler(euler);
    let minZ = Infinity;
    for (let i = 0; i < positions.count; i += 7) minZ = Math.min(minZ, a.fromBufferAttribute(positions, i).applyMatrix4(matrix).z);
    let area = 0;
    for (let t = 0; t + 2 < count; t += 3) {
      a.fromBufferAttribute(positions, vertex(t)).applyMatrix4(matrix);
      b.fromBufferAttribute(positions, vertex(t + 1)).applyMatrix4(matrix);
      c.fromBufferAttribute(positions, vertex(t + 2)).applyMatrix4(matrix);
      if (a.z - minZ < 0.3 && b.z - minZ < 0.3 && c.z - minZ < 0.3) {
        area += b.clone().sub(a).cross(c.clone().sub(a)).length() / 2;
      }
    }
    if (!best || area > best.area * 1.02) best = { area, rotation: [rx, ry, object.rotation[2]] };
  }
  updateObject(object.id, { rotation: best.rotation.map(v => ((v % 360) + 360) % 360) });
}

// ── Actions ─────────────────────────────────────────────────────────────────────
async function withButton(button, work) {
  if (button) button.disabled = true;
  try {
    await work();
  } catch (err) {
    toast(err.message, true);
  } finally {
    if (button) button.disabled = false;
  }
}

async function sliceplates(plateIds) {
  await flushSave();
  const project = await api("POST", `/api/projects/${state.project.id}/slice`, plateIds ? { plates: plateIds } : {});
  const queued = Object.entries(project.slices).filter(([id, entry]) => entry.state === "queued" && sliceEntry(Number(id))?.state !== "queued").length;
  applyProject(project);
  renderAll({ geometry: false });
  schedulePoll();
  toast(`Slicing ${plural(queued || plateIds?.length || 1, "plate")}…`);
}

// One Arrange: A does this plate, Shift+A every plate (onto as few as fit). The nester keeps each object's clearance,
// leaves pinned objects where they are and never mixes filaments on a plate; the engine select can switch to OrcaSlicer's.
async function arrange() {
  await flushSave();
  const button = $("btn-arrange");
  await withButton(button, async () => {
    const result = await api("POST", `/api/projects/${state.project.id}/plates/${state.plate}/arrange`, { engine: arrangeEngine() });
    applyProject(result);
    recordEdit();
    renderAll();
    toast(arrangeOutcome(result));
  });
}

async function arrangeAll() {
  await flushSave();
  await withButton($("btn-arrange"), async () => {
    if (!platesWithObjects().length) { toast("Nothing to arrange"); return; }
    const result = await api("POST", `/api/projects/${state.project.id}/arrange`, { engine: arrangeEngine() });
    applyProject(result);
    recordEdit();
    renderAll();
    state.viewport?.fit(state.viewport.objectsBox());
    toast(arrangeOutcome(result));
  });
}

// Every Arrange response carries "outcome", one line saying what happened (including objects skipped because their
// model failed to import); the toast always shows it, so Arrange is never a silent no-op.
const arrangeOutcome = result => result.outcome || "Arranged";

const arrangeEngine = () => $("arrange-engine").value === "orca" ? "orca" : "nester";

async function mergeInto(targetId) {
  const source = currentPlate(), target = plateById(targetId);
  if (!source || !target) return;
  const own = JSON.stringify(source.overrides || {}) !== JSON.stringify(target.overrides || {}) && Object.keys(source.overrides || {}).length;
  if (own && !confirm(`${source.name} has settings of its own. The merged plate keeps ${target.name}'s settings. Merge anyway?`)) return;
  await flushSave();
  await withButton($("merge-plate"), async () => {
    applyProject(await api("POST", `/api/projects/${state.project.id}/plates/${source.id}/merge`, { into: target.id }));
    state.plate = target.id;
    recordEdit();
    renderAll();
    state.viewport?.fit(state.viewport.objectsBox());
    toast(`${source.name} merged into ${target.name}`);
  });
}

// ── Plan plates ─────────────────────────────────────────────────────────────────
// The review sheet for the planner: POST /plan changes nothing; Accept applies it as one revision, so one Undo step reverses it.
// The seed time coefficients still overshoot about 2x, so the time is hidden until that is settled: flip this to show "≈ 1 h 05 min".
const SHOW_PLAN_SECONDS = true;  // approximate until a plate is sliced: the seeds in time_estimate.py are fitted to one plate
const planTime = seconds => {
  const minutes = Math.max(1, Math.round(seconds / 60));
  return `≈ ${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, "0")} min`;
};

// An object's footprint on the bed: its model size, scaled, turned by its rotation (degrees, about x then y, then z):
// returns the half-sides of the box before the turn about z, and that angle
function planFootprint(object) {
  const model = state.project.models.find(m => m.id === object.model);
  if (!model?.size) return null;
  const scale = object.scale || [1, 1, 1];
  const half = model.size.map((v, i) => v * (scale[i] ?? scale[0] ?? 1) / 2);
  const [rx, ry, rz] = (object.rotation || [0, 0, 0]).map(v => v * Math.PI / 180);
  const turn = ([x, y, z]) => {
    [y, z] = [y * Math.cos(rx) - z * Math.sin(rx), y * Math.sin(rx) + z * Math.cos(rx)];
    [x, z] = [x * Math.cos(ry) + z * Math.sin(ry), -x * Math.sin(ry) + z * Math.cos(ry)];
    return [x, y];
  };
  const corners = [-1, 1].flatMap(a => [-1, 1].flatMap(b => [-1, 1].map(c => turn([a * half[0], b * half[1], c * half[2]]))));
  return { half: [Math.max(...corners.map(c => Math.abs(c[0]))), Math.max(...corners.map(c => Math.abs(c[1])))], turn: rz * 180 / Math.PI };
}

function planMap(plate) {
  const [x0, y0, x1, y1] = bedLimits(state.bundleSettings.get(data().profile.bundle_id)?.machine).bed;
  const objects = plate.objects.map(object => {
    const { half, turn } = planFootprint(object) || { half: [8, 8], turn: 0 };
    const [x, y] = object.position, cy = y1 - y + y0;
    return `<rect class="plan-object${object.pinned ? " pinned" : ""}" x="${(x - half[0]).toFixed(1)}" y="${(cy - half[1]).toFixed(1)}" width="${(half[0] * 2).toFixed(1)}" height="${(half[1] * 2).toFixed(1)}" transform="rotate(${(-turn).toFixed(1)} ${x.toFixed(1)} ${cy.toFixed(1)})"><title>${esc(object.name)}</title></rect>`;
  }).join("");
  return `<svg class="plan-map" viewBox="${x0} ${y0} ${x1 - x0} ${y1 - y0}" role="img" aria-label="${esc(plate.name)}: ${plural(plate.parts, "object")}"><rect class="plan-bed" x="${x0}" y="${y0}" width="${x1 - x0}" height="${y1 - y0}"/>${objects}</svg>`;
}

function planSheetHtml(plan) {
  const before = platesWithObjects().length;
  const cards = plan.plates.map(plate => `<div class="plan-card">
      <div class="plan-card-head"><b>${esc(plate.name)}</b>${plate.new ? ' <span class="slicer-muted">new</span>' : ""}</div>
      ${planMap(plate)}
      <div class="slicer-muted">${plural(plate.parts, "object")} · ${esc(plate.filament)}${plate.turned ? ` · ${plate.turned} turned` : ""}</div>
      ${SHOW_PLAN_SECONDS && plate.time_estimate?.seconds ? `<div class="slicer-muted">${planTime(plate.time_estimate.seconds)}</div>` : ""}
    </div>`).join("");
  const kept = plan.kept.length ? `<h4>Kept as they are</h4><ul class="plan-kept">${plan.kept.map(k => `<li><b>${esc(k.name)}</b> <span class="slicer-muted">${esc(k.reason)}</span></li>`).join("")}</ul>` : "";
  const tilt = `<label class="slicer-check" title="Without this a part only goes onto a side of its own box; with it, a part may lean on an angled face"><input type="checkbox" id="plan-tilt"${state.planTilt ? " checked" : ""}> Allow tilting parts onto angled faces</label>`;
  const refused = plan.refused ? `<p class="slicer-error" role="alert">No plan is offered: ${esc(plan.refused)}. Nothing was changed.</p>` : "";
  return `<p class="slicer-muted">Now ${plural(before, "plate")}. Plan: ${esc(plan.summary)}${plan.plates.length > 1 ? ", longest first" : ""}.</p>
    ${refused}${tilt}
    ${plan.plates.length ? `<div class="plan-grid">${cards}</div>` : ""}${kept}`;
}

async function planPlates() {
  await flushSave();
  state.planTilt = false;
  await withButton($("btn-plan-plates"), async () => {
    await showPlan();
    $("dlg-plan").showModal();
  });
}

async function showPlan() {
  const plan = await api("POST", `/api/projects/${state.project.id}/plan`, { allow_tilt: !!state.planTilt });
  state.plan = plan;
  $("plan-body").innerHTML = planSheetHtml(plan);
  $("plan-tilt").addEventListener("change", async event => {
    state.planTilt = event.target.checked;
    await showPlan();
  });
  $("plan-error").textContent = "";
  $("plan-accept").disabled = !plan.plates.length;
}

async function acceptPlan() {
  const plan = state.plan;
  if (!plan) return;
  await withButton($("plan-accept"), async () => {
    try {
      const result = await api("POST", `/api/projects/${state.project.id}/plan/accept`, { revision: plan.revision, fingerprint: plan.fingerprint, allow_tilt: !!state.planTilt });
      applyProject(result);
      recordEdit();
      state.plan = null;
      $("dlg-plan").close();
      renderAll();
      state.viewport?.fit(state.viewport.objectsBox());
      toast(`Planned: ${result.plan.summary}. Undo puts it back`);
    } catch (err) {
      if (err.status !== 409) throw err;
      $("plan-error").textContent = err.message;   // "changed elsewhere" or "The plan changed, review it again"
      state.plan = null;   // nothing to accept any more: Accept does nothing until the sheet is opened again
    }
  });
}

async function layOutParts() {
  const placed = state.project.models.some(m => m.split?.id === state.project.split_id);
  if (placed && !confirm("Lay the parts out again? This replaces the parts on your plates, including any moves or settings you gave them.")) return;
  await flushSave();
  applyProject(await api("POST", `/api/projects/${state.project.id}/layout`, {}));
  resetEdits();
  state.plate = data().plates.find(p => plateObjects(p.id).length)?.id || data().plates[0].id;
  renderAll();
  schedulePoll();
  await setStep("plates");
  state.viewport?.fit(state.viewport.objectsBox());
  toast(`Parts packed onto ${plural(platesWithObjects().length, "plate")} — importing them now`);
}

async function placeWhole() {
  const placed = state.project.models.some(m => m.split?.id === state.project.split_id);
  if (placed && !confirm("Put the model on a plate again? This replaces what's on your plates from this model.")) return;
  await flushSave();
  applyProject(await api("POST", `/api/projects/${state.project.id}/whole`, {}));
  renderAll();
  schedulePoll();
  await setStep("plates");
}

async function setMode(mode) {
  if (mode === "preview" && sliceEntry()?.state !== "sliced") mode = "prepare";
  if (mode !== "preview") stopPlayback();
  state.mode = mode;
  $("layer-box").hidden = mode !== "preview";
  $("move-box").hidden = mode !== "preview";
  $("preview-panel").hidden = mode !== "preview";
  state.viewport?.setMode(mode);
  renderTools();
  renderObjectPanel();
  if (mode === "preview") await loadPreview();
}

async function loadPreview() {
  const entry = sliceEntry();
  const key = `${state.project.id}:${state.plate}:${entry?.sliced_at}`;
  if (state.preview?.key !== key) {
    showMessage("Loading the preview…");
    try {
      const [analysis, buffer] = await Promise.all([
        api("GET", `/api/projects/${state.project.id}/plates/${state.plate}/analysis`),
        binary(`/api/projects/${state.project.id}/plates/${state.plate}/toolpath`),
      ]);
      state.preview = { key, analysis, buffer };
    } catch (err) {
      showMessage("");
      toast(err.message, true);
      return;
    }
  }
  showMessage("");
  drawPreview(true);
}

function drawPreview(fit = false) {
  stopPlayback();
  const { analysis, buffer } = state.preview;
  const info = state.viewport?.showToolpath(analysis, buffer, state.hiddenFeatures);
  const layers = analysis.layers.length;
  const slider = $("layer-slider");
  slider.max = Math.max(0, layers - 1);
  if (fit || Number(slider.value) > layers - 1) slider.value = layers - 1;
  setLayer();
  if (fit && state.viewport?.printerShown) state.viewport.fitPrinter();
  else if (fit) state.viewport?.fit(state.viewport.toolpathBox(analysis));
  renderPreviewPanel(info);
}

function setLayer() {
  const index = Number($("layer-slider").value);
  const fraction = Number($("move-slider").value) / 1000;
  state.viewport?.setLayer(index, fraction);
  renderLayerReadout(index);
}

function renderLayerReadout(index) {
  const layers = state.preview?.analysis.layers || [];
  const layer = layers[index];
  const head = state.viewport?.printerShown && state.viewport.head;
  $("layer-readout").textContent = layer ? `${index + 1} / ${layers.length} · ${layer.z.toFixed(2)} mm` : "";
  $("layer-readout").title = head ? `Nozzle at X${head[0].toFixed(1)} Y${head[1].toFixed(1)} Z${head[2].toFixed(2)}` : "";
}

// ── Printer and playback ───────────────────────────────────────────────────────
const HEAD_SPEED = 250;   // mm/s of nozzle travel at 1× — roughly what the Centauri Carbon averages
const PRINTER_PREF = "spooler.slicer.printer";

function printerPrefs() {
  try { return { shown: true, shell: "glass", ...JSON.parse(localStorage.getItem(PRINTER_PREF) || "{}") }; }
  catch { return { shown: true, shell: "glass" }; }
}

function savePrinterPrefs(change) {
  try { localStorage.setItem(PRINTER_PREF, JSON.stringify({ ...printerPrefs(), ...change })); } catch { /* per-browser convenience only */ }
}

async function showPrinter(on, { save = true } = {}) {
  const viewport = state.viewport;
  if (!viewport) return;
  if (save) savePrinterPrefs({ shown: on });
  $("btn-printer").classList.toggle("active", on);
  $("btn-printer").setAttribute("aria-pressed", String(on));
  $("printer-shell").hidden = !on;
  const loading = on && !viewport.printer;
  if (loading && state.mode === "preview") showMessage("Loading the printer…");
  try {
    await viewport.showPrinter(on);
  } catch (err) {
    $("btn-printer").classList.remove("active");
    $("printer-shell").hidden = true;
    toast(`Printer model: ${err.message}`, true);
  } finally {
    if (loading && state.mode === "preview") showMessage("");
  }
  if (state.mode === "preview" && state.preview) {
    if (viewport.printerShown) viewport.fitPrinter();
    else viewport.fit(viewport.toolpathBox(state.preview.analysis));
    renderLayerReadout(Number($("layer-slider").value));
  }
}

function stopPlayback() {
  if (!state.playing) return;
  cancelAnimationFrame(state.playing.frame);
  state.playing = null;
  $("btn-play").textContent = "Play";
}

function startPlayback() {
  const viewport = state.viewport;
  if (!viewport?.drawn || state.mode !== "preview") return;
  const slider = $("layer-slider");
  if (Number(slider.value) >= Number(slider.max) && Number($("move-slider").value) >= 1000) {
    slider.value = 0;   // finished: play from the first layer
    $("move-slider").value = 0;
    setLayer();
  }
  let { count } = viewport.drawn;
  let last = performance.now();
  const step = now => {
    const seconds = Math.min(0.1, (now - last) / 1000);
    last = now;
    const at = viewport.advance(count, HEAD_SPEED * Number($("play-speed").value) * seconds);
    count = at.count;
    slider.value = at.layer;
    $("move-slider").value = Math.round(at.fraction * 1000);
    renderLayerReadout(at.layer);
    if (at.done) stopPlayback();
    else state.playing.frame = requestAnimationFrame(step);
  };
  state.playing = { frame: requestAnimationFrame(step) };
  $("btn-play").textContent = "Pause";
}

function renderPreviewPanel(info) {
  const { analysis } = state.preview;
  const entry = sliceEntry();
  const base = state.bundleSettings.get(data().profile.bundle_id);
  const filament = { ...(base?.filament || {}), ...data().profile.filament };
  const diameter = Number([].concat(filament.filament_diameter ?? 1.75)[0]) || 1.75;
  const density = Number([].concat(filament.filament_density ?? 1.24)[0]) || 1.24;
  const grams = mm => mm * Math.PI * (diameter / 2) ** 2 * density / 1000;
  const stats = analysis.feature_stats || {};
  const legend = (analysis.features || []).filter(name => stats[name]).map(name => `
    <label><input type="checkbox" data-feature="${esc(name)}"${state.hiddenFeatures.has(name) ? "" : " checked"}>
      <span class="swatch" style="background:${featureColor(name)}"></span><span>${esc(name)}</span>
      <span class="num">${grams(stats[name].extruded_mm).toFixed(1)} g</span><span class="num">${duration(stats[name].time_s) || "<1 min"}</span></label>`).join("");
  const temps = analysis.temperatures || {};
  $("preview-panel").innerHTML = `
    <dl class="slicer-stats">
      <dt>Plate</dt><dd>${esc(currentPlate()?.name)}</dd>
      <dt>Print time</dt><dd>${duration(entry?.estimate_s) || "—"}</dd>
      <dt>Filament</dt><dd>${entry?.estimate_g ? `${entry.estimate_g.toFixed(1)} g` : "—"}</dd>
      <dt>Layers</dt><dd>${analysis.layers.length}</dd>
      <dt>Nozzle / bed</dt><dd>${temps.nozzle?.max ?? "—"} °C / ${temps.bed?.max ?? "—"} °C</dd>
      <dt>Slicer</dt><dd>${esc(analysis.slicer || "OrcaSlicer")}</dd>
    </dl>
    ${entry?.outdated ? '<p class="slicer-slice-state warn">This plate changed after it was sliced.</p>' : ""}
    ${info?.stride > 1 ? `<p class="slicer-muted">Large toolpath: showing every ${info.stride}th move.</p>` : ""}
    <div class="slicer-legend">${legend || '<p class="slicer-muted">No extrusion features found.</p>'}</div>
    <a class="btn btn-secondary btn-sm" href="/api/projects/${state.project.id}/plates/${state.plate}/gcode" download>Download G-code</a>`;
}

// ── Queue ───────────────────────────────────────────────────────────────────────
async function loadJobs({ quiet = false } = {}) {
  clearTimeout(state.jobsTimer);
  if (!state.project) return;
  const id = state.project.id;
  try {
    const { jobs } = await api("GET", `/api/projects/${id}/jobs`);
    if (state.project?.id !== id) return;
    state.jobs = jobs;
    if (!quiet) {
      renderSteps();
      renderSend();
      renderJobs();
    }
  } catch (err) {
    if (!quiet) toast(err.message, true);
  }
  if (state.step === "queue") state.jobsTimer = setTimeout(loadJobs, 4000);
}

async function loadPrinters() {
  try {
    const printers = await api("GET", "/api/printers");
    state.printers = printers.filter(p => p.printer_type === "cc1");
    renderJobs();
  } catch (_) { /* the list stays as it was */ }
}

async function sendBatch() {
  const chosen = [...document.querySelectorAll("[data-send-plate]")].filter(x => x.checked).map(x => Number(x.dataset.sendPlate));
  if (!chosen.length) return;
  const repeats = chosen.filter(id => openJobPlates().has(id)).map(id => plateById(id).name);
  if (repeats.length && !confirm(`${listNames(repeats)} ${repeats.length === 1 ? "is" : "are"} already in the queue. Send another copy?`)) return;
  await flushSave();
  rememberDefaults({ options: { leveling: $("send-leveling").checked, timelapse: $("send-timelapse").checked } });
  await withButton($("btn-send-batch"), async () => {
    const result = await api("POST", `/api/projects/${state.project.id}/queue`, {
      plates: chosen, leveling: $("send-leveling").checked, timelapse: $("send-timelapse").checked, again: repeats.length > 0 });
    toast(`Sent ${plural(result.jobs.length, "plate")} to the queue`);
    document.querySelectorAll("[data-send-plate]").forEach(box => { box.checked = false; });  // sent: don't offer them again
    const queuedCount = result.jobs.length;
    await loadJobs();
    if (queuedCount > 1 && state.printers.length > 1) state.assigning = result.batch_id;
    renderJobs();
    $("job-batches").closest(".slicer-card").scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

async function assignPrinters(batchKey, button) {
  const printers = [...document.querySelectorAll("[data-assign-printer]")].filter(x => x.checked).map(x => x.dataset.assignPrinter);
  if (!printers.length) { toast("Choose at least one printer", true); return; }
  const jobIds = state.jobs.filter(j => (j.batch_id || `job-${j.id}`) === batchKey && j.state === "queued")
    .sort((a, b) => (a.plate_id || 0) - (b.plate_id || 0)).map(j => j.id);
  await withButton(button, async () => {
    await api("POST", "/api/jobs/assign", { job_ids: jobIds, printer_ids: printers });
    state.assigning = null;
    await loadJobs();
    toast(`Spread ${plural(jobIds.length, "plate")} over ${plural(printers.length, "printer")}`);
  });
}

// ── Dialogs ─────────────────────────────────────────────────────────────────────
async function openProjectsDialog() {
  const dialog = $("dlg-projects");
  $("project-list").innerHTML = '<p class="slicer-muted">Loading…</p>';
  if (!dialog.open) dialog.showModal();
  try {
    const [{ projects }, { splits }] = await Promise.all([api("GET", "/api/projects"), api("GET", "/api/splits").catch(() => ({ splits: [] }))]);
    const loose = splits.filter(s => !s.project_id);   // the server marks splits owned by any project, hidden quick ones included
    $("project-list").innerHTML = (projects.length ? '<h4 class="slicer-list-heading">Your projects</h4>' : "") + projects.map(p => `
      <div class="slicer-project-row">
        <div><strong>${esc(p.name)}</strong>${p.split_id ? '<span class="slicer-tag">from a model upload</span>' : ""}
          <small>${plural(p.plates, "plate")} · ${plural(p.objects, "object")} · edited ${new Date(p.updated_at * 1000).toLocaleString()}</small></div>
        <button class="btn btn-primary btn-sm" type="button" data-open-project="${p.id}">Open</button>
        <button class="btn btn-danger btn-sm" type="button" data-delete-project="${p.id}">Delete</button>
      </div>`).join("")
      + (loose.length ? '<h4 class="slicer-list-heading">Split models without a project</h4>' + loose.map(s => `
      <div class="slicer-project-row">
        <div><strong>${esc(s.source_name)}</strong><small>${s.parts ? plural(s.parts, "part") : esc(s.state)}</small></div>
        <span></span>
        <button class="btn btn-secondary btn-sm" type="button" data-adopt-split="${s.id}" data-name="${esc(s.source_name.replace(/\.[^.]+$/, ""))}">Open as project</button>
      </div>`).join("") : "")
      || '<p class="slicer-muted">No projects yet — upload a model to start.</p>';
  } catch (err) {
    $("project-list").innerHTML = `<p class="slicer-muted">${esc(err.message)}</p>`;
  }
}

async function uploadNewModel(file) {
  if (!/\.(stl|obj|3mf|gcode)$/i.test(file.name)) { toast("Choose an STL, OBJ or 3MF model, or a G-code file", true); return; }
  const progress = $("new-upload-progress"), bar = progress.querySelector(".job-upload-bar");
  progress.hidden = false;
  bar.style.width = "0%";
  $("btn-new-upload").disabled = true;
  try {
    const params = new URLSearchParams({ filename: file.name, scale: $("new-scale").value, up: $("new-up").value });
    const project = await upload(`/api/projects/upload?${params}`, file, pct => { bar.style.width = `${pct}%`; });
    $("dlg-projects").close();
    await openProject(project.id);
    toast(`Imported ${file.name}`);
    showImportNotes(project);
  } catch (err) {
    toast(err.message, true);
  } finally {
    progress.hidden = true;
    $("btn-new-upload").disabled = false;
  }
}

async function openSplitDialog() {
  const dialog = $("dlg-split");
  $("split-choices").innerHTML = '<p class="slicer-muted">Loading…</p>';
  dialog.showModal();
  try {
    const { splits } = await api("GET", "/api/splits");
    const ready = splits.filter(s => s.parts && (s.state === "split" || s.state === "queued") && !s.project_quick);   // a hidden quick project's model is not a part source
    $("split-choices").innerHTML = ready.map(s => `
      <div class="slicer-project-row">
        <div><strong>${esc(s.source_name)}</strong><small>${plural(s.parts, "part")}</small></div>
        <span></span>
        <button class="btn btn-primary btn-sm" type="button" data-add-split="${s.id}">Add parts</button>
      </div>`).join("") || '<p class="slicer-muted">No split models with parts yet.</p>';
  } catch (err) {
    $("split-choices").innerHTML = `<p class="slicer-muted">${esc(err.message)}</p>`;
  }
}

// A model that fits the bed can go on one plate whole ("Put it on a plate" on the Model step), and then Split is shown as not needed
const modelOnPlateWhole = () => !!state.project?.split_id
  && state.project.models.some(m => m.split?.id === state.project.split_id && m.split.part === 0);
const splitSkipped = () => modelOnPlateWhole() && !!splitStep.split?.analysis?.fits_whole;

// ── Wiring ──────────────────────────────────────────────────────────────────────
function bindUi() {
  document.querySelectorAll("dialog [data-close]").forEach(btn => btn.addEventListener("click", () => btn.closest("dialog").close()));

  $("step-bar").addEventListener("click", e => {
    const btn = e.target.closest("[data-step]");
    if (btn && !btn.disabled) setStep(btn.dataset.step);
  });
  $("step-actions").addEventListener("click", e => {
    const btn = e.target.closest("[data-go]");
    if (btn && !btn.disabled) setStep(btn.dataset.go);
  });

  $("project-name").addEventListener("change", async e => {
    try {
      applyProject(await api("PATCH", `/api/projects/${state.project.id}`, { name: e.target.value }), { keepLocalData: true });
      renderAll({ geometry: false });
    } catch (err) {
      toast(err.message, true);
    }
  });
  $("btn-export-3mf").addEventListener("click", exportProject);
  $("btn-projects").addEventListener("click", openProjectsDialog);
  $("btn-empty-projects").addEventListener("click", openProjectsDialog);
  $("btn-new-upload").addEventListener("click", () => $("new-model-input").click());
  const projectsDialog = $("dlg-projects");   // dropping a file on the Projects list imports it
  projectsDialog.addEventListener("dragover", e => { e.preventDefault(); projectsDialog.classList.add("dropping"); });
  projectsDialog.addEventListener("dragleave", e => { if (e.target === projectsDialog) projectsDialog.classList.remove("dropping"); });
  projectsDialog.addEventListener("drop", e => {
    e.preventDefault();
    projectsDialog.classList.remove("dropping");
    if (e.dataTransfer.files[0]) uploadNewModel(e.dataTransfer.files[0]);
  });
  $("new-model-input").addEventListener("change", e => { if (e.target.files[0]) uploadNewModel(e.target.files[0]); e.target.value = ""; });
  $("btn-new-project").addEventListener("click", async () => {
    try {
      const project = await api("POST", "/api/projects", { name: $("new-project-name").value });
      $("dlg-projects").close();
      $("new-project-name").value = "";
      await openProject(project.id);
    } catch (err) {
      toast(err.message, true);
    }
  });
  $("project-list").addEventListener("click", async e => {
    const open = e.target.closest("[data-open-project]");
    const remove = e.target.closest("[data-delete-project]");
    const adopt = e.target.closest("[data-adopt-split]");
    try {
      if (open) {
        $("dlg-projects").close();
        await openProject(Number(open.dataset.openProject));
      } else if (adopt) {
        const project = await api("POST", "/api/projects", { name: adopt.dataset.name, split_id: Number(adopt.dataset.adoptSplit) });
        $("dlg-projects").close();
        await openProject(project.id);
      } else if (remove && confirm("Delete this project, its uploaded model and its sliced plates? Jobs already in the queue stay there.")) {
        await api("DELETE", `/api/projects/${remove.dataset.deleteProject}`);
        if (state.project?.id === Number(remove.dataset.deleteProject)) {
          state.project = null;
          $("empty-state").hidden = false;
        }
        openProjectsDialog();
      }
    } catch (err) {
      toast(err.message, true);
    }
  });

  $("settings-scope-toggle").addEventListener("click", e => {
    const btn = e.target.closest("[data-scope-tab]");
    if (!btn) return;
    if (btn.dataset.scopeTab === "objects") {
      if (!selectedObject()) return toast("Select an object on the plate to change its own settings");
      state.settingsTab = "object";
    } else if (state.settingsTab === "object") {
      state.settingsTab = "process";
    }
    renderSettings();
  });
  for (const kind of PRESET_KINDS) $(`preset-${kind}-select`).addEventListener("change", e => choosePreset(kind, e.target.value));
  for (const kind of ["process", "filament"]) {
    $(`btn-save-${kind}`).addEventListener("click", () => openSavePreset(kind));
    $(`btn-delete-${kind}`).addEventListener("click", () => deletePreset(kind));
  }
  $("btn-import-preset").addEventListener("click", () => $("preset-file").click());
  $("preset-file").addEventListener("change", e => { const [file] = e.target.files; e.target.value = ""; if (file) importPreset(file); });
  $("bed-select").addEventListener("change", e => { data().bed = e.target.value; rememberDefaults({ plate_side: data().bed }); changed(); renderViewport(); });

  $("btn-save-profile-confirm").addEventListener("click", savePreset);

  $("btn-add-model").addEventListener("click", () => $("model-input").click());
  $("model-input").addEventListener("change", e => { chooseImport([...e.target.files]); e.target.value = ""; });
  $("btn-import").addEventListener("click", importFiles);
  $("btn-add-split").addEventListener("click", openSplitDialog);
  $("split-choices").addEventListener("click", async e => {
    const btn = e.target.closest("[data-add-split]");
    if (!btn) return;
    btn.disabled = true;
    try {
      await flushSave();
      applyProject(await api("POST", `/api/projects/${state.project.id}/models/from-split`, { split_id: Number(btn.dataset.addSplit) }));
      $("dlg-split").close();
      renderAll();
      schedulePoll();
      toast("Adding the parts — packed onto plates");
    } catch (err) {
      toast(err.message, true);
      btn.disabled = false;
    }
  });

  $("object-list").addEventListener("click", async e => {
    const retry = e.target.closest("[data-retry-model]");
    if (retry) {
      retry.disabled = true;
      try {
        const fresh = await api("POST", `/api/projects/${state.project.id}/models/${retry.dataset.retryModel}/retry`);
        state.project = { ...state.project, models: fresh.models };
        renderAll();
        schedulePoll();
      } catch (err) {
        toast(err.message, true);
        retry.disabled = false;
      }
      return;
    }
    const remove = e.target.closest("[data-remove-object]");
    if (remove) {
      const id = Number(remove.dataset.removeObject);
      data().objects = data().objects.filter(o => o.id !== id);
      if (state.selected === id) state.selected = null;
      changed();
      renderAll();
      return;
    }
    const row = e.target.closest("[data-object]");
    if (row) selectObject(Number(row.dataset.object));
  });

  $("plate-tabs").addEventListener("click", e => {
    const tab = e.target.closest("[data-plate]");
    if (!tab) return;
    if (tab.dataset.plate === "new") {
      const id = nextId(data().plates);
      data().plates.push({ id, name: `Plate ${id}`, overrides: {} });
      state.plate = id;
      changed();
      renderAll();
    } else if (tab.dataset.plate === "remove") {
      if (plateObjects().length) { toast("Move or remove the objects on this plate first", true); return; }
      data().plates = data().plates.filter(p => p.id !== state.plate);
      state.plate = data().plates[0].id;
      changed();
      renderAll();
    } else {
      selectPlate(Number(tab.dataset.plate));
    }
  });

  $("settings-scope").addEventListener("click", e => {
    const action = e.target.closest("[data-scope]")?.dataset.scope;
    const plate = currentPlate();
    if (!action || !plate) return;
    if (action === "apply-all") usePlateSettingsEverywhere();
    else if (action === "clear") {
      plate.overrides = {};
      changed();
      renderAll({ geometry: false });
    }
  });

  $("slicing-actions").addEventListener("click", e => {
    const btn = e.target.closest("[data-slice-all]");
    if (btn && !btn.disabled) withButton(btn, () => sliceplates(null));
    const send = e.target.closest("[data-slice-all-send]");
    if (send && !send.disabled) withButton(send, () => sliceAndSend(null)).catch(err => toast(err.message, true));
  });
  $("bricklayers").addEventListener("change", e => {
    if (e.target.closest("[data-brick], [data-brick-plate]")) changeBricklayers(e.target);
  });
  $("slicing-list").addEventListener("click", e => {
    const slice = e.target.closest("[data-slice-plate]");
    if (slice) {
      if (!slice.disabled) withButton(slice, () => sliceplates([Number(slice.dataset.slicePlate)]));
      return;
    }
    const row = e.target.closest("[data-select-plate]");
    if (row) selectPlate(Number(row.dataset.selectPlate));
  });

  $("send-plates").addEventListener("change", updateSendButton);
  $("btn-send-batch").addEventListener("click", sendBatch);
  $("job-batches").addEventListener("click", e => {
    const open = e.target.closest("[data-assign]");
    const apply = e.target.closest("[data-assign-apply]");
    if (open) { state.assigning = open.dataset.assign; renderJobs(); }
    else if (apply) assignPrinters(apply.dataset.assignApply, apply);
    else if (e.target.closest("[data-assign-cancel]")) { state.assigning = null; renderJobs(); }
  });
  $("job-batches").addEventListener("change", async e => {
    const select = e.target.closest("[data-job-printer]");
    if (!select) return;
    select.blur();
    try {
      await api("PATCH", `/api/jobs/${select.dataset.jobPrinter}`, { printer_id: select.value || null });
      await loadJobs();
    } catch (err) {
      toast(err.message, true);
    }
  });

  $("object-panel").addEventListener("change", e => {
    const object = selectedObject();
    if (!object) return;
    const field = e.target.dataset.field;
    if (e.target.matches("[data-orient-mode]")) return suggestOrientation(object, e.target.value);
    if (e.target.id === "object-plate") {
      object.plate = Number(e.target.value);
      state.selected = null;
      changed();
      renderAll();
      return;
    }
    if (e.target.id === "object-copies") { setCopies(object, Number(e.target.value)); return; }
    if (e.target.id === "object-pinned") {
      if (e.target.checked) object.pinned = true; else delete object.pinned;
      changed();
      return;
    }
    if (e.target.id === "object-filament") {
      const preset = e.target.value;
      setFilamentChoice(object.model, preset ? { preset, colour: filamentChoice(object.model)?.colour || "" } : null);
      return;
    }
    if (e.target.id === "object-filament-colour") {
      const current = filamentChoice(object.model);
      if (current) setFilamentChoice(object.model, { ...current, colour: e.target.value.trim() });
      return;
    }
    if (!field) return;
    const value = Number(e.target.value);
    if (!Number.isFinite(value)) return;
    const next = [...object[field]];
    next[Number(e.target.dataset.index)] = value;
    if (field === "scale") {
      if (value < 0.01) return;
      if ($("scale-uniform").checked) next.fill(value);
    }
    updateObject(object.id, { [field]: next });
  });
  $("object-panel").addEventListener("click", async e => {
    if (e.target.closest("[data-orient-accept]")) return acceptOrientation();
    if (e.target.closest("[data-orient-ignore]")) { state.orientSuggestion = null; return renderObjectPanel(); }
    const action = e.target.closest("[data-object-action]")?.dataset.objectAction;
    const object = selectedObject();
    if (!action || !object) return;
    if (action === "flat") await layFlat(object);
    else if (action === "orient") suggestOrientation(object);
    else if (action === "reset") updateObject(object.id, { rotation: [0, 0, 0] });
    else if (action === "settings") { state.settingsTab = "object"; renderSettings(); }
    else if (action === "duplicate") {
      const copy = { ...clone(object), id: nextId(data().objects), name: `${object.name} copy`, position: [object.position[0] + 15, object.position[1] + 15] };
      data().objects.push(copy);
      state.selected = copy.id;
      changed();
      renderAll();
    }
  });

  document.querySelectorAll("#prepare-tools [data-tool]").forEach(btn => btn.addEventListener("click", () => {
    state.viewport?.setTool(btn.dataset.tool);
    renderTools();
  }));
  $("spool-select").addEventListener("change", e => chooseSpool(e.target.value));
  $("spool-select").addEventListener("focus", () => loadSpools());
  $("plate-printer-select").addEventListener("change", e => {
    const plate = currentPlate();
    if (!plate) return;
    plate.printer_id = e.target.value || null;
    rememberDefaults({ printer: plate.printer_id });
    changed();
    renderFarm();
  });
  $("btn-slice-send").addEventListener("click", () => withButton($("btn-slice-send"), () => sliceAndSend([state.plate]))
    .catch(err => toast(err.message, true)));
  $("btn-arrange").addEventListener("click", arrange);
  $("btn-face").addEventListener("click", () => layOnFace(!state.layingOnFace));
  $("btn-undo").addEventListener("click", undo);
  $("btn-redo").addEventListener("click", redo);
  $("arrange-engine").addEventListener("change", e => {
    if (e.target.value === "orca") data().arrange_engine = "orca"; else delete data().arrange_engine;
    changed();
  });
  $("btn-orient-all").addEventListener("click", e => orientAll(e.shiftKey));
  $("btn-plan-plates").addEventListener("click", planPlates);
  $("plan-accept").addEventListener("click", acceptPlan);
  $("merge-plate").addEventListener("change", e => {
    const target = Number(e.target.value);
    e.target.value = "";
    if (target) mergeInto(target);
  });
  const fit = () => state.viewport?.fit(state.mode === "preview" && state.preview
    ? state.viewport.toolpathBox(state.preview.analysis) : state.viewport.objectsBox());
  $("btn-fit").addEventListener("click", fit);
  $("btn-fit-preview").addEventListener("click", fit);
  $("mode-prepare").addEventListener("click", () => setMode("prepare"));
  $("mode-preview").addEventListener("click", () => setMode("preview"));
  $("layer-slider").addEventListener("input", () => { stopPlayback(); $("move-slider").value = 1000; setLayer(); });
  $("move-slider").addEventListener("input", () => { stopPlayback(); setLayer(); });
  $("btn-play").addEventListener("click", () => (state.playing ? stopPlayback() : startPlayback()));
  $("btn-printer").addEventListener("click", () => showPrinter(!printerPrefs().shown));
  $("printer-shell").addEventListener("change", e => {
    state.viewport?.setShell(e.target.value);
    savePrinterPrefs({ shell: e.target.value });
  });
  $("preview-panel").addEventListener("change", e => {
    const name = e.target.dataset.feature;
    if (!name) return;
    if (e.target.checked) state.hiddenFeatures.delete(name);
    else state.hiddenFeatures.add(name);
    drawPreview();
  });

  const viewport = $("viewport");
  viewport.addEventListener("dragover", e => { e.preventDefault(); viewport.classList.add("dropping"); });
  viewport.addEventListener("dragleave", () => viewport.classList.remove("dropping"));
  viewport.addEventListener("drop", e => {
    e.preventDefault();
    viewport.classList.remove("dropping");
    chooseImport([...e.dataTransfer.files]);
  });

  document.addEventListener("keydown", e => {
    if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName) || document.querySelector("dialog[open]")) return;
    const key = e.key.toLowerCase();
    const command = e.ctrlKey || e.metaKey;
    if (state.step === "plates" && command) {
      if (key === "z" && !e.shiftKey) undo();
      else if (key === "y" || (key === "z" && e.shiftKey)) redo();
      else if (key === "c") copyObject();
      else if (key === "v") pasteObject();
      else return;
      e.preventDefault();
    } else if (state.step === "plates") {
      if ((e.key === "Delete" || e.key === "Backspace") && selectedObject()) removeSelected();
      else if (key === "m" || key === "w") { state.viewport?.setTool("translate"); renderTools(); }
      else if (key === "r" || key === "e") { state.viewport?.setTool("rotate"); renderTools(); }
      else if (key === "s") { state.viewport?.setTool("scale"); renderTools(); }
      else if (key === "f") layOnFace(!state.layingOnFace);
      else if (key === "a") (e.shiftKey ? arrangeAll : arrange)();
      else if (key === "o") suggestOrientation();
      else if (key === "z") zoomToSelection();
      else if ("0123456".includes(e.key) && e.key !== "") state.viewport?.setView(["iso", "top", "bottom", "front", "back", "left", "right"][Number(e.key)]);
      else if (e.key === "Escape") { if (state.layingOnFace) layOnFace(false); else selectObject(null); }
    } else if (state.step === "slice") {
      if (e.key === "f" || e.key === "F") fit();
      else if (e.key === "p" || e.key === "P") showPrinter(!printerPrefs().shown);
      else if (state.mode === "preview" && e.key === " ") {
        state.playing ? stopPlayback() : startPlayback();
        e.preventDefault();
      } else if (state.mode === "preview" && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
        stopPlayback();
        const slider = $("layer-slider");
        slider.value = Number(slider.value) + (e.key === "ArrowUp" ? 1 : -1);
        $("move-slider").value = 1000;
        setLayer();
        e.preventDefault();
      }
    }
  });
  window.addEventListener("beforeunload", e => {
    if (state.saveTimer || state.saving) e.preventDefault();
  });
}

function chooseImport(files) {
  const models = files.filter(f => /\.(stl|obj|3mf)$/i.test(f.name));
  if (!models.length) { toast("Choose STL, OBJ or 3MF files", true); return; }
  if (!state.project) { toast("Open or create a project first", true); return; }
  if (state.step !== "plates") { toast("Go to the Plates step to add models to a plate", true); return; }
  state.importFiles = models;
  $("import-files").textContent = models.map(f => f.name).join(", ");
  $("dlg-import").showModal();
}

// What an OrcaSlicer project import had to leave out or change
function showImportNotes(project) {
  const notes = project?.import_notes || [];
  if (notes.length) toast(`Opened with ${notes.length === 1 ? "a note" : `${notes.length} notes`}: ${notes.slice(0, 3).join(" · ")}${notes.length > 3 ? " …" : ""}`);
}

async function exportProject() {
  if (!state.project) return;
  const button = $("btn-export-3mf");
  button.disabled = true;
  try {
    await flushSave();
    const response = await fetch(`/api/projects/${state.project.id}/export.3mf`);
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || "The export failed");
    const link = document.createElement("a");
    link.href = URL.createObjectURL(await response.blob());
    link.download = `${state.project.name.replace(/[^\w .-]+/g, "_").trim() || "project"}.3mf`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 10000);
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.disabled = false;
  }
}

async function importFiles() {
  $("dlg-import").close();
  const progress = $("upload-progress"), bar = progress.querySelector(".job-upload-bar");
  progress.hidden = false;
  for (const file of state.importFiles) {
    bar.style.width = "0%";
    try {
      const params = new URLSearchParams({ filename: file.name, scale: $("import-scale").value, up: $("import-up").value, plate: state.plate });
      const project = await upload(`/api/projects/${state.project.id}/models/upload?${params}`, file, pct => { bar.style.width = `${pct}%`; });
      applyProject(project);
      renderAll();
      showImportNotes(project);
    } catch (err) {
      toast(`${file.name}: ${err.message}`, true);
    }
  }
  progress.hidden = true;
  schedulePoll();
}

boot();
