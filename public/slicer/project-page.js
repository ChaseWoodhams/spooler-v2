/* Spooler project workspace – the Print step (the project page). A progress header, a card for each printer, a board
   of every Plate grouped by what is happening to it (Needs attention, Printing, Queued, Ready, Done), a send bar for
   the ticked Ready Plates, and a detail card for the selected Plate. Mounted by main.js, which owns the step bar.
   Reads /api/projects/<id>/status, /api/projects/<id>/jobs, /api/jobs and /api/printers, and polls them while the
   step is on screen. Sending, Holds, Reprint, assigning printers, the warning check made when a Plate is sent and each
   printer's auto-start toggle go through the jobs and printer endpoints.
   Deep links: ?printer=<id> scrolls to that printer's card, ?plate=<id> selects that Plate. */

const STATUS = {
  unsliced: "Not sliced", sliced: "Sliced", waiting: "Waiting on plate", queued: "Queued",
  printing: "Printing", printed_ok: OUTCOME_LABEL.completed, failed: OUTCOME_LABEL.failed,
};
const BAR_ORDER = ["printed_ok", "unsliced", "sliced", "waiting", "queued", "printing", "failed"];  // plate order is never status order
const POLL_MS = 4000;

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

let host = null;   // the page's hooks, see projectPage.attach(hooks) in main.js
let shown = false;
let status = null; // GET /api/projects/<id>/status
let jobs = [];
let printers = [];
let allPrinters = []; // every printer the page was told about, whatever its type (printers keeps the ones Spooler can print on)
let awaitingPlates = new Set(); // Plate ids whose last print is waiting for an answer, so the card can offer the Outcome
let awaiting = new Set();   // printer ids whose last print is waiting for an answer (OutcomePrompt)
let focusedPrinter = false; // a push deep link (?printer=) is scrolled to once
const focusPrinterId = new URLSearchParams(location.search).get("printer"); // read at load: opening a project rewrites the URL
const focusPlateId = Number(new URLSearchParams(location.search).get("plate")) || null; // a link to one Plate (Queue, push, review): selected once
let focusedPlate = false;
let scrollToPlate = false;
let assigning = false;      // the "Assign printers" tick list is open
let assignPick = new Set(); // printer ids ticked on it
let allJobs = [];           // GET /api/jobs: every Job with its holds (the project list does not carry them)
let autoStart = {};         // printer id -> whether Bed cleared releases the next queued Job on it
let holdPicker = null;      // plate id whose "Hold after..." chooser is open
let holdChoice = "";        // the chooser's current value: "plate:<id>" or "job:<id>"
let warnPanel = null;       // { jobId, titles }: warnings found when a Plate was sent that wait for an accept
let selected = null;
let folds = new Set();   // board groups the user closed; remembered per Project

const memoryKey = () => `spooler.print.${host.projectId()}`;
function remember() {
  try { localStorage.setItem(memoryKey(), JSON.stringify({ selected, folds: [...folds], barFolds })); } catch (err) { /* private window: the page still works */ }
}
function recall() {
  try {
    const saved = JSON.parse(localStorage.getItem(memoryKey()) || "{}");
    selected = Number.isInteger(saved.selected) ? saved.selected : null;
    folds = new Set(Array.isArray(saved.folds) ? saved.folds : []);
    barFolds = saved.barFolds && typeof saved.barFolds === "object" ? { where: Boolean(saved.barFolds.where), options: Boolean(saved.barFolds.options) } : {};
  } catch (err) { selected = null; folds = new Set(); barFolds = {}; }
}
let timer = null;
let ticked = new Set();     // plate ids ticked for sending
let sendOpts = { leveling: false, timelapse: false };   // remembered from the last send
let dest = {};              // plate id -> printer id picked on the send bar (the project save may not have landed yet)
let barFolds = {};          // which send bar folds are open; remembered per Project with the board folds
let sending = false;
let autoTicked = false;     // a one-Plate Project ticks its Plate once, when Print first loads

