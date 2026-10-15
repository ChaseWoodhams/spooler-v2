/* Spooler workspace – the intake table of the New project dialog and of Add files. The first dropped model makes a hidden draft
   project; every model goes through the project's model upload, and the rows come from GET /api/projects/<id>/intake-files
   (polled while any model is still being analysed). Every draft call carries the draft's created_at (draft_token), so a tab
   holding a draft that has since been swept gets a 409 and stops instead of touching whichever project took the id. Create
   promotes the draft (POST .../create); closing the dialog first discards it with a beacon. A draft id is never remembered as
   the last-open project: it is only opened once it has been created.
   The same table also adds models to a project that is already open (addTo): the rows are just the models added now, and they
   stay in the project when the dialog closes (a model too big for the bed becomes a split on Done). Mounted by main.js, which
   passes { api, upload, toast, openCreated, importNotes }. */

import { importWithReview, isReviewed } from "./import-review.js?v=2";

const POLL_MS = 1500;
const POLL_FAILURES = 3;   // consecutive failed checks before the table stops asking
const MODEL = window.FolderImport.MODEL;

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const dim = v => v >= 10 ? String(Math.round(v)) : v.toFixed(v >= 1 ? 1 : 2);
const stem = file => file.replace(/\.[^.]+$/, "");

const UNITS = [["Millimetres", 1], ["Centimetres", 10], ["Inches", 25.4], ["Metres", 1000]];   // the scale that reads a model in that unit
const options = (list, current) => list.map(([label, value]) => `<option value="${value}"${String(value) === String(current) ? " selected" : ""}>${label}</option>`).join("");

const CHIPS = {
  fits: ["ok", "Fits the bed"], split: ["split", "Too big, will be split"], check_units: ["chk", "Check units"],
};

let host = null;       // { api, upload, toast, openCreated(result), importNotes(result) }
let projectId = null;  // the draft's id once the first model has landed (or the open project's id when adding to one)
let draftToken = null; // the draft's created_at, sent with every draft call
let drafting = null;   // the pending POST /api/projects (concurrent drops wait for the same draft)
let queue = [];        // files not yet on the server: { key, name, state: queued | uploading | failed, pct, error }
let intake = null;     // the last GET intake-files
let nameTouched = false;
let nameCleared = false;  // the name field was emptied by hand: leave it empty (Create says why) until something is typed
let timer = null;
let generation = 0;    // bumped when the draft is dropped, so work for the old one stops
let uploading = false;
let creating = false;
let seq = 0;
let failures = 0;      // consecutive failed intake-files checks
let existing = null;   // adding to an open project instead: { id, plate, known: ids of the models already in it }
let importNotes = [];  // what an OrcaSlicer 3MF added to an open project had to leave out or change (from the upload responses)
let message = "";      // the last thing worth saying inline (toasts sit behind the dialog)
let unitsOpen = null;  // the model whose "units and up axis" choice is open
let pack = null;       // the packing density the draft holds: { preset, gap, tight_supports? }
let arrangeOn = true;  // Create arranges the models onto plates at that density
let preview = null;    // the plan the dialog shows: { key, state: planning | ready | error, plan, counts, error }
let previewTimer = null;
let previewRun = 0;    // bumped per plan asked for, so an answer for an older question is dropped

/** Say something where the dialog shows it, and as a toast for when the dialog is closed */
function say(text, error = true) {
  message = text;
  host.toast(text, error);
  render();
}

/** A draft call's path with the draft token attached (an open project is not a draft, so it has none) */
function guarded(path) {
  return existing || draftToken == null ? path : `${path}${path.includes("?") ? "&" : "?"}draft_token=${encodeURIComponent(draftToken)}`;
}

