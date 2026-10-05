/* Spooler – dashboard and shell routing: Needs you, up next on printer cards, active project rows, and the in-app
   Projects page. Loaded after app.js; uses its globals (printers, queueJobs, toast, escHtml, escAttr, …). */
"use strict";

let _dashboard = { needs_you: { printers: [], prints: [] }, projects: [] };
let _dashboardTimer = null;
let _dashboardBusy = false;

// ─── Printer card: what is up next ────────────────────────────────────────────
function printerUpNext(printer) {
  const queued = (typeof queueJobs !== "undefined" ? queueJobs : []).filter(j => j.state === "queued" && j.printer_id === printer.id);
  if (!queued.length) return `<div class="card-upnext card-upnext-empty">Up next: nothing queued</div>`;
  const more = queued.length > 1 ? ` <span class="card-upnext-more">+${queued.length - 1} more</span>` : "";
  return `<div class="card-upnext" title="${escAttr(queued[0].name)}">Up next: <strong>${escHtml(queued[0].name)}</strong>${more}</div>`;
}

// ─── Data ─────────────────────────────────────────────────────────────────────
async function loadDashboard() {
  if (_dashboardBusy) return;
  _dashboardBusy = true;
  try {
    const resp = await fetch("/api/dashboard");
    if (resp.ok) { _dashboard = await resp.json(); renderDashboard(); }
  } catch (_) { /* server may be restarting */ }
  _dashboardBusy = false;
}

function refreshDashboardSoon() {
  if (_dashboardTimer) return;
  _dashboardTimer = setTimeout(() => { _dashboardTimer = null; loadDashboard(); }, 800);
}

// ─── Rendering ────────────────────────────────────────────────────────────────
function renderDashboard() {
  renderNeedsYou();
  renderDashProjects();
}

function _outcomeWord(print) {
  return { completed: "finished", failed: "failed", cancelled: "was cancelled", unknown: "ended (outcome unknown)" }[print.outcome] || "ended";
}

function renderNeedsYou() {
  const box = document.getElementById("needs-you");
  const { printers: waiting, prints } = _dashboard.needs_you;
  box.hidden = !waiting.length && !prints.length;
  if (box.hidden) { box.innerHTML = ""; return; }
  const rows = waiting.map(item => {
    const pid = escAttr(item.printer_id);
    const print = item.print;
    if (item.kind === "start_job") {
      return `<div class="needs-row">
        <div class="needs-text"><strong>${escHtml(item.printer_name)}</strong> is ready
          <span class="needs-sub">Bed cleared. Next up: ${escHtml(item.next_job.file_name || "queued job")}</span></div>
        <div class="needs-actions">
          <button class="btn btn-primary" onclick="dashReviewStart(${item.next_job.id})">Review &amp; start</button>
        </div></div>`;
    }
    const ok = print.outcome === "completed";
    const next = item.next_job ? ` Next up: ${escHtml(item.next_job.file_name || "queued job")}.` : "";
    return `<div class="needs-row">
      <div class="needs-text"><strong>${escHtml(item.printer_name)}</strong>: ${escHtml(print.file_name || "print")} ${_outcomeWord(print)}
        <span class="needs-sub">${ok ? "Printed OK? Clear the bed to continue." : "Check the print, then clear the bed."}${next}</span></div>
      <div class="needs-actions">
        <button class="btn btn-primary" onclick="confirmBedCleared('${pid}')">${ok ? "Printed OK · Bed cleared" : "Bed cleared"}</button>
        ${ok ? "" : `<button class="btn btn-secondary" onclick="dashOpenUnconfirmed()">Set outcome</button>`}
        <button class="btn btn-secondary" onclick="openPrinterDashboard('${pid}')">Printer</button>
      </div></div>`;
  });
  for (const print of prints) {
    const ok = print.outcome === "completed";
    rows.push(`<div class="needs-row">
      <div class="needs-text"><strong>${escHtml(print.printer_name || "Printer")}</strong>: ${escHtml(print.file_name || "print")}
        <span class="needs-sub">The printer reported ${escHtml(print.outcome)}. Confirm the outcome.</span></div>
      <div class="needs-actions">
        ${ok ? `<button class="btn btn-primary" onclick="dashConfirmPrint(${print.id})">Confirm completed</button>` : ""}
        <button class="btn btn-secondary" onclick="dashOpenUnconfirmed()">${ok ? "Correct" : "Set outcome"}</button>
      </div></div>`);
  }
  box.innerHTML = `<h2>Needs you <span class="needs-count">${rows.length}</span></h2>${rows.join("")}`;
}

