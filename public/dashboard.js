/* Spooler – dashboard and shell routing: Needs you, up next on printer cards, active project rows, and the in-app
   Projects page. Loaded after app.js; uses its globals (printers, queueJobs, toast, escHtml, escAttr, …). */
"use strict";

let _dashboard = { needs_you: { printers: [], prints: [] }, projects: [] };
let _dashboardTimer = null;
let _dashboardBusy = false;
let _dashLoad = "loading";   // loading → ready | error. A failed poll after a success keeps the last good data on screen.
// The Queue step is Print now: a link to either lands on Print.
const isPrintStep = step => step === "print" || step === "queue";

// ─── Printer card: what is up next ────────────────────────────────────────────
function printerUpNext(printer) {
  const queued = (typeof queueJobs !== "undefined" ? queueJobs : []).filter(j => j.state === "queued" && j.printer_id === printer.id);
  if (!queued.length) return `<div class="card-upnext card-upnext-empty">Up next: nothing queued</div>`;
  const more = queued.length > 1 ? ` <span class="card-upnext-more">+${queued.length - 1} more</span>` : "";
  return `<div class="card-upnext" title="${escAttr(queued[0].name)}">Up next: <strong>${escHtml(queued[0].name)}</strong>${more}</div>`;
}

const _bedClearedSignature = () => JSON.stringify(_dashboard.needs_you.printers.map(i => [i.printer_id, i.kind, i.next_job && i.next_job.id, i.auto_start]));

// ─── Data ─────────────────────────────────────────────────────────────────────
async function loadDashboard() {
  if (_dashboardBusy) return;
  _dashboardBusy = true;
  try {
    const resp = await fetchWithTimeout("/api/dashboard", {}, { timeoutMs: 8000, retries: 1 });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const before = _bedClearedSignature();
    const data = await resp.json();
    _dashboard = { needs_you: { printers: [], prints: [], ...data.needs_you }, projects: data.projects || [] };
    _dashLoad = "ready";
    renderDashboard();
    if (_bedClearedSignature() !== before) Object.values(printers).forEach(p => renderPrinter(p)); // cards show Bed cleared only when it is waiting
  } catch (_) {   // server may be restarting or slow
    if (_dashLoad !== "ready" && _dashLoad !== "error") { _dashLoad = "error"; renderDashboard(); }
  }
  _dashboardBusy = false;
}

function retryDashboard() {
  _dashLoad = "loading";
  renderDashboard();
  loadDashboard();
}