/** The table's rows and summary. For an open project only the models added in this session, not what was already there */
function view() {
  if (!existing || !intake) return intake;
  const rows = intake.rows.filter(r => !existing.known.has(r.model));
  const count = status => rows.filter(r => r.status === status).length;
  const summary = { models: rows.length, objects: rows.reduce((n, r) => n + r.copies, 0), split: count("split"), fits: count("fits"),
    check_units: count("check_units"), analysing: rows.filter(r => r.state !== "ready" && r.state !== "failed").length,
    failed: rows.filter(r => r.state === "failed").length };
  return { rows, summary };
}

function fileNames() {
  return [...(view()?.rows || []).map(r => r.file), ...queue.filter(q => q.state !== "failed").map(q => q.name)];
}

/** One model: its name; several: "first + N more" */
function autoName() {
  const names = fileNames();
  return !names.length ? "" : names.length === 1 ? stem(names[0]) : `${stem(names[0])} + ${names.length - 1} more`;
}

function syncName() {
  if (!nameTouched && !existing && !nameCleared) $("new-project-name").value = autoName();
}

function chipFor(row) {
  if (row.state === "failed") return '<span class="intake-chip chk">Couldn\'t read</span>';
  const chip = CHIPS[row.status];
  return chip ? `<span class="intake-chip ${chip[0]}">${chip[1]}</span>` : '<span class="intake-chip wait">Checking…</span>';
}

/** Up to three one-click unit choices, each with the size the model would have (the server lists the most common units first) */
function fixButtons(row) {
  return row.scale_fixes.slice(0, 3).map(f => `<button class="btn btn-secondary btn-sm" type="button" data-fix="${row.model}" data-scale="${f.scale}">${
    esc(f.unit[0].toUpperCase() + f.unit.slice(1))}: ${f.size.join(" × ")} mm</button>`).join(" ");
}

function rowHtml(row) {
  const size = row.size ? `${row.size.map(dim).join(" × ")} mm` : "";
  const note = row.state === "failed" ? `<small class="bad">${esc(row.error || "The model couldn't be analysed")}</small>`
    : row.status === "check_units" && row.scale_fixes?.length
      ? `<small class="warn">Looks wrong. ${fixButtons(row)}</small>`
      : row.status === "check_units" ? `<small class="warn">${esc(row.reason || "This size looks wrong")}</small>` : "";
  const choice = row.state === "ready" ? `<button class="intake-link" type="button" data-units="${row.model}" aria-expanded="${unitsOpen === row.model}">Units and up axis</button>` : "";
  const panel = unitsOpen === row.model && row.state === "ready" ? `<div class="intake-units-panel" data-units-panel="${row.model}">
      <label>Units <select data-units-scale>${options(UNITS, row.scale)}</select></label>
      <label>Up axis <select data-units-up>${options([["Z", "z"], ["Y", "y"]], row.up)}</select></label>
      <button class="btn btn-primary btn-sm" type="button" data-units-apply="${row.model}">Apply</button>
      <button class="btn btn-secondary btn-sm" type="button" data-units="${row.model}">Cancel</button>
    </div>` : "";
  return `<li class="intake-row" data-intake-row>
    <span class="intake-file" title="${esc(row.file)}">${esc(row.file)}${note}${choice}</span>
    <span class="intake-size">${size}</span>
    <input class="intake-copies" type="number" min="1" max="999" value="${row.copies}" aria-label="Copies of ${esc(row.file)}" data-copies="${row.object ?? ""}" ${row.state === "ready" && row.object ? "" : "disabled"} />
    ${chipFor(row)}
    ${row.state === "failed" ? `<button class="btn btn-secondary btn-sm" type="button" data-retry="${row.model}">Retry</button>` : ""}
    <button class="btn btn-secondary btn-sm intake-remove" type="button" data-remove="${row.model}" aria-label="Remove ${esc(row.file)}">✕</button>
    ${panel}
  </li>`;
}

