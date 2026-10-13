/* Spooler – print history panel: search, filters, details, notes and repeat prints.
   Loaded before app.js; uses app.js/jobs.js globals (printers, toast, escHtml, jobsApi, …) only at call time. */
"use strict";

const HISTORY_PAGE = 50;
const PRINT_OUTCOME_LABEL = OUTCOME_LABEL; // the one Outcome vocabulary (status-badge.js)

// The reason ids and labels live on the backend (GET /api/failure-reasons); loadFailureReasons() fetches them once.
let _failureReasons = { cancel_reasons: [], labels: {} };
let _failureReasonsRequest = null;
function loadFailureReasons() {
  if (!_failureReasonsRequest) {
    _failureReasonsRequest = fetch("/api/failure-reasons").then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then(data => { _failureReasons = data; })
      .catch(() => { _failureReasonsRequest = null; }); // try again next time; the form just has no reasons until then
  }
  return _failureReasonsRequest;
}
const failureReasonLabel = id => _failureReasons.labels[id] || id;

let historyPrints = [];
let historyTotal  = 0;
let _historyOpenId  = null;   // print whose details are expanded
let _historyDetails = {};     // print id → detail payload (with job and job events)
let _historyCurrency;         // undefined until fetched from Spoolman
let _historySearchTimer = null;
let _historyUnconfirmed = 0;  // Prints whose Outcome the user has not confirmed (all time, ignores the filters)
let _historyRequest = 0;
const _historyDrafts = {};
const _historyFeedback = {};
function historyFocusSelector(el) {
  for (const key of ["historyToggle", "historyNotes", "historyOutcome", "historyAction"]) {
    if (el.dataset[key]) return `[data-${key.replace(/[A-Z]/g, c => "-" + c.toLowerCase())}="${el.dataset[key]}"]` +
      (el.dataset.printId ? `[data-print-id="${el.dataset.printId}"]` : "");
  }
  return null;
}
function historySaveFeedback(id, text, error = false) {
  _historyFeedback[id] = { text, error };
  const el = document.querySelector(`[data-history-feedback="${id}"]`);
  if (el) { el.textContent = text; el.classList.toggle("error", error); }
}

const _histAttr = s => escAttr(escHtml(s));

function printOutcomeLabel(outcome) {
  return outcome ? PRINT_OUTCOME_LABEL[outcome] || outcome : "Printing";
}

function formatCost(value) {
  if (value == null) return "";
  if (_historyCurrency) {
    try {
      return new Intl.NumberFormat(undefined, { style: "currency", currency: _historyCurrency }).format(value);
    } catch (_) { /* unknown currency code */ }
  }
  return value.toFixed(2);
}

async function _loadCurrency() {
  if (_historyCurrency) return;
  try {
    const r = await fetch("/api/spoolman/api/v1/setting/currency");
    if (r.ok) _historyCurrency = JSON.parse((await r.json()).value);
  } catch (_) { /* Spoolman unreachable: costs show without a symbol */ }
}

function _historyParams() {
  const params = new URLSearchParams();
  const q = document.getElementById("history-search").value.trim();
  const printer = document.getElementById("history-printer").value;
  const outcome = document.getElementById("history-outcome").value;
  const days = Number(document.getElementById("history-range").value);
  if (q) params.set("q", q);
  if (printer) params.set("printer_id", printer);
  if (outcome === "unconfirmed") params.set("unconfirmed", "1");
  else if (outcome) params.set("outcome", outcome);
  if (days) params.set("since", String(Math.floor(Date.now() / 1000 - days * 86400)));
  return params;
}

async function loadHistory(append = false) {
  const params = _historyParams();
  params.set("limit", HISTORY_PAGE);
  params.set("offset", append ? historyPrints.length : 0);
  const request = ++_historyRequest;
  const results = document.getElementById("history-result-count");
  results.textContent = append ? "Loading more prints…" : "Loading history…";
  document.getElementById("btn-history-retry").hidden = true;
  document.getElementById("history-list").setAttribute("aria-busy", "true");
  try {
    await loadFailureReasons();
    const r = await fetch(`/api/prints?${params}`);
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    if (request !== _historyRequest) return; // superseded by a newer search
    const firstNew = append ? historyPrints.length : -1;
    historyPrints = append ? historyPrints.concat(data.prints) : data.prints;
    historyTotal = data.total;
    renderHistory(data.summary);
    // Load more: the button can disappear (all loaded) and focus would fall to the page, so land on the first new row
    if (append && data.prints.length) document.querySelector(`[data-history-toggle="${historyPrints[firstNew].id}"]`)?.focus();
  } catch (err) {
    if (request === _historyRequest && document.getElementById("panel-history").classList.contains("open")) {
      results.textContent = `Could not load history: ${err.message}. Previous results may be out of date.`;
      document.getElementById("btn-history-retry").hidden = false;
      toast(`Couldn't load history: ${err.message}`, true);
    }
  } finally {
    if (request === _historyRequest) document.getElementById("history-list").setAttribute("aria-busy", "false");
  }
}

