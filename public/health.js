/* Spooler – system health pill, connection banner and health panel.
   Loaded before app.js; uses app.js globals (printers, _wsConnected, toast, …) only at call time. */
"use strict";

const HEALTH_STALE_MS = 45000; // the server sends a snapshot every 15 s

let systemStatus    = null; // latest system_status snapshot
let _systemStatusAt = 0;    // Date.now() when it arrived

function applySystemStatus(status) {
  systemStatus = status;
  _systemStatusAt = Date.now();
  renderSystemHealth();
}

function fmtDuration(secs) {
  secs = Math.max(0, Math.round(secs || 0));
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

// Seconds elapsed since a server timestamp, measured against the snapshot's own
// clock so a browser on another device with a skewed clock still reads correctly.
function _ageOf(serverEpoch) {
  return systemStatus.generated_at - serverEpoch + (Date.now() - _systemStatusAt) / 1000;
}

function systemIssues() {
  if (!_wsConnected) return _wsAttempts ? [{ level: "down", text: "Spooler connection lost" }] : [];
  const s = systemStatus;
  if (!s) return [];
  const issues = [];
  if (s.spoolman?.ok === false) issues.push({ level: "down", text: "Spoolman unreachable" });
  if (Date.now() - _systemStatusAt > HEALTH_STALE_MS) issues.push({ level: "warn", text: "Health data out of date" });
  const pending = s.spoolman?.pending_deductions || 0;
  if (pending) issues.push({ level: "warn", text: `${pending} filament deduction${pending === 1 ? "" : "s"} queued` });
  const offline = Object.values(printers).filter(p => !p.connected);
  if (offline.length === 1) issues.push({ level: "warn", text: `${offline[0].name} offline` });
  else if (offline.length > 1) issues.push({ level: "warn", text: `${offline.length} printers offline` });
  const b = s.backups;
  if (b?.last_error) issues.push({ level: "warn", text: "Backup problem" });
  else if (b?.overdue && !b.running) issues.push({ level: "warn", text: b.latest ? "Backup overdue" : "No backup yet" });
  if (s.supervisor?.stale) issues.push({ level: "warn", text: "Supervisor not responding" });
  if ((s.spooler?.loop_lag_ms || 0) > 500) issues.push({ level: "warn", text: "Spooler responding slowly" });
  return issues;
}

function renderSystemHealth() {
  const issues = systemIssues();
  const pill  = document.getElementById("btn-system-health");
  const label = document.getElementById("health-pill-label");
  const level = issues.length ? issues[0].level
    : (_wsConnected && systemStatus) ? "ok" : "unknown";
  pill.dataset.state = level;
  label.textContent = issues.length
    ? issues[0].text + (issues.length > 1 ? ` +${issues.length - 1}` : "")
    : level === "ok" ? "All systems OK" : "Connecting…";
  pill.setAttribute("aria-label", `System health: ${label.textContent}`);
  pill.title = issues.length ? issues.map(i => i.text).join("\n") : "System health";

  const banner = document.getElementById("connection-banner");
  banner.hidden = _wsConnected || !_wsAttempts;
  if (!banner.hidden) {
    const secs = Math.ceil((_wsRetryAt - Date.now()) / 1000);
    banner.textContent = `Connection to Spooler lost — ${secs > 0 ? `retrying in ${secs}s` : "reconnecting…"} (attempt ${_wsAttempts})`;
  }

  if (document.getElementById("modal-system-health").classList.contains("open")) renderSystemHealthDetail();
}

// `next` is an action the operator can take; pass it only for warn/down rows (and
// unknown rows with a concrete reason). Healthy and informational rows get no Next line.
function healthRow(level, title, detail, next) {
  const dot = level ? `<span class="health-dot" data-state="${level}"></span>` : `<span class="health-dot-spacer"></span>`;
  const nextLine = next && level && level !== "ok"
    ? `<span class="health-row-next">Next: ${escHtml(next)}</span>` : "";
  return `<div class="health-row">${dot}<div class="health-row-main">` +
    `<span class="health-row-title">${escHtml(title)}</span>` +
    `<span class="health-row-detail">${escHtml(detail)}</span>` +
    `${nextLine}</div></div>`;
}

function renderSystemHealthDetail() {
  const body = document.getElementById("system-health-body");
  const freshness = document.getElementById("system-health-updated");
  const backupBtn = document.getElementById("btn-backup-now");
  const s = systemStatus;
  if (!_systemStatusAt) {
    freshness.textContent = "Health data has not arrived yet. Check the dashboard connection and wait for a report.";
  } else {
    const age = Math.max(0, Math.floor((Date.now() - _systemStatusAt) / 1000));
    freshness.textContent = `Last checked ${new Date(_systemStatusAt).toLocaleTimeString()} · ${fmtDuration(age)} ago` +
      (age > HEALTH_STALE_MS / 1000 ? " · Out of date; check the dashboard connection and wait for a fresh report." : " · Current");
  }
  const services = [healthRow(_wsConnected ? "ok" : "down", "Dashboard connection",
    _wsConnected ? "Connected" : `Reconnecting (attempt ${_wsAttempts})`,
    "Check that Spooler is running on its host computer. This page reconnects by itself once it is.")];
  if (!s) {
    body.innerHTML = `<div class="health-section"><h4>Services</h4>${services.join("")}</div>`;
    backupBtn.disabled = true;
    return;
  }

  const sp = s.spooler;
  services.push(healthRow(sp.loop_lag_ms > 500 ? "warn" : "ok", "Spooler",
    `Up ${fmtDuration(_ageOf(sp.started_at))} · event-loop lag ${Math.round(sp.loop_lag_ms)} ms · ` +
    `${sp.browser_clients} browser${sp.browser_clients === 1 ? "" : "s"}`,
    "Let any running slice or upload finish. If it stays slow, restart Spooler."));

  const sm = s.spoolman;
  let smDetail = sm.ok === null ? "Not checked yet"
    : sm.ok ? `Healthy${sm.version ? ` · v${sm.version}` : ""} · ${Math.round(sm.latency_ms)} ms`
    : `Unreachable for ${fmtDuration(_ageOf(sm.down_since))} · ${sm.error}`;
  if (sm.pending_deductions) smDetail += ` · ${sm.pending_deductions} deduction(s) waiting to be replayed`;
  services.push(healthRow(sm.ok === null ? "unknown" : sm.ok ? (sm.pending_deductions ? "warn" : "ok") : "down",
    "Spoolman", smDetail,
    sm.ok === null ? "Spooler has not finished its first Spoolman check since starting; reopen this panel in a few seconds."
    : !sm.ok ? "Start Spoolman (or run .\\start-local.ps1) and check SPOOLMAN_URL. Queued deductions replay once it answers."
    : sm.pending_deductions ? "Open Spoolman and confirm it responds; the queued deductions replay on their own." : ""));

  const sup = s.supervisor;
  if (sup) {
    const restarts = (sup.services || [])
      .map(x => `${x.name}: ${x.restarts} restart${x.restarts === 1 ? "" : "s"}`).join(", ");
    services.push(healthRow(sup.stale ? "warn" : "ok", "Supervisor", sup.stale
      ? `No update for ${fmtDuration(_ageOf(sup.updated_at))}`
      : `Running for ${fmtDuration(_ageOf(sup.started_at))} · ${restarts}`,
    "Check the supervisor window, or run .\\start-local.ps1 to start it again."));
  } else {
    services.push(healthRow("unknown", "Supervisor", "Not running — services won't restart automatically",
      "Run .\\start-local.ps1 from the Spooler folder to start the supervisor."));
  }

  const printerRows = (s.printers || []).map(link => {
    const live = printers[link.id];
    const connected = live ? live.connected : link.connected;
    const elapsed = (Date.now() - _systemStatusAt) / 1000;
    let detail;
    if (connected) {
      detail = link.connected_since ? `Connected for ${fmtDuration(_ageOf(link.connected_since))}` : "Connected";
      if (link.last_message_age_s != null) detail += ` · last message ${fmtDuration(link.last_message_age_s + elapsed)} ago`;
    } else {
      detail = link.disconnected_since ? `Offline for ${fmtDuration(_ageOf(link.disconnected_since))}` : "Offline";
      if (link.next_retry_in_s != null) {
        const wait = link.next_retry_in_s - elapsed;
        detail += wait > 0 ? ` · retry in ${fmtDuration(wait)}` : " · retrying";
      }
      if (link.reconnect_attempts) detail += ` (attempt ${link.reconnect_attempts})`;
      if (link.last_error) detail += ` · ${link.last_error}`;
    }
    return healthRow(connected ? "ok" : "warn", live?.name || link.name, detail,
      "Check the printer is powered on and on the same network. Spooler keeps retrying on its own.");
  });

  const b = s.backups;
  const backupRows = [];
  if (b.running) backupRows.push(healthRow("unknown", "Backup", "Backup in progress…"));
  backupRows.push(b.latest
    ? healthRow(b.last_error || b.overdue ? "warn" : "ok", "Latest backup",
        `${b.latest.name} · ${formatFileSize(b.latest.size)} · ${fmtDuration(_ageOf(b.latest.created_at))} ago`,
        "Press Back up now.")
    : healthRow("warn", "Latest backup", "No backup has been made yet", "Press Back up now."));
  if (b.last_error) backupRows.push(healthRow("warn", "Backup problem", b.last_error,
    "Fix the cause (free disk space, backup folder permissions), then press Back up now."));
  backupRows.push(healthRow(b.spoolman_db ? "ok" : "warn", "Coverage",
    (b.spoolman_db ? "Configuration + Spoolman database" : "Configuration only — set SPOOLMAN_DB_FILE to include Spoolman") +
    ` · every ${b.interval_hours} h · keeping ${b.keep} · ${b.count} stored`,
    "Set SPOOLMAN_DB_FILE in the .env file, restart Spooler, then press Back up now."));
  backupRows.push(healthRow(null, "Location", b.dir));
  backupRows.push(healthRow(null, "Recovery", "Keep a verified archive and use the Recovery instructions in the Spooler handoff documentation to restore it."));

  body.innerHTML =
    `<div class="health-section"><h4>Services</h4>${services.join("")}</div>` +
    `<div class="health-section"><h4>Printers</h4>${printerRows.join("") || '<p class="health-empty">No printers configured</p>'}</div>` +
    `<div class="health-section"><h4>Backups</h4>${backupRows.join("")}</div>`;
  if (!backupBtn.dataset.busy) backupBtn.disabled = !_wsConnected || b.running;
}

function openSystemHealth() {
  openModal(document.getElementById("modal-system-health"), { opener: document.activeElement });
  renderSystemHealthDetail();
}

function closeSystemHealth() {
  closeModal(document.getElementById("modal-system-health"));
}

document.getElementById("btn-system-health").addEventListener("click", openSystemHealth);
document.getElementById("btn-system-health-close").addEventListener("click", closeSystemHealth);
document.getElementById("modal-system-health").addEventListener("click", (e) => {
  if (e.target.id === "modal-system-health") closeSystemHealth();
});

document.getElementById("btn-backup-now").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  btn.dataset.busy = "1";
  btn.disabled = true;
  btn.textContent = "Backing up…";
  try {
    const r = await fetch("/api/backup", { method: "POST" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    const when = new Date((data.created_at || Date.now() / 1000) * 1000).toLocaleString();
    const size = typeof formatFileSize === "function" ? formatFileSize(data.size || 0) : `${data.size || 0} bytes`;
    const errors = data.errors || [];
    const feedback = document.getElementById("backup-feedback");
    feedback.hidden = false;
    feedback.dataset.state = errors.length ? "warn" : "ok";
    feedback.textContent = errors.length
      ? `Backup archive ${data.name} was created on ${when} (${size}) with problems: ${errors.join("; ")}. Check the coverage and problem rows above, correct the reported issue, then create and verify another backup. ${data.path ? `Archive location: ${data.path}.` : ""} Use the Recovery instructions in the Spooler handoff documentation to restore it.`
      : `Backup archive ${data.name} was verified on ${when} (${size}). Confirm its location and coverage above. For recovery, follow the Recovery instructions in the Spooler handoff documentation. ${data.path ? `Archive location: ${data.path}.` : ""}`;
    toast(errors.length ? `Backup saved with problems: ${errors.join("; ")}` : `Backup saved: ${data.name}`, errors.length > 0);
  } catch (err) {
    const feedback = document.getElementById("backup-feedback");
    feedback.hidden = false;
    feedback.dataset.state = "error";
    feedback.textContent = `Backup failed at ${new Date().toLocaleString()}: ${err.message}. No new verified archive was confirmed. Check available storage and backup-directory permissions, then retry. Existing backups remain listed above.`;
    toast(`Backup failed: ${err.message}`, true);
  } finally {
    delete btn.dataset.busy;
    btn.textContent = "Back up now";
    renderSystemHealthDetail();
  }
});

setInterval(() => { if (typeof _wsConnected !== "undefined") renderSystemHealth(); }, 5000);