function queuedHtml(item) {
  const failed = item.state === "failed";
  const note = failed ? `<small class="bad">${esc(item.error)}</small>` : "";
  return `<li class="intake-row" data-intake-row>
    <span class="intake-file" title="${esc(item.name)}">${esc(item.name)}${note}</span>
    <span class="intake-size"></span>
    <input class="intake-copies" type="number" value="1" aria-label="Copies of ${esc(item.name)}" disabled />
    ${failed ? '<span class="intake-chip chk">Upload failed</span>'
      : `<span class="intake-chip wait">${item.state === "uploading" ? `Uploading ${Math.round(item.pct)}%` : "Waiting…"}</span>`}
    <button class="btn btn-secondary btn-sm intake-remove" type="button" data-dismiss="${item.key}" aria-label="Remove ${esc(item.name)}">✕</button>
  </li>`;
}

function summaryText() {
  const s = view()?.summary;
  if (!s) return "";
  const bits = [plural(s.models, "model")];
  if (s.objects !== s.models) bits.push(plural(s.objects, "object"));
  if (s.analysing) bits.push(`checking ${s.analysing}`);
  if (s.split) bits.push(`${s.split} too big for the bed`);
  if (s.check_units) bits.push(`${s.check_units} to check`);
  if (s.failed) bits.push(`${s.failed} failed`);
  return bits.join(" · ");
}

const STEP_TEXT = {
  split: s => `${plural(s.split, "model")} bigger than the bed will be cut into parts that fit.`,
  plates: s => `${plural(s.objects, "object")} (copies included) arranged onto plates automatically.`,
  slice: () => {
    const choice = intake?.suggested_filament;
    return choice?.material ? `Suggested default: ${esc(choice.material.join(", "))} — ${esc(choice.name)}. Create adopts this material; change it in All settings before slicing.`
      : "Default material is unverified. Choose a Filament in All settings before slicing.";
  },
  print: () => "Send the plates to a printer and start them.",
};
const STEP_NAME = { split: "Split", plates: "Plates", slice: "Slice", print: "Print" };

/** The "What happens next" panel: the steps this project will take, the mixed-upload sentence and why Create is blocked */
function renderPlan() {
  const s = intake?.summary, box = $("new-intake");
  const steps = s?.steps || ["plates", "slice", "print"];
  box.querySelector(".plan-steps").innerHTML = s ? steps.map((step, i) => `<li><span class="n">${i + 1}</span><div><b>${STEP_NAME[step]}</b><small>${STEP_TEXT[step](s)}</small></div></li>`).join("") : "";
  maybePreview();
  renderPack();
  const mixed = box.querySelector(".plan-mixed");
  mixed.hidden = !(s?.split && s.fits);
  if (!mixed.hidden) {
    mixed.innerHTML = `Only the ${plural(s.split, "oversized model")} ${s.split === 1 ? "is" : "are"} split; the other ${s.fits} go to plates. <button class="intake-link" type="button" data-plan-change>Change</button>`;
  }
}

// ── Packing density and the plan preview ─────────────────────────────────────────
const PRESETS = [["tight", "Tight", 3], ["normal", "Normal", 6], ["roomy", "Roomy", 12]];

/** The plan can be made once every model is analysed and sized, and there is more than one object to place */
function planReady() {
  const s = view()?.summary;
  return !existing && !!projectId && !!s && s.objects > 1 && !s.analysing && !s.failed && !s.check_units && !uploading && !queue.length;
}

const previewKey = () => JSON.stringify([(view()?.rows || []).map(r => [r.model, r.state, r.status, r.scale, r.up, r.copies]), pack]);

/** Ask for a fresh plan when what it depends on has changed (shortly after, so typing or several fixes make one request) */
function maybePreview() {
  if (!planReady()) { clearTimeout(previewTimer); if (!projectId) preview = null; return; }
  const key = previewKey();
  if (preview?.key === key) return;
  preview = { key, state: "planning" };
  clearTimeout(previewTimer);
  previewTimer = setTimeout(() => runPreview(key), 350);
}

