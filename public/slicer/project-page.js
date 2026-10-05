/* Spooler project workspace – the Print step: the project page. A progress header, a printer card for each printer, a
   colour-coded map of every plate beside a Next up list, and a detail card for the selected plate. Mounted by main.js
   (which owns the step bar); reads /api/projects/<id>/status, /api/projects/<id>/jobs and /api/printers, and polls them
   while the step is on screen. Reprint, Hold and Bed cleared wait for their own backend and show disabled or not at all. */

const STATUS = {
  unsliced: "Not sliced", sliced: "Sliced", waiting: "Waiting on plate", queued: "Queued",
  printing: "Printing", printed_ok: "Printed OK", failed: "Failed",
};
const BAR_ORDER = ["printed_ok", "unsliced", "sliced", "waiting", "queued", "printing", "failed"];  // plate order is never status order
const POLL_MS = 4000;
const NEXT_UP = 6;

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

let host = null;   // { api, toast, projectId(), sliceState(plateId), onSlice(id), onOpenPlate(id), onSetPrinter(id, printerId), hasAssembly(), onOpenAssembly() }
let shown = false;
let status = null; // GET /api/projects/<id>/status
let jobs = [];
let printers = [];
let awaiting = new Set();   // printer ids whose last print is waiting for an answer (OutcomePrompt)
let focusedPrinter = false; // a push deep link (?printer=) is scrolled to once
let selected = null;
let timer = null;

function duration(seconds) {
  if (!seconds) return "";
  const minutes = Math.round(seconds / 60);
  return minutes >= 60 ? `${Math.floor(minutes / 60)} h ${minutes % 60} min` : `${minutes} min`;
}

const chip = s => `<span class="pp-chip st-${s}"><i class="pp-dot st-${s}"></i>${STATUS[s]}</span>`;
const printerName = id => printers.find(p => p.id === id)?.name || "";
const plateOf = id => status?.plates.find(p => p.id === id);

function header() {
  const { progress } = status;
  const c = progress.counts;
  const grams = progress.filament_g;
  const waiting = c.queued + c.waiting, toSlice = c.sliced + c.unsliced;
  return `<div class="pp-card pp-progress">
    <div class="pp-progress-head"><b>${progress.printed_ok} of ${plural(progress.total, "plate")} printed</b>
      <span class="slicer-muted">${c.failed} failed · ${c.printing} printing · ${waiting} waiting · ${toSlice} to slice</span></div>
    <div class="pp-bar" role="img" aria-label="${progress.printed_ok} of ${progress.total} plates printed">${BAR_ORDER.map(s =>
      `<i class="st-${s}" style="width:${progress.total ? c[s] / progress.total * 100 : 0}%" title="${STATUS[s]}: ${c[s]}"></i>`).join("")}</div>
    ${filament(grams)}
  </div>`;
}

function filament(grams) {
  if (!grams.total && !grams.used) return "";
  const pct = grams.total ? Math.min(100, grams.used / grams.total * 100) : 0;
  const open = grams.unknown_plates ? ` (${plural(grams.unknown_plates, "plate")} not sliced yet, so not counted)` : "";
  const spool = grams.spool ? ` · spool ${esc(grams.spool.name || "#" + grams.spool.id)}${grams.spool.remaining_g != null ? `: ${Math.round(grams.spool.remaining_g)} g left${
    grams.spool.remaining_g < grams.total - grams.used ? ` <b class="pp-warn">(not enough for the rest)</b>` : ""}` : ""}` : "";
  return `<div class="pp-filament"><div class="pp-bar pp-bar-filament" role="img" aria-label="${Math.round(grams.used)} of ${Math.round(grams.total)} grams of filament used"><i style="width:${pct}%"></i></div>
    <span class="slicer-muted">Filament: ${Math.round(grams.used)} g used of ${Math.round(grams.total)} g needed${open}${spool}</span></div>`;
}

