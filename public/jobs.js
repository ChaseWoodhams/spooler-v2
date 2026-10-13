/* Spooler – print queue panel and job editor.
   Loaded before app.js; uses app.js globals (printers, toast, escHtml, formatTime, …) only at call time. */
"use strict";

const JOB_READY_CODES = [0, 8, 9, 14]; // PrintInfo.Status values that let a new job start
const JOB_OPEN_STATES = ["queued", "starting", "printing"];
const JOB_STATE_LABEL = {
  queued: "Queued", starting: "Starting", printing: "Printing", completed: OUTCOME_LABEL.completed,
  cancelled: OUTCOME_LABEL.cancelled, failed: OUTCOME_LABEL.failed, unknown: OUTCOME_LABEL.unknown,
};
const JOB_STATE_CLASS = {
  queued: "idle", starting: "warmingup", printing: "printing", completed: "complete",
  cancelled: "cancelled", failed: "offline", unknown: "paused",
};

let queueJobs = [];
let _jobEditor = null;        // { mode: "edit", job }
let _queueRenderTimer = null;
let _assigningBatch = null;   // batch id whose printer chooser is open

// Links from the project workspace: /?queue=1 opens the queue, /?review=<job id> opens that job's review.
const _queueLink = (() => {
  const params = new URLSearchParams(location.search);
  return { open: params.has("queue") || params.has("review"), review: Number(params.get("review")) || null };
})();

const _jobAttr = s => escAttr(escHtml(s));

// ─── Data ─────────────────────────────────────────────────────────────────────
async function loadJobs() {
  try {
    const r = await fetch("/api/jobs");
    if (r.ok) applyJobs((await r.json()).jobs);
  } catch (_) { /* server may be restarting */ }
}

function applyJobs(list) {
  queueJobs = list || [];
  renderQueue();
  if (_queueLink.open) {
    const { review } = _queueLink;
    _queueLink.open = false;
    history.replaceState(null, "", location.pathname + location.hash);
    openQueue();
    const job = review && queueJobs.find(j => j.id === review);
    if (job?.state === "queued") openReview(job); // preview.js
    else if (review) toast("That job isn't waiting in the queue any more", true);
  }
}

async function jobsApi(method, path, body) {
  const r = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `Request failed (HTTP ${r.status})`);
  return data;
}

