/* Spooler workspace - the Plan plates review sheet (auto-plating ticket 19, prototype variant B).
   Plan plates starts the planner as a background job (POST /api/projects/<id>/plan), shows its progress, then shows the stored
   Proposal: the time both printers are done and how that changes, Accept and Dismiss, before/after timelines per printer, tiles
   for support, plates and tippy, a "partly estimated" note, a folded plate table, still-tippy Objects, and two "also considered"
   plans that can be reviewed instead. Nothing in the Project changes until Accept, which applies one stored plan as one
   revision (so one Undo reverses it). Mounted by main.js, which passes { api, toast, flush, projectId, applied }. */

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

const POLL_MS = 1000;
const POLL_FAILURES = 3;          // consecutive failed checks before the sheet gives up on a job
const LONG_S = 14 * 3600;         // a plate this long is drawn solid
const PHASES = { queued: "Waiting", reading: "Reading the project", searching: "Searching for a layout", confirming: "Slicing the best plans", done: "Done" };
const SUPERSEDED = "The project was changed elsewhere, so this plan no longer fits";
const NO_FIT = "Spooler has no time model for this project yet, so its starting estimates are used";

let host = null;     // { api, toast, flush(), projectId(), applied(result, plan) }
let run = null;      // the sheet in view: { gen, projectId, job, proposal, index, stale, phase: planning | ready | failed | gone }
let generation = 0;  // bumped per Plan plates, so a poll for an older run is dropped

// -- figures -------------------------------------------------------------------------------------------------------------

function dur(seconds) {
  const minutes = Math.max(0, Math.round(seconds / 60)), h = Math.floor(minutes / 60), m = minutes % 60;
  return h ? `${h} h ${String(m).padStart(2, "0")} min` : `${m} min`;
}
const grams = g => `${Math.round(g)} g`;
const signed = (n, unit) => `${n < 0 ? "-" : "+"}${unit(Math.abs(n))}`;
const shortName = name => String(name).replace(/^Plate\s+/i, "P");

/** "7 h 25 min sooner than now" / "1 h 05 min later than now" / "the same as now" */
function against(seconds, what = "than now") {
  if (Math.abs(seconds) < 60) return `about the same ${what.replace(/^than/, "as")}`;
  return `${dur(Math.abs(seconds))} ${seconds < 0 ? "sooner" : "later"} ${what}`;
}
const tone = n => n <= -60 ? "good" : n >= 60 ? "bad" : "";

function estimateTip(proposal) {
  const lines = proposal.calibration || [];
  return `${lines.length ? lines.join(". ") : NO_FIT}.`;
}

// -- the sheet ------------------------------------------------------------------------------------------------------------

function chip(text, cls, title) {
  return `<span class="ps-chip ${cls}"${title ? ` title="${esc(title)}" tabindex="0" aria-label="${esc(`${text}. ${title}`)}"` : ""}>${esc(text)}</span>`;
}

function headline(proposal, plan) {
  const before = proposal.before;
  const delta = before.plates ? `<span class="ps-delta ${tone(plan.change.makespan_s)}">${esc(against(plan.change.makespan_s))}</span>` : "";
  return `<section class="ps-head" aria-labelledby="ps-finish">
      <div class="ps-label" id="ps-finish">Both printers busy until</div>
      <div class="ps-big">${esc(dur(plan.makespan_s))} ${delta}</div>
      <div class="slicer-muted">${before.plates ? `Now ${esc(dur(before.makespan_s))}. ` : ""}Plan nozzle ${esc(proposal.plan_nozzle)} mm. ${esc(plural(plan.plates, "plate"))} planned${proposal.kept.length ? `. Kept as they are: ${esc(plural(proposal.kept.length, "plate"))}` : ""}.</div>
      <div class="ps-actions">
        <button class="btn btn-secondary" type="button" data-ps="dismiss">Dismiss</button>
        <button class="btn btn-primary" type="button" data-ps="accept"${run.stale || plan.can_accept === false ? " disabled" : ""}>Accept this plan</button>
      </div>
      <p class="slicer-muted ps-once">One Accept applies every plate change as a single change that Undo reverses. Pinned objects and plates that are queued, printing or printed are not touched.</p>
      <div class="ps-problem" id="ps-error" role="alert">${run.stale ? staleHtml(run.stale) : ""}</div>
    </section>`;
}