async function runPreview(key) {
  const run = ++previewRun, mine = generation, id = projectId;
  try {
    const plan = await host.api("POST", guarded(`/api/projects/${id}/plan`), {});
    if (run !== previewRun || mine !== generation) return;
    preview = { key, state: "ready", plan, counts: null };
    renderPack();
    const counts = await host.api("POST", guarded(`/api/projects/${id}/plan/densities`), {});   // plates at each preset: slower, so after the plan
    if (run !== previewRun || mine !== generation) return;
    preview.counts = counts;
  } catch (err) {
    if (run !== previewRun || mine !== generation) return;
    if (isGone(err)) { lostDraft(); return; }
    preview = { key, state: "error", error: err.message };
  }
  renderPack();
}

/** The plates of the plan that hold models staying whole: a model too big for the bed becomes a split, so the plate the plan gave it is not shown */
function shownPlates() {
  const oversized = new Set((view()?.rows || []).filter(r => r.status === "split").map(r => r.object));
  return (preview?.plan?.plates || []).filter(plate => plate.objects.some(o => !oversized.has(o.id)));
}

function mapHtml(plate, bed) {
  const [x0, y0, x1, y1] = bed;
  const shapes = plate.objects.filter(o => o.outline?.length > 2)
    .map(o => `<polygon class="plan-object" points="${o.outline.map(([x, y]) => `${x.toFixed(1)},${(y1 - y + y0).toFixed(1)}`).join(" ")}"><title>${esc(o.name)}</title></polygon>`).join("");
  return `<svg class="plan-map" viewBox="${x0} ${y0} ${x1 - x0} ${y1 - y0}" role="img" aria-label="${esc(plate.name)}: ${plural(plate.parts, "model")}"><rect class="plan-bed" x="${x0}" y="${y0}" width="${x1 - x0}" height="${y1 - y0}"/>${shapes}</svg>`;
}

const sentence = (n, noun) => `${n} ${noun}${n === 1 ? "" : "s"}`;

function renderPack() {
  const section = $("new-intake").querySelector(".pack"), s = view()?.summary;
  section.hidden = !(s && s.objects > 1 && pack);
  if (section.hidden) return;
  if (section.contains(document.activeElement) && document.activeElement.matches("input[type=number]")) return;   // not under a field being typed in
  const counts = preview?.counts, hiddenPlates = s.split || 0;
  section.querySelector(".pack-presets").innerHTML = [...PRESETS, ["custom", "Custom", pack.preset === "custom" ? pack.gap : null]].map(([key, label, gap]) => {
    const plates = counts?.[key] && !counts[key].refused ? Math.max(1, counts[key].plates - hiddenPlates) : null;
    const sub = key === "custom" ? (gap == null ? "your own gap" : `${gap} mm`) : `${gap} mm apart`;
    return `<button class="btn btn-secondary btn-sm" type="button" data-pack="${key}" aria-pressed="${pack.preset === key}">${label}<small>${sub}${plates ? ` · ${sentence(plates, "plate")}` : ""}</small></button>`;
  }).join("");
  const custom = section.querySelector(".pack-custom");
  custom.hidden = pack.preset !== "custom";
  if (!custom.hidden) custom.querySelector("input").value = pack.gap;
  section.querySelector("[data-pack-supports]").checked = !!pack.tight_supports;
  section.querySelector(".pack-note").textContent = pack.tight_supports ? "Models with supports are packed as close as the gap says."
    : "Models that need supports keep 22 mm apart; the rest keep the gap.";
  section.querySelector("[data-pack-arrange]").checked = arrangeOn;
  const box = section.querySelector(".pack-preview");
  if (!arrangeOn) { box.textContent = "Everything stays on plate 1 until you arrange it."; return; }
  if (!preview || preview.state === "planning") { box.textContent = "Planning…"; return; }
  if (preview.state === "error") { box.innerHTML = `<span class="warn">Couldn't plan: ${esc(preview.error)}. The models are added to plate 1.</span>`; return; }
  const plan = preview.plan;
  if (plan.refused) { box.innerHTML = `<span class="warn">No plan: ${esc(plan.refused)}. The models are added to plate 1.</span>`; return; }
  const plates = shownPlates();
  const later = s.split ? `<span class="slicer-muted">The ${sentence(s.split, "oversized model")} will be cut on the Split step, then packed the same way.</span>` : "";
  box.innerHTML = `<span>${plates.length ? `${sentence(plates.length, "plate")} for ${sentence(plates.reduce((n, p) => n + p.parts, 0), "model")}` : "Nothing to place yet"}${
    plates.some(p => p.turned) ? ` · ${plates.reduce((n, p) => n + p.turned, 0)} turned` : ""}</span>
    <div class="pack-maps">${plates.map(p => `<div class="pack-map-card">${mapHtml(p, plan.bed)}<small>${esc(p.name)} · ${p.parts}</small></div>`).join("")}</div>${later}`;
}