// While the first answer is pending or has failed, a section shows the shared loading / error block instead of staying blank.
function dashStateBlock(box, loadingCopy, errorCopy) {
  box.hidden = false;
  if (box.firstElementChild?.dataset.state === _dashLoad) return;
  stateBlock(box, _dashLoad, _dashLoad === "error" ? errorCopy : loadingCopy, retryDashboard);
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
  if (_dashLoad !== "ready") return dashStateBlock(box, "Checking what needs you…", "Couldn't check what needs you. Check that Spooler is running, then retry.");
  const { printers: waiting, prints } = _dashboard.needs_you;
  box.hidden = false;
  const rows = waiting.map(item => {
    const pid = escAttr(item.printer_id);
    const print = item.print;
    if (item.kind === "start_job") {
      return `<div class="needs-row">
        <div class="needs-text"><strong>${escHtml(item.printer_name)}</strong> is ready
          <span class="needs-sub">Bed cleared. Next up: ${escHtml(item.next_job.file_name || "queued job")}</span></div>
        <div class="needs-actions">
          <button class="btn btn-primary" data-focus-key="review-${item.next_job.id}" onclick="dashReviewStart(${item.next_job.id})">Review &amp; start</button>
        </div></div>`;
    }
    const ok = print.outcome === "completed";
    const next = item.next_job ? ` Next up: ${escHtml(item.next_job.file_name || "queued job")}.` : "";
    return `<div class="needs-row">
      <div class="needs-text"><strong>${escHtml(item.printer_name)}</strong>: ${escHtml(print.file_name || "print")} ${_outcomeWord(print)}
        <span class="needs-sub">${ok ? "Check it printed fine, then clear the bed to continue." : "Check the print, then clear the bed."}${next}</span></div>
      <div class="needs-actions">
        <button class="btn btn-primary" data-focus-key="bed-${pid}" onclick="confirmBedCleared('${pid}')">${ok ? "Printed OK · Bed cleared" : "Bed cleared"}</button>
        ${ok ? "" : `<button class="btn btn-secondary" data-focus-key="outcome-${pid}" onclick="dashOpenUnconfirmed()">Set outcome</button>`}
        <button class="btn btn-secondary" data-focus-key="printer-${pid}" onclick="openPrinterDashboard('${pid}')">Printer</button>
      </div></div>`;
  });
  for (const print of prints) {
    const ok = print.outcome === "completed";
    rows.push(`<div class="needs-row">
      <div class="needs-text"><strong>${escHtml(print.printer_name || "Printer")}</strong>: ${escHtml(print.file_name || "print")}
        <span class="needs-sub">The printer reported ${escHtml(print.outcome)}. Confirm the outcome.</span></div>
      <div class="needs-actions">
        ${ok ? `<button class="btn btn-primary" data-focus-key="confirm-${print.id}" onclick="dashConfirmPrint(${print.id})">Confirm completed</button>` : ""}
        <button class="btn btn-secondary" data-focus-key="correct-${print.id}" onclick="dashOpenUnconfirmed()">${ok ? "Correct" : "Set outcome"}</button>
      </div></div>`);
  }
  // One filled primary in the strip (the first, most urgent row); later rows keep their action as a secondary button
  for (let i = 1; i < rows.length; i++) rows[i] = rows[i].replace('btn btn-primary', 'btn btn-secondary');
  const html = rows.length
    ? `<h2>Needs you <span class="needs-count">${rows.length}</span></h2>${rows.join("")}`
    : `<p class="needs-empty"><strong>Needs you</strong> <span>Nothing needs you right now.</span></p>`;
  box.classList.toggle("needs-you-empty", !rows.length);
  setNeedsStatus(rows.length ? `${rows.length} ${rows.length === 1 ? "item needs" : "items need"} you` : "Nothing needs you");
  if (box.dataset.sig === html) return;   // the 5 s poll found the same answer: leave the rows (and the focus in them) alone
  const focusKey = box.contains(document.activeElement) ? document.activeElement.dataset.focusKey : "";
  box.dataset.sig = html;
  box.innerHTML = html;
  if (focusKey) box.querySelector(`[data-focus-key="${CSS.escape(focusKey)}"]`)?.focus();
}

// The count is announced from a status region that stays in the DOM, so it is heard when rows appear or go.
function setNeedsStatus(text) {
  const status = document.getElementById("needs-you-status");
  if (status && status.textContent !== text) status.textContent = text;
}