// ─── Rendering ────────────────────────────────────────────────────────────────
function renderHistory(summary) {
  updateHistoryFilterSummary();
  if (summary) {
    const bits = [`<span><strong>${summary.prints}</strong> print${summary.prints === 1 ? "" : "s"}</span>`];
    if (summary.success_rate != null) bits.push(`<span><strong>${Math.round(summary.success_rate * 100)}%</strong> completed</span>`);
    if (summary.filament_g != null) bits.push(`<span><strong>${Math.round(summary.filament_g)} g</strong> filament</span>`);
    if (summary.cost != null) bits.push(`<span><strong>${escHtml(formatCost(summary.cost))}</strong> material</span>`);
    if (summary.duration_s != null) bits.push(`<span><strong>${(summary.duration_s / 3600).toFixed(1)} h</strong> printing</span>`);
    document.getElementById("history-totals").innerHTML = bits.join("");
  }
  renderUnconfirmedChip();
  document.getElementById("btn-history-export").disabled = historyTotal === 0;
  const list = document.getElementById("history-list");
  const active = document.activeElement;
  const focusSelector = active && list.contains(active) ? historyFocusSelector(active) : null;
  for (const input of list.querySelectorAll("[data-history-notes]")) _historyDrafts[input.dataset.historyNotes] = input.value;
  list.innerHTML = historyPrints.map(historyItem).join("");
  for (const input of list.querySelectorAll("[data-history-notes]")) {
    if (Object.hasOwn(_historyDrafts, input.dataset.historyNotes)) input.value = _historyDrafts[input.dataset.historyNotes];
  }
  if (focusSelector) list.querySelector(focusSelector)?.focus({ preventScroll: true });
  document.getElementById("history-empty").hidden = historyPrints.length > 0;
  document.getElementById("btn-history-more").hidden = historyPrints.length >= historyTotal;
}

// "N unconfirmed" in the totals line (sets the Outcome filter) and a count on the History nav entry.
function renderUnconfirmedChip() {
  const totals = document.getElementById("history-totals");
  totals.querySelector("#btn-history-unconfirmed")?.remove();
  if (!_historyUnconfirmed) return;
  const chip = document.createElement("button");
  chip.type = "button";
  chip.id = "btn-history-unconfirmed";
  chip.className = "history-unconfirmed-chip";
  const active = document.getElementById("history-outcome").value === "unconfirmed";
  chip.setAttribute("aria-pressed", String(active));
  chip.textContent = `${_historyUnconfirmed} unconfirmed`;
  chip.title = "Show only the Prints whose Outcome you have not confirmed";
  totals.appendChild(chip);
}
function setUnconfirmedCount(n) {
  _historyUnconfirmed = n;
  for (const id of ["history-unconfirmed-count", "history-unconfirmed-count-mobile"]) {
    const badge = document.getElementById(id);
    if (!badge) continue;
    badge.hidden = n === 0;
    badge.innerHTML = `${n}<span class="sr-only"> unconfirmed</span>`;
  }
  renderUnconfirmedChip();
}
async function refreshUnconfirmedCount() {
  try {
    const r = await fetch("/api/prints?unconfirmed=1&limit=1");
    if (r.ok) setUnconfirmedCount(Number((await r.json()).total) || 0);
  } catch (_) { /* the count just stays as it was */ }
}

function _safeColor(hex) {
  return /^[0-9a-f]{6}([0-9a-f]{2})?$/i.test(hex || "") ? hex : "888888";
}