/** Choose a density: the draft holds it (so the plan and Create use it) and it is remembered for the next project */
async function choosePack(next) {
  pack = next;
  preview = null;
  renderPack();
  await act(() => host.api("POST", guarded(`/api/projects/${projectId}/pack`), { pack: next }));
  host.api("PATCH", "/api/defaults", { pack: next }).catch(() => { /* remembering is best effort */ });
}

/** Create's (or Done's) state and the plain reason beside it */
function refreshAction() {
  if (existing) {
    $("btn-add-done").disabled = !!blocker();
    $("add-reason").textContent = blocker();
    return;
  }
  $("btn-new-create").disabled = !canCreate();
  $("new-create-reason").textContent = creating ? "" : blocker();   // before any model is added it says to add one
}

function render() {
  const rows = view()?.rows || [], pending = queue.length;
  const line = $(existing ? "add-message" : "new-message");   // outside the table, which is hidden when there is nothing in it
  line.hidden = !message;
  line.textContent = message;
  const box = $(existing ? "add-intake" : "new-intake");
  box.hidden = !rows.length && !pending;
  if (!existing) $("dlg-projects").classList.toggle("has-intake", !box.hidden);
  if (!existing) renderPlan();
  if (document.activeElement?.matches?.(".intake-copies, .intake-units-panel select")) {
    // Don't rebuild the list under a field being typed in; the next poll catches up.
    refreshAction();
    return;
  }
  box.querySelector(".intake-list").innerHTML = rows.map(rowHtml).join("") + queue.map(queuedHtml).join("");
  box.querySelector(".intake-summary").textContent = summaryText();
  const notes = box.querySelector(".intake-notes");
  notes.hidden = !importNotes.length;
  notes.textContent = importNotes.length ? `Opened with ${importNotes.length === 1 ? "a note" : `${importNotes.length} notes`}: ${importNotes.join(" · ")}` : "";
  syncName();   // before the action, which wants the name
  refreshAction();
}

const busy = () => uploading || queue.some(q => q.state !== "failed") || (view()?.summary.analysing || 0) > 0;
/** Done (adding to an open project) and Escape wait for the work in flight and for every size to be settled */
const blocked = () => !!existing && !!blocker();

/** Why Create (or Done) can't be pressed yet, in plain words ("" when it can) */
function blocker() {
  const s = view()?.summary;
  if (failures >= POLL_FAILURES) return "Spooler isn't answering. Reload the page to carry on.";
  if (!existing && !s?.models && !queue.length) return "Add a model to continue.";
  if (busy()) return "Waiting for the models to be checked.";
  if (s?.failed || queue.some(q => q.state === "failed")) return "Retry or remove the model that couldn't be read.";
  if (s?.check_units) return `Check the size of ${plural(s.check_units, "model")} marked Check units.`;
  if (!existing && !$("new-project-name").value.trim()) return "Give the project a name.";
  return "";
}
const canCreate = () => !!projectId && !creating && !blocker() && !!intake?.summary.can_create;

