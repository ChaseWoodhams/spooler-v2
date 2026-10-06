/* Spooler project workspace – the Print step: the project page. A progress header, a printer card for each printer, a
   colour-coded map of every plate beside a Next up list, and a detail card for the selected plate. Mounted by main.js
   (which owns the step bar); reads /api/projects/<id>/status, /api/projects/<id>/jobs and /api/printers, and polls them
   while the step is on screen. Holds, Reprint, the queue-time warning check and each printer's auto-start toggle use the jobs and printer endpoints. */

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
const focusPrinterId = new URLSearchParams(location.search).get("printer"); // read at load: opening a project rewrites the URL
let allJobs = [];           // GET /api/jobs: every Job with its holds (the project list does not carry them)
let autoStart = {};         // printer id -> whether Bed cleared releases the next queued Job on it
let holdPicker = null;      // plate id whose "Hold after..." chooser is open
let holdChoice = "";        // the chooser's current value: "plate:<id>" or "job:<id>"
let warnPanel = null;       // { jobId, titles }: warnings found when queueing that wait for an accept
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
    <span class="slicer-muted">Filament: ${Math.round(grams.used)} g used of ${Math.round(grams.total)} g needed${open}${spool}</span>${perChoice(grams)}</div>`;
}

// Totals per filament choice; the project spool applies to the project default only
function perChoice(grams) {
  const groups = grams.by_filament || [];
  if (groups.length < 2 && !groups.some(g => !g.default)) return "";
  return groups.map(g => {
    const open = g.unknown_plates ? ` (${plural(g.unknown_plates, "plate")} not sliced yet)` : "";
    const spool = g.default && grams.spool ? ` · spool ${esc(grams.spool.name || "#" + grams.spool.id)}` : "";
    return `<span class="slicer-muted">${esc(g.label.charAt(0).toUpperCase() + g.label.slice(1))}: ${Math.round(g.used)} g used of ${Math.round(g.total)} g, ${plural(g.plates, "plate")}${open}${spool}</span>`;
  }).join("");
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
    const auto = autoStart[printer.id] !== false;
    return `<div class="pp-card pp-printer st-${cls}" id="pp-printer-${esc(printer.id)}"><b>${esc(printer.name)}</b> <span class="pp-chip st-${cls}">${label}</span>
      <div class="slicer-muted">${note}</div>
      <label class="pp-autostart" title="When on, Bed cleared starts the next queued plate on this printer by itself"><input type="checkbox" data-autostart="${esc(printer.id)}"${auto ? " checked" : ""}> Auto-start next plate</label>
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
    actions.push(btn("hold", "Hold after…", false, ' title="Wait for another plate or Job to print OK first"'));
  }
  if (plate.status === "failed") actions.push(btn("reprint", "Reprint", true, ' title="Queue this plate again from its stored G-code"'));
  actions.push(btn("open", "Open in Plates", false));
  if (host.hasAssembly?.()) actions.push(btn("assembly", "Assembly order", false, ' title="Which parts join in what order, from the Split step"'));
  const job = plate.job_id ? allJobs.find(j => j.id === plate.job_id) : null;
  const holds = job?.holds || [];
  const holdName = h => h.after_job_id != null ? `Job ${esc(allJobs.find(j => j.id === h.after_job_id)?.name || h.after_job_id)}`
    : esc(plateOf(h.after_plate_id)?.name || `plate ${h.after_plate_id}`);
  const holdList = holds.length ? `<div class="pp-holds">${holds.map(h =>
    `<span class="pp-hold${h.satisfied ? " done" : ""}">Hold: after ${holdName(h)}${h.satisfied ? " (done)" : ""}
      <button type="button" class="btn btn-secondary btn-sm" data-act="unhold" data-hold="${h.id}" aria-label="Remove hold">×</button></span>`).join("")}</div>` : "";
  const otherPlates = status.plates.filter(p => p.id !== plate.id && p.status !== "printed_ok");
  const otherJobs = allJobs.filter(j => ["queued", "starting", "printing"].includes(j.state) && j.id !== plate.job_id
    && !(j.project_id === host.projectId() && j.plate_id != null));
  const picker = holdPicker === plate.id && job ? `<div class="pp-holdpicker"><select data-holdchoice aria-label="Hold ${esc(plate.name)} after">
      <option value="">Choose what to wait for…</option>
      ${otherPlates.length ? `<optgroup label="Plates in this project">${otherPlates.map(p =>
        `<option value="plate:${p.id}"${holdChoice === "plate:" + p.id ? " selected" : ""}>${esc(p.name)}</option>`).join("")}</optgroup>` : ""}
      ${otherJobs.length ? `<optgroup label="Other Jobs">${otherJobs.map(j =>
        `<option value="job:${j.id}"${holdChoice === "job:" + j.id ? " selected" : ""}>${esc(j.name)}</option>`).join("")}</optgroup>` : ""}
    </select>${btn("hold-add", "Hold", true)}${btn("hold-cancel", "Cancel", false)}</div>` : "";
  const warnBox = warnPanel && job && warnPanel.jobId === job.id ? `<div class="pp-warnbox" role="alert"><b>Preflight warnings</b>
    <ul>${warnPanel.titles.map(t => `<li>${esc(t)}</li>`).join("")}</ul>
    <div class="slicer-muted">${warnPanel.canStart ? "Accept them now and Bed cleared can start this plate without asking again." : "Other checks are failing: open Review &amp; start to see them."}</div>
    <div class="pp-actions">${btn("accept-warnings", "Accept warnings", true, warnPanel.canStart ? "" : disabled("Fix the failing checks first"))}${btn("dismiss-warnings", "Not now", false)}</div></div>` : "";
  const picture = `/api/projects/${host.projectId()}/plates/${plate.id}/preview.png?v=${plate.layout}`;
  return `<div class="pp-card"><div><b>${esc(plate.name)}</b> ${chip(plate.status)}</div>
    ${plate.parts ? `<img class="pp-preview" src="${picture}" alt="${esc(plate.name)} as laid out" width="320" height="240" loading="lazy">` : ""}
    <div class="slicer-muted">${facts || "No slice yet"}</div>
    ${plate.status === "waiting" ? '<div class="slicer-muted">Waiting on another plate to print OK first</div>' : ""}
    ${editable ? `<label class="pp-printon">Print on <select data-printon aria-label="Print ${esc(plate.name)} on">${options}</select></label>` : ""}
    ${warnBox}${holdList}
    <div class="pp-actions">${actions.join("")}</div>${picker}</div>`;
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
    const [s, j, p, all] = await Promise.all([host.api("GET", `/api/projects/${id}/status`), host.api("GET", `/api/projects/${id}/jobs`),
      host.api("GET", "/api/printers"), host.api("GET", "/api/jobs")]);
    const cc1 = p.filter(x => x.printer_type === "cc1");
    const beds = await Promise.all(cc1.map(x => host.api("GET", `/api/printers/${encodeURIComponent(x.id)}/bed-cleared`).catch(() => null)));
    if (!shown || host.projectId() !== id) return;
    status = s; jobs = j.jobs; printers = cc1; allJobs = all.jobs;
    autoStart = Object.fromEntries(cc1.map((x, i) => [x.id, beds[i]?.auto_start !== false]));
    awaiting = new Set((await OutcomePrompt.awaiting()).map(a => a.printer_id));
    render();
    const focus = focusPrinterId;
    if (focus && !focusedPrinter) {   // opened from a push: bring that printer's card into view
      const card = document.getElementById(`pp-printer-${focus}`);
      if (card) { focusedPrinter = true; card.scrollIntoView({ block: "center" }); card.style.outline = "2px solid var(--accent, #ff6b2b)"; }
    }
  } catch (err) { /* keep what is on screen; the next tick tries again */ }
  if (shown) timer = setTimeout(refresh, POLL_MS);
}