function printerCards() {
  if (!printers.length) return `<div class="pp-card slicer-muted">No printers that the queue can use yet.</div>`;
  return `<div class="pp-printers">${printers.map(printer => {
    const running = jobs.find(j => j.printer_id === printer.id && (j.state === "printing" || j.state === "starting"));
    const queued = jobs.filter(j => j.printer_id === printer.id && j.state === "queued").length;
    const info = printer.status?.PrintInfo || {};
    const plate = running ? plateOf(running.plate_id) : null;
    const layers = running && info.TotalLayer ? ` · layer ${info.CurrentLayer || 0} of ${info.TotalLayer}` : "";
    const [cls, label, note] = !printer.connected ? ["failed", "Offline", "Not connected"]
      : running ? ["printing", "Printing", `${plate ? esc(plate.name) : esc(running.name)}${layers}`]
      : ["sliced", "Free", queued ? `${plural(queued, "plate")} queued for it` : "Nothing queued for it"];
    const asking = awaiting.has(printer.id);
    return `<div class="pp-card pp-printer st-${cls}" id="pp-printer-${esc(printer.id)}"><b>${esc(printer.name)}</b> <span class="pp-chip st-${cls}">${label}</span>
      <div class="slicer-muted">${note}</div>
      ${asking ? `<button type="button" class="btn btn-primary btn-sm" data-outcome="${esc(printer.id)}">Print ended: Printed OK · Bed cleared / Failed…</button>` : ""}</div>`;
  }).join("")}</div>`;
}

function plateMap() {
  return `<div class="pp-card"><div class="slicer-muted">Every plate at a glance. Colour is status; click one for its detail.</div>
    <div class="pp-grid">${status.plates.map(p =>
      `<button type="button" class="pp-cell st-${p.status}${p.id === selected ? " sel" : ""}" data-plate="${p.id}" title="${esc(p.name)}: ${STATUS[p.status]}">${p.id}</button>`).join("")}</div>
    <div class="pp-legend">${Object.keys(STATUS).map(chip).join("")}</div></div>`;
}

function nextUp() {
  const next = status.plates.filter(p => ["queued", "waiting", "sliced"].includes(p.status)).slice(0, NEXT_UP);
  const queued = jobs.filter(j => j.state === "queued");
  const canAssign = queued.length > 1 && printers.some(p => p.connected);
  return `<div class="pp-card"><b>Next up</b>
    ${next.map(p => `<div class="pp-next" data-plate="${p.id}"><span>${esc(p.name)}${p.printer_id ? ` → ${esc(printerName(p.printer_id) || "printer")}` : ""}</span>${chip(p.status)}</div>`).join("")
      || '<p class="slicer-muted">Nothing waiting. Slice a plate and add it to the queue.</p>'}
    <button type="button" class="btn btn-primary btn-sm" data-act="assign"${canAssign ? "" : " disabled"}
      title="Spread the queued plates over the connected printers so they finish about together, by estimated print time">Assign printers · balance by time</button></div>`;
}

function detail() {
  const plate = plateOf(selected);
  if (!plate) return `<div class="pp-card slicer-muted">Select a plate.</div>`;
  const slicing = ["queued", "slicing"].includes(host.sliceState(plate.id));
  const facts = [plural(plate.parts, "part"), duration(plate.estimate_s), plate.estimate_g ? `${Math.round(plate.estimate_g)} g` : ""].filter(Boolean).join(" · ");
  const editable = ["unsliced", "sliced", "waiting", "queued", "failed"].includes(plate.status);
  const options = ['<option value="">Next free printer</option>', ...printers.map(p =>
    `<option value="${esc(p.id)}"${p.id === plate.printer_id ? " selected" : ""}>${esc(p.name)}${p.connected ? "" : " (offline)"}</option>`)].join("");
  const btn = (act, label, primary, extra = "") => `<button type="button" class="btn ${primary ? "btn-primary" : "btn-secondary"} btn-sm" data-act="${act}"${extra}>${label}</button>`;
  const disabled = reason => ` disabled title="${reason}"`;
  const actions = [];
  if (plate.status === "unsliced") actions.push(btn("slice", slicing ? "Slicing…" : "Slice", true, slicing ? " disabled" : ""));
  if (plate.status === "sliced") {
    actions.push(btn("queue", "Add to queue", true, plate.outdated ? disabled("Changed since slicing: slice it again first") : ""));
    if (plate.outdated) actions.push(btn("slice", slicing ? "Slicing…" : "Slice again", true, slicing ? " disabled" : ""));
  }
  if (plate.status === "queued" || plate.status === "waiting") {
    actions.push(`<a class="btn btn-primary btn-sm" href="/?review=${plate.job_id}" title="Preview the toolpath, run the preflight checks and start">Review &amp; start</a>`);
    actions.push(btn("hold", "Hold after…", false, disabled("Holding a plate for another is not available yet")));
  }
  if (plate.status === "failed") actions.push(btn("reprint", "Reprint", true, disabled("Reprinting a failed plate is not available yet")));
  actions.push(btn("open", "Open in Plates", false));
  if (host.hasAssembly?.()) actions.push(btn("assembly", "Assembly order", false, ' title="Which parts join in what order, from the Split step"'));
  const picture = `/api/projects/${host.projectId()}/plates/${plate.id}/preview.png?v=${plate.layout}`;
  return `<div class="pp-card"><div><b>${esc(plate.name)}</b> ${chip(plate.status)}</div>
    ${plate.parts ? `<img class="pp-preview" src="${picture}" alt="${esc(plate.name)} as laid out" width="320" height="240" loading="lazy">` : ""}
    <div class="slicer-muted">${facts || "No slice yet"}</div>
    ${plate.status === "waiting" ? '<div class="slicer-muted">Waiting on another plate to print OK first</div>' : ""}
    ${editable ? `<label class="pp-printon">Print on <select data-printon aria-label="Print ${esc(plate.name)} on">${options}</select></label>` : ""}
    <div class="pp-actions">${actions.join("")}</div></div>`;
}