function duration(seconds) {
  if (!seconds) return "";
  const minutes = Math.round(seconds / 60);
  return minutes >= 60 ? `${Math.floor(minutes / 60)} h ${minutes % 60} min` : `${minutes} min`;
}

const chip = s => s === "printed_ok" || s === "failed" ? statusBadge(s === "failed" ? "failed" : "completed") : `<span class="pp-chip st-${s}"><i class="pp-dot st-${s}"></i>${STATUS[s]}</span>`;
const printerName = id => printers.find(p => p.id === id)?.name || "";
const plateOf = id => status?.plates.find(p => p.id === id);

function header() {
  const { progress } = status;
  const c = progress.counts;
  const grams = progress.filament_g;
  const waiting = c.queued + c.waiting, toSlice = c.sliced + c.unsliced;
  return `<div class="pp-card pp-progress">
    <div class="pp-progress-head"><b>${progress.printed_ok} of ${plural(progress.total, "plate")} completed</b>
      <span class="slicer-muted">${c.unsliced} unsliced · ${c.sliced} sliced · ${c.queued} queued Jobs · ${c.waiting} held Jobs · ${c.printing} printing · ${c.printed_ok} completed · ${c.failed} failed</span></div>
    <p class="slicer-muted">${progress.outcome_unknown ?? 'Unavailable:'} latest Outcome unknown${progress.outcome_unknown === 1 ? '' : 's'}${progress.outcome_unknown ? ' — verify these Prints in History. This overlaps the Plate status counts above; it is not another completed Plate.' : ''}</p>
    <div class="pp-bar" role="img" aria-label="${progress.printed_ok} of ${progress.total} plates completed">${BAR_ORDER.map(s =>
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
  if (!printers.length) return `<div class="pp-card slicer-muted">No printers that can print yet.</div>`;
  return `<div class="pp-printers">${printers.map(printer => {
    const running = jobs.find(j => j.printer_id === printer.id && (j.state === "printing" || j.state === "starting"));
    const waitingFor = jobs.filter(j => j.printer_id === printer.id && j.state === "queued");
    const queued = waitingFor.length;
    const nextName = queued ? plateOf(waitingFor[0].plate_id)?.name || waitingFor[0].name : "";
    const info = printer.status?.PrintInfo || {};
    const plate = running ? plateOf(running.plate_id) : null;
    const layers = running && info.TotalLayer ? ` · layer ${info.CurrentLayer || 0} of ${info.TotalLayer}` : "";
    const [cls, label, note] = !printer.connected ? ["failed", "Offline", "Not connected"]
      : running ? ["printing", "Printing", `${plate ? esc(plate.name) : esc(running.name)}${layers}`]
      : ["sliced", "Free", queued ? `${plural(queued, "Plate")} in the queue for it · next: ${esc(nextName)}` : "Nothing in the queue for it"];
    const asking = awaiting.has(printer.id);
    const auto = autoStart[printer.id] !== false;
    return `<div class="pp-card pp-printer st-${cls}" id="pp-printer-${esc(printer.id)}"><b>${esc(printer.name)}</b> <span class="pp-chip st-${cls}">${label}</span>
      <div class="slicer-muted">${note}</div>
      <label class="pp-autostart" title="When on, Bed cleared starts the next Plate in the queue on this printer by itself"><input type="checkbox" data-autostart="${esc(printer.id)}"${auto ? " checked" : ""}> Auto-start next Plate</label>
      <p class="slicer-muted">${auto ? 'After you confirm Bed cleared, the next eligible queued Job can start automatically through preflight. Holds wait for their prerequisite to print OK.' : 'Bed cleared records that the bed is free. Start the next Job from Review & start.'}</p>
      ${asking ? `<button type="button" class="btn btn-primary btn-sm" data-outcome="${esc(printer.id)}">Print ended: Printed OK · Bed cleared / Failed…</button>` : ""}</div>`;
  }).join("")}</div>`;
}

// What a Plate is doing, in words, from its open Job (a Print with no Job reads "printing now")
const JOB_WORD = { printing: "printing now", queued: "in the queue", starting: "starting to print" };
const PLATE_WORD = { unsliced: "needs slicing", sliced: "sliced", printed_ok: "completed", failed: "failed", waiting: "in the queue", queued: "in the queue", printing: "printing now" };
const GROUPS = [["attention", "Needs attention"], ["printing", "Printing"], ["queued", "Queued"], ["ready", "Ready"], ["done", "Done"]];

/** Sort every Plate into one board group, with the words that describe it and (for Needs attention) why. */
function boardGroups() {
  const rank = id => { const at = (status.print_order || []).indexOf(id); return at < 0 ? Infinity : at; };   // longest by the best available time; names stay as planned
  const groups = Object.fromEntries(GROUPS.map(([id]) => [id, []]));
  for (const plate of status.plates) {
    const job = plate.job_id ? jobs.find(j => j.id === plate.job_id) : null;
    const word = (job && JOB_WORD[job.state]) || PLATE_WORD[plate.status];
    const running = plate.status === "printing";
    const offline = running && job?.printer_id && !allPrinters.find(p => p.id === job.printer_id)?.connected;
    const reason = offline ? `${printerName(job.printer_id) || "Its printer"} is offline` : plate.outcome_unknown ? "Outcome unknown: check History"
      : plate.status === "failed" ? "Last print failed: Reprint it or check History" : "";
    const group = offline || plate.outcome_unknown || plate.status === "failed" ? "attention"
      : running ? "printing" : plate.status === "queued" || plate.status === "waiting" ? "queued"
      : plate.status === "printed_ok" ? "done" : "ready";
    groups[group].push({ plate, word, reason });
  }
  groups.ready.sort((a, b) => (a.plate.status === "unsliced") - (b.plate.status === "unsliced") || rank(a.plate.id) - rank(b.plate.id));   // sliced Plates first, in print order
  return GROUPS.filter(([id]) => groups[id].length).map(([id, title]) => ({ id, title, cards: groups[id] }));
}

/** The actions a Plate's own card offers: Start (through Review) for a queued one that is not held, the Outcome for one whose print ended, Reprint for a failed one, and another copy of one already sent. */
function cardActions(plate) {
  const acts = [];
  if (plate.status === "queued" && plate.job_id) acts.push(`<a class="btn btn-primary btn-sm pp-card-act" href="/?review=${plate.job_id}" title="Preview the toolpath, run the preflight checks and start">Start</a>`);
  if (awaitingPlates.has(plate.id)) acts.push(`<button type="button" class="btn btn-primary btn-sm pp-card-act" data-outcome-plate="${plate.id}">Printed OK / Failed…</button>`);
  if (plate.status === "failed") acts.push(`<button type="button" class="btn btn-primary btn-sm pp-card-act" data-act="reprint" data-card-plate="${plate.id}" title="Send this Plate again from its stored G-code">Reprint</button>`);
  if (["queued", "waiting", "printing"].includes(plate.status)) acts.push(`<button type="button" class="btn btn-secondary btn-sm pp-card-act" data-act="copy" data-card-plate="${plate.id}" title="Send this Plate again to print another copy">Send another copy</button>`);
  return acts.length ? `<span class="pp-card-acts">${acts.join("")}</span>` : "";
}

/** The Plates in the Ready group that can be sent: sliced, up to date, and not already in the queue. */
const sendable = plate => plate.status === "sliced" && !plate.outdated;
const readySendable = () => (boardGroups().find(g => g.id === "ready")?.cards || []).map(c => c.plate).filter(sendable);
const tickedPlates = () => readySendable().filter(p => ticked.has(p.id));
const destOf = plate => (plate.id in dest ? dest[plate.id] : plate.printer_id) || "";

function summaryHtml(plates) {
  if (!plates.length) return '<span class="slicer-muted">Tick the Plates to send.</span>';
  const timeKnown = plates.every(p => p.estimate_s != null), gramsKnown = plates.every(p => p.estimate_g != null);
  const seconds = plates.reduce((sum, p) => sum + (p.estimate_s || 0), 0), grams = plates.reduce((sum, p) => sum + (p.estimate_g || 0), 0);
  return `<b>${plural(plates.length, "Plate")} · ${timeKnown ? duration(seconds) || "0 min" : "time unknown"} · ${gramsKnown ? `${Number(grams.toFixed(1))} g` : "filament unknown"}</b>
    <small class="slicer-muted">${esc(host.spoolNote())}</small>`;
}

function whereHtml(plates) {
  if (!plates.length) return '<li class="slicer-muted">Tick the Plates to see where they go.</li>';
  return plates.map(plate => {
    const chosen = destOf(plate), missing = chosen && !printers.some(p => p.id === chosen);
    return `<li><b>${esc(plate.name)}</b> → <select data-send-printer="${plate.id}" aria-label="Destination printer for ${esc(plate.name)}"><option value="">Next free printer</option>${
      missing ? `<option value="${esc(chosen)}" selected>Unavailable printer (${esc(chosen)})</option>` : ""}${printers.map(p =>
      `<option value="${esc(p.id)}"${p.id === chosen ? " selected" : ""}>${esc(p.name)}${p.connected ? "" : " (offline)"}</option>`).join("")}</select>
      · ${esc(host.plateFilament(plate.id))} · ${esc(host.bedName())}</li>`;
  }).join("");
}

function sendBar() {
  const fold = (id, title, body) => `<details class="pp-barfold" data-barfold="${id}"${barFolds[id] ? " open" : ""}><summary>${title}</summary>${body}</details>`;
  return `<div class="pp-sendbar" id="pp-sendbar">
    <div id="pp-send-summary" class="pp-send-summary" role="status" aria-live="polite">${summaryHtml(tickedPlates())}</div>
    ${fold("where", "Where each Plate goes", `<ul class="pp-where" id="pp-send-where">${whereHtml(tickedPlates())}</ul>`)}
    ${fold("options", "Options and balancing", `<label class="slicer-check"><input type="checkbox" id="pp-send-leveling"${sendOpts.leveling ? " checked" : ""}> Level the bed before each Plate</label>
      <label class="slicer-check"><input type="checkbox" id="pp-send-timelapse"${sendOpts.timelapse ? " checked" : ""}> Time-lapse</label>
      <button type="button" id="pp-send-balance" class="btn btn-secondary btn-sm"${ticked.size ? "" : " disabled"}>Balance by estimated time</button>
      <p class="slicer-muted">Balancing gives the longest ticked Plates first to the connected printer with the least estimated work in this Project. Other Projects are not counted. Check each destination before sending.</p>`)}
    <button type="button" id="pp-send" class="btn btn-primary" title="Each Plate becomes a Job. Start each one when its printer's bed is clear; every start runs the preflight checks."${ticked.size && !sending ? "" : " disabled"}>${sendLabel(ticked.size)}</button></div>`;
}

const sendLabel = count => count ? `Send ${plural(count, "Plate")}` : "Send Plates";

/** Bring the send bar and the ticks on the cards up to date without redrawing the page (so screen readers hear the summary change). */
function updateBar() {
  const bar = $("pp-sendbar");
  if (!bar) return;
  const plates = tickedPlates();
  ticked = new Set(plates.map(p => p.id));
  $("pp-send-summary").innerHTML = summaryHtml(plates);
  if (!$("pp-send-where").contains(document.activeElement)) $("pp-send-where").innerHTML = whereHtml(plates);
  $("pp-send-balance").disabled = !plates.length;
  const send = $("pp-send");
  send.disabled = !plates.length || sending;
  send.textContent = sendLabel(plates.length);
  for (const box of $("project-page").querySelectorAll("[data-tick]")) {
    box.checked = ticked.has(Number(box.dataset.tick));
    box.closest("[data-plate-card]").classList.toggle("ticked", box.checked);
  }
}

function tick(plateId, on) {
  if (on) ticked.add(plateId); else ticked.delete(plateId);
  updateBar();
}

async function sendTicked() {
  const plates = tickedPlates();
  if (!plates.length || sending) return;
  sending = true; updateBar();
  try {
    const result = await host.sendPlates(plates.map(p => p.id), sendOpts);
    if (result) { host.toast(`Sent ${plural(result.jobs.length, "Plate")}`); ticked.clear(); }
  } catch (err) { host.toast(err.message, true); }
  sending = false;
  await refresh();
  updateBar();
}

function plateBoard() {
  const queued = jobs.filter(j => j.state === "queued");
  const canAssign = queued.length > 1 && printers.some(p => p.connected);
  const chooser = () => `<div class="pp-assign" role="group" aria-label="Printers to spread the queued Plates over">
      ${printers.map(p => `<label class="slicer-check"><input type="checkbox" data-assign-printer="${esc(p.id)}"${assignPick.has(p.id) ? " checked" : ""}> ${esc(p.name)}${p.connected ? "" : " (offline)"}</label>`).join("")}
      <div class="pp-actions"><button type="button" class="btn btn-primary btn-sm" data-act="assign-apply">Spread ${plural(queued.length, "Plate")} over these printers</button>
        <button type="button" class="btn btn-secondary btn-sm" data-act="assign-cancel">Cancel</button></div></div>`;
  return `<div class="pp-board"><a class="pp-queue-link" href="/?queue=1">Open the Queue page</a>${boardGroups().map(({ id, title, cards }) => `<section class="pp-card pp-group pp-group-${id}" data-group="${id}" aria-labelledby="pp-group-${id}">
    <div class="pp-group-head"><h3 id="pp-group-${id}"><button type="button" class="pp-fold" data-fold="${id}" aria-expanded="${!folds.has(id)}">${folds.has(id) ? "▸" : "▾"} ${title} <span class="slicer-muted">${cards.length}</span></button></h3>
      ${id === "ready" && cards.some(c => sendable(c.plate)) ? `<span class="pp-ticks"><button type="button" class="btn btn-secondary btn-sm" id="pp-send-all" title="Tick every Plate that is sliced and not already in the queue">All</button>
        <button type="button" class="btn btn-secondary btn-sm" id="pp-send-none">None</button></span>` : ""}
      ${id === "ready" && !cards.some(c => sendable(c.plate)) && cards.some(c => c.plate.status === "unsliced") ? `<p class="pp-slice-first" role="note"><strong>Slice first.</strong> No Plate here is sliced yet, so there is nothing to send.
        <button type="button" class="btn btn-primary btn-sm" data-go-slice>Go to Slice</button></p>` : ""}
      ${id === "queued" ? `<button type="button" class="btn btn-secondary btn-sm" data-act="assign"${canAssign ? "" : " disabled"}
        title="Choose the printers to spread the queued Plates over, so they finish about together by estimated print time">Assign printers · balance by time</button>` : ""}</div>
    ${id === "queued" && assigning ? chooser() : ""}
    <ul class="pp-plates"${folds.has(id) ? " hidden" : ""}>${cards.map(({ plate, word, reason }) => `<li class="pp-plate st-${plate.status}${plate.id === selected ? " sel" : ""}${ticked.has(plate.id) ? " ticked" : ""}" data-plate-card>
      ${id === "ready" && sendable(plate) ? `<input type="checkbox" class="pp-tick" data-tick="${plate.id}" aria-label="Send ${esc(plate.name)}, ${esc(word)}"${ticked.has(plate.id) ? " checked" : ""}>` : ""}
      <button type="button" class="pp-plate-btn" data-plate="${plate.id}" aria-pressed="${plate.id === selected}">${esc(plate.name)}</button>
      <span class="pp-state"><i class="pp-dot st-${plate.status}"></i>${word}</span>
      ${reason ? `<span class="pp-reason">${esc(reason)}</span>` : ""}${cardActions(plate)}</li>`).join("")}</ul></section>`).join("")}</div>`;
}

function detail() {
  const plate = plateOf(selected);
  if (!plate) return `<div class="pp-card slicer-muted">Select a plate.</div>`;
  const slicing = ["queued", "slicing"].includes(host.sliceState(plate.id));
  const facts = [plural(plate.parts, "object"), duration(plate.estimate_s), plate.estimate_g ? `${Math.round(plate.estimate_g)} g` : ""].filter(Boolean).join(" · ");
  const editable = ["unsliced", "sliced", "waiting", "queued", "failed"].includes(plate.status);
  const options = ['<option value="">Next free printer</option>', ...printers.map(p =>
    `<option value="${esc(p.id)}"${p.id === plate.printer_id ? " selected" : ""}>${esc(p.name)}${p.connected ? "" : " (offline)"}</option>`)].join("");
  const btn = (act, label, primary, extra = "", why = "") => `<button type="button" class="btn ${primary ? "btn-primary" : "btn-secondary"} btn-sm" data-act="${act}"${why ? ` aria-disabled="true" aria-describedby="pp-why-${plate.id}-${act}"` : ""}${extra}>${label}</button>${why ? `<small class="reason-line" id="pp-why-${plate.id}-${act}">${why}</small>` : ""}`;   // aria-disabled keeps the button focusable so its reason is reachable
  const actions = [];
  if (plate.status === "unsliced") actions.push(btn("slice", slicing ? "Slicing…" : "Slice", true, slicing ? " disabled" : ""));
  if (plate.status === "sliced") {
    actions.push(btn("send", "Send to printer", true, "", plate.outdated ? "Changed since slicing: slice it again first" : ""));
    if (plate.outdated) actions.push(btn("slice", slicing ? "Slicing…" : "Slice again", true, slicing ? " disabled" : ""));
  }
  if (plate.status === "queued" || plate.status === "waiting") {
    const held = plate.status === "waiting";   // a held Job cannot start yet, so it offers the Review but not a Start
    actions.push(`<a class="btn btn-primary btn-sm" href="/?review=${plate.job_id}" title="${held ? "Preview the toolpath and run the preflight checks. It cannot start until its hold is done" : "Preview the toolpath, run the preflight checks and start"}">${held ? "Review" : "Review &amp; start"}</a>`);
    actions.push(btn("hold", "Hold after…", false, ' title="Wait for another Plate or Job to print OK first"'));
  }
  if (plate.status === "failed") actions.push(btn("reprint", "Reprint", true, ' title="Send this Plate again from its stored G-code"'));
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
    <div class="pp-actions">${btn("accept-warnings", "Accept warnings", true, "", warnPanel.canStart ? "" : "Fix the failing checks first")}${btn("dismiss-warnings", "Not now", false)}</div></div>` : "";
  const picture = `/api/projects/${host.projectId()}/plates/${plate.id}/preview.png?v=${plate.layout}`;
  return `<div class="pp-card"><div><b>${esc(plate.name)}</b> ${chip(plate.status)}</div>
    ${plate.parts ? `<img class="pp-preview" src="${picture}" alt="${esc(plate.name)} as laid out" width="320" height="240" loading="lazy">` : ""}
    <div class="slicer-muted">${facts || "No slice yet"}</div>
    ${plate.status === "waiting" ? '<div class="slicer-muted">Waiting on another plate to print OK first</div>' : ""}
    ${plate.outcome_unknown ? '<p role="status">The latest Print has an unknown Outcome. Review its record in History before treating this Plate as completed.</p>' : ''}
    ${editable ? `<label class="pp-printon">Print on <select data-printon aria-label="Print ${esc(plate.name)} on">${options}</select></label>` : ""}
    ${warnBox}${holdList}
    <div class="pp-actions">${actions.join("")}</div>${picker}</div>`;
}