async function queuePrinterFile(printerId, file) {
  try {
    const job = await jobsApi("POST", "/api/jobs", {
      printer_id: printerId, printer_file: file.path,
      size: file.size, print_time: file.print_time, filament_mm: file.filament_mm,
    });
    toast(`Queued ${job.name} for ${jobPrinterName(printerId)}`);
    loadJobs();
  } catch (err) {
    toast(err.message, true);
  }
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
function jobPrinterName(id) {
  return id ? (printers[id]?.name || "Unknown printer") : "Unassigned";
}

function jobStartBlocker(job) {
  if (!job.printer_id) return "Choose a printer first";
  const p = printers[job.printer_id];
  if (!p) return "Printer not found";
  if (p.printer_type !== "cc1") return "The queue supports CC1 printers only";
  if (!p.connected) return `${p.name} is offline`;
  if (queueJobs.some(j => j.printer_id === p.id && (j.state === "starting" || j.state === "printing")))
    return `${p.name} is running another queued job`;
  const code = p.status?.PrintInfo?.Status;
  if (code === undefined || code === null) return `${p.name} hasn't reported its status yet`;
  if (!JOB_READY_CODES.includes(code) || (p.status?.CurrentStatus?.[0] ?? 0) !== 0) return `${p.name} is busy`;
  return null;
}

function jobPrinterOptions(selected) {
  const options = ['<option value="">Unassigned</option>'];
  for (const p of Object.values(printers).filter(p => p.printer_type === "cc1")) {
    options.push(`<option value="${_jobAttr(p.id)}"${p.id === selected ? " selected" : ""}>` +
      `${escHtml(p.name)}${p.connected ? "" : " (offline)"}</option>`);
  }
  if (selected && !printers[selected]) options.push(`<option value="${_jobAttr(selected)}" selected>Unknown printer</option>`);
  return options.join("");
}

function jobMetaText(job) {
  const bits = [];
  if (job.file_size) bits.push(formatFileSize(job.file_size));
  if (job.estimate_s) bits.push(formatTime(job.estimate_s));
  if (job.estimate_g) bits.push(`${Math.round(job.estimate_g)} g`);
  else if (job.estimate_mm) bits.push(`${(job.estimate_mm / 1000).toFixed(1)} m`);
  const opts = job.print_opts || {};
  bits.push(opts.smooth_plate ? "Smooth plate" : "Textured plate");
  if (opts.leveling) bits.push("Leveling");
  if (opts.timelapse) bits.push("Time-lapse");
  return bits.join(" · ");
}

function jobProgress(job) {
  if (job.state === "starting") {
    const pr = job.progress || {};
    if (pr.phase === "uploading" && pr.total) {
      const pct = pr.sent / pr.total * 100;
      return { text: `Uploading ${Math.floor(pct)}%`, pct };
    }
    const text = { sending: "Sending start command…", waiting: "Waiting for the printer to begin…" }[pr.phase];
    return { text: text || "Preparing…", pct: null };
  }
  const pi = printers[job.printer_id]?.status?.PrintInfo;
  if (!pi) return { text: "", pct: null };
  const bits = [];
  if (pi.Progress != null) bits.push(`${Math.round(pi.Progress)}%`);
  if (pi.TotalLayer) bits.push(`layer ${pi.CurrentLayer}/${pi.TotalLayer}`);
  if (pi.RemainTime) bits.push(`${formatTime(pi.RemainTime)} left`);
  return { text: bits.join(" · "), pct: pi.Progress ?? null };
}

// ─── Rendering ────────────────────────────────────────────────────────────────
function renderQueue() {
  const openCount = queueJobs.filter(j => JOB_OPEN_STATES.includes(j.state)).length;
  const badge = document.getElementById("queue-count");
  badge.hidden = !openCount;
  badge.textContent = openCount;

  const body = document.getElementById("queue-body");
  if (!document.getElementById("panel-queue").classList.contains("open")) return;
  const focused = document.activeElement;
  if (focused?.tagName === "SELECT" && body.contains(focused)) { refreshQueueSoon(); return; }

  if (_assigningBatch && body.querySelector(".queue-assign")?.contains(focused)) { refreshQueueSoon(); return; }

  const active = queueJobs.filter(j => j.state === "starting" || j.state === "printing");
  const queued = queueJobs.filter(j => j.state === "queued");
  const done   = queueJobs.filter(j => !JOB_OPEN_STATES.includes(j.state));
  body.innerHTML =
    queueSection("Active", active, jobActiveCard, "Nothing from the queue is printing.") +
    `<section class="queue-section"><h4>${escHtml(`Queued (${queued.length})`)}</h4>${queuedWithBatches(queued)
      || `<p class="queue-empty">${escHtml("The queue is empty. Use New project above to add a model.")}</p>`}</section>` +
    (done.length ? queueSection("Recently finished", done, jobDoneCard) : "");
}

// Plates sent together from a project arrive as a batch: consecutive queued jobs with the same batch_id are grouped
// under a header that can spread them over printers.
function queuedWithBatches(queued) {
  const blocks = [];
  queued.forEach((job, index) => {
    const last = blocks[blocks.length - 1];
    if (job.batch_id && last && last.batch === job.batch_id) last.items.push([job, index]);
    else blocks.push({ batch: job.batch_id || null, items: [[job, index]] });
  });
  return blocks.map(block => {
    const cards = block.items.map(([job, index]) => jobQueuedCard(job, index, queued)).join("");
    if (!block.batch || block.items.length < 2) return cards;
    const first = block.items[0][0];
    const title = (first.notes || first.name).split(" — ")[0];
    const choices = Object.values(printers).filter(p => p.printer_type === "cc1");
    const chooser = _assigningBatch !== block.batch ? "" : `<div class="queue-assign">
        ${choices.map(p => `<label class="queue-assign-choice"><input type="checkbox" data-assign-printer="${_jobAttr(p.id)}"${p.connected ? " checked" : ""}>
          ${escHtml(p.name)}${p.connected ? "" : " (offline)"}</label>`).join("") || '<p class="queue-empty">No CC1 printers yet.</p>'}
        <div class="job-controls">
          <button class="btn btn-primary btn-sm" data-batch-action="apply" data-batch="${_jobAttr(block.batch)}"${choices.length ? "" : " disabled"}>
            Spread ${block.items.length} plates over these printers</button>
          <button class="btn btn-secondary btn-sm" data-batch-action="cancel">Cancel</button>
        </div>
      </div>`;
    return `<div class="queue-batch">
      <div class="queue-batch-head">
        <span class="queue-batch-title" title="${_jobAttr(title)}">${escHtml(title)} · ${block.items.length} plates</span>
        <button class="btn btn-secondary btn-sm" data-batch-action="assign" data-batch="${_jobAttr(block.batch)}">Assign printers</button>
        ${first.project_id && !first.project_quick ? `<a class="btn btn-secondary btn-sm" href="/slicer.html?project=${first.project_id}&step=print${block.items.length === 1 && first.plate_id != null ? `&plate=${first.plate_id}` : ""}">Project</a>` : ""}
      </div>
      ${chooser}${cards}
    </div>`;
  }).join("");
}

function refreshQueueSoon() {
  if (_queueRenderTimer) return;
  _queueRenderTimer = setTimeout(() => { _queueRenderTimer = null; renderQueue(); }, 1000);
}

function queueSection(title, list, render, emptyText) {
  const items = list.map((job, i) => render(job, i, list)).join("");
  const empty = emptyText ? `<p class="queue-empty">${escHtml(emptyText)}</p>` : "";
  return `<section class="queue-section"><h4>${escHtml(title)}</h4>${items || empty}</section>`;
}

function jobTitle(job) {
  return `<div class="job-title-row">
    <span class="job-name" title="${_jobAttr(job.file_name)}">${escHtml(job.name)}</span>
    ${OUTCOME_KIND[job.state] ? outcomeBadge(job.state) : `<span class="status-badge ${JOB_STATE_CLASS[job.state]}">${JOB_STATE_LABEL[job.state]}</span>`}
  </div>`;
}

function jobActiveCard(job) {
  const progress = jobProgress(job);
  const pct = progress.pct == null ? null : Math.max(0, Math.min(100, progress.pct));
  const meta = [jobPrinterName(job.printer_id), progress.text].filter(Boolean).join(" · ");
  return `<div class="job-card job-card-active"><div class="job-main">
    ${jobTitle(job)}
    <div class="job-meta">${escHtml(meta)}</div>
    ${pct == null ? "" : `<div class="job-progress"><div class="job-progress-bar" style="width:${pct}%"></div></div>`}
    <div class="job-controls">
      <button class="btn btn-secondary btn-sm" data-job-action="finish" data-job-id="${job.id}"
        title="Release the printer if it went offline or the start hung">Mark failed</button>
    </div>
  </div></div>`;
}

function jobQueuedCard(job, index, list) {
  const blocker = jobStartBlocker(job);
  const printerControl = job.source === "printer"
    ? `<span class="job-fixed-printer" title="This file is stored on the printer">${escHtml(jobPrinterName(job.printer_id))}</span>`
    : `<select class="job-printer-select" data-job-field="printer" data-job-id="${job.id}">${jobPrinterOptions(job.printer_id)}</select>`;
  return `<div class="job-card">
    <div class="job-order">
      <button class="job-move" data-job-action="up" data-job-id="${job.id}" title="Move up"${index === 0 ? " disabled" : ""}>▲</button>
      <span class="job-position">${index + 1}</span>
      <button class="job-move" data-job-action="down" data-job-id="${job.id}" title="Move down"${index === list.length - 1 ? " disabled" : ""}>▼</button>
    </div>
    <div class="job-main">
      ${jobTitle(job)}
      <div class="job-meta">${escHtml(jobMetaText(job))}</div>
      ${job.notes ? `<div class="job-notes">${escHtml(job.notes)}</div>` : ""}
      <div class="job-controls">
        ${printerControl}
        <button class="btn btn-primary btn-sm" data-job-action="start" data-job-id="${job.id}"
          title="Preview the toolpath and run the preflight checks">Review &amp; start</button>
        <button class="btn btn-secondary btn-sm" data-job-action="edit" data-job-id="${job.id}">Edit</button>
        <button class="btn btn-danger btn-sm" data-job-action="delete" data-job-id="${job.id}">Delete</button>
      </div>
      ${blocker ? `<div class="job-blocker">${escHtml(blocker)}</div>` : ""}
    </div>
  </div>`;
}

function jobDoneCard(job) {
  const meta = [jobPrinterName(job.printer_id)];
  if (job.finished_at) meta.push(`${fmtDuration(Date.now() / 1000 - job.finished_at)} ago`);
  if (job.started_at && job.finished_at) meta.push(`ran ${formatTime(job.finished_at - job.started_at)}`);
  return `<div class="job-card"><div class="job-main">
    ${jobTitle(job)}
    <div class="job-meta">${escHtml(meta.join(" · "))}</div>
    ${job.error ? `<div class="job-error">${escHtml(job.error)}</div>` : ""}
    <div class="job-controls">
      <button class="btn btn-secondary btn-sm" data-job-action="requeue" data-job-id="${job.id}">Queue again</button>
      <button class="btn btn-danger btn-sm" data-job-action="delete" data-job-id="${job.id}">Delete</button>
    </div>
  </div></div>`;
}

// ─── Actions ──────────────────────────────────────────────────────────────────
async function moveJob(id, delta) {
  const order = queueJobs.filter(j => j.state === "queued").map(j => j.id);
  const from = order.indexOf(id);
  const to = from + delta;
  if (from < 0 || to < 0 || to >= order.length) return;
  [order[from], order[to]] = [order[to], order[from]];
  await jobsApi("POST", "/api/jobs/reorder", { order });
}

async function batchAction(btn) {
  const action = btn.dataset.batchAction;
  if (action === "assign" || action === "cancel") {
    _assigningBatch = action === "assign" ? btn.dataset.batch : null;
    renderQueue();
    return;
  }
  const printerIds = [...document.querySelectorAll("#queue-body [data-assign-printer]")].filter(x => x.checked).map(x => x.dataset.assignPrinter);
  if (!printerIds.length) { toast("Choose at least one printer", true); return; }
  const jobIds = queueJobs.filter(j => j.state === "queued" && j.batch_id === btn.dataset.batch).map(j => j.id);
  btn.disabled = true;
  try {
    await jobsApi("POST", "/api/jobs/assign", { job_ids: jobIds, printer_ids: printerIds });
    _assigningBatch = null;
    toast(`Spread ${jobIds.length} plates over ${printerIds.length} printer${printerIds.length === 1 ? "" : "s"}`);
  } catch (err) {
    toast(err.message, true);
    btn.disabled = false;
  }
  loadJobs();
}

document.getElementById("queue-body").addEventListener("click", async (e) => {
  const batchBtn = e.target.closest("[data-batch-action]");
  if (batchBtn) {
    if (!batchBtn.disabled) await batchAction(batchBtn);
    return;
  }
  const btn = e.target.closest("[data-job-action]");
  if (!btn || btn.disabled) return;
  const job = queueJobs.find(j => j.id === Number(btn.dataset.jobId));
  if (!job) return;
  const printer = jobPrinterName(job.printer_id);
  try {
    switch (btn.dataset.jobAction) {
      case "up":
      case "down":
        await moveJob(job.id, btn.dataset.jobAction === "up" ? -1 : 1);
        break;
      case "start":
        openReview(job); // preview.js: preflight checks and the toolpath preview, then start from there
        return;
      case "edit":
        openJobEditor({ mode: "edit", job });
        return;
      case "delete":
        if (!await confirmDialog({
          title: `Delete ${job.name}?`,
          body: job.state === "queued" ? "It leaves the queue and will not print. This cannot be undone." : "The Job is removed from the list. This cannot be undone.",
          confirmLabel: "Delete Job",
          danger: true,
        })) return;
        await jobsApi("DELETE", `/api/jobs/${job.id}`);
        break;
      case "requeue":
        await jobsApi("POST", `/api/jobs/${job.id}/requeue`);
        toast(`${job.name} is back at the front of the queue`);
        break;
      case "finish":
        if (!await confirmDialog({
          title: `Mark ${job.name} as failed?`,
          body: `It is recorded as failed and ${printer} is released. Use this only when the printer is no longer printing it.`,
          confirmLabel: "Mark failed",
          danger: true,
        })) return;
        await jobsApi("POST", `/api/jobs/${job.id}/finish`, { outcome: "failed" });
        break;
    }
  } catch (err) {
    toast(err.message, true);
  }
  loadJobs();
});

document.getElementById("queue-body").addEventListener("change", async (e) => {
  const select = e.target.closest('[data-job-field="printer"]');
  if (!select) return;
  select.blur();
  try {
    await jobsApi("PATCH", `/api/jobs/${select.dataset.jobId}`, { printer_id: select.value || null });
  } catch (err) {
    toast(err.message, true);
  }
  loadJobs();
});

// ─── Job editor modal ─────────────────────────────────────────────────────────
function setJobPlate(smooth) {
  document.getElementById("job-plate-textured").className = `btn print-plate-btn ${smooth ? "btn-secondary" : "btn-primary"}`;
  document.getElementById("job-plate-smooth").className = `btn print-plate-btn ${smooth ? "btn-primary" : "btn-secondary"}`;
  document.getElementById("job-plate-smooth").dataset.selected = smooth ? "1" : "";
  document.getElementById("job-plate-textured").setAttribute("aria-pressed", String(!smooth));
  document.getElementById("job-plate-smooth").setAttribute("aria-pressed", String(smooth));
}

function openJobEditor(editor) {
  _jobEditor = editor;
  const job = editor.job;
  const opts = job.print_opts || {};
  document.getElementById("job-modal-title").textContent = "Edit queued job";
  document.getElementById("job-modal-file").textContent = job.file_name;
  document.getElementById("job-input-name").value = job.name;
  const select = document.getElementById("job-input-printer");
  if (job.source === "printer") {
    select.innerHTML = `<option value="${_jobAttr(job.printer_id)}">${escHtml(jobPrinterName(job.printer_id))}</option>`;
    select.disabled = true;
  } else {
    select.innerHTML = jobPrinterOptions(job.printer_id);
    select.disabled = false;
  }
  setJobPlate(!!opts.smooth_plate);
  document.getElementById("job-opt-timelapse").checked = !!opts.timelapse;
  document.getElementById("job-opt-leveling").checked = !!opts.leveling;
  document.getElementById("job-input-notes").value = job.notes || "";
  const save = document.getElementById("btn-job-save");
  save.disabled = false;
  openModal(document.getElementById("modal-job"), { opener: document.activeElement, onClose: () => { _jobEditor = null; }, focus: document.getElementById("job-input-name") });
}

function closeJobEditor() {
  closeModal(document.getElementById("modal-job"));
}

async function saveJobEditor() {
  const editor = _jobEditor;
  if (!editor) return;
  const save = document.getElementById("btn-job-save");
  const printerId = document.getElementById("job-input-printer").value || null;
  const changes = {
    name: document.getElementById("job-input-name").value.trim(),
    notes: document.getElementById("job-input-notes").value,
    print_opts: {
      smooth_plate: document.getElementById("job-plate-smooth").dataset.selected === "1",
      timelapse: document.getElementById("job-opt-timelapse").checked,
      leveling: document.getElementById("job-opt-leveling").checked,
    },
  };
  save.disabled = true;
  try {
    if (editor.job.source !== "printer") changes.printer_id = printerId;
    await jobsApi("PATCH", `/api/jobs/${editor.job.id}`, changes);
    closeJobEditor();
  } catch (err) {
    toast(err.message, true);
    save.disabled = false;
  }
  loadJobs();
}

document.getElementById("job-plate-textured").addEventListener("click", () => setJobPlate(false));
document.getElementById("job-plate-smooth").addEventListener("click", () => setJobPlate(true));
document.getElementById("btn-job-cancel").addEventListener("click", closeJobEditor);
document.getElementById("btn-job-save").addEventListener("click", saveJobEditor);
document.getElementById("modal-job").addEventListener("click", (e) => {
  if (e.target.id === "modal-job") closeJobEditor();
});

// ─── Panel ────────────────────────────────────────────────────────────────────
function openQueue() {
  window.openSidePanel(document.getElementById("panel-queue"), document.activeElement);
  document.getElementById("btn-queue").classList.add("active");
  renderQueue();
  loadJobs();
}

function closeQueue() {
  window.closeSidePanel(document.getElementById("panel-queue"));
  document.getElementById("btn-queue").classList.remove("active");
}

document.getElementById("btn-queue").addEventListener("click", openQueue);
document.getElementById("btn-queue-close").addEventListener("click", () => {
  closeQueue();
});
document.getElementById("panel-backdrop").addEventListener("click", closeQueue);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeQueue();   // an open job editor / review handles Escape itself (modal.js)
});