/** The draft no longer exists (swept, or already created in another tab): stop, forget it, and say so */
function lostDraft() {
  dropDraft();
  say("This new project is no longer there (it timed out or was already created). Add your models again to start over.");
}

// The server says so in these words (409: the draft is no longer the one this page made; 404: it is deleted)
const GONE = /^(This new project was already|Project not found)/;
const isGone = err => !existing && GONE.test(err.message);

async function refresh() {
  if (!projectId) return;
  const mine = generation;
  try {
    const result = await host.api("GET", guarded(`/api/projects/${projectId}/intake-files`));
    if (mine !== generation) return;
    intake = result;
    failures = 0;
  } catch (err) {
    if (mine !== generation) return;
    if (isGone(err)) { lostDraft(); return; }
    if (++failures === POLL_FAILURES) say("Spooler isn't answering, so the models can't be checked. Reload the page to carry on.");   // once, not per poll
  }
  render();
  clearTimeout(timer);
  if (mine === generation && failures < POLL_FAILURES && (intake?.summary.analysing || uploading)) timer = setTimeout(refresh, POLL_MS);
}

async function ensureDraft() {
  if (projectId) return projectId;
  const mine = generation;
  drafting ||= host.api("POST", "/api/projects", { name: $("new-project-name").value.trim() || "Untitled project", draft: true });
  try {
    const project = await drafting;
    if (mine !== generation) { beacon(project.id, project.created_at); return null; }   // closed while it was being made
    projectId = project.id;
    draftToken = project.created_at ?? null;
    pack = project.data?.pack || { preset: "normal", gap: 6 };
  } finally { drafting = null; }
  return projectId;
}

async function drain() {
  if (uploading) return;
  uploading = true;
  const mine = generation;
  try {
    for (let item; mine === generation && (item = queue.find(q => q.state === "queued"));) {
      item.state = "uploading";
      render();
      try {
        const id = await ensureDraft();
        if (!id) return;
        const params = new URLSearchParams({ filename: item.name, scale: "1", up: "z" });   // millimetres, Z up; a wrong guess is fixed per model
        if (existing) params.set("plate", existing.plate);
        const onProgress = pct => { item.pct = pct; render(); };
        const result = isReviewed(item.file)   // a 3MF is inspected first; Continue with losses commits it, Cancel leaves the project as it was
          ? await importWithReview({ file: item.file, host, target: id, draftToken: existing ? null : draftToken,
              query: existing ? { plate: existing.plate } : {}, onProgress })
          : await host.upload(guarded(`/api/projects/${id}/models/upload?${params}`), item.file, onProgress);
        if (mine !== generation) return;
        if (result === null) {   // cancelled in the review: nothing was imported
          queue = queue.filter(q => q !== item);
          message = `Import of ${item.name} cancelled — the project was not changed.`;
          host.toast(message);
          render();
          continue;
        }
        if (result?.separate) host.toast(`${item.name} was imported as its own project, “${result.name}”.`);
        if (existing) importNotes = [...new Set([...importNotes, ...(result?.import_notes || [])])];
        queue = queue.filter(q => q !== item);
        await refresh();
      } catch (err) {
        if (mine !== generation) return;
        if (isGone(err) && projectId) { lostDraft(); return; }
        item.state = "failed";
        item.error = err.message;
        render();
      }
    }
  } finally {
    if (mine === generation) { uploading = false; await refresh(); }
  }
}