function staleHtml(message) {
  return `<span class="slicer-error">${esc(message)}</span> <button class="btn btn-secondary btn-sm" type="button" data-ps="again">Plan again</button>`;
}

function label(plan, plans) {
  if (plan.recommended) return "Recommended";
  const fewest = Math.min(...plans.map(p => p.plates)), least = Math.min(...plans.map(p => p.support_g));
  if (plan.plates === fewest && plan.plates < plans[0].plates) return "Fewer plates";
  if (plan.support_g === least && plan.support_g < plans[0].support_g) return "Least support";
  return "Another layout";
}

function lane(rows, scale, old) {
  const total = rows.reduce((n, r) => n + r.seconds, 0);
  const blocks = rows.map(r => {
    const cls = `ps-blk${old ? " old" : r.seconds > LONG_S ? " long" : ""}${r.partly_estimated || (old && r.estimated) ? " est" : ""}`;
    return `<span class="${cls}" style="flex: ${Math.max(r.seconds, 1)} 1 0" title="${esc(`${r.name}, ${dur(r.seconds)}${r.partly_estimated ? ", partly estimated" : ""}`)}">${esc(shortName(r.name))} ${(r.seconds / 3600).toFixed(r.seconds >= 36000 ? 0 : 1)}h</span>`;
  }).join("");
  return `<div class="ps-lane" role="img" aria-label="${esc(`${plural(rows.length, "plate")}, ${dur(total)}`)}">${rows.length ? `<div class="ps-track" style="width: ${(total / scale * 100).toFixed(2)}%">${blocks}</div>` : '<span class="slicer-muted ps-empty">No plates</span>'}</div>`;
}

function timelines(proposal, plan) {
  const same = (a, b) => String(a) === String(b);
  const inLane = (rows, id) => rows.filter(r => same(r.printer, id));
  const totals = proposal.printers.flatMap(p => [inLane(proposal.before.plate_rows, p.id), inLane(plan.plate_rows, p.id)].map(rows => rows.reduce((n, r) => n + r.seconds, 0)));
  const scale = Math.max(1, ...totals) * 1.02;
  const lanes = proposal.printers.map(p => {
    const now = inLane(proposal.before.plate_rows, p.id), next = inLane(plan.plate_rows, p.id);
    const sum = rows => rows.reduce((n, r) => n + r.seconds, 0);
    const nozzle = p.nozzle == null ? chip("nozzle not set", "warn") : `<span class="slicer-muted">${esc(p.nozzle)} mm nozzle installed</span>`;
    return `<div class="ps-printer">
        <div class="ps-printer-head"><b>${esc(p.name)}</b> ${nozzle}</div>
        ${p.message ? `<div class="ps-warn">${esc(p.message)}</div>` : ""}
        <div class="ps-lane-label slicer-muted">Now · ${esc(dur(sum(now)))}</div>${lane(now, scale, true)}
        <div class="ps-lane-label">Proposed · ${esc(dur(sum(next)))}</div>${lane(next, scale, false)}
      </div>`;
  }).join("");
  return `<section aria-labelledby="ps-lanes-h"><h4 id="ps-lanes-h">Who prints what, and for how long</h4>${lanes}
      <p class="slicer-muted">Block width is print time. Plates over 14 h are filled solid; a dashed outline is a partly estimated plate.</p></section>`;
}

function tiles(proposal, plan) {
  const before = proposal.before;
  const tile = (name, now, next, change, note) => `<div class="ps-tile"><div class="ps-label">${name}</div>
      <div class="ps-figure"><b>${next}</b> <span class="slicer-muted">was ${now}</span></div>${change ? `<div class="ps-tile-note ${tone(change.n)}">${esc(change.text)}</div>` : ""}${note ? `<div class="slicer-muted">${note}</div>` : ""}</div>`;
  const g = plan.change.support_g, p = plan.change.plates, t = plan.change.tippy;
  return `<div class="ps-tiles">
      ${tile("Support", grams(before.support_g), grams(plan.support_g), { n: g, text: g ? `${signed(g, grams)}` : "No change" })}
      ${tile("Plates", before.plates, plan.plates, { n: p, text: p ? `${signed(p, n => n)}` : "No change" })}
      ${tile("Tippy objects", before.tippy, plan.tippy, { n: t, text: t ? `${signed(t, n => n)}` : "No change" }, plan.tippy ? "Flagged below" : "")}
    </div>`;
}

