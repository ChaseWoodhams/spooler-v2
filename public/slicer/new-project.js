/* Spooler workspace – the New project page's intake table. The first dropped file makes a hidden draft project; every file
   goes through the project's model upload, and the rows come from GET /api/projects/<id>/intake-files (polled while any model
   is still being analysed). Create promotes the draft (POST .../create); closing the Projects page first discards it with a
   beacon. A draft id is never remembered as the last-open project: it is only opened once it has been created.
   The same table also adds files to a project that is already open (addTo): the rows are just the files added now, and they
   stay in the project when the dialog closes. Mounted by main.js, which passes { api, upload, toast, openCreated }. */

const POLL_MS = 1500;
const MODEL = /\.(stl|obj|3mf)$/i;

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const dim = v => v >= 10 ? String(Math.round(v)) : v.toFixed(v >= 1 ? 1 : 2);
const stem = file => file.replace(/\.[^.]+$/, "");

const UNITS = [["Millimetres", 1], ["Centimetres", 10], ["Inches", 25.4], ["Metres", 1000]];   // the scale that reads a file in that unit
const options = (list, current) => list.map(([label, value]) => `<option value="${value}"${String(value) === String(current) ? " selected" : ""}>${label}</option>`).join("");

const CHIPS = {
  fits: ["ok", "Fits the bed"], split: ["split", "Too big, will be split"], check_units: ["chk", "Check units"],
};

let host = null;     // { api, upload, toast, openCreated(result) }
let draft = null;    // the draft project's id, once the first file has landed
let drafting = null; // the pending POST /api/projects (concurrent drops wait for the same draft)
let queue = [];      // files not yet on the server: { key, name, state: queued | uploading | failed, pct, error }
let intake = null;   // the last GET intake-files
let nameTouched = false;
let timer = null;
let generation = 0;  // bumped when the draft is dropped, so work for the old one stops
let uploading = false;
let creating = false;
let seq = 0;
let existing = null; // adding to an open project instead: { id, plate, known: ids of the models already in it }

/** The table's rows and summary. For an open project only the files added in this session, not what was already there */
function view() {
  if (!existing || !intake) return intake;
  const rows = intake.rows.filter(r => !existing.known.has(r.model));
  const count = status => rows.filter(r => r.status === status).length;
  const summary = { files: rows.length, items: rows.reduce((n, r) => n + r.copies, 0), split: count("split"), fits: count("fits"),
    check_units: count("check_units"), analysing: rows.filter(r => r.state !== "ready" && r.state !== "failed").length,
    failed: rows.filter(r => r.state === "failed").length };
  return { rows, summary };
}

let unitsOpen = null; // the model whose "units and up axis" choice is open

function fileNames() {
  return [...(view()?.rows || []).map(r => r.file), ...queue.filter(q => q.state !== "failed").map(q => q.name)];
}

/** One file: its name; several: "first + N more" */
function autoName() {
  const names = fileNames();
  return !names.length ? "" : names.length === 1 ? stem(names[0]) : `${stem(names[0])} + ${names.length - 1} more`;
}

function syncName() {
  if (!nameTouched && !existing) $("new-project-name").value = autoName();
}

function chipFor(row) {
  if (row.state === "failed") return '<span class="intake-chip chk">Couldn\'t read</span>';
  const chip = CHIPS[row.status];
  return chip ? `<span class="intake-chip ${chip[0]}">${chip[1]}</span>` : '<span class="intake-chip wait">Checking…</span>';
}

/** Up to three one-click unit choices, each with the size the model would have (the server lists the most common units first) */
function fixButtons(row) {
  const fixes = row.scale_fixes?.length ? row.scale_fixes.slice(0, 3)
    : [{ scale: row.scale_fix.scale, label: `Use ×${row.scale_fix.factor}` }];   // an older server: the single guess
  return fixes.map(f => `<button class="btn btn-secondary btn-sm" type="button" data-fix="${row.model}" data-scale="${f.scale}">${
    f.unit ? `${esc(f.unit[0].toUpperCase() + f.unit.slice(1))}: ${f.size.join(" × ")} mm` : esc(f.label)}</button>`).join(" ");
}

