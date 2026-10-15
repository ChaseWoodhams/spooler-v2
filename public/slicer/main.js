/* Spooler project workspace (slicer.html): one page from a model upload to a finished print.
   Model (analysis, units) → Split (cuts, parts) → Plates (parts packed onto plates; move, merge, settings for the
   project, a plate or an object) → Slice (every plate, layer preview) → Print (send the plates, watch the Jobs, start
   them). Projects without an uploaded model start at Plates. Talks to /api/projects, /api/splits and /api/jobs. */
import * as orient from "./orient.js?v=2";
import { SettingsPanel } from "./settings.js?v=5";
import { splitStep } from "./split-step.js?v=5";
import { projectPage } from "./project-page.js?v=10";
import { newProject } from "./new-project.js?v=10";
import { importWithReview, isReviewed, showRecords } from "./import-review.js?v=2";
import { showExport } from "./export-plan.js?v=2";
import { Viewport, featureColor } from "./viewport.js?v=5";
import { reviewHtml, reviewTitle, blockers, acknowledgements } from "./review.js?v=1";

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clone = v => JSON.parse(JSON.stringify(v));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const listNames = names => names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names.at(-1)}` : names[0] || "";

const STEPS = [["model", "Model"], ["split", "Split"], ["plates", "Plates"], ["slice", "Slice"], ["print", "Print"]];
const OPEN_JOB_STATES = new Set(["queued", "starting", "printing"]);

const state = {
  project: null,
  plate: 1,
  selected: null,
  step: "plates",
  mode: "prepare",
  settingsTab: "process",
  reviewContext: null,
  reviewQuery: { plate: null, search: "", changed: false, object: "" },
  schema: null,
  bundles: [],
  bundleSettings: new Map(),
  saveTimer: null,
  saving: false,
  pendingSave: false,
  saveError: null,
  poller: null,
  preview: null,        // { key, analysis, buffer }
  playing: null,        // { frame } while the preview plays the print
  hiddenFeatures: new Set(),
  problems: new Map(),
  group: [],            // the other selected objects (Shift-click, Ctrl+A) besides state.selected
  serverProblems: [],   // the server's list of problem objects (GET /status): off the bed, over the purge area, overlaps, failed model
  problemReasons: {},   // plate id → why the server says that plate has a problem
  jobs: [],             // the Jobs sent from this project
  printers: [],
};

// ── Helpers ─────────────────────────────────────────────────────────────────────
function toast(message, error = false) {
  const el = document.createElement("div");
  el.className = `toast${error ? " toast-error" : ""}`;
  el.textContent = message;
  $("toast-area").appendChild(el);
  setTimeout(() => el.remove(), error ? 6000 : 3500);
}

// Every wait is bounded (net.js fetchWithTimeout): reads give up after 20 s and retry once; writes get 2 minutes because
// planning, packing and arranging compute on the server, and are never retried.
async function api(method, path, body, { timeoutMs = method === "GET" ? 20000 : 120000 } = {}) {
  const response = await fetchWithTimeout(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  }, { timeoutMs, retries: 1 });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (HTTP ${response.status})`);
    error.status = response.status;
    error.issues = data.issues || [];
    throw error;
  }
  return data;
}