function estimateNote(proposal, plan) {
  const failed = plan.did_not_slice_plates || [];
  const broken = failed.length ? `<p class="ps-note">${chip("did not slice", "bad")} OrcaSlicer could not slice ${esc(failed.join(", "))}, so this plan would not print as laid out. Accept is off for it: review another plan or plan again.</p>` : "";
  if (!plan.partly_estimated) return broken;
  const names = plan.partly_estimated_plates || [];
  const why = names.length ? `${esc(names.join(", "))} could not be sliced by OrcaSlicer, so ${names.length === 1 ? "its" : "their"} time and support are Spooler estimates and the finish time may be off.`
    : "Some figures are Spooler estimates (the slicing time ran out), so the finish time may be off.";
  return `${broken}<p class="ps-note">${chip("partly estimated", "est", estimateTip(proposal))} ${why}</p>`;
}

function details(proposal, plan) {
  const tip = estimateTip(proposal);
  const rows = plan.plate_rows.map(r => {
    const names = r.objects.map(o => esc(o.name)).join(", ");
    const turned = r.objects.filter(o => o.turned).length;
    const printer = proposal.printers.find(p => String(p.id) === String(r.printer))?.name || r.printer;
    return `<tr><th scope="row">${esc(r.name)} ${r.new ? chip("new", "plain") : ""} ${r.did_not_slice ? chip("did not slice", "bad") : ""} ${r.partly_estimated ? chip("partly estimated", "est", tip) : ""} ${r.supports ? chip("supports", "plain") : ""}</th>
        <td>${esc(printer)}</td><td>${esc(dur(r.seconds))}</td><td>${esc(grams(r.support_g))}</td>
        <td>${esc(plural(r.objects.length, "object"))}${turned ? `, ${turned} turned` : ""}<div class="slicer-muted">${names}</div></td></tr>`;
  }).join("");
  return `<details class="ps-fold"><summary>Plate details (${plan.plate_rows.length})</summary>
      <div class="ps-scroll"><table class="proposal-table"><thead><tr><th>Plate</th><th>Printer</th><th>Time</th><th>Support</th><th>Objects</th></tr></thead><tbody>${rows}</tbody></table></div></details>`;
}

function tippy(proposal, plan) {
  if (!plan.tippy_objects.length) return "";
  return `<section><h4>Still tippy: ${plan.tippy_objects.length} (was ${proposal.before.tippy})</h4><ul class="ps-list">${plan.tippy_objects.map(t =>
    `<li>${chip("tippy", "bad")} <b>${esc(t.name)}</b> on ${esc(t.plate)} <span class="slicer-muted">${esc(t.reason)}${t.brim ? ". A brim holds it down" : ""}</span></li>`).join("")}</ul></section>`;
}

function leftOut(proposal) {
  const unplanned = proposal.unplanned.length ? `<section><h4>Left unplanned</h4><ul class="ps-list">${proposal.unplanned.map(u =>
    `<li><b>${esc(u.name)}</b> <span class="slicer-muted">${esc(u.reason)}</span></li>`).join("")}</ul></section>` : "";
  const kept = proposal.kept.length ? `<section><h4>Kept as they are</h4><ul class="ps-list">${proposal.kept.map(k =>
    `<li><b>${esc(k.name)}</b> <span class="slicer-muted">${esc(k.reason)}</span></li>`).join("")}</ul></section>` : "";
  return unplanned + kept;
}