function rowHtml(row) {
  const size = row.size ? `${row.size.map(dim).join(" × ")} mm` : "";
  const note = row.state === "failed" ? `<small class="bad">${esc(row.error || "The file couldn't be analysed")}</small>`
    : row.status === "check_units" && (row.scale_fixes?.length || row.scale_fix)
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
  const bits = [plural(s.files, "file")];
  if (s.items !== s.files) bits.push(plural(s.items, "item"));
  if (s.analysing) bits.push(`checking ${s.analysing}`);
  if (s.split) bits.push(`${s.split} too big for the bed`);
  if (s.check_units) bits.push(`${s.check_units} to check`);
  if (s.failed) bits.push(`${s.failed} failed`);
  return bits.join(" · ");
}

function render() {
  const rows = view()?.rows || [], pending = queue.length;
  const box = $(existing ? "add-intake" : "new-intake");
  box.hidden = !rows.length && !pending;
  if (!existing) $("dlg-projects").classList.toggle("has-intake", !box.hidden);
  const action = existing ? $("btn-add-done") : $("btn-new-create");   // Done only waits for the work in flight; Create also wants a clean table
  if (document.activeElement?.matches?.(".intake-copies, .intake-units-panel select")) {
    // Don't rebuild the list under a field being typed in; the next poll catches up.
    action.disabled = existing ? busy() : !canCreate();
    return;
  }
  box.querySelector(".intake-list").innerHTML = rows.map(rowHtml).join("") + queue.map(queuedHtml).join("");
  box.querySelector(".intake-summary").textContent = summaryText();
  if (!existing) action.hidden = box.hidden;
  action.disabled = existing ? busy() : !canCreate();
  syncName();
}

const busy = () => uploading || queue.some(q => q.state !== "failed") || (view()?.summary.analysing || 0) > 0;
const canCreate = () => !!draft && !creating && !busy() && !!intake?.summary.can_create;

async function refresh() {
  if (!draft) return;
  const mine = generation;
  try {
    const result = await host.api("GET", `/api/projects/${draft}/intake-files`);
    if (mine !== generation) return;
    intake = result;
  } catch (err) {
    if (mine === generation) host.toast(err.message, true);
  }
  render();
  clearTimeout(timer);
  if (mine === generation && (intake?.summary.analysing || uploading)) timer = setTimeout(refresh, POLL_MS);
}

async function ensureDraft() {
  if (draft) return draft;
  const mine = generation;
  drafting ||= host.api("POST", "/api/projects", { name: $("new-project-name").value.trim() || "Untitled project", draft: true });
  try {
    const project = await drafting;
    if (mine !== generation) { navigator.sendBeacon?.(`/api/projects/${project.id}/discard-draft`); return null; }   // closed while it was being made
    draft = project.id;
  } finally { drafting = null; }
  return draft;
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
        const params = new URLSearchParams({ filename: item.name, scale: "1", up: "z" });   // millimetres, Z up; a wrong guess is fixed per file
        if (existing) params.set("plate", existing.plate);
        await host.upload(`/api/projects/${id}/models/upload?${params}`, item.file, pct => { item.pct = pct; render(); });
        if (mine !== generation) return;
        queue = queue.filter(q => q !== item);
        await refresh();
      } catch (err) {
        if (mine !== generation) return;
        item.state = "failed";
        item.error = err.message;
        render();
      }
    }
  } finally {
    if (mine === generation) { uploading = false; await refresh(); }
  }
}

/** Files dropped or picked: a row per model file at once, then uploaded one after another */
function add(files) {
  const models = files.filter(f => MODEL.test(f.name));
  if (!models.length) { host.toast("Choose STL, OBJ or 3MF files", true); return; }
  for (const file of models) queue.push({ key: ++seq, file, name: file.name, state: "queued", pct: 0 });
  render();
  drain();
}