function render() {
  const root = $("project-page");
  if (!shown || !status) return;
  if (document.activeElement?.matches?.("select") && root.contains(document.activeElement)) return;  // never rebuild a menu being used
  ticked = new Set(readySendable().filter(p => ticked.has(p.id)).map(p => p.id));   // a Plate that left Ready is no longer ticked
  if (!autoTicked) {   // sending the only Plate of a one-Plate Project takes one click
    autoTicked = true;
    if (status.plates.length === 1 && readySendable().length === 1) ticked.add(status.plates[0].id);
  }
  if (selected == null || !plateOf(selected)) selected = status.plates.find(p => p.status === "printing")?.id ?? status.plates[0]?.id ?? null;
  const active = root.contains(document.activeElement) ? document.activeElement : null;
  const activePlate = active?.getAttribute('data-plate'), activeName = active?.getAttribute('aria-label');
  root.innerHTML = `${header()}${printerCards()}
    <div class="pp-layout">${plateBoard()}<div class="pp-side">${detail()}</div></div>${readySendable().length ? sendBar() : ""}`;
  if (activePlate) [...root.querySelectorAll('[data-plate]')].find(button => button.getAttribute('data-plate') === activePlate && button.getAttribute('aria-label') === activeName)?.focus();
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
    status = s; jobs = j.jobs; printers = cc1; allPrinters = p; allJobs = all.jobs;
    autoStart = Object.fromEntries(cc1.map((x, i) => [x.id, beds[i]?.auto_start !== false]));
    if (focusPlateId && !focusedPlate && plateOf(focusPlateId)) {   // opened from a link to one Plate: select it, and open its group if it was closed
      focusedPlate = scrollToPlate = true;
      selected = focusPlateId;
      const group = boardGroups().find(g => g.cards.some(c => c.plate.id === focusPlateId));
      if (group) folds.delete(group.id);
      remember();
    }
    const waitingOn = await OutcomePrompt.awaiting();
    awaiting = new Set(waitingOn.map(a => a.printer_id));
    awaitingPlates = new Set(waitingOn.filter(a => a.project_id === id && a.plate_id != null).map(a => a.plate_id));
    render();
    if (scrollToPlate) {
      scrollToPlate = false;
      $("project-page").querySelector(`[data-plate="${focusPlateId}"]`)?.scrollIntoView({ block: "center" });
    }
    const focus = focusPrinterId;
    if (focus && !focusedPrinter) {   // opened from a push: bring that printer's card into view
      const card = document.getElementById(`pp-printer-${focus}`);
      if (card) { focusedPrinter = true; card.scrollIntoView({ block: "center" }); card.style.outline = "2px solid var(--accent, #ff6b2b)"; }
    }
  } catch (err) { /* keep what is on screen; the next tick tries again */ }
  if (shown) timer = setTimeout(refresh, POLL_MS);
}