async function binary(path) {
  const response = await fetchWithTimeout(path, {}, { timeoutMs: 60000, retries: 1 });
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

const data = () => state.project.data;
const plateById = id => data().plates.find(p => p.id === id) || null;
const currentPlate = () => plateById(state.plate);
const plateObjects = (plateId = state.plate) => data().objects.filter(o => o.plate === plateId);

// One definition of a problem object: the server's list (loadProblems). The viewport's live measure only adds to it while an
// object is being dragged or its move is not saved yet, so the step bar never disagrees with what the server will refuse.
function objectIssues(objectId) {
  const issues = state.serverProblems.filter(p => p.key === objectId).map(p => p.problem);
  const moving = state.viewport?.transform?.dragging || state.saving || state.saveTimer || state.pendingSave;
  const live = moving ? state.problems.get(objectId) : null;
  return live ? [...new Set([...issues, ...live.issues])] : issues;
}
// "Too close" is a warning: it marks the object amber and does not stop a Slice or a send; every other problem does
const SOFT_ISSUES = new Set(["too close"]);
const hardIssues = objectId => objectIssues(objectId).filter(issue => !SOFT_ISSUES.has(issue));
const plateHasProblem = plateId => plateObjects(plateId).some(o => hardIssues(o.id).length);
const anyProblem = () => state.project ? data().objects.some(o => hardIssues(o.id).length) : false;

async function loadProblems() {
  if (!state.project) return;
  const revision = state.project.revision;
  let status = null;
  try {
    status = await api("GET", `/api/projects/${state.project.id}/status`);
  } catch { /* the viewport's own measure still marks what it can */ }
  if (!state.project) return;
  if (state.project.revision !== revision) { loadProblems(); return; }   // edited meanwhile: this answer is for an older layout, ask again
  state.serverProblems = status ? status.plates.flatMap(p => p.problems || []) : [];
  state.problemReasons = status ? Object.fromEntries(status.plates.map(p => [p.id, p.problem_reason])) : {};
  const byObject = new Map();
  for (const p of state.serverProblems) byObject.set(p.key, [...(byObject.get(p.key) || []), p.problem]);
  state.viewport?.setServerIssues(byObject);
  renderObjects();
  renderSteps();
  renderSliceState();
}

// Select the first problem object (this plate's first) so the person sees what to fix.
async function showProblem() {
  const mine = plateObjects().find(o => hardIssues(o.id).length);
  const target = mine || data().objects.find(o => hardIssues(o.id).length);
  if (!target) return;
  if (state.step !== "plates") await setStep("plates");
  if (target.plate !== state.plate) await selectPlate(target.plate);
  selectObject(target.id);
  state.viewport?.fit(state.viewport.objectBox(target.id));
}
const selectedObject = () => data().objects.find(o => o.id === state.selected) || null;
// The selection is the selected object plus state.group, the others picked with Shift or Ctrl; only this plate's objects count
const selectionIds = () => {
  const here = new Set(plateObjects().map(o => o.id));
  return [state.selected, ...state.group].filter(id => id != null && here.has(id));
};
const selectedObjects = () => selectionIds().map(id => data().objects.find(o => o.id === id));
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
    seen.set(choice ? `${choice.preset}\n${choice.colour || ""}\n${choice.snapshot?.sha256 || ""}` : "", choiceLabel(choice));
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

// ── Loading ─────────────────────────────────────────────────────────────────────
async function boot() {
  document.querySelectorAll("[data-steps], #prepare-tools").forEach(el => { el.hidden = true; });  // shown once a project is open
  newProject.attach({ api, upload, toast, openCreated, importNotes: showImportNotes });
  bindFolds();
  bindPack();
  bindUi();   // first, so "Open or create a project" works even if the slower setup below fails
  const viewport = $("viewport");
  try {
    state.viewport = new Viewport(viewport, {
      onSelect: (id, how) => selectObject(id, how),
      onFace: face => faceClicked(face),
      onTransform: (id, change) => transformSelection(id, change),
      onProblems: problems => { state.problems = problems; renderObjects(); renderSteps(); renderSliceState(); },
    });
    state.viewport.setEditable(!document.body.classList.contains('defer-edit'));
    state.applySnap?.();
  } catch (err) {
    showMessage(`The 3D view isn't available in this browser: ${err.message}`);
  }
  const printer = printerPrefs();
  $("printer-shell").value = printer.shell;
  state.viewport?.setShell(printer.shell);
  showPrinter(printer.shown, { save: false });
  state.settings = new SettingsPanel({
    tabs: $("settings-tabs"), pages: $("settings-pages"), body: $("settings-body"), search: $("settings-search"),
    mode: $("settings-mode"), onResolve: resolveSavedSetting, onChange: changeSetting, onReset: resetSettings, onTab: tab => { state.settingsTab = tab; renderSettings(); },
  });
  projectPage.attach({
    api, toast,
    projectId: () => state.project.id,
    sliceState: plateId => sliceEntry(plateId)?.state,
    onSlice: plateId => requestPlateSlice(plateId),
    onOpenPlate: plateId => { selectPlate(plateId); setStep("plates"); },
    onOpenSlice: () => setStep("slice"),
    hasAssembly: () => !!splitIdsOf(state.project).length,
    onOpenAssembly: () => openAssemblyGuide().catch(err => toast(err.message, true)),
    sendPlates: (ids, options) => queuePlates(ids, options),
    balance: ids => balancePlates(ids.map(plateById)) ? Object.fromEntries(ids.map(id => [id, plateById(id).printer_id])) : null,
    plateFilament: plateId => plateFilamentLabel(plateId),
    bedName: () => data().bed === 'smooth' ? 'Smooth PEI' : 'Textured PEI',
    spoolNote: () => { const spool = state.spools?.find(s => s.id === data().profile.spool_id); return spool ? `Spool: ${spoolLabel(spool)}` : "No spool assigned; using preset values"; },
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
    onChanged: split => { renderSteps(); renderStepActions(); landOnSplit(split); },
  });
  $("settings-mode").value = remember("slicer.mode") || (remember("slicer.advanced") === "1" ? "advanced" : "simple");
  $("settings-mode").addEventListener("change", e => { remember("slicer.mode", e.target.value); renderFoldSummaries(); });
  $("review-effective-settings").addEventListener("click", () => reviewEffectiveSettings());
  $("effective-settings-review").addEventListener("input", event => {
    if (event.target.matches("[data-review-search]")) updateReviewQuery({ search: event.target.value });
  });
  $("effective-settings-review").addEventListener("change", event => {
    if (event.target.matches("[data-review-changed]")) updateReviewQuery({ changed: event.target.checked });
    if (event.target.matches("[data-review-scope]")) updateReviewQuery({ object: event.target.value });
  });
  $("effective-settings-review").addEventListener("click", event => {
    const control = event.target.closest('[data-control]');
    if (control) { openSettingControl(JSON.parse(control.dataset.control)); return; }
    const button = event.target.closest('[data-resolve]');
    if (button) resolveSavedSetting(JSON.parse(button.dataset.target), button.dataset.setting, button.dataset.resolve, button.dataset.destination);
  });
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
  const params = new URLSearchParams(location.search);
  const asked = Number(params.get("project")) || 0;
  if (asked) {
    try {
      await openProject(asked, { toPrinter: !!params.get("printer"), toPrint: stepAliases(params.get("step")) === "print" || !!params.get("plate") });
      return;
    } catch (err) {
      // A deleted or hidden project falls through to the Projects page. Anything else (Spooler busy or restarting) says so,
      // rather than silently showing the page as if the project were gone.
      if (err.status !== 404 && !err.quick) toast(`Couldn't open project #${asked}: ${err.message}. Choose it below, or reload to try again`, true);
    }
  }
  await showProjectsPage();
  if (params.has("new")) openProjectsDialog();   // the header's New project: the form over the page
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

const spoolLabel = s => `${s.name}${s.material ? ` · ${s.material}` : ''}${s.remaining_weight != null ? ` · ${Math.round(s.remaining_weight)} g` : " · Remaining unavailable"}${s.location ? ` · on ${s.location}` : ""}`;

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
  note.hidden = !spool && !state.spoolError && !chosen;
  if (spool) {
    const short = need && spool.remaining_weight != null && spool.remaining_weight < need * 1.05;
    note.className = `slicer-muted${short ? " warn" : ""}`;
    note.textContent = `${spoolLabel(spool)}` + (need ? ` · the sliced plates need about ${Math.round(need)} g${short ? " — not enough" : ""}` : "") + (state.spoolError ? ` · Spoolman unavailable: ${state.spoolError}. Showing the last loaded inventory; reconnect before trusting the assignment.` : '');
  } else if (!note.hidden) {
    note.textContent = state.spoolError ? `Spoolman unavailable: ${state.spoolError}. Reopen the Spool selector to retry. Manual preset values remain available.` : `Spool #${chosen} is not in the current inventory. Check its assignment before slicing.`;
  }

  const plate = currentPlate();
  const printer = plate?.printer_id || "";
  $("plate-printer-select").innerHTML = '<option value="">Any printer</option>'
    + state.printers.map(p => `<option value="${esc(p.id)}"${p.id === printer ? " selected" : ""}>${esc(p.name)}${p.connected ? "" : " (offline)"}</option>`).join("");
  const name = state.printers.find(p => p.id === printer)?.name;
  const button = $("btn-slice-send");
  button.textContent = name ? `Slice & send to ${name}` : "Slice & send";
  button.disabled = !plateObjects().length || !data().profile.bundle_id || isBusy(state.plate) || plateImporting(state.plate);
  if (state.step === "slice") renderStepActions();
  renderQuality();
  refreshFit();
}

const WARNING_ICON = '<svg class="fit-icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false"><path d="M8 1.5 15 14H1z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M8 6v4M8 11.5v1" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
// Each distinct warning once; "No spool is assigned" is a labelled warning that says it only informs and links to where a spool is assigned
function fitWarningsHtml(fit) {
  const seen = new Set();
  return fit.warnings.filter(w => !seen.has(w.text) && seen.add(w.text)).map(w => {
    if (!/^No spool is assigned to /.test(w.text)) return `<div class="fit ${esc(w.level)}">${esc(w.text)}</div>`;
    const href = fit.printer_id ? `/#printer=${encodeURIComponent(fit.printer_id)}` : "/";
    return `<div class="fit ${esc(w.level)} fit-labelled">${WARNING_ICON}<span><strong>Warning:</strong> ${esc(w.text)}. This does not block sending. <a href="${href}" target="_blank" rel="noopener">Assign a spool</a></span></div>`;
  }).join("");
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
    box.innerHTML = fitWarningsHtml(fit);
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
  renderFarm();   // the selector keeps showing the saved spool until the review is applied
  await openReview({ source: "spool", spool_id: id });
}

// ── Reviewed preset and spool changes ─────────────────────────────────────────────
// A newer preset or a Spoolman spool only reaches the Project through a server-side preview and an explicit,
// revision-checked apply; cancelling leaves everything as it was.
const review = { body: null, preview: null };

async function openReview(body) {
  if (!state.project) return;
  try {
    await flushSave();   // review the saved Project, never half-typed edits
    review.body = body;
    review.preview = await api("POST", `/api/projects/${state.project.id}/settings/review`, body);
  } catch (err) {
    toast(err.message, true);
    renderFarm();
    return;
  }
  renderReview();
  if (!$("dlg-review").open) $("dlg-review").showModal();
}

function renderReview(error = null) {
  const preview = review.preview;
  $("review-title").textContent = reviewTitle(preview);
  $("review-body").innerHTML = reviewHtml(preview, { error });
  updateReviewApply();
}

function updateReviewApply() {
  const preview = review.preview;
  const accepted = [...$("review-body").querySelectorAll("[data-accept]")].every(box => box.checked);
  $("btn-review-apply").disabled = preview.unchanged || blockers(preview).length > 0 || !accepted;
}

async function applyReview() {
  const preview = review.preview;
  const accept = {};
  for (const issue of acknowledgements(preview)) accept["accept_" + issue.code.replace(/-/g, "_")] = true;
  $("btn-review-apply").disabled = true;
  try {
    const project = await api("POST", `/api/projects/${state.project.id}/settings/adopt`,
      { ...review.body, revision: preview.revision, token: preview.token, ...accept });
    const before = editLog.current;
    applyProject(project);
    await loadBundles();
    await bundleSettings(data().profile.bundle_id);
    if (before && before !== JSON.stringify(data())) { editLog.undo.push(before); editLog.redo = []; }   // one ordinary, undoable edit
    editLog.current = JSON.stringify(data());
    if (review.body.source === "spool") rememberDefaults({ spool: review.body.spool_id, profile: data().profile.bundle_id });
    $("dlg-review").close();
    $("save-state").textContent = "Saved";
    renderAll();
    toast(review.body.source === "spool" ? "Spool properties applied" : "Preset update applied");
  } catch (err) {
    if (err.status === 409 && !err.issues.length) {   // the Project or the source changed since the preview: show the new one
      try {
        review.preview = await api("POST", `/api/projects/${state.project.id}/settings/review`, review.body);
        renderReview(`${err.message}. The review below is current.`);
        return;
      } catch (again) { err = again; }
    }
    renderReview(err.message);
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
  toast("Slicing — it will be sent to the printer when it's done");
}

// The Slice step's send: a current Slice goes to its printer as it is; an outdated or missing one is sliced first.
async function sendThisPlate() {
  const plate = state.plate;
  if (!await confirmDialog({
    title: `${sendThisPlateState().label}?`,
    body: "This creates a Job for the printer. Start it there when the bed is clear.",
    confirmLabel: "Send Plate",
  })) return;
  if (!isUpToDate(plate)) return sliceAndSend([plate]);
  if (!await queuePlates([plate], {})) return;   // the same send as Print: saves first and remembers the choices
  rememberDefaults({ printer: currentPlate()?.printer_id || null });
  await loadJobs();
  toast("Sent to the printer");
}

async function bundleSettings(bundleId) {
  if (!bundleId) return null;
  if (!state.bundleSettings.has(bundleId)) state.bundleSettings.set(bundleId, await api("GET", `/api/slicer/profiles/${bundleId}/settings`));
  return state.bundleSettings.get(bundleId);
}

// "Opening project…" while the project's data is fetched, with an in-place Retry when that fails.
// outcome: undefined = loading, null = done, an Error = failed.
function showOpening(id, options, outcome) {
  const box = $("project-opening");
  box.hidden = outcome === null;
  if (outcome === null) stateBlock(box);
  else if (outcome) stateBlock(box, "error", `Couldn't open this project: ${outcome.message}`, () => openProject(id, options).catch(err => toast(err.message, true)), "Retry opening");
  else stateBlock(box, "loading", "Opening project…");
}

async function openProject(id, { toPrinter = false, toPrint = false, restoring = false } = {}) {
  const options = { toPrinter, toPrint, restoring };
  showOpening(id, options);
  let project;
  try {
    project = await api("GET", `/api/projects/${id}`);
  } catch (err) {
    showOpening(id, options, err.status === 404 ? null : err);
    throw err;
  }
  showOpening(id, options, null);
  if (restoring && (project.quick || project.draft)) throw Object.assign(new Error("A hidden quick project is never reopened on its own"), { quick: true });   // the caller falls back to the list
  document.body.classList.remove("view-dashboard");
  $("projects-dashboard").hidden = true;
  state.clockSkew = project.server_time - Date.now() / 1000;
  state.project = project;
  state.plate = project.data.plates[0].id;
  state.selected = null;
  state.preview = null;
  state.viewport?.clearGeometries();   // model ids restart at 1 in every project: never reuse another project's shapes
  state.loadedModels = new Map();
  resetEdits();
  state.jobs = [];
  if (!project.quick && !project.draft) remember("slicer.project", String(id));   // a hidden quick or draft project is never restored as the last-open project
  history.replaceState(null, "", `?project=${id}`);
  syncShellRoute(id);
  await bundleSettings(project.data.profile.bundle_id).catch(err => toast(err.message, true));
  if (splitIdsOf(project).length) await splitStep.open(unplacedSplitIds(project)[0] ?? splitIdsOf(project)[0]).catch(err => toast(err.message, true));
  else splitStep.close();
  await loadJobs({ quiet: true });
  renderAll();
  // Print absorbed the old Queue step: a remembered "queue", a ?step=queue link and a push link (?printer=) all land on Print.
  const saved = stepAliases(remember(`slicer.step.${id}`));
  const landing = (toPrinter || toPrint) && stepAvailable("print") ? "print" : saved && stepAvailable(saved) ? saved : defaultStep();
  await setStep(landing, { force: true });   // the steps and sidebar are up now; the 3D models stream in behind a loading block
  loadProblems();
  schedulePoll();
  maybeAutoPlan();
  loadOpeningGeometry(id);
}

// Fetch the project's 3D models after it is shown: a large project is never a blank, dead page while they download.
async function loadOpeningGeometry(id) {
  const message = $("viewport-message");
  const pending = state.project?.models.filter(m => m.state === "ready").length;
  if (pending) stateBlock(message, "loading", "Loading the 3D models…"), message.hidden = false;
  try {
    const failed = await loadGeometry();
    if (state.project?.id !== id) return;
    if (failed) {
      message.hidden = false;
      stateBlock(message, "error", `${plural(failed, "model")} didn't load.`, () => loadOpeningGeometry(id), "Retry models");
      return;
    }
    stateBlock(message);
    message.hidden = true;
    state.viewport?.fit(state.viewport.objectsBox());
  } catch (err) {
    toast(err.message, true);
  }
}

// Inside the app shell, record the project now open in the shell's address and frame route. Otherwise the shell still
// names the project this page first opened, and returning to Projects (or reloading) reopens that one instead.
function syncShellRoute(id) {
  if (window.self === window.top) return;
  try {
    const frame = window.frameElement;
    const shown = Number(new URLSearchParams(parent.location.hash.slice(1)).get("projects")) || 0;
    if (!frame || shown === id) return;   // already right; keep a push link's &printer= as it is
    frame.dataset.route = `${id}|`;
    parent.history.replaceState(null, "", `#projects=${id}`);
  } catch (_) { /* not the Spooler shell */ }
}

// After a multi-file import the objects all sit on plate 1 at the same spot: once every model has finished importing (so the
// planner has real sizes), land on Plates and apply the plan (orient, space, split across plates) as one undo step. Only a plan with
// a problem (an object too big for any plate, no valid layout) opens the review sheet instead.
async function maybeAutoPlan() {
  const id = state.project?.id;
  let wanted = null;
  try { wanted = sessionStorage.getItem("slicer.autoplan"); } catch (_) { /* storage blocked */ }
  if (!id || wanted !== String(id)) return;
  if (state.project.models.some(m => m.state === "importing")) return;   // the poller calls this again as imports finish
  try { sessionStorage.removeItem("slicer.autoplan"); } catch (_) { /* storage blocked */ }
  if (!data().objects.length || $("dlg-plan").open) return;
  if (state.step !== "plates" && !unplacedSplitIds(state.project).length) await setStep("plates", { force: true });   // a split still to be cut keeps its step
  try {
    await flushSave();
    state.planTilt = false;
    const plan = await api("POST", `/api/projects/${id}/plan`, { allow_tilt: false });
    const clean = plan.plates.length && !plan.refused && !plan.problems.length;
    if (!clean) { await planPlates(); return; }   // no valid layout: show the review sheet
    // A part too big for any plate does not hold the rest back: it sits on a plate of its own and the toast says so.
    const result = await api("POST", `/api/projects/${id}/plan/accept`, { revision: plan.revision, fingerprint: plan.fingerprint, allow_tilt: false });
    applyProject(result);
    recordEdit();
    renderAll();
    state.viewport?.fit(state.viewport.objectsBox());
    toast(`Arranged automatically: ${result.plan.summary}. Undo puts it back`, !!(plan.too_big.length || plan.tippy?.length));
  } catch (err) {
    toast(`Couldn't arrange the models automatically: ${err.message}`, true);
  }
}

/** Ask maybeAutoPlan to plan this project's plates once its models have imported */
function requestAutoPlan(id) {
  try { sessionStorage.setItem("slicer.autoplan", String(id)); } catch (_) { /* storage blocked */ }
}

/** After Create with an oversized model the project opens on Model while that model is analysed and its cuts planned; the
    moment they are ready it moves on to Split, where the planned cuts wait (unless the user has gone elsewhere meanwhile) */
function landOnSplit(split) {
  if (!state.landOnSplit || !split || split.id !== state.landOnSplit) return;
  if (split.state === "failed") { state.landOnSplit = null; return; }
  if (split.analysis && split.state === "ready") {
    state.landOnSplit = null;
    if (state.step === "model" && stepAvailable("split")) setStep("split");
  }
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
  let failed = 0;
  for (const model of ready) {
    try {
      state.viewport.addModelGeometry(model.id, await binary(`/api/projects/${state.project.id}/models/${model.id}/preview`));
      state.loadedModels.set(model.id, model.sha256);
    } catch (err) {
      failed++;
      toast(err.message, true);
    }
  }
  renderViewport();
  return failed;
}

// ── Steps ───────────────────────────────────────────────────────────────────────
// A project can own several splits (one per oversized file); each is laid out, or placed whole, on its own.
const splitIdsOf = project => project.split_ids || (project.split_id ? [project.split_id] : []);
const unplacedSplitIds = project => splitIdsOf(project).filter(id => !project.models.some(m => m.split?.id === id));

function stepAvailable(step) {
  const project = state.project;
  if (!project) return false;
  const split = splitIdsOf(project).length ? splitStep.split : null;
  if (isGcodeProject() && step !== "print") return false;   // an imported G-code Plate goes straight to Print
  switch (step) {
    case "model":  return !!splitIdsOf(project).length;
    case "split":  return !!split?.analysis;
    case "plates": return !splitIdsOf(project).length || data().objects.length > 0;
    case "slice":  return data().objects.length > 0;
    case "print":  return data().objects.length > 0 || state.jobs.length > 0 || isGcodeProject();   // with nothing sliced, Print says to slice first
  }
  return false;
}

// What unlocks a locked step, in words; "" when the step is open
function stepLockedReason(step) {
  if (stepAvailable(step)) return "";
  switch (step) {
    case "split":  return "The model check must finish first";
    case "plates": return "Split the model first";
    case "slice":  return "Put the model on a plate first";
    case "print":  return "Put the model on a plate first";
  }
  return "Not available for this project";
}

function stepDone(step) {
  const split = splitStep.split;
  switch (step) {
    case "model":  return !!split?.analysis;
    case "split":  return !!split?.result || data().objects.some(o => state.project.models.find(m => m.id === o.model)?.split?.part === 0);
    case "plates": return data().objects.length > 0 && !anyProblem();   // no check mark while a problem object remains
    case "slice":  return platesWithObjects().length > 0 && platesWithObjects().every(p => isUpToDate(p.id));
  }
  return false;   // Print is never "done": the project page shows how far along it is
}

// The Queue step was folded into Print; its old names still arrive from remembered steps and links.
const stepAliases = step => step === "queue" ? "print" : step;

function defaultStep() {
  if (isGcodeProject()) return "print";
  if (unplacedSplitIds(state.project).length || splitIdsOf(state.project).length && !data().objects.length) return splitStep.split?.result ? "split" : "model";
  if (!data().objects.length) return "plates";
  if (state.jobs.length) return "print";   // something was sent: the Project page is where it stands
  return platesWithObjects().every(p => isUpToDate(p.id)) ? "print" : "plates";   // every Plate sliced: the next thing to do is send them, not slice again
}

async function setStep(step, { force = false } = {}) {
  if (!state.project || (!force && !stepAvailable(step))) return;
  if (!stepAvailable(step)) step = isGcodeProject() ? "print" : stepAvailable("plates") ? "plates" : "model";
  await flushSave();
  if (state.step !== step) document.querySelector(".slicer-sidebar").scrollTop = 0;
  state.step = step;
  remember(`slicer.step.${state.project.id}`, step);
  const splitView = step === "model" || step === "split";
  const pageView = step === "print";
  document.body.classList.toggle("print-step", pageView);
  document.body.dataset.workflowStep = step;
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
  if (pageView) loadPrinters();   // the send bar's destinations and balancing read the current printers
  if (!splitView && !pageView) {
    state.viewport?.resize();
    await setMode(step === "slice" && sliceEntry()?.state === "sliced" ? "preview" : "prepare");
  }
  renderAll({ geometry: !splitView && !pageView });
  // The camera follows its target: a different mode or plate than the last fit re-fits (the bed no longer sits small and low after Slice -> Plates)
  if (!splitView && !pageView && state.mode === "prepare") {
    const target = `prepare:${state.project.id}:${state.plate}`;
    if (state.fitTarget !== target) { state.fitTarget = target; state.viewport?.fit(state.viewport.objectsBox()); }
  }
}

function renderSteps() {
  if (!state.project) { $("step-bar").innerHTML = ""; return; }
  const steps = STEPS.filter(([id]) => isGcodeProject() ? id === "print" : splitIdsOf(state.project).length || (id !== "model" && id !== "split"));
  $("step-bar").innerHTML = steps.map(([id, label], index) => {
    const skipped = id === "split" && splitSkipped();   // a model that fits the bed skips Split, but the step stays openable
    const reason = stepLockedReason(id);
    return `${index ? '<span class="slicer-step-sep"></span>' : ""}
    <button class="slicer-step${id === state.step ? " active" : ""}${skipped ? " skipped" : stepDone(id) ? " done" : ""}" type="button" data-step="${id}"${id === state.step ? ' aria-current="step"' : ''}
      ${reason ? ` aria-disabled="true" aria-describedby="step-why-${id}" title="${esc(reason)}"` : ""}${skipped ? ' title="This model fits the bed, so it was put on a plate whole. Open Split to cut it anyway."' : ""}><span class="num">${skipped ? "–" : stepDone(id) && id !== state.step ? "✓" : index + 1}</span>${label}${skipped ? "<small> not needed</small>" : ""}${reason ? `<span class="sr-only" id="step-why-${id}">Unavailable. ${esc(reason)}</span>` : ""}</button>`;
  }).join("");
}

function renderStepActions() {
  const el = $("step-actions");
  if (!state.project) { el.innerHTML = ""; return; }
  const next = (step, label) => `<button class="btn btn-primary" type="button" data-go="${step}"${stepAvailable(step) ? "" : " disabled"}>${label}</button>`;
  const send = sendThisPlateState();
  const fwd = state.step === "model" || state.step === "split" ? splitStep.forward() : null;
  const importing = importProgress();
  const progress = importing ? `<span class="slicer-import-progress" data-role="import-progress" role="status"><progress max="${importing.total}" value="${importing.done}" aria-label="Models imported"></progress> ${importing.done} of ${importing.total} models imported</span>` : "";
  el.innerHTML = progress + (fwd ? `<button class="btn btn-primary" type="button" data-split-forward="${fwd.action}"${fwd.why ? ' aria-disabled="true" aria-describedby="split-forward-why"' : ""}>${esc(fwd.label)}</button>`
        + (fwd.why ? `<small class="reason-line" id="split-forward-why">${esc(fwd.why)}</small>` : "") : "")
    + (state.step === "plates" ? next("slice", "Slice →")
    : state.step === "slice" ? next("print", "Print →") + `<button class="btn btn-secondary" type="button" data-send-this-plate${send.why ? ' aria-disabled="true" aria-describedby="send-this-plate-why"' : ""}>${esc(send.label)}</button>`
      + (send.why ? `<small class="reason-line" id="send-this-plate-why">${esc(send.why)}</small>` : "") : "");
  $("btn-slice-send").hidden = state.step === "slice";   // the Slice step has its own send in the header
}

// Models still importing onto Plates: how many of them are done
function importProgress() {
  const models = state.project?.models || [];
  const used = models.filter(m => data().objects.some(o => o.model === m.id));
  const importing = used.filter(m => m.state === "importing");
  return importing.length ? { done: used.length - importing.length, total: used.length } : null;
}

// The Slice step's secondary action (the forward action is "Print →"): send the selected Plate to its printer, or to the next free one when none is chosen.
// `why` is set only for a real blocker.
function sendThisPlateState() {
  const plate = currentPlate();
  const printer = state.printers.find(p => p.id === plate?.printer_id);
  const name = plate?.name || "this plate";
  const label = printer ? `Send ${name} to ${printer.name}` : `Send ${name} to the next free printer`;
  if (!plate) return { label, why: "" };
  const why = !plateObjects().length ? "This plate is empty"
    : plateImporting(plate.id) ? "The models on this plate are still importing"
    : anyProblem() ? (state.problemReasons[plate.id] || "Fix the objects marked in red first")
    : !data().profile.bundle_id ? "Choose a slicing profile first"
    : isBusy(plate.id) ? "This plate is being sliced" : "";
  return { label, why };
}

// ── Saving ──────────────────────────────────────────────────────────────────────
// Undo and redo keep snapshots of the project's data (objects, plates, profile changes). Rapid edits of the same kind
// (typing in a setting) share one step.
const editLog = { undo: [], redo: [], current: null, at: 0 };
const EDIT_LIMIT = 60;

function resetEdits() {
  state.saveError = null;
  state.settings?.pending.clear();
  state.settings?.localIssues.clear();
  state.settings?.setIssues([]);
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
    state.saveError = null;
    state.settings.setIssues([]);
    if (localChangedMeanwhile) state.project.revision = project.revision;
    $("save-state").textContent = "Saved";
    loadProblems();
  } catch (err) {
    state.saveError = err;
    state.settings.setIssues(err.issues);
    $("save-state").textContent = "Not saved";
    toast(err.message, true);
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
  if (state.saveError) throw state.saveError;
  if (state.settings.pending.size) throw new Error('Correct the invalid setting values before continuing');
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
      await refreshProject();
    } catch (_) { /* Spooler may be restarting */ }
    schedulePoll();
  }, 1500);
}