/** Models dropped or picked: a row per model at once, then uploaded one after another. Anything else in the drop is named */
function add(files) {
  const models = files.filter(f => MODEL.test(f.name));
  const skipped = files.filter(f => !MODEL.test(f.name));
  if (!models.length) { host.toast("Choose STL, OBJ or 3MF models", true); return; }
  message = skipped.length
    ? `Left out ${skipped.map(f => f.name).join(", ")}: only STL, OBJ and 3MF models go in together.${existing ? "" : " Drop a G-code file on its own to open it as a project."}` : "";
  if (message) host.toast(message, true);
  for (const file of models) queue.push({ key: ++seq, file, name: file.name, state: "queued", pct: 0 });
  render();
  drain();
}

function dropDraft() {
  generation++;
  $("new-message").hidden = $("add-message").hidden = true;
  clearTimeout(timer);
  projectId = draftToken = drafting = intake = unitsOpen = pack = preview = null;
  clearTimeout(previewTimer);
  importNotes = [];
  message = "";
  queue = [];
  uploading = false;
  creating = false;
  failures = 0;
  if (existing) { existing = null; $("add-intake").hidden = true; return; }
  if (!nameTouched) $("new-project-name").value = "";
  nameTouched = nameCleared = false;
  $("new-intake").hidden = true;
  $("dlg-projects").classList.remove("has-intake");
  refreshAction();   // back to "Add a model to continue"
}

/** Models for a project that is already open: they upload straight into it and show here until Done */
function addTo(project, files) {
  if (existing && existing.id !== project.id) dropDraft();
  if (!existing) {
    projectId = project.id;
    existing = { id: project.id, plate: project.plate, known: new Set(project.models) };
  }
  add(files);
}

/** Done: turn each model too big for the bed into a split, forget the table, and say what was added (the project keeps it all).
    Resolves to { added, splits } */
async function finishAdding() {
  const rows = view()?.rows || [], id = existing?.id;
  if (!existing) return { added: rows.length, splits: 0 };
  const oversized = rows.filter(r => r.status === "split").map(r => r.model);
  if (importNotes.length) host.importNotes({ import_notes: importNotes });   // the dialog hides toasts, so say it once it has closed
  dropDraft();
  let splits = 0;
  for (const model of oversized) {
    try {
      await host.api("POST", `/api/projects/${id}/models/${model}/make-split`, {});
      splits++;
    } catch (err) { host.toast(`Couldn't turn a model that is too big for the bed into a split: ${err.message}`, true); }
  }
  return { added: rows.length, splits };
}

function beacon(id, token) {
  const url = `/api/projects/${id}/discard-draft${token == null ? "" : `?draft_token=${encodeURIComponent(token)}`}`;
  if (!navigator.sendBeacon?.(url)) fetch(url, { method: "POST", keepalive: true }).catch(() => { /* the idle sweep removes it */ });
}

/** The dialog was cancelled or closed: delete the draft (best effort, also when the tab is going away). A draft still being
    made is discarded the moment it exists (ensureDraft sees the new generation) */
function discard() {
  if (creating || existing || (!projectId && !drafting)) return;
  const id = projectId, token = draftToken;
  dropDraft();
  if (id) beacon(id, token);
}

/** Change (mixed upload): a model is usually only "too big" because of its units or up axis, so open the units choice of the
    first oversized model, where it can be put right */
function changePlan() {
  const row = view()?.rows.find(r => r.status === "split");
  if (!row) return;
  unitsOpen = row.model;
  render();
  $("new-intake").querySelector(`[data-units-panel="${row.model}"] select`)?.focus();
}

async function act(handler) {
  message = "";
  try { await handler(); } catch (err) {
    if (isGone(err)) { lostDraft(); return; }
    say(err.message);
  }
  await refresh();
}

async function create() {
  if (!canCreate()) return;
  creating = true;
  message = "";
  render();
  try {
    const objects = intake.summary.objects;
    const result = await host.api("POST", guarded(`/api/projects/${projectId}/create`), { name: $("new-project-name").value.trim(), arrange: arrangeOn });
    const id = projectId;
    dropDraft();
    await host.openCreated(result, { plan: false, id });   // Create has already arranged the models at the chosen density, so they don't sit stacked on plate 1
  } catch (err) {
    creating = false;
    if (isGone(err)) { lostDraft(); return; }
    say(err.message);
  }
}