function render() {
  const root = $("project-page");
  if (!shown || !status) return;
  if (document.activeElement?.matches?.("select") && root.contains(document.activeElement)) return;  // never rebuild a menu being used
  if (selected == null || !plateOf(selected)) selected = status.plates.find(p => p.status === "printing")?.id ?? status.plates[0]?.id ?? null;
  root.innerHTML = `${header()}${printerCards()}
    <div class="pp-layout">${plateMap()}<div class="pp-side">${nextUp()}${detail()}</div></div>`;
}

async function refresh() {
  clearTimeout(timer);
  if (!shown) return;
  const id = host.projectId();
  try {
    const [s, j, p] = await Promise.all([host.api("GET", `/api/projects/${id}/status`), host.api("GET", `/api/projects/${id}/jobs`),
      host.api("GET", "/api/printers")]);
    if (!shown || host.projectId() !== id) return;
    status = s; jobs = j.jobs; printers = p.filter(x => x.printer_type === "cc1");
    awaiting = new Set((await OutcomePrompt.awaiting()).map(a => a.printer_id));
    render();
    const focus = new URLSearchParams(location.search).get("printer");
    if (focus && !focusedPrinter) {   // opened from a push: bring that printer's card into view
      const card = document.getElementById(`pp-printer-${focus}`);
      if (card) { focusedPrinter = true; card.scrollIntoView({ block: "center" }); card.style.outline = "2px solid var(--accent, #ff6b2b)"; }
    }
  } catch (err) { /* keep what is on screen; the next tick tries again */ }
  if (shown) timer = setTimeout(refresh, POLL_MS);
}

async function act(name, button) {
  const plate = plateOf(selected);
  if (name === "open") return host.onOpenPlate(selected);
  if (name === "assembly") return host.onOpenAssembly();
  if (name === "slice") await host.onSlice(selected);
  else if (name === "queue") {
    await host.api("POST", `/api/projects/${host.projectId()}/plates/${selected}/queue`, plate.printer_id ? { printer_id: plate.printer_id } : {});
    host.toast(`${plate.name} is in the queue`);
  } else if (name === "assign") {
    const ids = jobs.filter(j => j.state === "queued").map(j => j.id), ready = printers.filter(p => p.connected).map(p => p.id);
    await host.api("POST", "/api/jobs/assign", { job_ids: ids, printer_ids: ready });
    host.toast(`Spread ${plural(ids.length, "plate")} over ${plural(ready.length, "printer")}`);
  }
  await refresh();
}

function bind() {
  const root = $("project-page");
  root.addEventListener("click", async e => {
    const ask = e.target.closest("[data-outcome]");
    if (ask) { OutcomePrompt.open(ask.dataset.outcome); return; }
    const cell = e.target.closest("[data-plate]");
    const button = e.target.closest("[data-act]");
    if (button && !button.disabled) {
      button.disabled = true;
      try { await act(button.dataset.act, button); } catch (err) { host.toast(err.message, true); } finally { button.disabled = false; }
    } else if (cell) { selected = Number(cell.dataset.plate); render(); }
  });
  root.addEventListener("change", async e => {
    const select = e.target.closest("[data-printon]");
    if (!select) return;
    const plate = plateOf(selected), printerId = select.value || null;
    select.blur();
    try {
      if (plate.job_id && plate.status !== "printing") await host.api("PATCH", `/api/jobs/${plate.job_id}`, { printer_id: printerId });
      host.onSetPrinter(selected, printerId);
      await refresh();
    } catch (err) { host.toast(err.message, true); }
  });
}

export const projectPage = {
  attach(hooks) { host = hooks; bind(); },
  show() { shown = true; focusedPrinter = false; status = null; selected = null; $("project-page").innerHTML = '<p class="slicer-muted">Loading…</p>'; refresh(); },
  hide() { shown = false; clearTimeout(timer); },
  /** Redraw from what is already loaded (a slice finished, the selection moved). */
  redraw() { render(); },
};