/** Show a just-sent Job's preflight warnings so the user can accept them once. */
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
  else if (name === "send") {
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
  } else if (name === "copy") {
    if (!await confirmDialog({
      title: `Send another copy of ${plate.name}?`,
      body: `${plate.name} is already ${plate.status === "printing" ? "printing" : "in the queue"}. Another copy is queued as well.`,
      confirmLabel: "Send another copy",
    })) return;
    const result = await host.sendPlates([plate.id], { ...sendOpts, again: true });
    if (result) host.toast(`Sent another copy of ${plate.name}`);
  } else if (name === "accept-warnings") {
    await host.api("POST", `/api/jobs/${warnPanel.jobId}/acknowledge-warnings`, {});
    warnPanel = null;
    host.toast("Warnings accepted: later auto-starts will go ahead on them");
  } else if (name === "dismiss-warnings") warnPanel = null;
  else if (name === "assign") {   // opens the tick list: the connected printers start ticked
    assigning = !assigning;
    assignPick = new Set(printers.filter(p => p.connected).map(p => p.id));
    return render();
  } else if (name === "assign-cancel") { assigning = false; return render(); }
  else if (name === "assign-apply") {
    const ids = jobs.filter(j => j.state === "queued").map(j => j.id), chosen = printers.filter(p => assignPick.has(p.id)).map(p => p.id);
    if (!chosen.length) throw new Error("Choose at least one printer");
    await host.api("POST", "/api/jobs/assign", { job_ids: ids, printer_ids: chosen });
    assigning = false;
    host.toast(`Spread ${plural(ids.length, "Plate")} over ${plural(chosen.length, "printer")}`);
  }
  await refresh();
}