function dropDraft() {
  generation++;
  clearTimeout(timer);
  draft = drafting = intake = unitsOpen = null;
  queue = [];
  uploading = false;
  creating = false;
  if (existing) { existing = null; $("add-intake").hidden = true; return; }
  if (!nameTouched) $("new-project-name").value = "";
  nameTouched = false;
  $("new-intake").hidden = true;
  $("btn-new-create").hidden = true;
  $("dlg-projects").classList.remove("has-intake");
}

/** Files for a project that is already open: they upload straight into it and show here until Done */
function addTo(project, files) {
  if (existing && existing.id !== project.id) dropDraft();
  if (!existing) {
    draft = project.id;
    existing = { id: project.id, plate: project.plate, known: new Set(project.models) };
  }
  add(files);
}

/** Done: forget the table and say how many files it holds (the project keeps them) */
function finishAdding() {
  const added = view()?.rows.length || 0;
  if (existing) dropDraft();
  return added;
}

/** The page was cancelled or closed: delete the draft (best effort, also when the tab is going away) */
function discard() {
  const id = draft;
  if (!id || creating || existing) return;
  dropDraft();
  const url = `/api/projects/${id}/discard-draft`;
  if (!navigator.sendBeacon?.(url)) fetch(url, { method: "POST", keepalive: true }).catch(() => { /* the idle sweep removes it */ });
}

async function act(handler) {
  try { await handler(); } catch (err) { host.toast(err.message, true); }
  await refresh();
}

async function create() {
  if (!canCreate()) return;
  creating = true;
  render();
  try {
    const items = intake.summary.items;
    const result = await host.api("POST", `/api/projects/${draft}/create`, { name: $("new-project-name").value.trim() });
    const id = draft;
    dropDraft();
    await host.openCreated(result, { plan: items > 1, id });
  } catch (err) {
    creating = false;
    host.toast(err.message, true);
    render();
  }
}

function bind() {
  $("new-project-name").addEventListener("input", e => { nameTouched = !!e.target.value.trim(); });
  $("btn-new-create").addEventListener("click", create);
  $("dlg-projects").addEventListener("close", discard);
  window.addEventListener("pagehide", discard);
  for (const list of [$("new-intake"), $("add-intake")]) {
  list.addEventListener("click", e => {
    const remove = e.target.closest("[data-remove]"), retry = e.target.closest("[data-retry]");
    const units = e.target.closest("[data-units]"), apply = e.target.closest("[data-units-apply]");
    const fix = e.target.closest("[data-fix]"), dismiss = e.target.closest("[data-dismiss]");
    if (dismiss) { queue = queue.filter(q => q.key !== Number(dismiss.dataset.dismiss)); render(); }
    else if (units) { unitsOpen = unitsOpen === Number(units.dataset.units) ? null : Number(units.dataset.units); render(); }
    else if (apply) {
      const panel = apply.closest("[data-units-panel]");
      unitsOpen = null;
      act(() => host.api("POST", `/api/projects/${draft}/models/${apply.dataset.unitsApply}/settings`,
        { scale: Number(panel.querySelector("[data-units-scale]").value), up: panel.querySelector("[data-units-up]").value }));
    }
    else if (remove) act(() => host.api("DELETE", `/api/projects/${draft}/models/${remove.dataset.remove}`));
    else if (retry) act(() => host.api("POST", `/api/projects/${draft}/models/${retry.dataset.retry}/retry`, {}));
    else if (fix) act(() => host.api("POST", `/api/projects/${draft}/models/${fix.dataset.fix}/settings`, { scale: Number(fix.dataset.scale) }));
  });
  list.addEventListener("change", e => {
    const field = e.target.closest("[data-copies]");
    if (!field) return;
    const copies = Math.max(1, Math.round(Number(field.value) || 1));
    act(() => host.api("POST", `/api/projects/${draft}/copies`, { object: Number(field.dataset.copies), copies }));
  });
  }
}

export const newProject = {
  attach(deps) { host = deps; bind(); },
  add,
  addTo,
  finishAdding,
  busy: () => busy(),
  discard,
};