function considered(proposal, shown) {
  const others = proposal.plans.filter(p => p.index !== shown.index);
  if (!others.length) return "";
  return `<section><h4>Also considered</h4><div class="ps-cards">${others.map(p => {
    const dPlates = p.plates - shown.plates, dSupport = p.support_g - shown.support_g, dTime = p.makespan_s - shown.makespan_s;
    const bits = [dPlates ? `${plural(Math.abs(dPlates), "plate")} ${dPlates < 0 ? "fewer" : "more"}` : "same plates",
      Math.abs(dSupport) >= 1 ? `${grams(Math.abs(dSupport))} ${dSupport < 0 ? "less" : "more"} support` : "same support",
      Math.abs(dTime) >= 60 ? against(dTime, "than the plan shown") : "same finish time"];
    return `<div class="ps-card"><b>${esc(label(p, proposal.plans))}</b>
        <div class="slicer-muted">${esc(bits.join(", "))}</div>
        <div>${esc(plural(p.plates, "plate"))} · done in ${esc(dur(p.makespan_s))} · ${esc(grams(p.support_g))} support · ${esc(p.tippy)} tippy</div>
        ${p.partly_estimated ? chip("partly estimated", "est", estimateTip(proposal)) : ""}
        <button class="btn btn-secondary btn-sm" type="button" data-ps="review" data-plan="${p.index}">Review this one instead</button></div>`;
  }).join("")}</div></section>`;
}

function readyHtml() {
  const { proposal, index } = run;
  if (!proposal.plans.length) {
    return `<p class="slicer-muted">${esc(proposal.summary)}.</p>${printerNotes(proposal)}${leftOut(proposal)}
      <div class="ps-actions"><button class="btn btn-secondary" type="button" data-ps="dismiss">Close</button></div>`;
  }
  const plan = proposal.plans[index];
  const viewing = index ? `<p class="ps-banner">You are reviewing an alternative plan (${esc(label(plan, proposal.plans).toLowerCase())}), not the recommended one. <button class="btn btn-secondary btn-sm" type="button" data-ps="review" data-plan="0">Back to the recommended plan</button></p>` : "";
  return `${viewing}${headline(proposal, plan)}${timelines(proposal, plan)}${tiles(proposal, plan)}${estimateNote(proposal, plan)}${details(proposal, plan)}${tippy(proposal, plan)}${leftOut(proposal)}${considered(proposal, plan)}`;
}

/** Printers that get nothing because their nozzle is unset or the wrong size, when there is no plan to hang the message on */
function printerNotes(proposal) {
  const notes = proposal.printers.filter(p => p.message);
  return notes.length ? `<ul class="ps-list">${notes.map(p => `<li class="ps-warn">${esc(p.message)}</li>`).join("")}</ul>` : "";
}

function planningHtml() {
  const progress = run.job?.progress || { phase: "queued", percent: 0, message: "Starting" };
  const percent = Math.max(0, Math.min(100, Math.round(progress.percent)));
  return `<div class="ps-progress" role="status">
      <div class="ps-phase"><b>${esc(PHASES[progress.phase] || progress.phase)}</b> <span class="slicer-muted">${esc(progress.message)}</span></div>
      <div class="ps-bar" role="progressbar" aria-label="Planning" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"><i style="width: ${percent}%"></i></div>
      <p class="slicer-muted">Planning takes up to a minute. Nothing in the project changes until you accept. You can close this and the plan will wait: a message appears when it is ready.</p>
    </div>
    <div class="ps-actions"><button class="btn btn-secondary" type="button" data-ps="close">Close</button></div>`;
}

function failedHtml(message) {
  return `<p class="slicer-error" role="alert">${esc(message)}</p>
    <div class="ps-actions"><button class="btn btn-secondary" type="button" data-ps="close">Close</button><button class="btn btn-primary" type="button" data-ps="again">Plan again</button></div>`;
}

function render() {
  const dialog = $("dlg-plan");
  if (!run || !dialog.open || !dialog.classList.contains("ps-mode")) return;
  const scroll = $("plan-body").scrollTop, open = dialog.querySelector(".ps-fold")?.open;
  $("plan-body").innerHTML = run.phase === "planning" ? planningHtml() : run.phase === "ready" ? readyHtml() : failedHtml(run.message);
  if (open) dialog.querySelector(".ps-fold").open = true;
  $("plan-body").scrollTop = scroll;
}

// -- the job --------------------------------------------------------------------------------------------------------------

function show(title) {
  const dialog = $("dlg-plan");
  dialog.classList.add("ps-mode");
  $("plan-title").textContent = title;
  $("plan-error").textContent = "";
  if (!dialog.open) dialog.showModal();
}

