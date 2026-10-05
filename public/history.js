/* Spooler – print history panel: search, filters, details, notes and repeat prints.
   Loaded before app.js; uses app.js/jobs.js globals (printers, toast, escHtml, jobsApi, …) only at call time. */
"use strict";

const HISTORY_PAGE = 50;
const PRINT_OUTCOME_LABEL = { completed: "Completed", cancelled: "Cancelled", failed: "Failed", unknown: "Outcome unknown" };
const PRINT_OUTCOME_CLASS = { completed: "complete", cancelled: "cancelled", failed: "offline", unknown: "paused" };

const FAILURE_REASON_LABEL = {
  adhesion: "Adhesion / came loose", spaghetti: "Spaghetti", warping: "Warping", layer_shift: "Layer shift",
  clog: "Clog / under-extrusion", filament: "Filament ran out or tangled", surface_quality: "Surface quality",
  power_network: "Power or network", other: "Other", cancelled_on_purpose: "Cancelled on purpose",
};
const FAILURE_REASON_IDS = Object.keys(FAILURE_REASON_LABEL).filter(r => r !== "cancelled_on_purpose");

let historyPrints = [];
let historyTotal  = 0;
let _historyOpenId  = null;   // print whose details are expanded
let _historyDetails = {};     // print id → detail payload (with job and job events)
let _historyCurrency;         // undefined until fetched from Spoolman
let _historySearchTimer = null;
let _historyRequest = 0;

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
  try {
    const r = await fetch(`/api/prints?${params}`);
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    if (request !== _historyRequest) return; // superseded by a newer search
    historyPrints = append ? historyPrints.concat(data.prints) : data.prints;
    historyTotal = data.total;
    renderHistory(data.summary);
  } catch (err) {
    if (request === _historyRequest && document.getElementById("panel-history").classList.contains("open")) {
      toast(`Couldn't load history: ${err.message}`, true);
    }
  }
}

// ─── Rendering ────────────────────────────────────────────────────────────────
function renderHistory(summary) {
  if (summary) {
    const bits = [`<span><strong>${summary.prints}</strong> print${summary.prints === 1 ? "" : "s"}</span>`];
    if (summary.success_rate != null) bits.push(`<span><strong>${Math.round(summary.success_rate * 100)}%</strong> completed</span>`);
    if (summary.filament_g) bits.push(`<span><strong>${Math.round(summary.filament_g)} g</strong> filament</span>`);
    if (summary.cost) bits.push(`<span><strong>${escHtml(formatCost(summary.cost))}</strong> material</span>`);
    if (summary.duration_s) bits.push(`<span><strong>${(summary.duration_s / 3600).toFixed(1)} h</strong> printing</span>`);
    document.getElementById("history-totals").innerHTML = bits.join("");
  }
  document.getElementById("history-list").innerHTML = historyPrints.map(historyItem).join("");
  document.getElementById("history-empty").hidden = historyPrints.length > 0;
  document.getElementById("btn-history-more").hidden = historyPrints.length >= historyTotal;
}

function _safeColor(hex) {
  return /^[0-9a-f]{6}([0-9a-f]{2})?$/i.test(hex || "") ? hex : "888888";
}