function historyItem(p) {
  const started = new Date(p.started_at * 1000);
  const when = started.toLocaleString(undefined, { ...(started.getFullYear() === new Date().getFullYear() ? {} : { year: "numeric" }),
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const stats = [
    p.duration_s != null ? `${(p.duration_s === 0 ? '0s' : formatTime(p.duration_s))} observed` : "Duration not recorded",
    p.filament_g != null ? `${p.filament_g} g observed` : "Filament not recorded",
    p.cost != null ? formatCost(p.cost) : "",
  ].filter(Boolean);
  const open = _historyOpenId === p.id;
  return `<div class="history-item${open ? " open" : ""}">
    <button class="history-row" data-history-toggle="${p.id}" aria-expanded="${open}" aria-controls="history-detail-${p.id}">
      <span class="history-date">${escHtml(when)}</span>
      <span class="history-file" title="${_histAttr(p.file_name)}">
        ${p.quick && p.thumbnail ? `<img class="history-thumb" src="${_histAttr(p.thumbnail)}" alt="" loading="lazy" onerror="this.remove()">` : ""}${p.color_hex ? `<span class="history-swatch" style="background:#${_safeColor(p.color_hex)}"></span>` : ""}${escHtml(p.quick && p.project_name ? p.project_name : p.file_name)}${p.quick ? ` <span class="history-quick">Quick print</span>` : ""}
      </span>
      <span class="history-printer">${escHtml(p.printer_name)}</span>
      <span class="history-badges">${outcomeBadge(p.outcome)}${p.outcome_unconfirmed ? unconfirmedBadge() : ""}</span>
      <span class="history-stats">${escHtml(stats.join(" · "))}</span>
    </button>
    ${open ? historyDetail(p) : ""}
  </div>`;
}

function historyDetail(p) {
  const detail = _historyDetails[p.id];
  const at = ts => ts != null ? new Date(ts * 1000).toLocaleString() : "—";
  const facts = [
    ["Timing", "Started", at(p.started_at)],
    ["Timing", "Ended", p.ended_at != null ? at(p.ended_at) : p.outcome ? "End time not recorded" : "Still printing"],
    ["Timing", "Observed duration", p.duration_s != null ? (p.duration_s === 0 ? '0s' : formatTime(p.duration_s)) : "Not recorded"],
    ["Timing", "Slicer estimate · duration", p.estimate_s != null ? (p.estimate_s === 0 ? '0s' : formatTime(p.estimate_s)) : "Not available"],
    ["Timing", "Layers", p.total_layers != null ? `${p.layer != null ? p.layer : "Not recorded"} of ${p.total_layers}` : "Not recorded"],
    ["Filament", "Observed filament", (p.filament_g != null ? `${p.filament_g} g` : "Weight not recorded") +
      " · " + (p.filament_mm != null ? `${(p.filament_mm / 1000).toFixed(2)} m` : "Length not recorded")],
    ["Filament", "Slicer estimate · filament", p.estimate_g != null ? `${p.estimate_g} g` : "Not available"],
    ["Filament", "Material", [p.vendor, p.material, p.filament_name].filter(Boolean).join(" ") || "—"],
    ["Filament", "Cost", p.cost != null ? formatCost(p.cost) : "—"],
    ["Job", "Queue job", p.job_id ? `#${p.job_id}${detail?.job ? ` · ${detail.job.name}` : ""}` : "Started outside the queue"],
  ];
  if (p.failure_reasons?.length) facts.push(["Job", "Failure reasons", p.failure_reasons.map(failureReasonLabel).join(", ")]);
  if (p.detail) facts.push(["Job", "Details", p.detail]);
  if (p.source === "imported") facts.push(["Job", "Source", "Imported from the previous history log"]);

  // Older records lack many of these: fold the empty rows into one line instead of a wall of "Not recorded"
  const missing = ([, , v]) => /^(not recorded|not available|—|end time not recorded)$/i.test(v) || /^weight not recorded · length not recorded$/i.test(v);
  const factRows = list => list.map(([, k, v]) => `<dt>${escHtml(k)}</dt><dd>${escHtml(v)}</dd>`).join("");
  const groups = ["Timing", "Filament", "Job"].map(g => [g, facts.filter(f => f[0] === g && !missing(f))]).filter(([, rows]) => rows.length)
    .map(([g, rows]) => `<section class="history-group" aria-labelledby="history-group-${p.id}-${g}"><h4 id="history-group-${p.id}-${g}">${g}</h4><dl class="history-facts">${factRows(rows)}</dl></section>`).join("");
  const notRecorded = facts.filter(missing);
  const folded = notRecorded.length ? `<details class="history-missing"><summary>${notRecorded.length} detail${notRecorded.length === 1 ? "" : "s"} not recorded</summary><dl class="history-facts">${factRows(notRecorded)}</dl></details>` : "";

  const spools = (p.spools || []).map(s =>
    `<li><span class="history-swatch" style="background:#${_safeColor(s.color_hex)}"></span>` +
    `Spool #${s.spool_id} · ${escHtml([s.vendor, s.material, s.filament_name].filter(Boolean).join(" ") || "unknown filament")}` +
    ` · ${s.grams} g${s.cost != null ? ` · ${escHtml(formatCost(s.cost))}` : ""}</li>`).join("");

  const events = detail?.job_events?.length
    ? `<details class="history-events"><summary>Job timeline (${detail.job_events.length} events)</summary><ol>` +
      detail.job_events.map(e => `<li><time>${escHtml(new Date(e.at * 1000).toLocaleString())}</time> ` +
        `${escHtml(e.event)}${e.detail ? ` — ${escHtml(e.detail)}` : ""}</li>`).join("") +
      "</ol></details>"
    : "";

  return `<div class="history-detail" id="history-detail-${p.id}" role="region" aria-label="Details for Print ${p.id}">
    ${groups}${folded}
    ${spools ? `<section class="history-group" aria-labelledby="history-spools-${p.id}"><h4 id="history-spools-${p.id}">Spools used</h4><ul class="history-spools">${spools}</ul></section>` : ""}
    ${events}
    ${p.outcome ? historyOutcomeForm(p) : ""}
    <label class="history-notes">
      Notes
      <textarea data-history-notes="${p.id}" rows="3" maxlength="4000"
        placeholder="What worked, what failed, settings to reuse…">${escHtml(p.notes || "")}</textarea>
    </label>
    <p class="history-save-feedback${_historyFeedback[p.id]?.error ? " error" : ""}" data-history-feedback="${p.id}" role="status" aria-live="polite">${escHtml(_historyFeedback[p.id]?.text || "")}</p>
    <div class="job-controls">
      <button class="btn btn-secondary btn-sm" data-history-action="save" data-print-id="${p.id}">Save notes</button>
      ${p.outcome ? `<button class="btn btn-primary btn-sm" data-history-action="repeat" data-print-id="${p.id}">Print again</button>` : ""}
      ${p.quick ? quickPrintButtons(p) : ""}
    </div>
  </div>`;
}

// A quick print can be printed again exactly as it was, or promoted to a normal Project. Both need its hidden project
// (and Reprint its stored G-code); without them the Print stays in History and the buttons are disabled.
function quickPrintButtons(p) {
  const gone = "The hidden project for this quick print is gone";
  // aria-disabled (not disabled) keeps the button focusable, so a keyboard user reaches the reason that sits under it
  const button = (action, label, why) => {
    const reasonId = `qp-why-${p.id}-${action}`;
    return `<span class="qp-action"><button class="btn btn-secondary btn-sm" data-history-action="${action}" data-print-id="${p.id}"${why ? ` aria-disabled="true" aria-describedby="${reasonId}"` : ""}>${label}</button>${why ? `<small class="reason-line" id="${reasonId}">${_histAttr(why)}</small>` : ""}</span>`;
  };
  const reprintWhy = !p.outcome ? "Still printing" : !p.reprint_available ? (p.project_available ? "The stored G-code for this print is gone" : gone) : "";
  const openWhy = p.project_available ? "" : gone;
  const changeWhy = !p.outcome ? "Still printing" : openWhy;
  return [button("reprint", "Reprint", reprintWhy), button("reprint-changes", "Reprint with changes", changeWhy),
    button("open-project", "Open as project", openWhy)].join(" ");
}

// Reprint with changes reopens the Quick print modal on the Print's hidden project, which still holds the printer, preset,
// spool, plate side and options that print used. Slice & print there queues a NEW Job; this Print is never touched.
// Unchanged, the project's existing Slice is reused (same G-code, no re-slice); any changed option makes it Outdated, so it slices again.
function reprintWithChanges(record) {
  if (!window.QuickPrint) { toast("The Quick print window isn't loaded", true); return; }
  closeHistory();
  window.QuickPrint.open({ projectId: record.project_id, printerId: record.printer_id });
}

// What saving would do, in words, so the change is not a surprise.
function outcomeReadback(p, outcome, reasons) {
  const to = printOutcomeLabel(outcome);
  const why = reasons.length ? ` Reasons: ${reasons.map(failureReasonLabel).join(", ")}.` : "";
  if (outcome === p.outcome) return `Outcome stays ${to}${p.outcome_unconfirmed ? " and is marked confirmed" : ""}.${why}`;
  return `Outcome changes from ${printOutcomeLabel(p.outcome)} to ${to} and is marked confirmed.${why}`;
}
function updateOutcomeReadback(id) {
  const p = historyPrints.find(x => x.id === id), out = document.querySelector(`[data-history-readback="${id}"]`);
  if (!p || !out) return;
  const outcome = document.querySelector(`[data-history-outcome="${id}"]`).value;
  const reasons = [...document.querySelectorAll(`[data-history-reason="${id}"]:checked`)].map(c => c.value);
  out.textContent = outcomeReadback(p, outcome, reasons);
}

function historyOutcomeForm(p) {
  const reasons = _failureReasons.cancel_reasons
    .map(r => `<label${r === "cancelled_on_purpose" && p.outcome !== "cancelled" ? " hidden" : ""}><input type="checkbox" data-history-reason="${p.id}" value="${r}"${(p.failure_reasons || []).includes(r) ? " checked" : ""}> ${escHtml(failureReasonLabel(r))}</label>`).join(" ");
  const options = ["completed", "cancelled", "failed", "unknown"]
    .map(o => `<option value="${o}"${o === p.outcome ? " selected" : ""}>${escHtml(PRINT_OUTCOME_LABEL[o])}</option>`).join("");
  return `<fieldset class="history-outcome-form">
    <legend>${p.outcome_unconfirmed ? "Confirm or correct the outcome" : "Correct the outcome"}</legend>
    <select data-history-outcome="${p.id}" aria-label="Outcome">${options}</select>
    <fieldset class="history-reasons"${p.outcome === "completed" || p.outcome === "unknown" ? " hidden" : ""}>
      <legend>What went wrong? (pick at least one for a failed print)</legend>${reasons}</fieldset>
    <p class="history-readback" data-history-readback="${p.id}" role="status" aria-live="polite">${escHtml(outcomeReadback(p, p.outcome, p.failure_reasons || []))}</p>
    <button class="btn btn-secondary btn-sm" data-history-action="outcome" data-print-id="${p.id}">${p.outcome_unconfirmed ? "Confirm outcome" : "Save outcome"}</button>
  </fieldset>`;
}

function onPrintRecorded(record) {
  const grams = record.filament_g ? ` · ${Math.round(record.filament_g)} g` : "";
  toast(`${record.printer_name}: ${record.file_name} — ${printOutcomeLabel(record.outcome).toLowerCase()}${grams}`,
    record.outcome !== "completed");
  refreshUnconfirmedCount();
  if (!document.getElementById("panel-history").classList.contains("open")) return;
  if (document.activeElement?.closest?.(".history-detail")) return; // don't clobber unsaved notes
  loadHistory();
}

// ─── Events ───────────────────────────────────────────────────────────────────
document.getElementById("history-list").addEventListener("click", async (e) => {
  const toggle = e.target.closest("[data-history-toggle]");
  if (toggle) {
    const id = Number(toggle.dataset.historyToggle);
    _historyOpenId = _historyOpenId === id ? null : id;
    renderHistory();
    if (_historyOpenId === id && !_historyDetails[id]) {
      try {
        const r = await fetch(`/api/prints/${id}`);
        if (r.ok) {
          _historyDetails[id] = await r.json();
          if (_historyOpenId === id && !document.activeElement?.closest(".history-detail")) renderHistory();
        }
      } catch (_) { /* details stay basic */ }
    }
    return;
  }

  const btn = e.target.closest("[data-history-action]");
  if (!btn || btn.getAttribute("aria-disabled") === "true") return;
  const id = Number(btn.dataset.printId);
  const record = historyPrints.find(p => p.id === id);
  if (!record) return;
  const restoreActionFocus = document.activeElement === btn;
  btn.disabled = true;
  try {
    if (btn.dataset.historyAction === "save") {
      const notes = document.querySelector(`[data-history-notes="${id}"]`).value;
      const updated = await jobsApi("PATCH", `/api/prints/${id}`, { notes });
      record.notes = updated.notes;
      _historyDetails[id] = updated;
      delete _historyDrafts[id];
      historySaveFeedback(id, "Notes saved.");
      toast("Notes saved");
    } else if (btn.dataset.historyAction === "outcome") {
      const outcome = document.querySelector(`[data-history-outcome="${id}"]`).value;
      const reasons = [...document.querySelectorAll(`[data-history-reason="${id}"]:checked`)].map(c => c.value);
      if (outcome === "failed" && !reasons.length) {   // the same rule as the Failed sheet (outcome-prompt.js)
        historySaveFeedback(id, "Pick at least one failure reason before saving a failed print.", true);
        document.querySelector(`[data-history-reason="${id}"]`)?.focus();
        btn.disabled = false;
        return;
      }
      const updated = await jobsApi("POST", `/api/prints/${id}/outcome`, { outcome, failure_reasons: reasons });
      Object.assign(record, updated);
      _historyDetails[id] = updated;
      historySaveFeedback(id, "Outcome saved and confirmed.");
      renderHistory();
      refreshUnconfirmedCount();
      document.querySelector(`[data-history-action="outcome"][data-print-id="${id}"]`)?.focus();
      toast("Outcome saved");
    } else if (btn.dataset.historyAction === "reprint") {
      const job = await jobsApi("POST", `/api/prints/${id}/reprint`, {});
      loadJobs();
      loadHistory();
      toastAction(`Queued ${job.name} again`, "Open queue", () => { closeHistory(); openQueue(); });
    } else if (btn.dataset.historyAction === "reprint-changes") {
      reprintWithChanges(record);
    } else if (btn.dataset.historyAction === "open-project") {
      const project = await jobsApi("POST", `/api/projects/${record.project_id}/open-as-project`, {});
      toastAction(`${project.name} is now a project`, "Open project", () => { location.href = `/slicer.html?project=${project.id}`; });
      loadHistory();
    } else if (btn.dataset.historyAction === "repeat") {
      const job = await jobsApi("POST", `/api/prints/${id}/repeat`, {});
      loadJobs();
      toastAction(`Queued ${job.name} again`, "Open queue", () => { closeHistory(); openQueue(); });
    }
  } catch (err) {
    historySaveFeedback(id, `Could not save: ${err.message}. Your entries are retained. Try again.`, true);
    toast(err.message, true);
  }
  btn.disabled = false;
  if (restoreActionFocus) document.querySelector(`[data-history-action="${btn.dataset.historyAction}"][data-print-id="${id}"]`)?.focus();
});

document.getElementById("history-list").addEventListener("change", (e) => {
  const reason = e.target.closest("[data-history-reason]");
  if (reason) { updateOutcomeReadback(Number(reason.dataset.historyReason)); return; }
  const select = e.target.closest("[data-history-outcome]");
  if (!select) return;
  const box = select.parentElement.querySelector(".history-reasons");
  box.hidden = !["failed", "cancelled"].includes(select.value);
  for (const c of box.querySelectorAll('[value="cancelled_on_purpose"]')) {
    c.parentElement.hidden = select.value !== "cancelled";
    if (select.value !== "cancelled") c.checked = false;
  }
  updateOutcomeReadback(Number(select.dataset.historyOutcome));
});

document.getElementById("history-search").addEventListener("input", () => {
  clearTimeout(_historySearchTimer);
  _historySearchTimer = setTimeout(() => loadHistory(), 300);
});
for (const id of ["history-printer", "history-outcome", "history-range"]) {
  document.getElementById(id).addEventListener("change", () => loadHistory());
}
document.getElementById("btn-history-more").addEventListener("click", () => loadHistory(true));
// Fetched, not navigated to: a failure shows a toast and leaves the page where it is.
document.getElementById("btn-history-export").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  if (historyTotal === 0) return;
  btn.disabled = true;
  try {
    const r = await fetch(`/api/prints/export.csv?${_historyParams()}`);
    if (!r.ok) {
      let message = `HTTP ${r.status}`;
      try { message = (await r.json()).error || message; } catch (_) { /* not JSON */ }
      throw new Error(message);
    }
    const url = URL.createObjectURL(await r.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = (/filename="([^"]+)"/.exec(r.headers.get("Content-Disposition") || "") || [])[1] || "spooler-prints.csv";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(`Exported ${historyTotal} print${historyTotal === 1 ? "" : "s"}`);
  } catch (err) {
    toast(`Couldn't export history: ${err.message}`, true);
  } finally {
    btn.disabled = historyTotal === 0;
  }
});
document.getElementById("history-totals").addEventListener("click", (e) => {
  if (!e.target.closest("#btn-history-unconfirmed")) return;
  const outcome = document.getElementById("history-outcome");
  outcome.value = outcome.value === "unconfirmed" ? "" : "unconfirmed";
  loadHistory();
});