/** Plan this project's plates: start the planner job and show its progress, then its Proposal */
async function open() {
  await host.flush();
  const mine = ++generation, projectId = host.projectId();
  run = { gen: mine, projectId, job: null, proposal: null, index: 0, stale: null, phase: "planning" };
  show("Plan plates");
  render();
  try {
    run.job = await host.api("POST", `/api/projects/${projectId}/plan`, {});
  } catch (err) {
    if (mine === generation) { run.phase = "failed"; run.message = err.message; render(); }
    return;
  }
  render();
  poll(mine, 0);
}

function poll(mine, failures) {
  setTimeout(async () => {
    if (mine !== generation) return;
    let job;
    try {
      job = await host.api("GET", `/api/projects/${run.projectId}/plan/jobs/${run.job.id}`);
    } catch (err) {
      if (mine !== generation) return;
      if (err.status === 404 || failures + 1 >= POLL_FAILURES) { run.phase = "failed"; run.message = err.status === 404 ? err.message : `Spooler is not answering: ${err.message}`; render(); return; }
      poll(mine, failures + 1);
      return;
    }
    if (mine !== generation) return;
    run.job = job;
    if (job.state === "done" && job.proposal) { ready(job.proposal); return; }
    if (job.state === "failed") { run.phase = "failed"; run.message = `Planning failed: ${job.error || "unknown error"}`; render(); return; }
    if (job.state === "cancelled") { run.phase = "failed"; run.message = "A newer plan replaced this one."; render(); return; }
    render();
    poll(mine, 0);
  }, POLL_MS);
}

function ready(proposal) {
  run.proposal = proposal;
  run.phase = "ready";
  run.index = 0;
  run.stale = proposal.superseded ? SUPERSEDED : null;
  const dialog = $("dlg-plan");
  if (dialog.open && dialog.classList.contains("ps-mode")) { render(); return; }
  const mine = run.gen, token = proposal.token;
  host.toast("The plan is ready", false, { action: { label: "Review", onClick: () => reopen(mine, token) } });
}

/** Bring back the sheet for a finished run whose dialog was closed */
function reopen(mine, token) {
  if (!run || run.gen !== mine || run.proposal?.token !== token) return;
  show("Plan plates");
  render();
}

// -- actions --------------------------------------------------------------------------------------------------------------

async function accept(button) {
  const { proposal, index } = run, projectId = run.projectId;
  button.disabled = true;
  try {
    const result = await host.api("POST", `/api/projects/${projectId}/plan/accept`, { proposal: proposal.token, plan: index });
    close();
    host.applied(result, result.plan);
  } catch (err) {
    if (err.status === 409 || err.status === 404) {
      run.stale = err.status === 404 ? "That plan is gone." : err.message === "The project was changed elsewhere — reload it" ? SUPERSEDED : err.message;
      render();
    } else {
      button.disabled = false;
      $("ps-error").innerHTML = `<span class="slicer-error">${esc(err.message)}</span>`;
    }
  }
}

async function dismiss() {
  const proposal = run?.proposal, projectId = run?.projectId;
  close();
  if (!proposal) return;
  try { await host.api("POST", `/api/projects/${projectId}/plan/dismiss`, { proposal: proposal.token }); }
  catch (_) { /* already gone or the project went away: nothing to throw away */ }
}

/** Close the dialog; a run still going keeps going and says when it is ready */
function close() {
  const dialog = $("dlg-plan");
  if (dialog.open) dialog.close();
}

function onClick(event) {
  const button = event.target.closest("[data-ps]");
  if (!button || !run) return;
  switch (button.dataset.ps) {
    case "accept": accept(button); break;
    case "dismiss": dismiss(); break;
    case "close": close(); break;
    case "again": open().catch(err => host.toast(err.message, true)); break;
    case "review": run.index = Number(button.dataset.plan); render(); $("plan-body").scrollTo({ top: 0 }); break;
  }
}

/** The dialog closed (or another sheet took it over): this is no longer the sheet in view */
function released() {
  $("dlg-plan").classList.remove("ps-mode");
}

export const planSheet = {
  attach(h) {
    host = h;
    $("plan-body").addEventListener("click", onClick);
    $("dlg-plan").addEventListener("close", () => { if (!$("dlg-plan").open) released(); });   // not when Plan again reopened it
  },
  open,
  dur,
  /** Another use of the dialog (an Assistant Proposal) takes it over */
  release() { released(); },
  get active() { return !!run && $("dlg-plan").classList.contains("ps-mode"); },
};