function historyItem(p) {
  const when = new Date(p.started_at * 1000).toLocaleString(undefined,
    { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  const stats = [
    p.duration_s ? formatTime(p.duration_s) : "",
    p.filament_g ? `${Math.round(p.filament_g)} g` : "",
    p.cost != null ? formatCost(p.cost) : "",
  ].filter(Boolean);
  const open = _historyOpenId === p.id;
  return `<div class="history-item${open ? " open" : ""}">
    <button class="history-row" data-history-toggle="${p.id}" aria-expanded="${open}">
      <span class="history-date">${escHtml(when)}</span>
      <span class="history-file" title="${_histAttr(p.file_name)}">
        ${p.color_hex ? `<span class="history-swatch" style="background:#${_safeColor(p.color_hex)}"></span>` : ""}${escHtml(p.file_name)}
      </span>
      <span class="history-printer">${escHtml(p.printer_name)}</span>
      <span class="status-badge ${p.outcome ? PRINT_OUTCOME_CLASS[p.outcome] : "printing"}">${escHtml(printOutcomeLabel(p.outcome))}</span>
      ${p.outcome_unconfirmed ? `<span class="status-badge paused" title="Reported by the printer, not confirmed by you">Unconfirmed</span>` : ""}
      <span class="history-stats">${escHtml(stats.join(" · "))}</span>
    </button>
    ${open ? historyDetail(p) : ""}
  </div>`;
}

function historyDetail(p) {
  const detail = _historyDetails[p.id];
  const at = ts => ts ? new Date(ts * 1000).toLocaleString() : "—";
  const facts = [
    ["Started", at(p.started_at)],
    ["Ended", p.ended_at ? at(p.ended_at) : "Still printing"],
    ["Duration", p.duration_s ? formatTime(p.duration_s) + (p.estimate_s ? ` (slicer estimate ${formatTime(p.estimate_s)})` : "") : "—"],
    ["Layers", p.total_layers ? `${p.layer ?? 0} of ${p.total_layers}` : "—"],
    ["Filament", p.filament_g != null
      ? `${p.filament_g} g · ${((p.filament_mm || 0) / 1000).toFixed(2)} m` + (p.estimate_g ? ` (slicer estimate ${Math.round(p.estimate_g)} g)` : "")
      : "—"],
    ["Material", [p.vendor, p.material, p.filament_name].filter(Boolean).join(" ") || "—"],
    ["Cost", p.cost != null ? formatCost(p.cost) : "—"],
    ["Queue job", p.job_id ? `#${p.job_id}${detail?.job ? ` · ${detail.job.name}` : ""}` : "Started outside the queue"],
  ];
  if (p.failure_reasons?.length) facts.push(["Failure reasons", p.failure_reasons.map(r => FAILURE_REASON_LABEL[r] || r).join(", ")]);
  if (p.detail) facts.push(["Details", p.detail]);
  if (p.source === "imported") facts.push(["Source", "Imported from the previous history log"]);

  const spools = (p.spools || []).map(s =>
    `<li><span class="history-swatch" style="background:#${_safeColor(s.color_hex)}"></span>` +
    `Spool #${s.spool_id} · ${escHtml([s.vendor, s.material, s.filament_name].filter(Boolean).join(" ") || "unknown filament")}` +
    ` · ${s.grams} g${s.cost != null ? ` · ${escHtml(formatCost(s.cost))}` : ""}</li>`).join("");

  const events = detail?.job_events?.length
    ? `<details class="history-events"><summary>Job timeline (${detail.job_events.length} events)</summary><ol>` +
      detail.job_events.map(e => `<li><time>${escHtml(new Date(e.at * 1000).toLocaleTimeString())}</time> ` +
        `${escHtml(e.event)}${e.detail ? ` — ${escHtml(e.detail)}` : ""}</li>`).join("") +
      "</ol></details>"
    : "";

  return `<div class="history-detail">
    <dl class="history-facts">${facts.map(([k, v]) => `<dt>${escHtml(k)}</dt><dd>${escHtml(v)}</dd>`).join("")}</dl>
    ${spools ? `<ul class="history-spools">${spools}</ul>` : ""}
    ${events}
    ${p.outcome ? historyOutcomeForm(p) : ""}
    <label class="history-notes">
      Notes
      <textarea data-history-notes="${p.id}" rows="3" maxlength="4000"
        placeholder="What worked, what failed, settings to reuse…">${escHtml(p.notes || "")}</textarea>
    </label>
    <div class="job-controls">
      <button class="btn btn-secondary btn-sm" data-history-action="save" data-print-id="${p.id}">Save notes</button>
      ${p.outcome ? `<button class="btn btn-primary btn-sm" data-history-action="repeat" data-print-id="${p.id}">Print again</button>` : ""}
    </div>
  </div>`;
}

function historyOutcomeForm(p) {
  const reasons = [...FAILURE_REASON_IDS, "cancelled_on_purpose"]
    .map(r => `<label${r === "cancelled_on_purpose" && p.outcome !== "cancelled" ? " hidden" : ""}><input type="checkbox" data-history-reason="${p.id}" value="${r}"${(p.failure_reasons || []).includes(r) ? " checked" : ""}> ${escHtml(FAILURE_REASON_LABEL[r])}</label>`).join(" ");
  const options = ["completed", "cancelled", "failed", "unknown"]
    .map(o => `<option value="${o}"${o === p.outcome ? " selected" : ""}>${escHtml(PRINT_OUTCOME_LABEL[o])}</option>`).join("");
  return `<fieldset class="history-outcome-form">
    <legend>${p.outcome_unconfirmed ? "Confirm or correct the outcome" : "Correct the outcome"}</legend>
    <select data-history-outcome="${p.id}" aria-label="Outcome">${options}</select>
    <div class="history-reasons"${p.outcome === "completed" || p.outcome === "unknown" ? " hidden" : ""}>${reasons}</div>
    <button class="btn btn-secondary btn-sm" data-history-action="outcome" data-print-id="${p.id}">${p.outcome_unconfirmed ? "Confirm outcome" : "Save outcome"}</button>
  </fieldset>`;
}

function onPrintRecorded(record) {
  const grams = record.filament_g ? ` · ${Math.round(record.filament_g)} g` : "";
  toast(`${record.printer_name}: ${record.file_name} — ${printOutcomeLabel(record.outcome).toLowerCase()}${grams}`,
    record.outcome !== "completed");
  if (!document.getElementById("panel-history").classList.contains("open")) return;
  if (document.activeElement?.matches?.("[data-history-notes]")) return; // don't clobber unsaved notes
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
          if (_historyOpenId === id) renderHistory();
        }
      } catch (_) { /* details stay basic */ }
    }
    return;
  }

  const btn = e.target.closest("[data-history-action]");
  if (!btn) return;
  const id = Number(btn.dataset.printId);
  const record = historyPrints.find(p => p.id === id);
  if (!record) return;
  btn.disabled = true;
  try {
    if (btn.dataset.historyAction === "save") {
      const notes = document.querySelector(`[data-history-notes="${id}"]`).value;
      const updated = await jobsApi("PATCH", `/api/prints/${id}`, { notes });
      record.notes = updated.notes;
      _historyDetails[id] = updated;
      toast("Notes saved");
    } else if (btn.dataset.historyAction === "outcome") {
      const outcome = document.querySelector(`[data-history-outcome="${id}"]`).value;
      const reasons = [...document.querySelectorAll(`[data-history-reason="${id}"]:checked`)].map(c => c.value);
      const updated = await jobsApi("POST", `/api/prints/${id}/outcome`, { outcome, failure_reasons: reasons });
      Object.assign(record, updated);
      _historyDetails[id] = updated;
      toast("Outcome saved");
      loadHistory();
    } else if (btn.dataset.historyAction === "repeat") {
      const job = await jobsApi("POST", `/api/prints/${id}/repeat`, {});
      loadJobs();
      toastAction(`Queued ${job.name} again`, "Open queue", () => { closeHistory(); openQueue(); });
    }
  } catch (err) {
    toast(err.message, true);
  }
  btn.disabled = false;
});

