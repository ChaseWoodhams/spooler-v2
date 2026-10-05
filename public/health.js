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
  pill.title = issues.length ? issues.map(i => i.text).join("\n") : "System health";

  const banner = document.getElementById("connection-banner");
  banner.hidden = _wsConnected || !_wsAttempts;
  if (!banner.hidden) {
    const secs = Math.ceil((_wsRetryAt - Date.now()) / 1000);
    banner.textContent = `Connection to Spooler lost — ${secs > 0 ? `retrying in ${secs}s` : "reconnecting…"} (attempt ${_wsAttempts})`;
  }

  if (document.getElementById("modal-system-health").classList.contains("open")) renderSystemHealthDetail();
}

function healthRow(level, title, detail) {
  const dot = level ? `<span class="health-dot" data-state="${level}"></span>` : `<span class="health-dot-spacer"></span>`;
  return `<div class="health-row">${dot}<div class="health-row-main">` +
    `<span class="health-row-title">${escHtml(title)}</span>` +
    `<span class="health-row-detail">${escHtml(detail)}</span></div></div>`;
}

function renderSystemHealthDetail() {
  const body = document.getElementById("system-health-body");
  const backupBtn = document.getElementById("btn-backup-now");
  const s = systemStatus;
  const services = [healthRow(_wsConnected ? "ok" : "down", "Dashboard connection",
    _wsConnected ? "Connected" : `Reconnecting (attempt ${_wsAttempts})`)];
  if (!s) {
    body.innerHTML = `<div class="health-section"><h4>Services</h4>${services.join("")}</div>`;
    backupBtn.disabled = true;
    return;
  }

  const sp = s.spooler;
  services.push(healthRow(sp.loop_lag_ms > 500 ? "warn" : "ok", "Spooler",
    `Up ${fmtDuration(_ageOf(sp.started_at))} · event-loop lag ${Math.round(sp.loop_lag_ms)} ms · ` +
    `${sp.browser_clients} browser${sp.browser_clients === 1 ? "" : "s"}`));

  const sm = s.spoolman;
  let smDetail = sm.ok === null ? "Not checked yet"
    : sm.ok ? `Healthy${sm.version ? ` · v${sm.version}` : ""} · ${Math.round(sm.latency_ms)} ms`
    : `Unreachable for ${fmtDuration(_ageOf(sm.down_since))} · ${sm.error}`;
  if (sm.pending_deductions) smDetail += ` · ${sm.pending_deductions} deduction(s) waiting to be replayed`;
  services.push(healthRow(sm.ok === null ? "unknown" : sm.ok ? (sm.pending_deductions ? "warn" : "ok") : "down",
    "Spoolman", smDetail));

  const sup = s.supervisor;
  if (sup) {
    const restarts = (sup.services || [])
      .map(x => `${x.name}: ${x.restarts} restart${x.restarts === 1 ? "" : "s"}`).join(", ");
    services.push(healthRow(sup.stale ? "warn" : "ok", "Supervisor", sup.stale
      ? `No update for ${fmtDuration(_ageOf(sup.updated_at))}`
      : `Running for ${fmtDuration(_ageOf(sup.started_at))} · ${restarts}`));
  } else {
    services.push(healthRow("unknown", "Supervisor", "Not running — services won't restart automatically"));
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
    return healthRow(connected ? "ok" : "warn", live?.name || link.name, detail);
  });

  const b = s.backups;
  const backupRows = [];
  if (b.running) backupRows.push(healthRow("unknown", "Backup", "Backup in progress…"));
  backupRows.push(b.latest
    ? healthRow(b.last_error || b.overdue ? "warn" : "ok", "Latest backup",
        `${b.latest.name} · ${formatFileSize(b.latest.size)} · ${fmtDuration(_ageOf(b.latest.created_at))} ago`)
    : healthRow("warn", "Latest backup", "No backup has been made yet"));
  if (b.last_error) backupRows.push(healthRow("warn", "Backup problem", b.last_error));
  backupRows.push(healthRow(b.spoolman_db ? "ok" : "warn", "Coverage",
    (b.spoolman_db ? "Configuration + Spoolman database" : "Configuration only — set SPOOLMAN_DB_FILE to include Spoolman") +
    ` · every ${b.interval_hours} h · keeping ${b.keep} · ${b.count} stored`));
  backupRows.push(healthRow(null, "Location", b.dir));

  body.innerHTML =
    `<div class="health-section"><h4>Services</h4>${services.join("")}</div>` +
    `<div class="health-section"><h4>Printers</h4>${printerRows.join("") || '<p class="health-empty">No printers configured</p>'}</div>` +
    `<div class="health-section"><h4>Backups</h4>${backupRows.join("")}</div>`;
  if (!backupBtn.dataset.busy) backupBtn.disabled = !_wsConnected || b.running;
}

function openSystemHealth() {
  document.getElementById("modal-system-health").classList.add("open");
  renderSystemHealthDetail();
}

function closeSystemHealth() {
  document.getElementById("modal-system-health").classList.remove("open");
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
    if (data.errors?.length) toast(`Backup saved with problems: ${data.errors.join("; ")}`, true);
    else toast(`Backup saved: ${data.name}`);
  } catch (err) {
    toast(`Backup failed: ${err.message}`, true);
  } finally {
    delete btn.dataset.busy;
    btn.textContent = "Back up now";
    btn.disabled = false;
  }
});

setInterval(() => { if (typeof _wsConnected !== "undefined") renderSystemHealth(); }, 5000);