// Pull the project's latest state from Spooler: models, slices, and (when nothing local is unsaved) the whole layout.
// Runs on the poll while something is in progress, and whenever the page is shown again.
async function refreshProject() {
  if (!state.project) return;
  const id = state.project.id;
  const fresh = await api("GET", `/api/projects/${id}`);
  if (state.project?.id !== id) return;   // another project was opened meanwhile
  state.clockSkew = fresh.server_time - Date.now() / 1000;
  const before = state.project;
  const newModels = fresh.models.filter(m => m.state === "ready" && before.models.find(b => b.id === m.id)?.state !== "ready");
  const sliceChanged = JSON.stringify(fresh.slices) !== JSON.stringify(before.slices);
  const layoutChanged = !state.saving && !state.saveTimer && !state.saveError && !state.settings.pending.size && fresh.revision !== before.revision;
  state.project = { ...before, models: fresh.models, slices: fresh.slices };
  if (layoutChanged) applyProject(fresh);
  if (newModels.length || fresh.models.some((m, i) => m.state !== before.models[i]?.state)) loadProblems();   // a model failed or finished
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
  renderAll({ geometry: layoutChanged });   // a layout changed elsewhere (another tab, Quick print) moves the objects too
  maybeAutoPlan();
  schedulePoll();   // something may have started elsewhere: follow it
}

// Bring the page up to date when it is shown again: switching back to this tab, or to Projects in the app shell.
// Skipped while an edit is unsaved or a step is mid-request, so it never overwrites what is being changed here.
function refreshWhenShown() {
  if (!state.project) {   // the Projects page: its list may have moved on (a slice finished, another tab)
    if (document.body.classList.contains("view-dashboard") && !document.querySelector("dialog[open]")) loadProjectsPage();
    return;
  }
  if (state.saving || state.saveTimer || state.pendingSave) return;
  refreshProject().catch(() => { /* Spooler may be restarting; the poll or the next show tries again */ });
  loadJobs();
  loadPrinters();
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshWhenShown(); });
// The app shell asks before reloading for an update (net.js), and refreshes this page when Projects is shown again.
window.spoolerBusy = () => !!(state.saving || state.saveTimer || state.pendingSave || state.saveError || state.settings?.pending.size || document.querySelector("dialog[open]"));
window.spoolerRefresh = refreshWhenShown;
window.spoolerHome = showProjectsPage;   // the shell's Projects button: back to the page that lists them

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
  renderFoldSummaries();
}

// The folded sections say what is in them in one line, and remember whether they were open
const shortName = name => String(name || "").replace(/ @.*$/, "").replace(/^Elegoo /, "");
function renderFoldSummaries() {
  const text = id => { const o = $(id).selectedOptions?.[0]; return o && o.value ? shortName(o.textContent.replace(/ \*$/, "")) : ""; };
  $("material-summary").textContent = [text("preset-machine-select"), text("preset-filament-select"), text("preset-process-select"), text("bed-select").replace(/ \(Side .\)/, "")]
    .filter(Boolean).join(" · ") || "Choose a printer and profile";
  $("settings-summary").textContent = `${$("settings-mode").selectedOptions[0]?.textContent || "Simple"} · ${state.settingsTab === "object" ? "this object" : "project and plates"}`;
}

function bindFolds() {
  for (const fold of document.querySelectorAll(".slicer-fold")) {
    const saved = remember(`slicer.fold.${fold.id}`);
    if (saved) fold.open = saved === "1";
    fold.addEventListener("toggle", () => remember(`slicer.fold.${fold.id}`, fold.open ? "1" : "0"));
  }
}

// ── Quality ─────────────────────────────────────────────────────────────────────
// Fine / Standard / Draft … buttons: the process presets of the nozzle installed in the plate's printer
// (the machine preset's nozzle when the printer's isn't set), ordered by layer height.
const QUALITY_ORDER = ["Extra Fine", "Fine", "Optimal", "Standard", "Strength", "Draft", "Extra Draft"];
const nozzleOf = machine => /(\d\.\d+) nozzle$/.exec(machine || "")?.[1];

async function printerHardware(printerId) {
  if (!printerId) return null;
  state.hardware ||= {};
  if (!(printerId in state.hardware)) {
    state.hardware[printerId] = null;
    try { state.hardware[printerId] = await api("GET", `/api/printers/${encodeURIComponent(printerId)}/hardware`); } catch { /* keep null */ }
    renderQuality();
  }
  return state.hardware[printerId];
}

function qualityTarget() {
  const names = currentPresets();
  const printerId = currentPlate()?.printer_id;
  const saved = printerId ? state.hardware?.[printerId]?.nozzle_diameter : null;
  const nozzle = saved ? String(saved) : nozzleOf(names?.machine);
  const machine = state.presets?.machines.find(m => nozzleOf(m) === nozzle);
  return { names, nozzle, machine, fromPrinter: !!saved, printerId };
}

function renderQuality() {
  const box = $("quality-buttons"), note = $("quality-note");
  const { names, nozzle, machine, fromPrinter, printerId } = qualityTarget();
  if (printerId) printerHardware(printerId);
  const presets = machine && state.presets ? state.presets.processes[machine] || [] : [];
  const parsed = presets.map(name => {
    const [, height, tier] = /^(\d\.\d+)mm (.+?) @/.exec(name) || [];
    return { name, height, tier };
  }).filter(p => p.tier && p.tier !== "Strength")   // Strength changes walls and infill, not just the layer height
    .sort((a, b) => a.height - b.height);
  box.innerHTML = parsed.map(p => `<button type="button" data-quality="${esc(p.name)}" aria-pressed="${p.name === names?.process}"
    title="${esc(p.name)}">${esc(p.tier)}<small>${p.height} mm</small></button>`).join("");
  note.hidden = !nozzle;
  const material = printerId ? state.hardware?.[printerId]?.nozzle_material : null;
  note.textContent = nozzle ? `Layer heights for a ${nozzle} mm ${material || "nozzle"}`
    + (fromPrinter ? " · the installed nozzle of this plate's printer" : "")
    + (nozzle === "0.7" ? " · 0.7 mm profiles are scaled from the 0.8 mm ones" : "") : "";
}

async function chooseQuality(process) {
  const { names, machine } = qualityTarget();
  if (!names || !machine) return;
  if (machine !== names.machine) {   // the printer's nozzle differs from the project's: move to it first
    await choosePreset("machine", machine);
    if (currentPresets()?.machine !== machine) return;
  }
  await choosePreset("process", process);
}

// Pick another preset: the choice is locked in as a new profile; the other kinds keep their changes
async function choosePreset(kind, name) {
  const names = currentPresets();
  if (!names || names[kind] === name) return renderProfile();
  if (presetChanges(kind) && !await confirmDialog({
    title: `Switch the ${kind} preset to ${name}?`,
    body: `Your unsaved ${kind} changes are discarded.`,
    confirmLabel: "Switch preset",
    danger: true,
  })) return renderProfile();
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
  if (!await confirmDialog({
    title: `Delete the ${kind} preset “${names[kind]}”?`,
    body: "Projects already using it keep working.",
    confirmLabel: "Delete preset",
    danger: true,
  })) return;
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
  if (plateImporting(plateId)) return "busy";
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

const unverifiedKeys = entry => (entry.observed?.differences || []).slice(0, 3).map(d => d.key).join(", ") + ((entry.observed?.difference_count || 0) > 3 ? "…" : "");

function plateStatus(plateId) {
  const entry = sliceEntry(plateId);
  if (!plateObjects(plateId).length) return { text: "Empty", cls: "" };
  if (plateImporting(plateId)) return { text: "Importing models…", cls: "" };
  if (!entry) return { text: "Not sliced yet", cls: "" };
  if (entry.state === "queued") return { text: "Slice in progress — waiting for the slicer", cls: "" };
  if (entry.state === "slicing") return { text: `Slice in progress — ${slicingText(entry)}`, cls: "" };
  if (entry.state === "failed") return { text: `Failed Slice: ${entry.error || 'The slicer could not finish'}. Retry slicing this Plate.`, cls: "bad" };
  if (entry.outdated) return { text: "Outdated Slice — objects, placement or relevant settings changed. Slice this Plate again before sending it.", cls: "warn" };
  if (entry.observed?.state === "mismatch") return { text: `Unverified Slice — Orca's output differs from the expected settings (${unverifiedKeys(entry)}). Review the differences before sending.`, cls: "warn" };
  const text = ['Current Slice', duration(entry.estimate_s), entry.estimate_g != null ? `${Math.round(entry.estimate_g)} g` : "Filament estimate unavailable", plural(entry.layers, "layer")].filter(Boolean).join(" · ");
  const printer = state.printers.find(p => p.id === entry.sent_to)?.name;
  if (entry.send_error) return { text: `${text} · not sent: ${entry.send_error}`, cls: "warn" };
  if (entry.sent_job) return { text: `${text} · sent${printer ? ` to ${printer}` : ""}`, cls: "good" };
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
  $("brick-summary").textContent = project.enabled ? "On" : mode === "on" ? "On for this plate" : "Off";
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
      aria-pressed="${plate.id === state.plate}" title="${esc(plate.name)} · ${plural(count, "object")}${plateMix(plate.id) ? ` · ${esc(plateMix(plate.id))}` : ""}"><span class="dot ${plateDot(plate.id)}"></span>${esc(plate.name)} <small>${count}</small>${plateBadge(plate)}</button>`;
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
  const focused = document.activeElement?.closest('.slicer-object-select')?.closest('[data-object]')?.dataset.object;
  const models = state.project.models;
  const rows = plateObjects().map(object => {
    const model = models.find(m => m.id === object.model);
    const issues = objectIssues(object.id);
    const status = model?.state === "importing" ? '<small>importing…</small>'
      : model?.state === "failed" ? `<small class="bad" title="${esc(model.error || "")}">Couldn't import — ${esc(shortReason(model.error))}</small>`
      : issues.length ? `<small class="${issues.some(i => !SOFT_ISSUES.has(i)) ? "bad" : "warn"}">${esc(issues.join(", "))}</small>`
      : `<small>${(model?.size || []).map(v => Math.round(v * (object.scale[0] || 1))).join(" × ")} mm</small>`;
    const overrides = Object.keys(object.overrides || {}).length;
    const chosen = selectionIds().includes(object.id);
    return `<div class="slicer-object${chosen ? " selected" : ""}${model?.state === "failed" ? " failed" : ""}" data-object="${object.id}" draggable="true" title="Drag onto a plate tab to move it there. Shift-click adds to the selection.">
      <div class="slicer-object-name"><button type="button" class="slicer-object-select" aria-label="Select ${esc(object.name)}" aria-pressed="${chosen}">${esc(object.name)}</button><br>${status}</div>
      ${model?.state === "failed" ? `<button class="btn btn-secondary btn-sm slicer-retry-btn" type="button" data-retry-model="${model.id}" title="Try importing this model again">Retry</button>`
        : overrides ? `<span class="slicer-badge" title="Settings changed for this object">${overrides}</span>` : "<span></span>"}
      <button class="slicer-icon-btn" type="button" data-remove-object="${object.id}" title="Remove from the plate" aria-label="Remove ${esc(object.name)} from the plate">✕</button>
    </div>`;
  });
  $("object-list").innerHTML = rows.join("") || '<p class="slicer-muted">No objects on this plate. Add a model or drop STL files on the bed.</p>';
  if (focused) $('object-list').querySelector(`[data-object="${focused}"] .slicer-object-select`)?.focus();
}

function effectiveProcess(base) {
  return { ...(base?.process || {}), ...data().profile.process };
}