// ─── Panel ────────────────────────────────────────────────────────────────────
function openHistory() {
  window.openSidePanel(document.getElementById("panel-history"), document.activeElement);
  document.getElementById("btn-history").classList.add("active");
  const select = document.getElementById("history-printer");
  const current = select.value;
  select.innerHTML = '<option value="">All printers</option>' + Object.values(printers)
    .map(p => `<option value="${_histAttr(p.id)}">${escHtml(p.name)}</option>`).join("");
  select.value = current;
  refreshUnconfirmedCount();
  _loadCurrency().then(() => {
    if (document.getElementById("history-list").getAttribute("aria-busy") !== "true" && document.getElementById("btn-history-retry").hidden) renderHistory();
  });
  loadHistory();
}

function closeHistory() {
  window.closeSidePanel(document.getElementById("panel-history"));
  document.getElementById("btn-history").classList.remove("active");
}

document.getElementById("btn-history").addEventListener("click", openHistory);
document.getElementById("btn-history-close").addEventListener("click", closeHistory);

// Filter context stays visible when the controls are collapsed on phones.
const historyFilters = document.querySelector("#panel-history .history-filters");
historyFilters.id = "history-filter-controls";
const historyFilterBar = document.createElement("div");
historyFilterBar.className = "history-filter-bar";
historyFilterBar.innerHTML = '<button class="btn btn-secondary btn-sm" id="btn-history-filters" aria-expanded="true" aria-controls="history-filter-controls">Hide filters</button><span id="history-result-count" role="status" aria-live="polite"></span><span id="history-filter-summary"></span><span id="history-filter-chips" class="history-chips"></span><button id="btn-history-retry" class="btn btn-secondary btn-sm" hidden>Retry history</button>';
historyFilters.before(historyFilterBar);
// On a phone the filters start collapsed; the active ones stay visible as chips you can clear one by one.
if (window.matchMedia("(max-width: 640px)").matches) {
  historyFilters.hidden = true;
  const toggle = document.getElementById("btn-history-filters");
  toggle.setAttribute("aria-expanded", "false");
  toggle.textContent = "Show filters";
}
document.getElementById("btn-history-retry").addEventListener("click", () => loadHistory());
document.getElementById("btn-history-filters").addEventListener("click", e => {
  historyFilters.hidden = !historyFilters.hidden;
  e.currentTarget.setAttribute("aria-expanded", String(!historyFilters.hidden));
  e.currentTarget.textContent = historyFilters.hidden ? "Show filters" : "Hide filters";
});
function updateHistoryFilterSummary() {
  const bits = [];
  const search = document.getElementById("history-search").value.trim();
  if (search) bits.push(`Search: ${search}`);
  for (const id of ["history-printer", "history-outcome", "history-range"]) {
    const el = document.getElementById(id);
    if (el.value) bits.push(el.selectedOptions[0].textContent);
  }
  document.getElementById("history-filter-summary").textContent = bits.join(" · ") || "All prints · all time";
  const chips = document.getElementById("history-filter-chips");
  chips.replaceChildren();
  const addChip = (label, clear) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "history-chip";
    chip.textContent = `${label} ✕`;
    chip.setAttribute("aria-label", `Clear filter: ${label}`);
    chip.addEventListener("click", () => { clear(); loadHistory(); });
    chips.appendChild(chip);
  };
  if (search) addChip(`Search: ${search}`, () => { document.getElementById("history-search").value = ""; });
  for (const id of ["history-printer", "history-outcome", "history-range"]) {
    const el = document.getElementById(id);
    if (el.value) addChip(el.selectedOptions[0].textContent, () => { el.value = ""; });
  }
  document.getElementById("history-result-count").textContent = historyPrints.length < historyTotal
    ? `Showing ${historyPrints.length} of ${historyTotal}` : `${historyTotal} result${historyTotal === 1 ? "" : "s"}`;
}

// The count on the History entry is wanted before the panel is ever opened.
refreshUnconfirmedCount();