/** Show a just-queued Job's preflight warnings so the user can accept them once. */
async function checkWarnings(job) {
  warnPanel = null;
  if (!job?.id) return;
  try {
    const report = await host.api("GET", `/api/jobs/${job.id}/preflight`);
    const titles = report.checks.filter(c => c.level === "warn").map(c => c.title);
    if (titles.length) warnPanel = { jobId: job.id, titles, canStart: report.can_start };
  } catch (err) { /* the Review dialog shows the checks */ }
}

async function act(name, button) {
  const plate = plateOf(selected);
  if (name === "open") return host.onOpenPlate(selected);
  if (name === "assembly") return host.onOpenAssembly();
  if (name === "slice") await host.onSlice(selected);
  else if (name === "queue") {
    const queued = await host.api("POST", `/api/projects/${host.projectId()}/plates/${selected}/queue`, plate.printer_id ? { printer_id: plate.printer_id } : {});
    host.toast(`${plate.name} is in the queue`);
    checkWarnings(queued).then(render);   // the preflight can take a moment: do not hold the button for it
  } else if (name === "hold") { holdPicker = holdPicker === plate.id ? null : plate.id; holdChoice = ""; return render(); }
  else if (name === "hold-cancel") { holdPicker = null; return render(); }
  else if (name === "hold-add") {
    const [kind, id] = holdChoice.split(":");
    if (!id) throw new Error("Choose what to wait for first");
    await host.api("POST", `/api/jobs/${plate.job_id}/holds`, kind === "job" ? { after_job_id: Number(id) }
      : { after_project_id: host.projectId(), after_plate_id: Number(id) });
    holdPicker = null;
    host.toast(`${plate.name} now waits for ${kind === "job" ? "that Job" : plateOf(Number(id))?.name}`);
  } else if (name === "unhold") {
    await host.api("DELETE", `/api/jobs/${plate.job_id}/holds/${button.dataset.hold}`);
    host.toast("Hold removed");
  } else if (name === "reprint") {
    await host.api("POST", `/api/projects/${host.projectId()}/plates/${selected}/reprint`, {});
    host.toast(`${plate.name} is back in the queue`);
  } else if (name === "accept-warnings") {
    await host.api("POST", `/api/jobs/${warnPanel.jobId}/acknowledge-warnings`, {});
    warnPanel = null;
    host.toast("Warnings accepted: later auto-starts will go ahead on them");
  } else if (name === "dismiss-warnings") warnPanel = null;
  else if (name === "assign") {
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
    const choice = e.target.closest("[data-holdchoice]");
    if (choice) { holdChoice = choice.value; return; }
    const toggle = e.target.closest("[data-autostart]");
    if (toggle) {
      const printerId = toggle.dataset.autostart, enabled = toggle.checked;
      try {
        await host.api("POST", `/api/printers/${encodeURIComponent(printerId)}/auto-start`, { enabled });
        autoStart[printerId] = enabled;
        host.toast(`Auto-start ${enabled ? "on" : "off"} for ${printerName(printerId)}`);
      } catch (err) { toggle.checked = !enabled; host.toast(err.message, true); }
      return;
    }
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