function bindPack() {
  const section = $("new-intake").querySelector(".pack");
  section.querySelector("[data-pack-gap]").addEventListener("change", e => {
    const gap = Math.min(40, Math.max(2, Number(e.target.value) || 6));
    choosePack({ preset: "custom", gap, ...(pack?.tight_supports ? { tight_supports: true } : {}) });
  });
  section.querySelector("[data-pack-supports]").addEventListener("change", e => {
    if (!pack) return;
    const { tight_supports, ...rest } = pack;
    choosePack(e.target.checked ? { ...rest, tight_supports: true } : rest);
  });
  section.querySelector("[data-pack-arrange]").addEventListener("change", e => { arrangeOn = e.target.checked; renderPack(); });
}

function bind() {
  bindPack();
  refreshAction();   // Create is on show from the start, with the reason it cannot be used yet
  $("new-project-name").addEventListener("input", e => { nameTouched = !!e.target.value.trim(); nameCleared = !e.target.value; refreshAction(); });
  $("btn-new-create").addEventListener("click", create);
  $("dlg-projects").addEventListener("close", discard);
  window.addEventListener("pagehide", discard);
  for (const list of [$("new-intake"), $("add-intake")]) {
    list.addEventListener("click", e => {
      const remove = e.target.closest("[data-remove]"), retry = e.target.closest("[data-retry]");
      const units = e.target.closest("[data-units]"), apply = e.target.closest("[data-units-apply]");
      const fix = e.target.closest("[data-fix]"), dismiss = e.target.closest("[data-dismiss]"), change = e.target.closest("[data-plan-change]");
      const density = e.target.closest("[data-pack]");
      if (density && pack) {
        const key = density.dataset.pack;
        choosePack(key === "custom" ? { preset: "custom", gap: pack.preset === "custom" ? pack.gap : pack.gap || 6, ...(pack.tight_supports ? { tight_supports: true } : {}) }
          : { preset: key, gap: PRESETS.find(p => p[0] === key)[2], ...(pack.tight_supports ? { tight_supports: true } : {}) });
        return;
      }
      const base = `/api/projects/${projectId}/models`;
      if (change) changePlan();
      else if (dismiss) { queue = queue.filter(q => q.key !== Number(dismiss.dataset.dismiss)); render(); }
      else if (units) { unitsOpen = unitsOpen === Number(units.dataset.units) ? null : Number(units.dataset.units); render(); }
      else if (apply) {
        const panel = apply.closest("[data-units-panel]");
        unitsOpen = null;
        act(() => host.api("POST", guarded(`${base}/${apply.dataset.unitsApply}/settings`),
          { scale: Number(panel.querySelector("[data-units-scale]").value), up: panel.querySelector("[data-units-up]").value }));
      }
      else if (remove) act(() => host.api("DELETE", guarded(`${base}/${remove.dataset.remove}`)));
      else if (retry) act(() => host.api("POST", guarded(`${base}/${retry.dataset.retry}/retry`), {}));
      else if (fix) act(() => host.api("POST", guarded(`${base}/${fix.dataset.fix}/settings`), { scale: Number(fix.dataset.scale) }));
    });
    list.addEventListener("change", e => {
      const field = e.target.closest("[data-copies]");
      if (!field) return;
      const copies = Math.max(1, Math.round(Number(field.value) || 1));
      act(() => host.api("POST", guarded(`/api/projects/${projectId}/copies`), { object: Number(field.dataset.copies), copies }));
    });
  }
}

export const newProject = {
  attach(deps) { host = deps; bind(); },
  add,
  addTo,
  finishAdding,
  blocked,
  discard,
};