function bindSend(root) {
  root.addEventListener("toggle", e => { const id = e.target.dataset?.barfold; if (id && barFolds[id] !== e.target.open) { barFolds[id] = e.target.open; remember(); } }, true);
  root.addEventListener("click", e => {
    if (e.target.closest("#pp-send-all")) { ticked = new Set(readySendable().map(p => p.id)); return updateBar(); }
    if (e.target.closest("#pp-send-none")) { ticked.clear(); return updateBar(); }
    if (e.target.closest("#pp-send")) return sendTicked();
    if (e.target.closest("#pp-send-balance")) {
      const map = host.balance([...ticked]);
      if (map) { Object.assign(dest, map); host.toast("Assignments suggested by estimated time. Review each destination before sending."); updateBar(); }
      return;
    }
    const card = e.target.closest("[data-plate-card]");
    const box = card?.querySelector("[data-tick]");
    if (box && e.target !== box && !e.target.closest("a, button, input, select, label")) tick(Number(box.dataset.tick), !box.checked);
  });
  root.addEventListener("change", e => {
    if (e.target.matches("[data-tick]")) return tick(Number(e.target.dataset.tick), e.target.checked);
    if (e.target.id === "pp-send-leveling") sendOpts.leveling = e.target.checked;
    else if (e.target.id === "pp-send-timelapse") sendOpts.timelapse = e.target.checked;
    else if (e.target.matches("[data-send-printer]")) {
      const id = Number(e.target.dataset.sendPrinter), printerId = e.target.value || null;
      dest[id] = printerId;
      host.onSetPrinter(id, printerId);
    }
  });
}