function renderDashProjects() {
  const box = document.getElementById("dash-projects");
  const rows = _dashboard.projects;
  box.hidden = !rows.length;
  if (!rows.length) { box.innerHTML = ""; return; }
  box.innerHTML = `<h2>Active projects</h2>` + rows.map(p => `
    <a class="dash-project" href="#projects=${p.id}">
      <span class="dash-project-name">${escHtml(p.name)}</span>
      <span class="dash-project-bar"><span style="width:${p.percent}%"></span></span>
      <span class="dash-project-count">${p.printed_ok} of ${p.total} printed</span>
    </a>`).join("");
}

// ─── Actions ──────────────────────────────────────────────────────────────────
function dashReviewStart(jobId) {
  const job = queueJobs.find(j => j.id === jobId);
  if (job) openReview(job); // preview.js: preflight checks, then Start
  else { toast("That job isn't waiting in the queue any more", true); loadDashboard(); }
}

async function dashConfirmPrint(printId) {
  try {
    await jobsApi("POST", `/api/prints/${printId}/outcome`, { outcome: "completed", failure_reasons: [] });
    toast("Outcome confirmed");
  } catch (err) {
    toast(err.message, true);
  }
  loadDashboard();
}

function dashOpenUnconfirmed() {
  const select = document.getElementById("history-outcome");
  if (select) select.value = "unconfirmed";
  openHistory(); // history.js
}

// ─── Shell routing: Printers dashboard, a printer page, or the in-app Projects page ──
function syncShellView() {
  const hash = location.hash;
  const projects = hash === "#projects" || hash.startsWith("#projects=");
  document.body.classList.toggle("view-projects", projects);
  document.body.classList.toggle("view-printer", hash.startsWith("#printer="));
  document.getElementById("projects-view").hidden = !projects;
  document.getElementById("btn-slicer").classList.toggle("active", projects);
  document.getElementById("btn-printers-nav").classList.toggle("active", !projects);
  if (projects) {
    const frame = document.getElementById("projects-frame");
    const params = new URLSearchParams(hash.slice(1)); // #projects=<id>[&confirm=1] (confirm: straight after an import)
    const id = Number(params.get("projects")) || 0;
    const printer = id ? params.get("printer") || "" : ""; // from a push: focus that printer's card on the project page
    const target = id ? `/slicer.html?project=${id}${printer ? `&printer=${encodeURIComponent(printer)}` : ""}${params.has("confirm") ? "&confirm=1" : ""}` : "/slicer.html";
    const route = `${id}|${printer}`;
    if (!frame.getAttribute("src") || (id && frame.dataset.route !== route)) {
      frame.dataset.route = route;
      frame.src = target;
    }
  } else {
    loadDashboard();
  }
}

// Links to a project from the queue panel and elsewhere stay in the shell.
document.addEventListener("click", event => {
  const link = event.target.closest('a[href^="/slicer.html"]');
  if (!link || link.closest("#projects-view") || event.ctrlKey || event.metaKey || event.shiftKey) return;
  const id = new URL(link.href).searchParams.get("project");
  event.preventDefault();
  location.hash = id ? `#projects=${id}` : "#projects";
});

window.addEventListener("hashchange", syncShellView);
if (typeof applyJobs === "function") {
  const _applyJobs = applyJobs;
  applyJobs = list => {
    _applyJobs(list);
    Object.values(printers).forEach(renderPrinter); // the Up next line on each card
    refreshDashboardSoon();
  };
}
setInterval(() => { if (!document.hidden && !document.body.classList.contains("view-projects")) loadDashboard(); }, 5000);
syncShellView();
loadDashboard();