function renderSettings() {
  const review = $("effective-settings-review");
  if (review && state.reviewContext !== effectiveReviewContext()) review.innerHTML = "";
  if (!state.project || !state.schema) return;
  if (state.settingsTab === "object" && !$("fold-settings").open) $("fold-settings").open = true;
  const base = state.bundleSettings.get(data().profile.bundle_id);
  const plate = currentPlate();
  const object = state.step === "plates" ? selectedObject() : null;
  if (state.settingsTab === "object" && !object) state.settingsTab = "process";
  const objectsScope = state.settingsTab === "object";
  document.querySelectorAll("[data-scope-tab]").forEach(btn => {
    btn.setAttribute('aria-pressed', String((btn.dataset.scopeTab === 'objects') === objectsScope));
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
    state.settings.show({ tab: kind, tabs, layout: kind, base: base[kind], changes: data().profile[kind], target: { kind }, note,
      inheritance: { machine: base.machine, process: effectiveProcess(base) } });
  }
}

function effectiveReviewContext() {
  return state.project ? JSON.stringify([state.project.id, state.plate, state.project.revision, data()]) : null;
}

async function resolveSavedSetting(target, key, action, destination) {
  try {
    await flushSave();
    const projectId = state.project.id, revision = state.project.revision;
    const before = JSON.stringify(data());
    const body = {revision, scope: ['object','plate'].includes(target.kind) ? target.kind : 'project',
      kind: ['object','plate'].includes(target.kind) ? 'process' : target.kind, affected_id: target.id, key, action, destination};
    let project;
    try { project = await api('POST', `/api/projects/${projectId}/settings/resolve`, body); }
    catch (error) {
      const collision = error.issues?.find(i => i.code === 'setting-move-collision');
      if (!collision || !confirm(`${key}: ${destination} currently has ${JSON.stringify(collision.destination_value)}. Replace it with ${JSON.stringify(collision.value)}?`)) throw error;
      project = await api('POST', `/api/projects/${projectId}/settings/resolve`, {...body, replace:true});
    }
    if (state.project.id !== projectId) return;
    const newerLocalEdits = before !== JSON.stringify(data());
    applyProject(project, {keepLocalData:newerLocalEdits});
    if (newerLocalEdits) {
      state.project.revision = project.revision;
      toast('The setting was resolved. Review your newer unsaved edits before saving.', true);
    } else {
      recordEdit();
      state.settings.setIssues([]);
      $("save-state").textContent = 'Saved';
      renderAll();
      await reviewEffectiveSettings();
    }
  } catch (error) { toast(error.message, true); }
}

function settingResolutionButtons(issue) {
  if (!issue.resolution_required || !['project','plate','object'].includes(issue.scope) || !issue.key) return '';
  const target = {kind: issue.scope === 'project' ? issue.kind : issue.scope, id:issue.affected_id};
  const attrs = `data-target="${esc(JSON.stringify(target))}" data-setting="${esc(issue.key)}"`;
  const destinations = issue.scope === 'object' ? (issue.valid_scopes || []) : issue.scope === 'plate' ? ['project'] : [];
  return `<button type="button" data-resolve="remove" ${attrs}>Remove saved override</button>` +
    destinations.map(d => `<button type="button" data-resolve="move" data-destination="${d}" ${attrs}>Move to ${d === 'plate' ? 'Plate' : 'Project'}</button>`).join('');
}

// The server owns precedence and every row below. Review presents its captured response; search, the changed filter and
// the Object drill-down are requests to the server, never browser-side merges.
const REVIEW_SECTIONS = [["material", "Material"], ["nozzle", "Nozzle"], ["bed", "Bed"], ["temperature", "Temperature"], ["cooling", "Cooling"],
  ["flow", "Flow"], ["retraction", "Retraction"], ["other", "Other process and filament values"], ["machine", "Machine (read-only)"]];
const SOURCE_NAMES = { preset: "preset", "alternate-preset": "alternate Filament", project: "project", plate: "plate", object: "object",
  bed: "bed selection", bricklayers: "BrickLayers", "orca-default": "Orca default" };

function reviewValue(v) {
  if (v === null || v === undefined || v === "") return "—";
  return Array.isArray(v) ? v.join(", ") : String(v);
}

function reviewRow(r) {
  const notes = [];
  if (r.enforced) notes.push(`<span>Enforced: ${esc(r.enforced_reason || SOURCE_NAMES[r.source] || r.source)}.</span>`);
  if (r.note) notes.push(`<span>${esc(r.note)}</span>`);
  if (r.inactive) notes.push(`<span>Inactive: ${esc(r.inactive_reason)} Stored value is retained.</span>`);
  if (r.read_only && !r.enforced) notes.push(`<span>Read-only: ${esc(r.read_only_reason || "")}</span>`);
  if (r.reset_to) notes.push(`<span>Reset returns to ${r.reset_to.value == null ? "the Orca default" : esc(reviewValue(r.reset_to.value))} (${esc(SOURCE_NAMES[r.reset_to.source] || r.reset_to.source)}).</span>`);
  if (r.control) notes.push(`<button type="button" class="btn btn-secondary btn-sm" data-control="${esc(JSON.stringify(r.control))}" aria-label="Go to ${esc(r.label)} control">Edit</button>`);
  const chain = (r.chain || []).map(c => SOURCE_NAMES[c.source] || c.source).join(" › ");
  return `<tr><th scope="row">${esc(r.label)}${r.label !== r.key ? ` <small>${esc(r.key)}</small>` : ""}</th>
    <td>${esc(reviewValue(r.has_inherited ? r.inherited : null))}</td><td>${esc(reviewValue(r.requested))}</td><td>${esc(reviewValue(r.effective))}</td>
    <td>${esc(SOURCE_NAMES[r.source] || r.source)}${chain ? ` <small>${esc(chain)}</small>` : ""}</td><td>${notes.join(" ")}</td></tr>`;
}

function reviewResults(result) {
  const review = result.review;
  const bySection = new Map();
  for (const r of review.rows) bySection.set(r.section, [...(bySection.get(r.section) || []), r]);
  const material = review.rows.find(r => r.key === "filament_type");
  const nozzle = review.rows.find(r => r.key === "nozzle_diameter");
  const bed = review.rows.find(r => r.key === "curr_bed_type");
  const issues = (result.issues || []).map(i => `<div class="slicer-warning">${esc(i.scope || "")} ${esc(i.affected_id || "")}: ${esc(i.message)}${i.affected_names?.length ? ` — Objects: ${esc(i.affected_names.join(", "))}` : ""}${"value" in i ? ` Saved value: ${esc(JSON.stringify(i.value))}.` : ""} ${settingResolutionButtons(i)}${i.control && ["project", "plate", "object"].includes(i.control.scope) ? `<button type="button" class="btn btn-secondary btn-sm" data-control="${esc(JSON.stringify(i.control))}" aria-label="Go to ${esc(i.key)} control">Go to control</button>` : ""}${i.read_only ? " <small>Inherited and read-only: it is passed through unchanged and cannot be edited here.</small>" : ""}</div>`).join("");
  const sections = REVIEW_SECTIONS.filter(([id]) => bySection.has(id)).map(([id, title]) => `<details${id !== "machine" ? " open" : ""}><summary>${title} (${bySection.get(id).length})</summary>
    <table><thead><tr><th scope="col">Setting</th><th scope="col">Inherited</th><th scope="col">Requested</th><th scope="col">Effective</th><th scope="col">Source</th><th scope="col">Notes</th></tr></thead>
    <tbody>${bySection.get(id).map(reviewRow).join("")}</tbody></table></details>`).join("");
  return `<p><b>${esc(reviewValue(material?.effective))}</b> · Nozzle ${esc(reviewValue(nozzle?.effective))} mm · ${esc(reviewValue(bed?.effective))}</p>
    <p>${esc(review.filament_basis.explanation)}</p>${issues}
    <p class="slicer-muted">Changed means different from ${esc(review.changed_basis)}.</p>
    ${sections || "<p>No settings match.</p>"}
    <p>Object settings are sent separately for ${(result.objects || []).length} Objects.</p>`;
}

function effectiveReviewTitle(review) {
  return review.scope.kind === "object" ? `Settings sent to Orca — ${review.scope.name}` : "Settings sent to Orca";
}

function openSettingControl(control) {
  if (control.scope === "object") {
    if (!plateObjects().some(o => o.id === control.id)) { toast("That Object is no longer on this Plate", true); return; }
    state.settingsTab = "object";
    selectObject(control.id);
  } else state.settingsTab = control.scope === "plate" ? "plate" : control.kind === "filament" ? "filament" : "process";
  $("fold-settings").open = true;
  $("settings-search").value = control.key;
  renderSettings();
  $("settings-body").querySelector(`[data-key="${control.key}"]`)?.focus();
}

let reviewRequest = 0;
async function reviewEffectiveSettings({ partial = false } = {}) {
  const el = $("effective-settings-review");
  const ticket = ++reviewRequest;
  try {
    await flushSave();
    const projectId = state.project.id, plateId = currentPlate().id, revision = state.project.revision;
    if (state.reviewQuery.plate !== plateId) state.reviewQuery = { plate: plateId, search: "", changed: false, object: "" };
    const query = state.reviewQuery;
    if (!partial) el.textContent = "Reviewing…";
    const params = new URLSearchParams({ revision });
    if (query.search.trim()) params.set("search", query.search.trim());
    if (query.changed) params.set("changed", "1");
    if (query.object) params.set("object", query.object);
    const result = await api("GET", `/api/projects/${projectId}/plates/${plateId}/effective-settings?${params}`);
    if (ticket !== reviewRequest || state.project.id !== projectId || currentPlate().id !== plateId || state.project.revision !== revision) return;
    state.reviewContext = effectiveReviewContext();
    if (partial && el.querySelector("[data-review-results]")) {
      el.querySelector("[data-review-title]").textContent = effectiveReviewTitle(result.review);
      el.querySelector("[data-review-results]").innerHTML = reviewResults(result);
      return;
    }
    const objects = plateObjects(plateId);
    el.innerHTML = `<h4 data-review-title>${esc(effectiveReviewTitle(result.review))}</h4><p>Expected inputs for this ${result.review.scope.kind}, not measured output from a completed Slice.</p>
      <div class="slicer-review-controls" style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">
        <input type="search" data-review-search aria-label="Search reviewed settings" placeholder="Search labels or keys" value="${esc(query.search)}">
        <label><input type="checkbox" data-review-changed${query.changed ? " checked" : ""}> Changed values only</label>
        <label>Review scope <select data-review-scope aria-label="Review scope"><option value="">Plate</option>${objects.map(o => `<option value="${o.id}"${String(o.id) === String(query.object) ? " selected" : ""}>${esc(o.name)}</option>`).join("")}</select></label>
      </div>
      <div data-review-results>${reviewResults(result)}</div>`;
  } catch (err) { if (ticket === reviewRequest) el.textContent = err.message; }
}

let reviewTimer = null;
function updateReviewQuery(change) {
  state.reviewQuery = { ...state.reviewQuery, ...change };
  clearTimeout(reviewTimer);
  reviewTimer = setTimeout(() => reviewEffectiveSettings({ partial: true }), 150);
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
  const blocked = anyProblem();
  const reason = state.problemReasons[state.plate] || "Fix the objects marked in red first";
  const text = blocked && status.cls !== "bad" ? reason : `${currentPlate()?.name}: ${status.text}`;
  el.innerHTML = esc(text) + (blocked ? ' <button class="btn btn-secondary btn-sm" type="button" data-show-problem>Show</button>' : "");
  el.title = text;
  el.className = `slicer-slice-state ${blocked ? "bad" : status.cls}`;
}

function renderSlicing() {
  if (!state.project || state.step !== "slice") return;
  const plates = platesWithObjects();
  const todo = plates.filter(p => !isUpToDate(p.id) && !isBusy(p.id) && !plateImporting(p.id));
  const noProfile = !data().profile.bundle_id;
  $("slicing-actions").innerHTML = !plates.length ? "" : plates.every(p => isUpToDate(p.id))
    ? '<span class="slicer-all-sliced" role="status">✓ All plates sliced</span>'
    : `<button class="btn btn-primary btn-sm" type="button" data-slice-all${noProfile ? " disabled" : ""}${noProfile ? ' aria-describedby="slice-all-why"' : ""}>
    ${todo.length === plates.length ? `Slice all ${plural(plates.length, "plate")}` : `Slice ${plural(todo.length, "plate")}`}</button>${noProfile ? '<small id="slice-all-why" class="slicer-muted">Choose a slicing profile first</small>' : ""}`;
  $("slicing-list").innerHTML = plates.map(plate => {
    const status = plateStatus(plate.id);
    const again = sliceEntry(plate.id)?.state === "sliced" || sliceEntry(plate.id)?.state === "failed";
    return `<div class="slicer-plate-row${plate.id === state.plate ? " selected" : ""}" data-select-plate="${plate.id}">
      <span class="dot ${plateDot(plate.id)}"></span>
      <div><strong>${esc(plate.name)}</strong> ${plateBadge(plate)}<small class="${status.cls}">${plural(plateObjects(plate.id).length, "object")} · ${esc(status.text)}</small></div>
      <button class="btn btn-secondary btn-sm" type="button" data-slice-plate="${plate.id}"${isBusy(plate.id) || plateImporting(plate.id) ? " disabled" : ""}>
        ${again || isUpToDate(plate.id) ? "Slice again" : "Slice"}</button>
    </div>`;
  }).join("") || '<p class="slicer-muted">No plates with objects.</p>';
  const done = plates.filter(p => isUpToDate(p.id));
  const failed = plates.filter(p => sliceEntry(p.id)?.state === "failed");
  const seconds = done.reduce((sum, p) => sum + (sliceEntry(p.id).estimate_s || 0), 0);
  const grams = done.reduce((sum, p) => sum + (sliceEntry(p.id).estimate_g || 0), 0);
  const failedText = failed.length ? ` · ${failed.length} failed` : "";
  $("slicing-totals").textContent = !plates.length ? "" : done.length
    ? `${done.length} of ${plural(plates.length, "plate")} sliced${failedText} · ${duration(seconds)} of printing · ${Math.round(grams)} g of filament`
    : `0 of ${plural(plates.length, "plate")} sliced${failedText}`;
}

function plateFilamentLabel(id) {
  if (isImportedPlate(id)) return 'Imported G-code — verify material in Job review';
  const labels = new Set(plateObjects(id).map(o => {
    const choice = filamentChoice(o.model);
    return choice ? choiceLabel(choice) : currentPresets()?.filament || 'Project filament preset';
  }));
  return [...labels].join(', ') || currentPresets()?.filament || 'Imported G-code material';
}

/** Give the longest Plates first to the connected printer with the least estimated work in this Project; false (with a toast) when it cannot. Sets each Plate's printer; the `plates` list itself is left as it was. */
function balancePlates(plates) {
  const ready = state.printers.filter(p => p.connected);
  if (!ready.length) { toast('No connected printers. Choose assignments manually or reconnect a printer.', true); return false; }
  if (plates.some(p => sliceEntry(p.id)?.estimate_s == null)) { toast('Time estimates are missing. Assign destination printers manually or slice these Plates again.', true); return false; }
  const loads = new Map(ready.map(p => [p.id, state.jobs.filter(j => j.printer_id === p.id && OPEN_JOB_STATES.has(j.state)).reduce((sum,j) => sum + (j.estimate_s || 0),0)]));
  const longestFirst = [...plates].sort((a,b) => (sliceEntry(b.id)?.estimate_s || 0) - (sliceEntry(a.id)?.estimate_s || 0));
  for (const plate of longestFirst) {
    const printer = [...ready].sort((a,b) => loads.get(a.id) - loads.get(b.id))[0];
    plate.printer_id = printer.id;
    loads.set(printer.id, loads.get(printer.id) + (sliceEntry(plate.id)?.estimate_s || 0));
  }
  changed();
  return true;
}

function renderViewport() {
  const viewport = state.viewport;
  if (!viewport || !state.project) return;
  const base = state.bundleSettings.get(data().profile.bundle_id);
  viewport.setBed(bedLimits(base?.machine), data().bed === "smooth");
  viewport.setObjects(plateObjects(), state.project.models, state.selected);
  viewport.setGroup(state.group.filter(id => plateObjects().some(o => o.id === id)));
  describeViewport();
}

// The 3D canvas is a picture to assistive tech: say what is on the plate and what is selected.
function describeViewport() {
  const canvas = state.viewport?.renderer?.domElement;
  if (!canvas || !state.project) return;
  const objects = plateObjects();
  const names = selectionIds().map(id => objects.find(o => o.id === id)?.name).filter(Boolean);
  const plate = data().plates.find(p => p.id === state.plate);
  const selection = names.length === 0 ? "nothing selected" : names.length === 1 ? names[0] + " selected" : names.length + " objects selected";
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", "3D view of " + (plate?.name || "the plate") + ": " + plural(objects.length, "object") + ", " + selection);
}

function renderTools() {
  if (!state.project) return;
  const preview = state.mode === "preview";
  $("prepare-tools").hidden = state.step !== "plates";
  $("view-tools").hidden = state.step !== "slice";
  $("mode-prepare").classList.toggle("active", !preview);
  $("mode-preview").classList.toggle("active", preview);
  $('mode-prepare').setAttribute('aria-pressed',String(!preview));
  $('mode-preview').setAttribute('aria-pressed',String(preview));
  $("mode-preview").disabled = sliceEntry()?.state !== "sliced";
  $("align-menu").disabled = !selectionIds().length;
  renderPackControls();
  document.querySelectorAll("#prepare-tools [data-tool]").forEach(b => {
    const active = b.dataset.tool === state.viewport?.tool;
    b.classList.toggle("active", active); b.setAttribute('aria-pressed', String(active));
  });
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
  if (selectionIds().length > 1) {
    panel.innerHTML = `
    <h5>${plural(selectionIds().length, "object")} selected</h5>
    <p class="slicer-muted">Drag, rotate or scale one and the rest follow. Arrow keys nudge them (Shift 10 mm, Alt 0.1 mm); comma and full stop turn them 15°. The Align menu lines them up.</p>
    <label class="slicer-field">Move to <select id="group-plate"><option value="">Plate…</option>${data().plates.filter(p => p.id !== state.plate).map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join("")}<option value="new">New plate</option></select></label>
    <div class="slicer-actions-row">
      <button class="btn btn-secondary btn-sm" type="button" data-object-action="turn-left" title="Turn each 90° anticlockwise">Turn −90°</button>
      <button class="btn btn-secondary btn-sm" type="button" data-object-action="turn-right" title="Turn each 90° clockwise">Turn +90°</button>
      <button class="btn btn-secondary btn-sm" type="button" data-object-action="flat-all">Lay flat</button>
      <button class="btn btn-secondary btn-sm" type="button" data-object-action="remove-all" aria-label="Remove the selected objects">Remove</button>
      <button class="btn btn-secondary btn-sm" type="button" data-object-action="single">Select only one</button>
    </div>`;
    return;
  }
  const input = (field, index, value, step) =>
    `<input type="number" aria-label="${{position:'Position',rotation:'Rotation',scale:'Scale'}[field]} ${'XYZ'[index]} (${field === 'position' ? 'mm' : field === 'rotation' ? 'degrees' : 'factor'})" step="${step}" data-field="${field}" data-index="${index}" value="${Math.round(value * 1000) / 1000}">`;
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
      <button class="btn btn-secondary btn-sm" type="button" data-object-action="turn-left" title="Turn 90° anticlockwise (Shift+, )">Turn −90°</button>
      <button class="btn btn-secondary btn-sm" type="button" data-object-action="turn-right" title="Turn 90° clockwise (Shift+. )">Turn +90°</button>
      <button class="btn btn-secondary btn-sm" type="button" data-object-action="reset">Reset rotation</button>
    </div>
    <div class="slicer-actions-row">
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
      ${filamentOptions(list, choice)}
    </select></label>
    ${choice ? `<p class="slicer-muted">${choice.snapshot ? `Saved material: ${esc(choice.snapshot.material.join(", "))}.` : "Material snapshot pending verification; save and review before slicing."} Shared by every Copy of this Model. Project default Filament changes and Spoolman tuning do not apply.</p>` : ""}
    ${choice ? `<button type="button" class="btn btn-secondary btn-sm" id="object-filament-refresh" data-model="${object.model}" title="Review the catalogue's current version of this preset before this Model uses it">Review preset update</button>` : ""}
    <label class="slicer-field"${choice ? "" : " hidden"}>Colour label only <input type="text" id="object-filament-colour" maxlength="${FILAMENT_COLOUR_MAX}" placeholder="e.g. grey" value="${esc(choice?.colour || "")}"></label>
    <label class="slicer-check" title="The planner, Arrange and Orient leave a pinned object where it is; you can still move it by hand"><input type="checkbox" id="object-pinned"${object.pinned ? " checked" : ""}> Pin</label>
    ${mix ? `<p class="slicer-warn" role="alert">${esc(mix)}. Slicing is refused until each plate holds one filament.</p>` : ""}`;
}
const FILAMENT_COLOUR_MAX = 40;
// Filament presets grouped by brand (the first word of the preset's name), so a long list reads as a few short ones
function filamentOptions(list, choice) {
  const option = name => `<option value="${esc(name)}"${choice?.preset === name ? " selected" : ""}>${esc(name.replace(/ @.*$/, ""))}</option>`;
  const brandOf = name => name.replace(/ @.*$/, "").split(" ")[0] || "Other";
  const brands = [...new Set(list.map(brandOf))];
  if (brands.length < 2) return list.map(option).join("");
  return brands.map(brand => `<optgroup label="${esc(brand)}">${list.filter(name => brandOf(name) === brand).map(option).join("")}</optgroup>`).join("");
}

function showMessage(text) {
  $("viewport-message").hidden = !text;
  $("viewport-message").textContent = text || "";
}

// ── Editing ─────────────────────────────────────────────────────────────────────
function selectObject(id, { additive = false } = {}) {
  if (additive) {   // Shift or Ctrl adds the object to the selection, or takes it out again
    if (!id) return;
    if (id === state.selected) state.selected = state.group.shift() ?? null;
    else if (state.group.includes(id)) state.group = state.group.filter(g => g !== id);
    else if (state.selected == null) state.selected = id;
    else state.group = [...state.group, id];
  } else {
    state.selected = id;
    state.group = [];
  }
  if (!state.selected && state.settingsTab === "object") state.settingsTab = "process";
  state.viewport?.select(state.selected);
  state.viewport?.setGroup(state.group);
  describeViewport();
  renderTools();
  renderObjects();
  renderObjectPanel();
  renderSettings();
}

async function selectPlate(plateId) {
  if (!plateById(plateId)) return;
  state.plate = plateId;
  state.selected = null;
  state.group = [];
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

// ── Packing density ─────────────────────────────────────────────────────────────
// How far apart Arrange and Plan plates keep objects. The project has one choice and a plate may have its own; "too close" is judged by it.
function renderPackControls() {
  if (!state.project) return;
  const project = data().pack || { preset: "normal", gap: 6 }, plate = currentPlate()?.pack;
  const value = plate ? `plate:${plate.preset}` : `project:${project.preset}`;
  $("pack-select").value = [...$("pack-select").options].some(o => o.value === value) ? value : "project:custom";
  const custom = !plate && project.preset === "custom";
  $("pack-gap-field").hidden = !custom;
  if (custom && document.activeElement !== $("pack-gap")) $("pack-gap").value = project.gap;
  $("pack-supports").checked = !!project.tight_supports;
  $("pack-select").title = plate ? `This plate keeps objects ${plate.gap} mm apart; the project default is ${project.gap} mm.`
    : `Objects are kept ${project.gap} mm apart${project.tight_supports ? "" : ", 22 mm when they need supports"}.`;
}

async function setPack(change) {
  await flushSave();
  await withButton(null, async () => {
    const result = await api("POST", `/api/projects/${state.project.id}/pack`, change);
    applyProject(result);
    recordEdit();
    renderAll();
    loadProblems();
  });
}

const projectPack = () => {
  const { tight_supports, ...rest } = data().pack || { preset: "normal", gap: 6 };
  return { rest, tight_supports: !!tight_supports };
};
const withSupports = (pack, tight) => (tight ? { ...pack, tight_supports: true } : pack);

function bindPack() {
  const menus = [...document.querySelectorAll(".tool-more")];   // a More menu closes after a button in it is used, or on a click anywhere else
  for (const more of menus) more.addEventListener("click", e => { if (e.target.closest("button")) more.open = false; });
  document.addEventListener("click", e => { for (const more of menus) if (more.open && !more.contains(e.target)) more.open = false; });
  document.addEventListener("keydown", e => { if (e.key === "Escape") for (const more of menus) more.open = false; });
  $("pack-select").addEventListener("change", e => {
    const [scope, preset] = e.target.value.split(":");
    if (scope === "plate") setPack({ plate_id: state.plate, plate_pack: preset === "inherit" ? null : { preset } });
    else if (preset === "custom") setPack({ pack: withSupports({ preset: "custom", gap: projectPack().rest.gap || 6 }, projectPack().tight_supports) });
    else setPack({ pack: withSupports({ preset }, projectPack().tight_supports) });
  });
  $("pack-gap").addEventListener("change", e => {
    const gap = Math.min(40, Math.max(2, Number(e.target.value) || 6));
    setPack({ pack: withSupports({ preset: "custom", gap }, projectPack().tight_supports) });
  });
  $("pack-supports").addEventListener("change", e => {
    const { rest } = projectPack();
    setPack({ pack: withSupports({ preset: rest.preset, ...(rest.preset === "custom" ? { gap: rest.gap } : {}) }, e.target.checked) });
  });
}

// Several objects change in one step of the history. `changes`: Map of object id → fields to set.
function updateObjects(changes, { merge = false } = {}) {
  for (const [id, change] of changes) {
    const object = data().objects.find(o => o.id === id);
    if (object) Object.assign(object, change);
  }
  changed({ merge });
  renderViewport();
  renderObjectPanel();
}

const round2 = v => Math.round(v * 100) / 100;

// A drag with the Move, Rotate or Scale tool changes the selected object; the rest of the selection changes by the same amount
function transformSelection(id, change) {
  const primary = data().objects.find(o => o.id === id);
  const others = selectedObjects().filter(o => o.id !== id);
  if (!primary || !selectionIds().includes(id) || !others.length) return updateObject(id, change);
  const changes = new Map([[id, change]]);
  for (const o of others) {
    const c = {};
    if (change.position) c.position = [round2(o.position[0] + change.position[0] - primary.position[0]), round2(o.position[1] + change.position[1] - primary.position[1])];
    if (change.rotation) c.rotation = [o.rotation[0], o.rotation[1], round2(((o.rotation[2] + change.rotation[2] - primary.rotation[2]) % 360 + 360) % 360)];
    if (change.scale) c.scale = o.scale.map((v, i) => Math.round(Math.min(100, Math.max(0.01, v * change.scale[i] / primary.scale[i])) * 10000) / 10000);
    changes.set(o.id, c);
  }
  updateObjects(changes);
}

function selectAllHere() {
  const ids = plateObjects().map(o => o.id);
  state.selected = ids[0] ?? null;
  state.group = ids.slice(1);
  state.viewport?.select(state.selected);
  renderViewport();
  renderTools();
  renderObjects();
  renderObjectPanel();
}

function nudge([dx, dy]) {
  const merge = Date.now() - (state.nudgedAt || 0) < 800;   // a held arrow key is one step to undo
  state.nudgedAt = Date.now();
  updateObjects(new Map(selectedObjects().map(o => [o.id, { position: [round2(o.position[0] + dx), round2(o.position[1] + dy)] }])), { merge });
}

function turnSelection(degrees) {
  const merge = Date.now() - (state.nudgedAt || 0) < 800;
  state.nudgedAt = Date.now();
  updateObjects(new Map(selectedObjects().map(o => [o.id,
    { rotation: [o.rotation[0], o.rotation[1], round2(((o.rotation[2] + degrees) % 360 + 360) % 360)] }])), { merge });
}

// Align, centre or spread the selection by where each object stands (its exact box from above)
const BED_EDGE_GAP_MM = 5;
const LINE_UP = ["left", "right", "front", "back", "centre-x", "centre-y"];
function layOutSelection(action) {
  const ids = selectionIds();
  if (!ids.length || !state.viewport) return;
  const list = ids.map(id => [id, state.viewport.footprint(id)]).filter(([, b]) => b);
  if (!list.length) return;
  if (LINE_UP.includes(action) && list.length < 2) { toast("Select two or more objects to line them up. “On the bed” works on one.", true); return; }
  if (action.startsWith("spread") && list.length < 3) { toast("Select three or more objects to spread them out", true); return; }
  const all = { x0: Math.min(...list.map(([, b]) => b.min.x)), x1: Math.max(...list.map(([, b]) => b.max.x)),
                y0: Math.min(...list.map(([, b]) => b.min.y)), y1: Math.max(...list.map(([, b]) => b.max.y)) };
  const [bx0, by0, bx1, by1] = state.viewport.limits.bed;
  const moves = new Map();   // object id → [dx, dy]
  const each = fn => { for (const [id, b] of list) moves.set(id, fn(b)); };
  const unit = (dx, dy) => each(() => [dx, dy]);   // the selection moves as one
  const spread = axis => {
    const x = axis === "x";
    const lo = b => x ? b.min.x : b.min.y, hi = b => x ? b.max.x : b.max.y;
    const sorted = [...list].sort(([, a], [, b]) => lo(a) + hi(a) - lo(b) - hi(b));
    const gap = ((x ? all.x1 - all.x0 : all.y1 - all.y0) - sorted.reduce((sum, [, b]) => sum + hi(b) - lo(b), 0)) / (sorted.length - 1);
    let cursor = x ? all.x0 : all.y0;
    for (const [id, b] of sorted) {
      moves.set(id, x ? [cursor - lo(b), 0] : [0, cursor - lo(b)]);
      cursor += hi(b) - lo(b) + gap;
    }
  };
  switch (action) {
    case "left": each(b => [all.x0 - b.min.x, 0]); break;
    case "right": each(b => [all.x1 - b.max.x, 0]); break;
    case "front": each(b => [0, all.y0 - b.min.y]); break;
    case "back": each(b => [0, all.y1 - b.max.y]); break;
    case "centre-x": each(b => [(all.x0 + all.x1) / 2 - (b.min.x + b.max.x) / 2, 0]); break;
    case "centre-y": each(b => [0, (all.y0 + all.y1) / 2 - (b.min.y + b.max.y) / 2]); break;
    case "spread-x": spread("x"); break;
    case "spread-y": spread("y"); break;
    case "bed-centre": unit((bx0 + bx1) / 2 - (all.x0 + all.x1) / 2, (by0 + by1) / 2 - (all.y0 + all.y1) / 2); break;
    case "bed-left": unit(bx0 + BED_EDGE_GAP_MM - all.x0, 0); break;
    case "bed-right": unit(bx1 - BED_EDGE_GAP_MM - all.x1, 0); break;
    case "bed-front": unit(0, by0 + BED_EDGE_GAP_MM - all.y0); break;
    case "bed-back": unit(0, by1 - BED_EDGE_GAP_MM - all.y1); break;
    default: return;
  }
  updateObjects(new Map(selectedObjects().map(o => {
    const [dx, dy] = moves.get(o.id) || [0, 0];
    return [o.id, { position: [round2(o.position[0] + dx), round2(o.position[1] + dy)] }];
  })));
}

// Move objects to another plate ("new" makes one), then give them a free spot there so they don't land on what is already on it
async function moveToPlate(ids, target) {
  const moving = data().objects.filter(o => ids.includes(o.id));
  if (!moving.length) return;
  if (target === "new") {
    target = nextId(data().plates);
    data().plates.push({ id: target, name: `Plate ${target}`, overrides: {} });
  }
  if (moving.every(o => o.plate === target)) return;
  const there = plateObjects(target).length;
  for (const o of moving) o.plate = target;
  changed();
  await selectPlate(target);
  selectObject(moving[0].id);
  if (moving.length > 1) {
    state.group = moving.slice(1).map(o => o.id);
    state.viewport?.setGroup(state.group);
    renderObjects();
    renderObjectPanel();
  }
  const placed = there ? settleOnPlate(moving.map(o => o.id)) : true;
  toast(`${plural(moving.length, "object")} moved to ${plateById(target).name}${placed ? "" : ". There was no free spot for all of them: press A to arrange the plate"}`);
}

// Put these objects on the first free spot of the current plate (scanning from the front left), clear of the others and the purge area
function settleOnPlate(ids) {
  const viewport = state.viewport;
  if (!viewport) return true;
  const [bx0, by0, bx1, by1] = viewport.limits.bed, gap = 8;
  const solid = [];   // [x0, y0, x1, y1] of what is in the way, grown by the gap
  if (viewport.limits.exclude) { const [a, b, c, d] = viewport.limits.exclude; solid.push([a - gap, b - gap, c + gap, d + gap]); }
  for (const o of plateObjects()) {
    const b = ids.includes(o.id) ? null : viewport.footprint(o.id);
    if (b) solid.push([b.min.x - gap, b.min.y - gap, b.max.x + gap, b.max.y + gap]);
  }
  let ok = true;
  for (const id of ids) {
    const object = data().objects.find(o => o.id === id), box = viewport.footprint(id);
    if (!box) continue;
    const w = box.max.x - box.min.x, h = box.max.y - box.min.y;
    let spot = null;
    for (let y = by0 + BED_EDGE_GAP_MM; !spot && y + h <= by1 - BED_EDGE_GAP_MM + 0.01; y += 5) {
      for (let x = bx0 + BED_EDGE_GAP_MM; x + w <= bx1 - BED_EDGE_GAP_MM + 0.01; x += 5) {
        if (!solid.some(([a, b, c, d]) => x < c && x + w > a && y < d && y + h > b)) { spot = [x, y]; break; }
      }
    }
    if (!spot) { ok = false; continue; }
    object.position = [round2(object.position[0] + spot[0] - box.min.x), round2(object.position[1] + spot[1] - box.min.y)];
    solid.push([spot[0] - gap, spot[1] - gap, spot[0] + w + gap, spot[1] + h + gap]);
  }
  changed();
  renderViewport();
  return ok;
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

async function usePlateSettingsEverywhere() {
  const source = currentPlate();
  const overrides = source.overrides || {};
  const differing = data().plates.filter(p => p.id !== source.id && Object.keys(p.overrides || {}).length
    && JSON.stringify(p.overrides) !== JSON.stringify(overrides));
  if (differing.length && !await confirmDialog({
    title: `Use ${source.name}'s settings on every plate?`,
    body: `${listNames(differing.map(p => p.name))} ${differing.length === 1 ? "has its" : "have their"} own settings, which are replaced.`,
    confirmLabel: "Replace settings",
    danger: true,
  })) return;
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
    const result = await api("GET", `/api/projects/${state.project.id}/objects/${object.id}/orientation?mode=${mode}${face}`, undefined, { timeoutMs: 90000 });
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
    state.orientSuggestion = null;
    applyProject(result);
    if (result.orient.oriented.length) recordEdit();
    renderAll();
    toast(result.outcome || "Oriented");   // the server's one line, "Turned 0 objects, ..." included
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
  const ids = new Set(selectionIds());
  if (!ids.size) return;
  data().objects = data().objects.filter(o => !ids.has(o.id));
  state.selected = null;
  state.group = [];
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
  if (own && !await confirmDialog({
    title: `Merge ${source.name} into ${target.name}?`,
    body: `${source.name} has settings of its own. The merged plate keeps ${target.name}'s settings.`,
    confirmLabel: "Merge plates",
  })) return;
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
    if (object.outline?.length > 2) {   // the real outline from above, as the server checks it for overlaps
      const points = object.outline.map(([px, py]) => `${px.toFixed(1)},${(y1 - py + y0).toFixed(1)}`).join(" ");
      return `<polygon class="plan-object${object.pinned ? " pinned" : ""}" points="${points}"><title>${esc(object.name)}</title></polygon>`;
    }
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
  const tilt = `<label class="slicer-check" title="Without this an object only goes onto a side of its own box; with it, an object may lean on an angled face"><input type="checkbox" id="plan-tilt"${state.planTilt ? " checked" : ""}> Allow tilting objects onto angled faces</label>`;
  const here = data().pack || { preset: "normal", gap: 6 };
  const density = `<label class="slicer-field">Spacing <select id="plan-pack" title="How far apart the plan keeps objects (22 mm for objects that need supports unless you chose otherwise). The same choice is in the toolbar.">${
    [["tight", "Tight (3 mm)"], ["normal", "Normal (6 mm)"], ["roomy", "Roomy (12 mm)"]].map(([v, l]) => `<option value="${v}"${here.preset === v ? " selected" : ""}>${l}</option>`).join("")
  }${here.preset === "custom" ? `<option value="custom" selected>Custom (${here.gap} mm)</option>` : ""}</select></label>`;
  const refused = plan.refused ? `<p class="slicer-error" role="alert">No plan is offered: ${esc(plan.refused)}. Nothing was changed.</p>` : "";
  const summary = plan.refused ? "" : `<p class="slicer-muted">Now ${plural(before, "plate")}. Plan: ${esc(plan.summary)}${plan.plates.length > 1 ? ", longest first" : ""}.</p>`;
  return `${summary}
    ${refused}${density}${tilt}
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
    try {
      await showPlan();
    } catch (err) {
      event.target.checked = state.planTilt = !state.planTilt;   // the sheet still shows the plan that was asked for
      $("plan-error").textContent = err.message;
    }
  });
  $("plan-pack").addEventListener("change", async event => {
    const previous = (data().pack || { preset: "normal" }).preset;
    try {
      const { rest, tight_supports } = projectPack();
      applyProject(await api("POST", `/api/projects/${state.project.id}/pack`, { pack: withSupports({ preset: event.target.value }, tight_supports) }));
      recordEdit();
      renderAll();
      loadProblems();
      await showPlan();
    } catch (err) {
      event.target.value = previous;
      $("plan-error").textContent = err.message;
    }
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

// After one split is laid out, the next unplaced one opens on Model; with none left the project goes on to Plates
async function afterSplitPlaced() {
  const next = unplacedSplitIds(state.project)[0];
  if (next === undefined) return setStep("plates");
  await splitStep.open(next).catch(err => toast(err.message, true));
  return setStep("model");
}

async function layOutParts() {
  const splitId = splitStep.split?.id ?? state.project.split_id;
  const placed = state.project.models.some(m => m.split?.id === splitId);
  if (placed && !await confirmDialog({
    title: "Lay the parts out again?",
    body: "This replaces the parts on your plates, including any moves or settings you gave them.",
    confirmLabel: "Lay out again",
    danger: true,
  })) return;
  await flushSave();
  applyProject(await api("POST", `/api/projects/${state.project.id}/layout`, { split_id: splitId }));
  resetEdits();
  state.plate = data().plates.find(p => plateObjects(p.id).length)?.id || data().plates[0].id;
  renderAll();
  schedulePoll();
  await afterSplitPlaced();
  state.viewport?.fit(state.viewport.objectsBox());
  toast(`Parts packed onto ${plural(platesWithObjects().length, "plate")} — importing them now`);
}

async function placeWhole() {
  const splitId = splitStep.split?.id ?? state.project.split_id;
  const placed = state.project.models.some(m => m.split?.id === splitId);
  if (placed && !await confirmDialog({
    title: "Put the model on a plate again?",
    body: "This replaces what's on your plates from this model.",
    confirmLabel: "Put on a plate",
    danger: true,
  })) return;
  await flushSave();
  applyProject(await api("POST", `/api/projects/${state.project.id}/whole`, { split_id: splitId }));
  renderAll();
  schedulePoll();
  await afterSplitPlaced();
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
  if (fit) state.fitTarget = `preview:${state.project.id}:${state.plate}`;
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

/** Orca's own configuration compared with the saved inputs: differences by setting and scope, recorded normalization, and what is unavailable. */
function observedOutputHtml(check) {
  const table = (attr, caption, head, rows) => rows.length ? `<table ${attr}><caption>${caption}</caption><thead><tr>${head.map(h => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>` : "";
  const settingRow = (d, extra) => `<tr><th scope="row">${esc(d.key)}</th><td>${esc(d.scope)}</td><td>${showValue(d.expected)}</td><td>${showValue(d.observed)}</td>${extra ? `<td>${esc(d.reason)}</td>` : ""}</tr>`;
  return `<section data-observed-output data-observed-state="${esc(check.state)}"><h4>Orca output check</h4><p class="${check.trusted ? "" : "warn"}">${esc(check.label)}</p>`
    + (check.reason ? `<p>${esc(check.reason)}</p>` : "")
    + (check.compared ? `<p class="slicer-muted">${check.compared} reported settings compared${check.source ? ` (from ${esc(check.source)})` : ""}.</p>` : "")
    + table("data-observed-differences", "Reported differently by Orca (affects the print)", ["Setting", "Set by", "Expected", "Orca reported"], (check.differences || []).map(d => settingRow(d)))
    + table("data-observed-normalized", "Known Orca normalization (recorded, not a mismatch)", ["Setting", "Set by", "Expected", "Orca reported", "Why"], (check.normalized || []).map(d => settingRow(d, true)))
    + ((check.unavailable || []).length ? `<p class="slicer-muted" data-observed-unavailable>Not reported by Orca, so unverified: ${esc(check.unavailable.map(u => u.key).join(", "))}.</p>` : "")
    + (check.post_processing ? `<p class="warn" data-observed-post>${esc(check.post_processing.note)}</p>` : "")
    + (check.object_overrides ? `<p class="slicer-muted">${esc(check.object_overrides.note)}</p>` : "")
    + (check.limits || []).map(l => `<p class="slicer-muted">${esc(l)}</p>`).join("")
    + (check.state === "mismatch" ? '<p><button type="button" data-send-unverified>Send anyway (I accept these differences)</button></p>' : "")
    + "</section>";
}

async function sendUnverified() {
  const plate = state.plate;
  // "Send anyway" accepts the differences of the Slice that was reviewed: the server refuses it if that Slice has been replaced
  const shown = (await api("GET", `/api/projects/${state.project.id}/plates/${plate}/slice-inputs`)).slice;
  if (!await queuePlates([plate], { acceptUnverified: { [plate]: { fingerprint: shown.fingerprint, sliced_at: shown.sliced_at } } })) return;
  await loadJobs();
  toast("Sent to the printer");
}

const showValue = value => esc(typeof value === "string" ? value : JSON.stringify(value) ?? "—");
async function loadSliceInputs(details) {
  const body = details.querySelector("[data-slice-inputs-body]");
  const projectId = state.project.id, plateId = state.plate;
  body.textContent = "Loading…";
  try {
    const review = await api("GET", `/api/projects/${projectId}/plates/${plateId}/slice-inputs`);
    if (state.project.id !== projectId || state.plate !== plateId || !body.isConnected) return;
    const saved = review.saved;
    const rows = review.differences.filter(d => d.kind !== "objects").map(d => `<tr><th scope="row">${esc(d.key)}</th><td>${showValue(d.saved)}</td><td>${showValue(d.current)}</td></tr>`);
    const objectsChanged = review.differences.some(d => d.kind === "objects");
    body.innerHTML = `<p>${esc(review.label)}</p>` + (saved.state === "saved"
      ? `<p data-saved-inputs>Saved inputs: ${esc(saved.inputs.filament.filament_type ?? "")} on ${esc(saved.inputs.process.curr_bed_type ?? "")}, project revision ${saved.inputs.revision}.</p>`
      : `<p data-saved-unavailable class="warn">Saved inputs unavailable: ${esc(saved.reason)}</p>`)
      + (review.current.state === "unresolved" ? '<p class="warn">The current Project inputs cannot be resolved right now.</p>' : "")
      + (rows.length || objectsChanged ? `<table data-slice-differences><caption>Changed since this Slice</caption><thead><tr><th>Setting</th><th>Saved</th><th>Current Project</th></tr></thead><tbody>${rows.join("")}${objectsChanged ? '<tr><th scope="row">Objects</th><td colspan="2">Placement or geometry changed</td></tr>' : ""}</tbody></table>`
        : saved.state === "saved" ? "<p>The current Project matches the saved inputs.</p>" : "")
      + (review.observed ? observedOutputHtml(review.observed) : "");
  } catch (err) { body.textContent = err.message; }
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
      <dt>Print time</dt><dd>${duration(entry?.estimate_s) || "—"}</dd>
      <dt>Filament</dt><dd>${entry?.estimate_g ? `${entry.estimate_g.toFixed(1)} g` : "—"}</dd>
      <dt>Layers</dt><dd>${analysis.layers.length}</dd>
    </dl>
    ${entry?.outdated ? `<p class="slicer-slice-state warn">${entry.outdated_reason === "inputs-unresolved" ? "This plate's inputs can no longer be resolved, so this Slice cannot be confirmed as current." : "This plate changed after it was sliced."}</p>` : ""}
    ${entry?.observed?.state === "mismatch" ? `<p class="slicer-slice-state warn">Unverified: Orca's output differs from the expected settings (${esc(unverifiedKeys(entry))}). Review the differences below before sending.</p>` : ""}
    <details class="slicer-subfold" data-slice-inputs${state.sliceInputsOpen ? " open" : ""}>
      <summary>Inputs this Slice was prepared with</summary>
      <div data-slice-inputs-body class="slicer-muted">Open to load the saved inputs.</div>
    </details>
    ${info?.stride > 1 ? `<p class="slicer-muted">Large toolpath: showing every ${info.stride}th move.</p>` : ""}
    <details class="slicer-subfold" data-preview-more${state.previewMore ? " open" : ""}>
      <summary>Features and details</summary>
      <div class="slicer-legend">${legend || '<p class="slicer-muted">No extrusion features found.</p>'}</div>
      <dl class="slicer-stats">
        <dt>Plate</dt><dd>${esc(currentPlate()?.name)}</dd>
        <dt>Nozzle / bed</dt><dd>${temps.nozzle?.max ?? "—"} °C / ${temps.bed?.max ?? "—"} °C</dd>
        <dt>Slicer</dt><dd>${esc(analysis.slicer || "OrcaSlicer")}</dd>
      </dl>
    </details>
    <a class="btn btn-secondary btn-sm" href="/api/projects/${state.project.id}/plates/${state.plate}/gcode" download>Download G-code</a>`;
}

// ── Jobs and printers (the Print step reads and sends through these) ────────────────
async function loadJobs({ quiet = false } = {}) {
  if (!state.project) return;
  const id = state.project.id;
  try {
    const { jobs } = await api("GET", `/api/projects/${id}/jobs`);
    if (state.project?.id !== id) return;
    state.jobs = jobs;
    if (!quiet) renderSteps();
  } catch (err) {
    if (!quiet) toast(err.message, true);
  }
}

async function loadPrinters() {
  try {
    const printers = await api("GET", "/api/printers");
    state.printers = printers.filter(p => p.printer_type === "cc1");
  } catch (_) { /* the list stays as it was */ }
}

/** Save the project, then turn the given Plates into Jobs (the Print step and the Slice step both send through here). */
async function queuePlates(plates, { leveling, timelapse, again = false, acceptUnverified = null }) {
  await flushSave();
  if ($('save-state').textContent === 'Not saved') {
    toast('Project changes could not be saved. Retry saving before sending these Plates.', true); return null;
  }
  if (leveling !== undefined || timelapse !== undefined) rememberDefaults({ options: { leveling, timelapse } });
  return api("POST", `/api/projects/${state.project.id}/queue`, { plates, leveling, timelapse, again, ...(acceptUnverified ? { accept_unverified: acceptUnverified } : {}) });
}

// ── Dialogs ─────────────────────────────────────────────────────────────────────
// The New project form: a dialog over whatever page is showing
function openProjectsDialog() {
  const dialog = $("dlg-projects");
  if (!dialog.open) dialog.showModal();
  $("new-project-name").focus();
}

// ── The Projects page: every project with where it has got to ────────────────────
const projectChips = p => {
  const chips = [];
  if (p.busy && p.slicing) chips.push(["busy", p.slicing === 1 ? "Slicing a plate" : `Slicing ${p.slicing} plates`]);
  if (p.printing) chips.push(["busy", p.printing === 1 ? "Printing" : `${p.printing} printing`]);
  if (p.queued) chips.push(["go", `${p.queued} in the queue`]);
  if (p.sliced && !p.queued && !p.printing) chips.push(["go", p.sliced === p.plates ? "All plates sliced" : `${p.sliced} of ${plural(p.plates, "plate")} sliced`]);
  if (p.printed_ok) chips.push(["sb-completed", `${p.printed_ok} completed`, "completed"]);
  if (p.failed) chips.push(["sb-failed", `${p.failed} failed`, "failed"]);
  if (p.outcome_unknown) chips.push(["sb-unknown", p.outcome_unknown === 1 ? "Needs outcome" : `${p.outcome_unknown} need outcomes`, "unknown"]);
  if (!p.sliced && !p.slicing && !p.queued && !p.printing) chips.push(["", p.objects ? "Not sliced yet" : "Empty"]);
  return chips.map(([kind, text, icon]) => `<span class="pd-chip ${kind}">${icon ? statusIcon(icon) : ""}${esc(text)}</span>`).join("");
};

// "3 hours ago", never a locale timestamp; the exact time is in the tooltip
function ago(seconds) {
  const gap = Math.max(0, Date.now() / 1000 - seconds), n = (value, unit) => `${value} ${unit}${value === 1 ? "" : "s"} ago`;
  if (gap < 60) return "just now";
  if (gap < 3600) return n(Math.floor(gap / 60), "minute");
  if (gap < 86400) return n(Math.floor(gap / 3600), "hour");
  if (gap < 30 * 86400) return n(Math.floor(gap / 86400), "day");
  if (gap < 365 * 86400) return n(Math.floor(gap / (30 * 86400)), "month");
  return n(Math.floor(gap / (365 * 86400)), "year");
}

// The whole row opens the Project: the name is the one real button, stretched over the row (Delete sits above it)
function projectRow(p) {
  const when = new Date(p.updated_at * 1000);
  const thumb = p.objects ? `<img class="pd-thumb" src="/api/projects/${p.id}/plates/1/preview.png" alt="" width="56" height="56" loading="lazy" decoding="async">` : "";
  return `<div class="slicer-project-row" data-name="${esc(p.name.toLowerCase())}">
      ${thumb}<div><strong>${esc(p.name)}</strong>${p.split_id ? '<span class="slicer-tag">from a model upload</span>' : ""}
        <small>${plural(p.plates, "plate")} · ${plural(p.objects, "object")} · edited <time datetime="${when.toISOString()}" title="${esc(when.toLocaleString())}">${ago(p.updated_at)}</time></small>
        <div class="pd-chips">${projectChips(p)}</div></div>
      <button class="btn btn-primary btn-sm pd-open" type="button" data-open-project="${p.id}" aria-label="Open ${esc(p.name)}">Open</button>
      <button class="btn btn-ghost-quiet btn-sm project-delete" type="button" data-delete-project="${p.id}" aria-label="Delete ${esc(p.name)}">Delete</button>
    </div>`;
}

async function loadProjectsPage() {
  const list = $("project-list");
  if (!list.children.length || list.querySelector(".state-block")) stateBlock(list, "loading", "Loading your projects…");
  try {
    const [{ projects }, { splits }] = await Promise.all([api("GET", "/api/projects"), api("GET", "/api/splits").catch(() => ({ splits: [] }))]);
    const loose = splits.filter(s => !s.project_id);   // the server marks splits owned by any project, hidden quick ones included
    list.innerHTML = (projects.length ? `<h4 class="slicer-list-heading">Your projects</h4>${projects.map(projectRow).join("")}` : `<div class="pd-empty">
        <p>No projects yet. Name one, drop your models in, and Spooler arranges them onto plates.</p>
        <button class="btn btn-primary" type="button" data-pd-new>New project</button></div>`)
      + (loose.length ? '<h4 class="slicer-list-heading">Split models without a project</h4>' + loose.map(s => `
      <div class="slicer-project-row">
        <div><strong>${esc(s.source_name)}</strong><small>${s.parts ? plural(s.parts, "part") : esc(s.state)}</small></div>
        <span></span>
        <button class="btn btn-secondary btn-sm" type="button" data-adopt-split="${s.id}" data-name="${esc(s.source_name.replace(/\.[^.]+$/, ""))}">Open as project</button>
      </div>`).join("") : "");
    filterProjects();
  } catch (err) {
    stateBlock(list, "error", `Couldn't load your projects: ${err.message}`, loadProjectsPage, "Retry projects");
  }
}