function renderDashProjects() {
  const box = document.getElementById("dash-projects");
  if (_dashLoad !== "ready") return dashStateBlock(box, "Loading active projects…", "Couldn't load active projects. Check that Spooler is running, then retry.");
  const rows = _dashboard.projects;
  box.hidden = !rows.length;
  if (!rows.length) { box.innerHTML = ""; return; }
  box.innerHTML = `<h2>Active projects</h2>` + rows.map(p => `
    <a class="dash-project" href="#projects=${p.id}">
      <span class="dash-project-name">${escHtml(p.name)}</span>
      <span class="dash-project-bar"><span style="width:${p.percent}%"></span></span>
      <span class="dash-project-count">${p.printed_ok} of ${p.total} completed</span>
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
  const wasShown = document.body.classList.contains("view-projects");
  document.body.classList.toggle("view-projects", projects);
  document.body.classList.toggle("view-printer", hash.startsWith("#printer="));
  document.getElementById("projects-view").hidden = !projects;
  document.getElementById("btn-slicer").classList.toggle("active", projects);
  document.getElementById("btn-printers-nav").classList.toggle("active", !projects);
  if (projects) {
    const frame = document.getElementById("projects-frame");
    const params = new URLSearchParams(hash.slice(1)); // #projects=<id>[&printer=<id>][&plate=<id>][&step=print]
    const id = Number(params.get("projects")) || 0;
    const fresh = params.get("projects") === "new"; // header and Queue "New project": the Projects page opens on its New project form
    const printer = id ? params.get("printer") || "" : ""; // from a push: focus that printer's card on the project page
    const plate = id ? Number(params.get("plate")) || 0 : 0; // a link to one Plate: Print opens with it selected
    const toPrint = id && isPrintStep(params.get("step")); // a link to the Project's printing stage
    const target = id ? `/slicer.html?project=${id}${printer ? `&printer=${encodeURIComponent(printer)}` : ""}${plate ? `&plate=${plate}` : ""}${toPrint ? "&step=print" : ""}` : fresh ? "/slicer.html?new=1" : "/slicer.html";
    const route = fresh ? "new" : `${id}|${printer}${toPrint ? "|print" : ""}${plate ? `|${plate}` : ""}`;
    if (!frame.getAttribute("src") || ((id || fresh) && frame.dataset.route !== route)) {
      frame.dataset.route = route;
      frame.src = target;
    } else if (!wasShown || (hash === "#projects" && frame.dataset.route && frame.dataset.route !== "0|")) {
      // The same project kept in the frame since Projects was last shown: bring it up to date (a Quick print, another tab, a slice finishing).
      // Clicking Projects while a project or the New project form is open lands here too: back to the list of projects.
      try {
        const page = frame.contentWindow, call = hash === "#projects" ? page?.spoolerHome : page?.spoolerRefresh;
        if (call) call.call(page);
        else if (hash === "#projects") { frame.dataset.route = "0|"; frame.src = "/slicer.html"; }   // the page never started (a failed load): load it again
      } catch (_) {
        if (hash === "#projects") { frame.dataset.route = "0|"; frame.src = "/slicer.html"; }   // an error page sits in the frame
      }
    }
  } else {
    loadDashboard();
  }
}

// Links to a project from the queue panel and elsewhere stay in the shell.
document.addEventListener("click", event => {
  const link = event.target.closest('a[href^="/slicer.html"]');
  if (!link || link.closest("#projects-view") || event.ctrlKey || event.metaKey || event.shiftKey) return;
  const query = new URL(link.href).searchParams;
  const id = query.get("project");
  event.preventDefault();
  location.hash = id ? `#projects=${id}${isPrintStep(query.get("step")) ? "&step=print" : ""}${Number(query.get("plate")) ? `&plate=${Number(query.get("plate"))}` : ""}` : "#projects";
});

window.addEventListener("hashchange", syncShellView);
// Clicking Projects when the hash already says #projects fires no hashchange, so apply the view here too.
document.getElementById("btn-slicer").addEventListener("click", () => {
  if (location.hash === "#projects") {
    syncShellView();
    try { const frame = document.getElementById("projects-frame"); frame.contentWindow?.spoolerHome?.(); } catch (_) { /* still loading */ }
  }
});
if (typeof applyJobs === "function") {
  const _applyJobs = applyJobs;
  applyJobs = list => {
    _applyJobs(list);
    Object.values(printers).forEach(renderPrinter); // the Up next line on each card
    refreshDashboardSoon();
  };
}
setInterval(() => { if (!document.hidden && !document.body.classList.contains("view-projects")) loadDashboard(); }, 5000);
renderDashboard();   // the loading blocks, until the first answer
syncShellView();
loadDashboard();

// ─── Side nav: exactly one item is aria-current="page" ──────────────────────
// Panels opened from the nav (History, Spools) win over the page behind them; otherwise Projects or Printers.
function syncNavCurrent() {
  const nav = document.querySelector("nav.sidenav");
  if (!nav) return;
  const items = [...nav.querySelectorAll(".sidenav-item")];
  const order = ["btn-history", "btn-spools", "btn-slicer", "btn-printers-nav"];
  const current = order.map(id => document.getElementById(id)).find(el => el?.classList.contains("active"));
  for (const item of items) {
    if (item === current) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
}
new MutationObserver(syncNavCurrent).observe(document.querySelector("nav.sidenav"), { subtree: true, attributes: true, attributeFilter: ["class"] });
syncNavCurrent();

// ─── Phone bottom bar: More looks active while its menu or Settings (reached through it) is open ─────
function syncMoreActive() {
  const more = document.getElementById("btn-mobile-more");
  if (!more) return;
  const open = more.getAttribute("aria-expanded") === "true" || !!document.querySelector("#modal-app-settings.open");
  more.classList.toggle("active", open);
}
const _moreBtn = document.getElementById("btn-mobile-more"), _settingsModalEl = document.getElementById("modal-app-settings");
if (_moreBtn) new MutationObserver(syncMoreActive).observe(_moreBtn, { attributes: true, attributeFilter: ["aria-expanded"] });
if (_settingsModalEl) new MutationObserver(syncMoreActive).observe(_settingsModalEl, { attributes: true, attributeFilter: ["class"] });
syncMoreActive();