function bind() {
  const root = $("project-page");
  bindSend(root);
  root.addEventListener("click", async e => {
    const ask = e.target.closest("[data-outcome]");
    if (ask) { OutcomePrompt.open(ask.dataset.outcome); return; }
    const fold = e.target.closest("[data-fold]");
    if (fold) { const g = fold.dataset.fold; folds.has(g) ? folds.delete(g) : folds.add(g); remember(); render(); return; }
    const outcomeCard = e.target.closest("[data-outcome-plate]");
    if (outcomeCard) {
      const item = (await OutcomePrompt.awaiting()).find(a => a.project_id === host.projectId() && a.plate_id === Number(outcomeCard.dataset.outcomePlate));
      if (item) OutcomePrompt.open(item.printer_id);
      return;
    }
    const own = e.target.closest("[data-card-plate]");
    if (own) selected = Number(own.dataset.cardPlate);   // a card's action acts on that card's Plate
    if (e.target.closest("[data-go-slice]")) { host.onOpenSlice(); return; }
    const cell = e.target.closest("[data-plate]");
    const button = e.target.closest("[data-act]");
    if (button && !button.disabled && button.getAttribute("aria-disabled") !== "true") {
      button.disabled = true;
      try { await act(button.dataset.act, button); } catch (err) { host.toast(err.message, true); } finally { button.disabled = false; }
    } else if (cell) { selected = Number(cell.dataset.plate); remember(); render(); }
  });
  root.addEventListener("change", async e => {
    const choice = e.target.closest("[data-holdchoice]");
    if (choice) { holdChoice = choice.value; return; }
    const pick = e.target.closest("[data-assign-printer]");
    if (pick) { if (pick.checked) assignPick.add(pick.dataset.assignPrinter); else assignPick.delete(pick.dataset.assignPrinter); return; }
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

async function loadSendOptions() {
  try {
    const { options } = (await host.api("GET", "/api/defaults")).defaults;
    sendOpts = { leveling: Boolean(options.leveling), timelapse: Boolean(options.timelapse) };
    for (const [id, on] of [["pp-send-leveling", sendOpts.leveling], ["pp-send-timelapse", sendOpts.timelapse]]) if ($(id)) $(id).checked = on;
  } catch (err) { /* the boxes stay as they are */ }
}

export const projectPage = {
  attach(hooks) { host = hooks; bind(); },
  show() { shown = true; focusedPrinter = false; assigning = false; status = null; recall(); ticked = new Set(); autoTicked = false; dest = {}; loadSendOptions(); $("project-page").innerHTML = '<p class="slicer-muted">Loading…</p>'; refresh(); },
  hide() { shown = false; clearTimeout(timer); },
  /** Redraw from what is already loaded (a slice finished, the selection moved). */
  redraw() { render(); },
};