function filterProjects() {
  const q = $("pd-search").value.trim().toLowerCase();
  document.querySelectorAll("#project-list .slicer-project-row[data-name]").forEach(row => { row.hidden = !!q && !row.dataset.name.includes(q); });
}

/** Leave the open project (its changes are saved first) and show the Projects page */
async function showProjectsPage() {
  if (state.project) {
    try { await flushSave(); } catch (err) { toast(err.message, true); return; }
  }
  clearTimeout(state.poller);
  state.project = null;
  state.selected = null;
  state.group = [];
  splitStep.close();
  document.body.classList.add("view-dashboard");
  $("projects-dashboard").hidden = false;
  document.title = "Projects – Spooler";
  history.replaceState(null, "", location.pathname);
  try {   // inside the app shell the address says Projects, with no project in it
    const frame = window.frameElement;
    if (frame && window.self !== window.top) { frame.dataset.route = ""; parent.history.replaceState(null, "", "#projects"); }
  } catch (_) { /* not the Spooler shell */ }
  await loadProjectsPage();
}

async function uploadNewModel(file) {
  if (!/\.(stl|obj|3mf|gcode)$/i.test(file.name)) { toast("Choose an STL, OBJ or 3MF model, or a G-code file", true); return; }
  const progress = $("new-upload-progress"), bar = progress.querySelector(".job-upload-bar");
  progress.hidden = false;
  bar.style.width = "0%";
  $("btn-new-upload").disabled = true;
  try {
    const params = new URLSearchParams({ filename: file.name, scale: "1", up: "z" });   // millimetres, Z up; a wrong guess is fixed per file later
    const onProgress = pct => { bar.style.width = `${pct}%`; };
    let project = isReviewed(file)   // a 3MF is inspected and reviewed in staging before anything is created
      ? await importWithReview({ file, host: { api, upload }, onProgress })
      : await upload(`/api/projects/upload?${params}`, file, onProgress);
    if (!project) { toast("Import cancelled — nothing was created"); return; }
    const name = $("new-project-name").value.trim();
    if (name && name !== project.name) project = await api("PATCH", `/api/projects/${project.id}`, { name });
    // A 3MF holding several objects on one plate opens crammed together: plan it like a batch import does
    if (project.data?.objects?.length > 1 && new Set(project.data.objects.map(o => o.plate)).size === 1) {
      requestAutoPlan(project.id);
    }
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

// A lone G-code or 3MF keeps its own path (a G-code project; an Orca 3MF adopts its settings). Every other drop goes to the intake table.
function uploadNew(files, folder = "") {
  if (files.length === 1 && !folder && /\.(3mf|gcode)$/i.test(files[0].name)) uploadNewModel(files[0]);
  else if (files.length) newProject.add(files);
}

// Create promoted the draft: open it on the step the server named (Model while an oversized model waits, then Split once its
// cuts are planned; else Plates). The models that fit are planned onto plates too, so they don't sit stacked on plate 1.
async function openCreated({ project, step, arranged }, { plan, id }) {
  if (plan && project.data.objects.length > 1) requestAutoPlan(id);   // every model starts on plate 1 at one spot
  $("dlg-projects").close();
  await openProject(project.id);
  let target = step;
  if (step === "model") {
    state.landOnSplit = unplacedSplitIds(state.project)[0] ?? null;
    if (splitStep.split?.analysis && splitStep.split.state === "ready") { target = "split"; state.landOnSplit = null; }   // already analysed and planned
  }
  if (stepAvailable(target)) await setStep(target, { force: true });
  if (arranged?.summary) toast(`Arranged: ${arranged.summary}. Undo puts it back`, !!arranged.too_big?.length);
  else if (arranged?.error) toast(`The models are on plate 1: ${arranged.error}`, true);
}

// The Print page's Assembly guide action: one guide per split. The only split opens its guide panel directly; several
// splits are listed with their guide state, and each opens its own guide (there is no combined guide).
const GUIDE_STATE_TEXT = { none: "No guide yet", generating: "Generating", ready: "Ready", failed: "Failed" };
const guideStateText = g => g.state === "ready" && g.out_of_date ? "Out of date" : GUIDE_STATE_TEXT[g.state] || g.state;

async function showGuideFor(splitId) {
  $("dlg-guides").close();
  if (splitStep.split?.id !== splitId) await splitStep.open(splitId);
  await setStep("split");
  document.querySelector("[data-guide]")?.scrollIntoView({ block: "center" });
}

async function openAssemblyGuide() {
  const { guides } = await api("GET", `/api/projects/${state.project.id}/guides`);
  if (guides.length === 1) {
    if (splitStep.split?.id !== guides[0].split_id) await splitStep.open(guides[0].split_id);
    await setStep("split");
    document.querySelector("[data-guide]")?.scrollIntoView({ block: "center" });
    return;
  }
  $("guide-choices").innerHTML = guides.map(g => `
    <div class="slicer-project-row">
      <div><strong>${esc(g.name)}</strong><small>${esc(guideStateText(g.guide))}</small></div>
      <button class="btn btn-secondary btn-sm" type="button" data-guide-split="${g.split_id}">Open guide</button>
    </div>`).join("") || '<p class="slicer-muted">This project has no splits.</p>';
  $("dlg-guides").showModal();
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
const modelOnPlateWhole = () => !!splitStep.split
  && !!state.project?.models.some(m => m.split?.id === splitStep.split.id && m.split.part === 0);
const splitSkipped = () => modelOnPlateWhole() && !!splitStep.split?.analysis?.fits_whole;

// ── Wiring ──────────────────────────────────────────────────────────────────────
function bindUi() {
  document.querySelectorAll('dialog').forEach(dialog => {
    const heading = dialog.querySelector('h3');
    if (heading) { heading.id ||= `${dialog.id}-title`; dialog.setAttribute('aria-labelledby', heading.id); }
  });
  // Phones defer 3D editing (same breakpoint as the CSS); desktop opens editable with the gizmo. Re-evaluated when the width crosses it.
  const phone = window.matchMedia('(max-width: 640px)');
  const setDefer = defer => {
    document.body.classList.toggle('defer-edit', defer);
    document.body.dataset.viewportEditable = String(!defer);
    state.viewport?.setEditable(!defer);
    const toggle = $('btn-mobile-edit');
    toggle.textContent = defer ? 'Show editing controls' : 'Defer 3D editing';
    toggle.setAttribute('aria-expanded', String(!defer));
  };
  setDefer(phone.matches);
  phone.addEventListener('change', e => setDefer(e.matches));
  $('btn-mobile-edit').addEventListener('click', () => setDefer(!document.body.classList.contains('defer-edit')));
  $('btn-sidebar').addEventListener('click', e => {
    const hidden = document.body.classList.toggle('sidebar-collapsed');
    e.currentTarget.textContent = hidden ? 'Show sidebar' : 'Hide sidebar';
    e.currentTarget.setAttribute('aria-expanded', String(!hidden));
  });
  const compactPreview = matchMedia('(max-width: 640px)');
  const movePreview = () => {
    const panel = $('preview-panel');
    if (compactPreview.matches) $('viewport').after(panel);
    else $('viewport').append(panel);
  };
  compactPreview.addEventListener('change', movePreview); movePreview();
  $("guide-choices").addEventListener("click", e => {
    const button = e.target.closest("[data-guide-split]");
    if (button) showGuideFor(Number(button.dataset.guideSplit)).catch(err => toast(err.message, true));
  });
  document.querySelectorAll("dialog [data-close]").forEach(btn => btn.addEventListener("click", () => btn.closest("dialog").close()));

  $("step-bar").addEventListener("click", e => {
    const btn = e.target.closest("[data-step]");
    if (btn && btn.getAttribute("aria-disabled") !== "true") setStep(btn.dataset.step);
  });
  $("slice-state").addEventListener("click", e => {
    if (e.target.closest("[data-show-problem]")) showProblem();
  });
  $("step-actions").addEventListener("click", e => {
    const btn = e.target.closest("[data-go]");
    if (btn && !btn.disabled) setStep(btn.dataset.go);
    const fwd = e.target.closest("[data-split-forward]");
    if (fwd && fwd.getAttribute("aria-disabled") !== "true") withButton(fwd, () => splitStep.perform(fwd.dataset.splitForward));
    const send = e.target.closest("[data-send-this-plate]");
    if (send && !send.disabled && send.getAttribute("aria-disabled") !== "true") withButton(send, sendThisPlate);
  });

  $("project-name").addEventListener("keydown", e => {   // Enter saves (through change), Escape puts the saved name back
    if (e.key === "Enter") e.target.blur();
    else if (e.key === "Escape") { e.target.value = state.project.name; e.target.blur(); }
  });
  $("project-name").addEventListener("change", async e => {
    const name = e.target.value.trim();
    if (!name || name === state.project.name) { e.target.value = state.project.name; return; }   // a blank title is not a rename
    try {
      applyProject(await api("PATCH", `/api/projects/${state.project.id}`, { name }), { keepLocalData: true });
      renderAll({ geometry: false });
    } catch (err) {
      e.target.value = state.project.name;
      toast(err.message, true);
    }
  });
  $("btn-export-3mf").addEventListener("click", exportProject);
  $("btn-import-records").addEventListener("click", () => {
    $("btn-import-records").closest("details").open = false;
    if (state.project) showRecords({ api }, state.project.id).catch(err => toast(err.message, true));
  });
  $("btn-projects").addEventListener("click", showProjectsPage);
  $("btn-empty-projects").addEventListener("click", showProjectsPage);
  $("btn-pd-new").addEventListener("click", openProjectsDialog);
  $("pd-search").addEventListener("input", filterProjects);
  $("project-list").addEventListener("error", e => { if (e.target.matches?.(".pd-thumb")) e.target.remove(); }, true);   // no picture yet: the row just has none
  $("btn-new-upload").addEventListener("click", () => $("new-model-input").click());
  $("new-drop").addEventListener("keydown", e => {
    if (e.target !== e.currentTarget || (e.key !== "Enter" && e.key !== " ")) return;   // the buttons inside keep their own keys
    e.preventDefault();
    $("new-model-input").click();
  });
  const projectsDialog = $("dlg-projects");   // dropping a file on the Projects list imports it
  projectsDialog.addEventListener("dragover", e => { e.preventDefault(); projectsDialog.classList.add("dropping"); });
  projectsDialog.addEventListener("dragleave", e => { if (e.target === projectsDialog) projectsDialog.classList.remove("dropping"); });
  projectsDialog.addEventListener("drop", e => {
    e.preventDefault();
    projectsDialog.classList.remove("dropping");
    FolderImport.dropped(e.dataTransfer).then(({ files, folder }) => uploadNew(files, folder));
  });
  $("new-model-input").addEventListener("change", e => { uploadNew([...e.target.files]); e.target.value = ""; });
  $("btn-new-folder").addEventListener("click", () => $("new-folder-input").click());
  $("new-folder-input").addEventListener("change", e => {
    const { files, folder } = FolderImport.picked(e.target.files);
    e.target.value = "";
    if (files.length) newProject.add(files); else toast("That folder has no STL, OBJ or 3MF models", true);
  });
  $("btn-new-project").addEventListener("click", async () => {
    try {
      const project = await api("POST", "/api/projects", { name: $("new-project-name").value });
      $("dlg-projects").close();
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
      if (e.target.closest("[data-pd-new]")) openProjectsDialog();
      else if (open) {
        $("dlg-projects").close();
        await openProject(Number(open.dataset.openProject));
      } else if (adopt) {
        const project = await api("POST", "/api/projects", { name: adopt.dataset.name, split_id: Number(adopt.dataset.adoptSplit) });
        $("dlg-projects").close();
        await openProject(project.id);
      } else if (remove) {
        const name = remove.closest(".slicer-project-row")?.querySelector("strong")?.textContent || "this Project";
        if (!await confirmDialog({
          title: `Delete the Project ${name}?`,
          body: "Its uploaded model and its sliced plates are deleted too, and this cannot be undone. Jobs already in the queue stay there.",
          confirmLabel: "Delete Project",
          danger: true,
        })) return;
        await api("DELETE", `/api/projects/${remove.dataset.deleteProject}`);
        if (state.project?.id === Number(remove.dataset.deleteProject)) {
          state.project = null;
          $("empty-state").hidden = false;
        }
        loadProjectsPage();
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
  $("quality-buttons").addEventListener("click", e => {
    const button = e.target.closest("[data-quality]");
    if (button) chooseQuality(button.dataset.quality);
  });
  for (const kind of ["process", "filament"]) {
    $(`btn-save-${kind}`).addEventListener("click", () => openSavePreset(kind));
    $(`btn-delete-${kind}`).addEventListener("click", () => deletePreset(kind));
  }
  $("btn-import-preset").addEventListener("click", () => $("preset-file").click());
  $("preset-file").addEventListener("change", e => { const [file] = e.target.files; e.target.value = ""; if (file) importPreset(file); });
  $("bed-select").addEventListener("change", e => { data().bed = e.target.value; rememberDefaults({ plate_side: data().bed }); changed(); renderViewport(); });

  $("btn-save-profile-confirm").addEventListener("click", savePreset);
  $("btn-refresh-filament").addEventListener("click", () => openReview({ source: "preset", target: { kind: "default" } }));
  $("btn-review-apply").addEventListener("click", applyReview);
  $("review-body").addEventListener("change", updateReviewApply);
  $("dlg-review").addEventListener("close", () => { review.preview = null; renderFarm(); });

  $("btn-add-model").addEventListener("click", () => $("model-input").click());
  $("model-input").addEventListener("change", e => { chooseImport([...e.target.files]); e.target.value = ""; });
  $("btn-add-folder").addEventListener("click", () => $("folder-input").click());
  $("folder-input").addEventListener("change", e => { chooseFolder([...e.target.files]); e.target.value = ""; });
  $("btn-add-files").addEventListener("click", () => $("model-input").click());
  $("dlg-add").addEventListener("close", finishAdd);
  $("dlg-add").addEventListener("cancel", e => { if (newProject.blocked()) e.preventDefault(); });   // Escape waits for the uploads and the unit checks
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
    if (row) selectObject(Number(row.dataset.object), { additive: e.shiftKey || e.ctrlKey || e.metaKey });
  });
  // Drag an object (or the whole selection, when the object is in it) onto a plate tab
  $("object-list").addEventListener("dragstart", e => {
    const id = Number(e.target.closest("[data-object]")?.dataset.object);
    if (!id) return;
    const ids = selectionIds().includes(id) ? selectionIds() : [id];
    e.dataTransfer.setData("application/x-spooler-objects", JSON.stringify(ids));
    e.dataTransfer.effectAllowed = "move";
  });
  const dropTab = e => e.target.closest("[data-plate]");
  const droppable = e => e.dataTransfer.types.includes("application/x-spooler-objects");
  $("plate-tabs").addEventListener("dragover", e => {
    const tab = dropTab(e);
    if (!tab || !droppable(e) || tab.dataset.plate === "remove") return;
    e.preventDefault();
    document.querySelectorAll(".slicer-plate-tab.drop").forEach(t => t.classList.remove("drop"));
    tab.classList.add("drop");
  });
  $("plate-tabs").addEventListener("dragleave", e => dropTab(e)?.classList.remove("drop"));
  $("plate-tabs").addEventListener("drop", e => {
    const tab = dropTab(e);
    if (!tab || !droppable(e)) return;
    e.preventDefault();
    tab.classList.remove("drop");
    moveToPlate(JSON.parse(e.dataTransfer.getData("application/x-spooler-objects")), tab.dataset.plate === "new" ? "new" : Number(tab.dataset.plate));
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


  $("object-panel").addEventListener("change", e => {
    if (e.target.id === "group-plate") {
      if (e.target.value) moveToPlate(selectionIds(), e.target.value === "new" ? "new" : Number(e.target.value));
      return;
    }
    const object = selectedObject();
    if (!object) return;
    const field = e.target.dataset.field;
    if (e.target.matches("[data-orient-mode]")) return suggestOrientation(object, e.target.value);
    if (e.target.id === "object-plate") {
      object.plate = Number(e.target.value);
      state.selected = null;
      state.group = [];
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
    const refresh = e.target.closest("#object-filament-refresh");
    if (refresh) return openReview({ source: "preset", target: { kind: "model", model_id: Number(refresh.dataset.model) } });
    const action = e.target.closest("[data-object-action]")?.dataset.objectAction;
    const object = selectedObject();
    if (!action || !object) return;
    if (action === "turn-left" || action === "turn-right") turnSelection(action === "turn-left" ? -90 : 90);
    else if (action === "flat-all") for (const o of selectedObjects()) await layFlat(o);
    else if (action === "remove-all") removeSelected();
    else if (action === "single") selectObject(state.selected);
    else if (action === "flat") await layFlat(object);
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

  $("align-menu").addEventListener("change", e => {
    const action = e.target.value;
    e.target.value = "";
    if (action) layOutSelection(action);
  });
  $("btn-select-all").addEventListener("click", selectAllHere);
  const applySnap = () => {
    const on = $("snap-on").checked;
    remember("slicer.snap", on ? $("snap-step").value : "off");
    state.viewport?.setSnap(on ? Number($("snap-step").value) : 0, on ? 15 : 0);
    $("snap-step").disabled = !on;
  };
  const savedSnap = remember("slicer.snap");
  if (savedSnap && savedSnap !== "off" && [...$("snap-step").options].some(o => o.value === savedSnap)) { $("snap-on").checked = true; $("snap-step").value = savedSnap; }
  $("snap-on").addEventListener("change", applySnap);
  $("snap-step").addEventListener("change", applySnap);
  state.applySnap = applySnap;
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
  $("preview-panel").addEventListener("toggle", e => {
    if (e.target.matches("[data-preview-more]")) state.previewMore = e.target.open;
    if (e.target.matches("[data-slice-inputs]")) { state.sliceInputsOpen = e.target.open; if (e.target.open) loadSliceInputs(e.target); }
  }, true);
  $("preview-panel").addEventListener("click", e => { if (e.target.closest("[data-send-unverified]")) sendUnverified().catch(err => toast(err.message, true)); });
  $("preview-panel").addEventListener("change", e => {
    const name = e.target.dataset.feature;
    if (!name) return;
    if (e.target.checked) state.hiddenFeatures.delete(name);
    else state.hiddenFeatures.add(name);
    drawPreview();
  });

  for (const viewport of [$("viewport"), $("split-stage")]) {   // files dropped on the 3D view of any step
    viewport.addEventListener("dragover", e => { e.preventDefault(); viewport.classList.add("dropping"); });
    viewport.addEventListener("dragleave", () => viewport.classList.remove("dropping"));
    viewport.addEventListener("drop", e => {
      e.preventDefault();
      viewport.classList.remove("dropping");
      FolderImport.dropped(e.dataTransfer).then(({ files }) => chooseImport(files));
    });
  }

  document.addEventListener("keydown", e => {
    if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName) || document.querySelector("dialog[open]")) return;
    const key = e.key.toLowerCase();
    const command = e.ctrlKey || e.metaKey;
    if (state.step === "plates" && command) {
      if (key === "z" && !e.shiftKey) undo();
      else if (key === "y" || (key === "z" && e.shiftKey)) redo();
      else if (key === "a") selectAllHere();
      else if (key === "c") copyObject();
      else if (key === "v") pasteObject();
      else return;
      e.preventDefault();
    } else if (state.step === "plates") {
      if ((e.key === "Delete" || e.key === "Backspace") && selectedObject()) removeSelected();
      else if (e.key.startsWith("Arrow") && selectionIds().length) {   // nudge: 1 mm, Shift 10 mm, Alt 0.1 mm
        const step = e.shiftKey ? 10 : e.altKey ? 0.1 : 1;
        nudge({ ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] }[e.key]);
        e.preventDefault();
      } else if ((e.key === "," || e.key === ".") && selectionIds().length) turnSelection((e.key === "." ? 1 : -1) * (e.shiftKey ? 90 : 15));
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

// Files for the open project, on any step: they go through the same intake table as New project
function chooseImport(files) {
  if (!files.some(f => FolderImport.MODEL.test(f.name))) { toast("Choose STL, OBJ or 3MF models", true); return; }   // the table names anything it leaves out
  if (!state.project) { toast("Open or create a project first", true); return; }
  if (isGcodeProject()) { toast("A G-code project has no models to add to", true); return; }
  if (!$("dlg-add").open) {
    state.addedWasEmpty = !data().objects.length;   // adding to a project that already has a layout leaves that layout alone
    $("add-note").textContent = ADD_NOTES[state.step] || ADD_NOTES.plates;
    $("dlg-add").showModal();
  }
  newProject.addTo({ id: state.project.id, plate: state.plate, models: state.project.models.map(m => m.id) }, files);
}

// What adding models does to the work already done, by step
const TOO_BIG = " A model too big for the bed becomes a split of its own, on the Model step.";
const ADD_NOTES = {
  model: `The models join the project as whole models on the Plates step. The split you are working on is not changed.${TOO_BIG}`,
  split: `The models join the project as whole models on the Plates step. The split you are working on is not changed.${TOO_BIG}`,
  plates: `The models are added to the current plate. Plates with a finished Slice become outdated and need slicing again.${TOO_BIG}`,
  slice: `The models are added to the current plate, which marks its Slice outdated: slice it again before sending it.${TOO_BIG}`,
};

// Done: the project now holds the added models (a too-big one as a split), so bring the workspace up to date
async function finishAdd() {
  const { added, splits } = await newProject.finishAdding();
  if (!added || !state.project) return;
  try {
    await flushSave();
    applyProject(await api("GET", `/api/projects/${state.project.id}`));
    if (splits) await splitStep.open(unplacedSplitIds(state.project)[0] ?? splitIdsOf(state.project)[0]).catch(err => toast(err.message, true));
    renderAll();
    await loadGeometry();
    if (state.addedWasEmpty && added - splits > 1) {   // they all landed on one plate at the same spot: plan once they've imported
      requestAutoPlan(state.project.id);
      maybeAutoPlan();
    }
    if (splits) {
      toast(`${plural(splits, "model")} too big for the bed became ${splits === 1 ? "a split" : "splits"}: cut ${splits === 1 ? "it" : "them"} on the Model step`);
      await setStep("model");
    }
    schedulePoll();
  } catch (err) {
    toast(err.message, true);
  }
}

// A folder pick: every STL inside it (subfolders included), in path order
function chooseFolder(files) {
  const stls = FolderImport.picked(files).files.filter(f => /\.stl$/i.test(f.name));
  if (!stls.length) { toast("That folder has no STL models", true); return; }
  chooseImport(stls);
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
    await showExport({ api, toast, save: flushSave }, state.project.id);
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.disabled = false;
  }
}

boot();