document.getElementById("history-list").addEventListener("change", (e) => {
  const select = e.target.closest("[data-history-outcome]");
  if (!select) return;
  const box = select.parentElement.querySelector(".history-reasons");
  box.hidden = !["failed", "cancelled"].includes(select.value);
  for (const c of box.querySelectorAll('[value="cancelled_on_purpose"]')) {
    c.parentElement.hidden = select.value !== "cancelled";
    if (select.value !== "cancelled") c.checked = false;
  }
});

document.getElementById("history-search").addEventListener("input", () => {
  clearTimeout(_historySearchTimer);
  _historySearchTimer = setTimeout(() => loadHistory(), 300);
});
for (const id of ["history-printer", "history-outcome", "history-range"]) {
  document.getElementById(id).addEventListener("change", () => loadHistory());
}
document.getElementById("btn-history-more").addEventListener("click", () => loadHistory(true));
document.getElementById("btn-history-export").addEventListener("click", () => {
  window.location.href = `/api/prints/export.csv?${_historyParams()}`;
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
  _loadCurrency().then(() => renderHistory());
  loadHistory();
}

function closeHistory() {
  window.closeSidePanel(document.getElementById("panel-history"));
  document.getElementById("btn-history").classList.remove("active");
}

document.getElementById("btn-history").addEventListener("click", openHistory);
document.getElementById("btn-history-close").addEventListener("click", closeHistory);
