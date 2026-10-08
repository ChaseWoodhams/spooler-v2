/* Spooler – frontend app.js */
"use strict";

const WS_URL  = location.protocol === "https:"
  ? `wss://${location.hostname}:${window.SPOOLER_WSS_PORT ?? 8766}`
  : `ws://${location.hostname}:${window.SPOOLER_WS_PORT ?? 8765}`;
const SPOOLMAN_URL = "/api/spoolman/api/v1";
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS  = 15000;

// ─── Theme ────────────────────────────────────────────────────────────────────
(function () {
  if (localStorage.getItem("theme") === "light") {
    document.body.classList.add("light-mode");
  }
})();

// Redirect to login on any 401 from our own API
const _fetch = window.fetch.bind(window);
window.fetch = async function(url, options) {
  const resp = await _fetch(url, options);
  if (resp.status === 401 && typeof url === "string" && url.startsWith("/api/")) {
    location.replace("/login");
  }
  return resp;
};

let ws       = null;
let printers = {}; // id → printer data
let spools   = []; // spool inventory from Spoolman
let trayMap  = {}; // printer_id → { tray_id_str → spoolman_spool_id }
let _prevActiveTray = {}; // printer_id → last seen active_tray_id

// ─── Spoolman field helpers ────────────────────────────────────────────────────
function spoolName(s)       { return [s.filament?.vendor?.name, s.filament?.material, s.filament?.name].filter(Boolean).join(" ") || `Spool ${s.id}`; }
function spoolColorHex(s)   { const h = s.filament?.color_hex || "888888"; return h.startsWith("#") ? h : "#" + h; }
function spoolRemaining(s)  { return Math.round(s.remaining_weight ?? 0); }
function spoolQuantityText(s) { return s.remaining_weight == null ? "Remaining unavailable" : `${spoolRemaining(s)} g remaining`; }
function spoolTotal(s)      { return Math.round(s.initial_weight ?? 1000); }
function spoolPct(s)        { const t = spoolTotal(s); return t > 0 ? Math.round(spoolRemaining(s) / t * 100) : 0; }
function spoolAssignedTo(s) { return s.location || null; }
// Find a printer by its Spoolman location string (name or legacy ID/IP).
function printerByLocation(loc) {
  if (!loc) return null;
  return printers[loc] || Object.values(printers).find(p => p.name === loc) || null;
}
// The location string to store in Spoolman for a given printer ID — the printer's name.
function printerLocation(printerId) {
  return printers[printerId]?.name || printerId;
}

let currentPickerPrinterId = null;
let currentPickerTrayId    = null; // non-null → picker is in tray-link mode
let _currentFilePrinterId  = null;
let _currentFileList       = []; // cached file objects for the open file browser
let _fileListTimer = null;
let _fileBrowserOpener = null;
const _cameraPreferences = new Map();
let _cameraResizeTimer = null;
let _materialDensityMap = {}; // material name → density g/cm³
let _filamentMetaSequence = 0;
let _activePrinterId = null;
let _detailJogStep = 1;

// ─── Filament metadata (brands + materials from SpoolmanDB) ───────────────────
async function loadFilamentMeta({ preserve = true } = {}) {
  const sequence = ++_filamentMetaSequence;
  const status = document.getElementById("spool-catalogue-status");
  const retry = document.getElementById("btn-spool-catalogue-retry");
  const bSel = document.getElementById("spool-input-brand");
  const mSel = document.getElementById("spool-input-material");
  const brandOtherInput = document.getElementById("spool-input-brand-other");
  const materialOtherInput = document.getElementById("spool-input-material-other");
  let oldBrand, oldMaterial, oldBrandText, oldMaterialText;
  const rememberSelection = () => {
    oldBrand = preserve ? bSel.value : "";
    oldMaterial = preserve ? mSel.value : "";
    oldBrandText = brandOtherInput.value;
    oldMaterialText = materialOtherInput.value;
  };
  status.textContent = "Loading brand and material catalogue… You can enter either manually using Other.";
  status.dataset.state = "loading";
  retry.hidden = true;
  try {
    const r = await fetch("/api/filament-meta", { signal: AbortSignal.timeout(15000) });
    if (!r.ok) throw new Error(`Catalogue request failed (${r.status})`);
    const data = await r.json();
    if (sequence !== _filamentMetaSequence) return;
    rememberSelection();
    const brands = Array.isArray(data.brands) ? data.brands : [];
    const materials = Array.isArray(data.materials) ? data.materials : [];

    _materialDensityMap = {};
    materials.forEach(m => { _materialDensityMap[m.name] = m.density; });

    bSel.innerHTML = `<option value="">${brands.length ? "– Select brand –" : "– No brands in catalogue –"}</option>`;
    brands.forEach(b => {
      const o = document.createElement("option");
      o.value = o.textContent = b;
      bSel.appendChild(o);
    });
    const bOther = document.createElement("option");
    bOther.value = "__other__"; bOther.textContent = "Other…";
    bSel.appendChild(bOther);

    // Rebuild material dropdown
    mSel.innerHTML = `<option value="">${materials.length ? "– Select material –" : "– No materials in catalogue –"}</option>`;
    materials.forEach(m => {
      const o = document.createElement("option");
      o.value = o.textContent = m.name;
      mSel.appendChild(o);
    });
    const mOther = document.createElement("option");
    mOther.value = "__other__"; mOther.textContent = "Other…";
    mSel.appendChild(mOther);
    const restore = (select, textInput, oldValue, oldText, values) => {
      if (!oldValue) return;
      if (oldValue === "__other__" || !values.includes(oldValue)) {
        select.value = "__other__";
        textInput.value = oldValue === "__other__" ? oldText : oldValue;
        textInput.style.display = "";
      } else select.value = oldValue;
    };
    restore(bSel, brandOtherInput, oldBrand, oldBrandText, brands);
    restore(mSel, materialOtherInput, oldMaterial, oldMaterialText, materials.map(m => m.name));
    if (!oldMaterial && materials.some(m => m.name === "PLA")) mSel.value = "PLA";
    status.textContent = brands.length || materials.length
      ? `Catalogue loaded: ${brands.length} brands and ${materials.length} materials. Choose Other to enter a value manually.`
      : "Catalogue is empty. Choose Other to enter the brand and material manually.";
    status.dataset.state = brands.length || materials.length ? "ok" : "empty";
  } catch (error) {
    if (sequence !== _filamentMetaSequence) return;
    rememberSelection();
    _materialDensityMap = {};
    bSel.innerHTML = '<option value="">Catalogue unavailable</option><option value="__other__">Other…</option>';
    mSel.innerHTML = '<option value="">Catalogue unavailable</option><option value="__other__">Other…</option>';
    if (oldBrand === "__other__" || oldBrand) { bSel.value = "__other__"; brandOtherInput.value = oldBrand === "__other__" ? oldBrandText : oldBrand; brandOtherInput.style.display = ""; }
    if (oldMaterial === "__other__" || oldMaterial) { mSel.value = "__other__"; materialOtherInput.value = oldMaterial === "__other__" ? oldMaterialText : oldMaterial; materialOtherInput.style.display = ""; }
    status.textContent = `Could not load the catalogue (${error.message}). Keep entering values manually with Other, or retry.`;
    status.dataset.state = "error";
    retry.hidden = false;
  }
}

// ─── WebSocket connection ──────────────────────────────────────────────────────
let _wsConnected = false;
let _wsAttempts  = 0;  // consecutive closed/failed connections since the last success
let _wsRetryAt   = 0;

function connect() {
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    console.log("[WS] Connected");
    _wsConnected = true;
    _wsAttempts = 0;
    renderSystemHealth();
    send({ action: "list_printers" });
    loadHistory();
    loadJobs();
    fetchSpools();
  };

  ws.onmessage = (ev) => {
    try {
      handleMessage(JSON.parse(ev.data));
    } catch (e) {
      console.error("[WS] Bad message", e);
    }
  };

  ws.onclose = (ev) => {
    if (ev.code === 1008) { location.replace("/login"); return; }
    _wsConnected = false;
    _wsAttempts++;
    // Back off while Spooler is down (e.g. restarting) instead of hammering it every few seconds
    const base  = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** (_wsAttempts - 1));
    const delay = Math.round(base * (0.8 + Math.random() * 0.4));
    _wsRetryAt = Date.now() + delay;
    console.warn(`[WS] Disconnected, retrying in ${delay} ms…`);
    renderSystemHealth();
    setTimeout(connect, delay);
  };

  ws.onerror = () => ws.close();
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(obj));
  }
}

// ─── Message handling ──────────────────────────────────────────────────────────
function handleMessage(msg) {
  switch (msg.type) {
    case "system_status":
      applySystemStatus(msg.status);
      break;
    case "jobs":
      applyJobs(msg.jobs);
      break;
    case "printer_update":
      const linkChanged = printers[msg.printer.id]?.connected !== msg.printer.connected;
      printers[msg.printer.id] = msg.printer;
      if (linkChanged) renderSystemHealth();
      refreshQueueSoon();  // live progress and start availability in the queue panel
      renderPrinter(msg.printer);
      if (_activePrinterId === msg.printer.id) renderPrinterDetail(msg.printer);
      else if (location.hash === `#printer=${encodeURIComponent(msg.printer.id)}`) _showPrinterDashboard(msg.printer.id);
      syncEmptyState();
      _checkActiveTrayChange(msg.printer);
      break;
    case "printer_removed":
      delete printers[msg.printer_id];
      const el = document.getElementById(`card-${CSS.escape(msg.printer_id)}`);
      if (el) el.remove();
      if (_activePrinterId === msg.printer_id) closePrinterDashboard();
      syncEmptyState();
      break;
    case "info":
      toast(msg.message);
      break;
    case "error":
      if (_currentFilePrinterId && /file list/i.test(msg.message || "")) renderFileList([], msg.message);
      toast(msg.message, true);
      break;
    case "print_recorded":
      onPrintRecorded(msg.print);
      break;
    case "spool_empty":
      spools = spools.map(s => s.id === msg.spool.id ? msg.spool : s);
      Object.values(printers).forEach(p => renderPrinter(p));
      toast(`⚠ Spool empty: ${spoolName(msg.spool)} — assign a new spool`, true);
      break;
    case "spool_low":
      spools = spools.map(s => s.id === msg.spool.id ? msg.spool : s);
      Object.values(printers).forEach(p => renderPrinter(p));
      toast(`Spool low: ${spoolName(msg.spool)} — ${spoolRemaining(msg.spool)}g remaining`);
      break;
    case "file_list":
      if (msg.printer_id === _currentFilePrinterId) renderFileList(msg.files, msg.error);
      break;
    case "file_info":
      _applyFileInfo(msg);
      break;
    case "tray_map":
      trayMap = msg.tray_map || {};
      Object.values(printers).forEach(p => renderPrinter(p));
      break;
    case "cc2_discovered":
      for (const ip of (msg.ips || [])) {
        toastAction(`CC2 found at ${ip} — enter access code to add`, "Add", () => {
          resetPrinterForm();
          document.getElementById("input-ip").value = ip;
          document.getElementById("input-name").value = `CC2 (${ip})`;
          inputType.value = "cc2";
          labelAccessCode.style.display = "flex";
          openPrinters();
          inputAccessCode.focus();
        }, 12000);
      }
      break;
  }
}

function _checkActiveTrayChange(printer) {
  const ci = printer.status?.canvas_info;
  if (!ci) return;
  const trayId = ci.active_tray_id ?? -1;
  const prev   = _prevActiveTray[printer.id] ?? -2;
  _prevActiveTray[printer.id] = trayId;
  if (trayId < 0 || trayId === prev) return;
  // Active tray changed — auto-assign the linked spool if one is set
  const linked = (trayMap[printer.id] || {})[String(trayId)];
  if (linked != null) {
    console.log(`[Tray] Active tray ${trayId} → auto-assigning spool ${linked}`);
    assignSpool(printer.id, linked);
  }
}

// ─── Status helpers ────────────────────────────────────────────────────────────
// PrintInfo.Status codes (from CarbonicSidecar / elegoo-homeassistant)
const PRINT_STATUS = {
  0:  "idle",
  1:  "homing",
  2:  "printing",    // bed dropping
  3:  "printing",
  4:  "printing",    // lifting
  5:  "pausing",
  6:  "paused",
  7:  "stopping",
  8:  "cancelled",
  9:  "complete",
  10: "checking",
  12: "recovering",
  13: "printing",    // printing (recovery)
  14: "cancelled",
  15: "warming up",
  16: "warming up",  // preheating
  18: "warming up",
  19: "warming up",
  20: "leveling",
  21: "warming up",
};

// CurrentStatus[0] codes (machine-level state)
const MACHINE_STATUS = {
  0: "idle", 1: "printing", 2: "transferring", 3: "testing",
  4: "testing", 5: "leveling", 6: "tuning", 7: "stopping",
  8: "stopped", 9: "homing", 10: "loading", 11: "tuning", 12: "recovering",
};

function getPrintStatus(printer) {
  if (!printer.connected) return "offline";
  const pi = printer.status?.PrintInfo;
  const code = pi?.Status;
  if (code === undefined || code === null) return "idle";
  // Special case: CurrentStatus[0] === 9 means homing (between prints)
  const machineCode = printer.status?.CurrentStatus?.[0];
  if (machineCode === 9 && code === 0) return "homing";
  return PRINT_STATUS[code] ?? "idle";
}

function statusClass(s) {
  if (["printing", "homing", "recovering"].includes(s))        return "printing";
  if (["warming up", "leveling", "checking"].includes(s))      return "warmingup";
  if (["pausing", "paused"].includes(s))                       return "paused";
  if (["stopping", "cancelled"].includes(s))                   return "cancelled";
  if (s === "complete")                                         return "complete";
  if (s === "offline")                                          return "offline";
  return "idle";
}

function isActivelyPrinting(printer) {
  const s = getPrintStatus(printer);
  return ["printing", "homing", "warming up", "leveling", "checking", "recovering"].includes(s);
}

// Returns the printer object that currently has this spool loaded as its active tray, or null.
function getSpoolActivePrinter(spoolId) {
  for (const [pid, trays] of Object.entries(trayMap)) {
    const p = printers[pid];
    if (!p) continue;
    const activeTray = p.status?.canvas_info?.active_tray_id ?? -1;
    if (activeTray >= 0 && trays[String(activeTray)] === spoolId) return p;
  }
  return null;
}

function isPaused(printer) {
  const s = getPrintStatus(printer);
  return ["paused", "pausing"].includes(s);
}

function getProgress(printer) {
  const pi = printer.status?.PrintInfo;
  if (!pi) return null;
  if ((pi.TotalLayer ?? 0) > 0)
    return Math.min(100, Math.round((pi.CurrentLayer / pi.TotalLayer) * 100));
  if ((pi.TotalTicks ?? 0) > 0)
    return Math.min(100, Math.round((pi.CurrentTicks / pi.TotalTicks) * 100));
  return null;
}

function getFilename(printer) {
  return printer.status?.PrintInfo?.Filename || printer.status?.PrintInfo?.FileName || "";
}

function formatTime(secs) {
  if (!secs || secs < 0) return "--:--";
  secs = Math.round(secs);
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function formatTimeLong(secs) {
  if (!secs || secs < 0) return "--";
  secs = Math.round(secs);
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  return `${String(h).padStart(2, "0")}h${String(m).padStart(2, "0")}m${String(s).padStart(2, "0")}s`;
}

function tempColor(t) {
  if (!t) return "";
  if (t > 150) return "hot";
  if (t > 50)  return "warm";
  return "cool";
}

// ─── Camera stream watchdog ───────────────────────────────────────────────────
// An MJPEG <img> fires no event when its stream ends or stalls — it just keeps the
// last frame (CC1 firmware drops its feed at print start). Sample each feed's pixels
// and reconnect any whose picture hasn't changed for CAMERA_STALL_MS.
const CAMERA_STALL_MS = 20000;
const CAMERA_CHECK_MS = 5000;
const _cameraProbe = document.createElement("canvas");
_cameraProbe.width = 16;
_cameraProbe.height = 12;
const _cameraProbeCtx = _cameraProbe.getContext("2d", { willReadFrequently: true });

function _cameraSignature(img) {
  if (!img.complete || !img.naturalWidth) return null;
  try {
    _cameraProbeCtx.drawImage(img, 0, 0, _cameraProbe.width, _cameraProbe.height);
    const px = _cameraProbeCtx.getImageData(0, 0, _cameraProbe.width, _cameraProbe.height).data;
    let h = 2166136261;
    for (let i = 0; i < px.length; i++) h = Math.imul(h ^ px[i], 16777619);
    return h >>> 0;
  } catch {
    return null;
  }
}

function _watchCameraStream(img, url) {
  img.dataset.cameraSrc = url;
  if (img._camCheckedAt == null) {
    img._camSig = null;
    img._camChangedAt = Date.now();
    img._camCheckedAt = Date.now();
  }
}

function _restartCameraStream(img) {
  img._camSig = null;
  img._camChangedAt = Date.now();
  img.style.display = "";
  img.src = `${img.dataset.cameraSrc}?t=${Date.now()}`;
}

setInterval(() => {
  if (document.hidden) return;
  const now = Date.now();
  for (const img of document.querySelectorAll("img[data-camera-src]")) {
    // Time spent hidden or detached doesn't count as a stall.
    if (now - img._camCheckedAt > CAMERA_CHECK_MS * 2) img._camChangedAt = now;
    img._camCheckedAt = now;
    const sig = _cameraSignature(img);
    if (sig !== null && sig !== img._camSig) {
      img._camSig = sig;
      img._camChangedAt = now;
    } else if (now - img._camChangedAt > CAMERA_STALL_MS) {
      _restartCameraStream(img);
    }
  }
}, CAMERA_CHECK_MS);

// ─── Rendering ─────────────────────────────────────────────────────────────────
function renderPrinter(printer) {
  const grid = document.getElementById("printer-grid");
  const cardId = `card-${printer.id}`;
  let card = document.getElementById(cardId);

  if (!card) {
    card = document.createElement("div");
    card.className = "printer-card";
    card.id = cardId;
    grid.appendChild(card);
  }
  card.classList.add("printer-card-interactive");
  card.tabIndex = 0;
  card.setAttribute("aria-label", `${printer.name}, ${printer.connected ? "Connected" : "Offline"}. Open printer controls`);
  card.onclick = (event) => {
    if (!event.target.closest("button, input, select, a, label")) openPrinterDashboard(printer.id);
  };
  card.onkeydown = (event) => {
    if ((event.key === "Enter" || event.key === " ") && event.target === card) {
      event.preventDefault();
      openPrinterDashboard(printer.id);
    }
  };

  const status   = getPrintStatus(printer);
  const sc       = statusClass(status);
  const progress = getProgress(printer);
  const filename = getFilename(printer);
  const printing = isActivelyPrinting(printer);
  const paused   = isPaused(printer);
  const connected = printer.connected;

  const nozzle     = printer.status?.TempOfNozzle    ?? printer.status?.NozzleTemp    ?? 0;
  const nozzleTgt  = printer.status?.TempTargetNozzle?? printer.status?.NozzleTempTarget ?? 0;
  const bed        = printer.status?.TempOfHotbed     ?? printer.status?.BedTemp       ?? 0;
  const bedTgt     = printer.status?.TempTargetHotbed ?? printer.status?.BedTempTarget ?? 0;
  const chamber    = printer.status?.TempOfBox        ?? printer.status?.ChamberTemp   ?? 0;
  const chamberTgt = printer.status?.TempTargetBox    ?? 0;

  const elapsed   = printer.status?.PrintInfo?.PrintTime  ?? 0;
  const remaining = printer.status?.PrintInfo?.RemainTime ?? 0;
  const filamentMm = printer.filament_mm ?? 0;
  const filamentG  = printer.filament_g  ?? 0;
  const lightOn    = getLightOn(printer);

  const cameraUrl = _activePrinterId !== printer.id && printer.connected && printer.camera_url
    ? `/api/camera/${encodeURIComponent(printer.id)}`
    : null;

  // Preserve the existing camera img element so its MJPEG stream connection
  // survives innerHTML replacement (every printer_update would kill it otherwise)
  const prevCameraImg = card.querySelector('.card-camera img');
  const cameraExpanded = _cameraPreferences.get(String(printer.id)) ?? window.innerWidth > 640;

  card.innerHTML = `
    <!-- Header -->
    <div class="card-header">
      <div class="status-dot ${sc}"></div>
      <div class="card-header-info">
        <div class="card-title">${escHtml(printer.name)}</div>
        <div class="card-subtitle">${escHtml(printer.ip)}${printer.attrs?.FirmwareVersion ? ` · fw ${escHtml(printer.attrs.FirmwareVersion)}` : ""}</div>
      </div>
      <span class="status-badge ${sc}">${status}</span>
      <button class="card-files-btn" onclick="openPrinterDashboard('${escAttr(printer.id)}')" title="Open printer controls" aria-label="Open printer controls">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3"/>
          <path d="M1 14h6M9 8h6M17 16h6"/>
        </svg>
      </button>
      <button class="card-files-btn" onclick="openFileBrowser('${escAttr(printer.id)}')" title="Browse files">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
        </svg>
      </button>
    </div>

    <!-- Camera -->
    <div class="card-camera${cameraExpanded ? "" : " camera-compact"}" id="card-camera-${escAttr(printer.id)}">
      <div class="camera-placeholder" style="display:flex">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1">
          <path d="M23 7l-7 5 7 5V7z"/>
          <rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
        </svg>
        <span>${connected ? "No camera feed" : "Printer offline"}</span>
      </div>
      <button class="camera-view-toggle" type="button" aria-controls="card-camera-${escAttr(printer.id)}" aria-expanded="${cameraExpanded}" onclick="togglePrinterCamera('${escAttr(printer.id)}')">${cameraExpanded ? "Collapse camera" : "Expand camera"}</button>
    </div>

    <!-- Progress (only when printing/paused) -->
    ${(printing || paused) ? `
    <div class="card-progress-wrap">
      <div class="progress-header">
        <span class="progress-filename" title="${escAttr(filename)}">${escHtml(filename || "Unknown file")}</span>
        <span class="progress-pct">${progress ?? 0}%</span>
      </div>
      <div class="progress-bar-bg">
        <div class="progress-bar-fill" style="width:${progress ?? 0}%"></div>
      </div>
      <div class="progress-info">
        <span>Elapsed: ${formatTime(elapsed)}</span>
        <span>Remaining: ${formatTime(remaining)}</span>
      </div>
      ${filamentMm > 0 ? `
      <div class="filament-info">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3"/>
        </svg>
        <span>${(filamentMm / 1000).toFixed(2)} m &nbsp;·&nbsp; ~${filamentG} g</span>
      </div>` : ""}
    </div>
    ` : ""}

    ${typeof printerUpNext === "function" ? printerUpNext(printer) : ""}

    <!-- Spool (hidden for canvas printers — tray linking handles assignment) -->
    ${!printer.status?.canvas_info?.canvas_list?.length ? (() => {
      const s = spools.find(sp => printerByLocation(spoolAssignedTo(sp))?.id === printer.id);
      const pct = s ? spoolPct(s) : 0;
      const isEmpty = s && s.remaining_weight != null && spoolRemaining(s) === 0;
      const isLow   = s && s.remaining_weight != null && !isEmpty && spoolTotal(s) > 0 && pct < 10;
      const barColor = isEmpty ? "var(--red)" : isLow ? "var(--yellow)" : null;
      return `<div class="card-spool${isEmpty ? " spool-empty" : isLow ? " spool-low" : ""}">
        ${s ? `
          <div class="spool-dot" style="background:${escAttr(spoolColorHex(s))}"></div>
          <div class="spool-card-info">
            <div class="spool-card-name">${escHtml(spoolName(s))}${isEmpty ? ' <span class="spool-tag empty">Empty</span>' : isLow ? ' <span class="spool-tag low">Low</span>' : ""}</div>
            <div class="spool-bar-wrap">
              <div class="spool-bar-fill" style="width:${pct}%${barColor ? ";background:" + barColor : ""}"></div>
            </div>
            <div class="spool-card-remaining">${spoolQuantityText(s)} · ${spoolTotal(s)} g initial${_spoolLoadState === "error" ? " · Inventory unavailable; verify loaded material" : ""}</div>
          </div>
        ` : `<span class="spool-none">No spool assigned</span>`}
        <button class="btn btn-sm btn-secondary spool-assign-btn"
                onclick="openSpoolPicker('${escAttr(printer.id)}')">
          ${s ? "Change" : "Assign"}
        </button>
      </div>`;
    })() : ""}

    <!-- Canvas / multi-material trays (CC2 with canvas unit) -->
    ${(() => {
      const ci = printer.status?.canvas_info;
      if (!ci || !ci.canvas_list?.length) return "";
      const activeTrayId = ci.active_tray_id ?? -1;
      const trays = ci.canvas_list.flatMap(c => c.tray_list || []);
      if (!trays.length) return "";
      const pid = printer.id;
      const trayHtml = trays.map(t => {
        const color      = t.filament_color || "#888888";
        const active     = t.tray_id === activeTrayId;
        const label      = t.filament_type || "?";
        const fullName   = [t.brand, t.filament_name].filter(Boolean).join(" ");
        const linkedId   = (trayMap[pid] || {})[String(t.tray_id)];
        const linkedSpool = linkedId != null ? spools.find(s => s.id === linkedId) : null;
        const linkedChip = linkedSpool
          ? `<div class="canvas-tray-spool" title="${escAttr(spoolName(linkedSpool))}">
               <div class="canvas-tray-spool-dot" style="background:${escAttr(spoolColorHex(linkedSpool))}"></div>
               <span>${escHtml(spoolName(linkedSpool).split(" ").slice(0,2).join(" "))}</span>
             </div>`
          : `<div class="canvas-tray-spool canvas-tray-spool-empty">No spool</div>`;
        return `<div class="canvas-tray${active ? " canvas-tray-active" : ""}"
                     title="${escAttr(fullName || label)}"
                     onclick="openTrayPicker('${escAttr(pid)}', ${t.tray_id})">
          <div class="canvas-tray-num">${t.tray_id + 1}</div>
          <div class="canvas-tray-dot" style="background:${escAttr(color)}"></div>
          <div class="canvas-tray-label">${escHtml(label)}</div>
          ${linkedChip}
        </div>`;
      }).join("");
      return `<div class="card-canvas">
        <div class="canvas-header">Canvas <span class="canvas-tray-count">${trays.length} slots</span></div>
        <div class="canvas-trays">${trayHtml}</div>
      </div>`;
    })()}

    <!-- Temperatures -->
    <div class="card-temps">
      <div class="temp-block">
        <div class="temp-label">Nozzle</div>
        <div class="temp-value ${tempColor(nozzle)}">${nozzle ? nozzle.toFixed(0) : "--"}<span style="font-size:12px;font-weight:400">°C</span></div>
        <div class="temp-target">Target: ${nozzleTgt ? nozzleTgt.toFixed(0) + "°C" : "--"}</div>
      </div>
      <div class="temp-block">
        <div class="temp-label">Bed</div>
        <div class="temp-value ${tempColor(bed)}">${bed ? bed.toFixed(0) : "--"}<span style="font-size:12px;font-weight:400">°C</span></div>
        <div class="temp-target">Target: ${bedTgt ? bedTgt.toFixed(0) + "°C" : "--"}</div>
      </div>
      <div class="temp-block">
        <div class="temp-label">Chamber</div>
        <div class="temp-value ${tempColor(chamber)}">${chamber ? chamber.toFixed(0) : "--"}<span style="font-size:12px;font-weight:400">°C</span></div>
        <div class="temp-target">Target: ${chamberTgt ? chamberTgt.toFixed(0) + "°C" : "--"}</div>
      </div>
    </div>

    <!-- Controls -->
    <div class="card-controls">
      ${connected && !printing && !paused ? `
        <button class="btn btn-primary btn-sm" onclick="confirmBedCleared('${escAttr(printer.id)}')" title="Confirm the printed plate is off the bed; releases the next queued job when auto-start is on">
          Printed OK · Bed cleared
        </button>
      ` : ""}
      ${printing ? `
        <button class="btn btn-secondary btn-sm" onclick="printerAction('${escAttr(printer.id)}','pause')">
          <svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
          Pause
        </button>
        <button class="btn btn-danger btn-sm" onclick="confirmStop('${escAttr(printer.id)}')">
          <svg viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>
          Stop
        </button>
      ` : ""}
      ${paused ? `
        <button class="btn btn-primary btn-sm" onclick="printerAction('${escAttr(printer.id)}','resume')">
          <svg viewBox="0 0 24 24" fill="currentColor"><polygon points="5,3 19,12 5,21"/></svg>
          Resume
        </button>
        <button class="btn btn-danger btn-sm" onclick="confirmStop('${escAttr(printer.id)}')">
          <svg viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>
          Stop
        </button>
      ` : ""}
      <button class="btn btn-secondary btn-sm" onclick="QuickPrint.pick({ printerId: '${escAttr(printer.id)}' })"
              title="Pick a model or G-code file and print it on ${escAttr(printer.name)} without making a Project" ${!connected ? "disabled" : ""}>Quick print here</button>
      <div class="controls-spacer"></div>
      <button class="btn btn-sm ${lightOn ? "btn-primary" : "btn-secondary"}"
              onclick="printerAction('${escAttr(printer.id)}','${lightOn ? "light_off" : "light_on"}')"
              title="${lightOn ? "Turn chamber light off" : "Turn chamber light on"}"
              aria-label="${escAttr(`${lightOn ? "Turn chamber light off" : "Turn chamber light on"} – ${printer.name}`)}"
              aria-pressed="${lightOn}"
              ${!connected ? "disabled" : ""}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="5"/>
          <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>
        </svg>
      </button>
      ${(printing || paused) ? (() => {
        const sf = printer.printer_type === "cc2"
          ? (printer.status?.SpeedFactor ?? 100)
          : (printer.status?.PrintInfo?.PrintSpeedPct ?? 100);
        const sid = `speed-val-${printer.id}`;
        return `<div class="card-speed">
          <span class="speed-label">Speed: <span id="${escAttr(sid)}">${sf}%</span></span>
          <input type="range" class="speed-slider" min="10" max="200" step="5" value="${sf}"
                 oninput="document.getElementById('${escAttr(sid)}').textContent=this.value+'%'"
                 onchange="setSpeed('${escAttr(printer.id)}',+this.value)">
        </div>`;
      })() : ""}
    </div>
  `;

  const cameraDiv = card.querySelector('.card-camera');
  const placeholder = cameraDiv.querySelector('.camera-placeholder');
  if (cameraUrl) {
    if (prevCameraImg) {
      // Re-attach preserved stream; clear src first if printer just came back online
      // to force reconnect (avoids stale broken stream from a prior disconnect)
      if (!prevCameraImg._connected && connected) {
        prevCameraImg.src = cameraUrl;
      }
      prevCameraImg._connected = connected;
      cameraDiv.insertBefore(prevCameraImg, cameraDiv.firstChild);
      placeholder.style.display = 'none';
      prevCameraImg.style.display = '';
    } else {
      const img = document.createElement('img');
      img.src = cameraUrl;
      img.alt = 'Camera feed';
      img._connected = connected;
      img.addEventListener('load',  () => { img.parentElement?.querySelector('.camera-placeholder')?.style.setProperty('display', 'none'); });
      img.addEventListener('error', () => { img.style.display = 'none'; img.parentElement?.querySelector('.camera-placeholder')?.style.setProperty('display', 'flex'); });
      _watchCameraStream(img, cameraUrl);
      cameraDiv.insertBefore(img, cameraDiv.firstChild);
    }
  }
}

window.togglePrinterCamera = function togglePrinterCamera(printerId) {
  const camera = document.querySelector(`#card-${CSS.escape(printerId)} .card-camera`);
  if (!camera || !printers[printerId]) return;
  const expanded = camera.classList.contains("camera-compact");
  _cameraPreferences.set(String(printerId), expanded);
  renderPrinter(printers[printerId]);
  document.querySelector(`#card-${CSS.escape(printerId)} .camera-view-toggle`)?.focus();
};

window.addEventListener("resize", () => {
  clearTimeout(_cameraResizeTimer);
  _cameraResizeTimer = setTimeout(() => {
    for (const printer of Object.values(printers)) renderPrinter(printer);
  }, 120);
});

function _printerCoords(printer) {
  const raw = printer.status?.CurrenCoord ?? printer.status?.CurrentCoord;
  const values = Array.isArray(raw) ? raw : String(raw || "").split(",");
  const nums = values.slice(0, 3).map(value => Number.parseFloat(value));
  return {
    x: Number.isFinite(nums[0]) ? nums[0] : null,
    y: Number.isFinite(nums[1]) ? nums[1] : null,
    z: Number.isFinite(nums[2]) ? nums[2] : null,
  };
}

function _coordText(value) {
  return value == null ? "--" : value.toFixed(2);
}

function _fanSpeed(printer, key) {
  const value = printer.status?.CurrentFanSpeed?.[key];
  return Number.isFinite(Number(value)) ? Math.max(0, Math.min(100, Math.round(Number(value)))) : 0;
}

function openPrinterDashboard(printerId) {
  if (!printers[printerId]) return;
  const nextHash = `#printer=${encodeURIComponent(printerId)}`;
  if (location.hash !== nextHash) location.hash = nextHash;
  else _showPrinterDashboard(printerId);
}

function _showPrinterDashboard(printerId) {
  const printer = printers[printerId];
  if (!printer) return;
  const previousPrinterId = _activePrinterId;
  if (previousPrinterId && previousPrinterId !== printerId && printers[previousPrinterId]) {
    _activePrinterId = null;
    renderPrinter(printers[previousPrinterId]);
  }
  _activePrinterId = printerId;
  document.getElementById("empty-state").style.display = "none";
  document.getElementById("printer-grid").style.display = "none";
  const detail = document.getElementById("printer-detail");
  detail.hidden = false;
  renderPrinterDetail(printer);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function closePrinterDashboard() {
  const previousPrinterId = _activePrinterId;
  _activePrinterId = null;
  document.getElementById("printer-detail").hidden = true;
  if (previousPrinterId && printers[previousPrinterId]) renderPrinter(printers[previousPrinterId]);
  syncEmptyState();
}

function returnToPrinterGrid() {
  if (location.hash.length > 1) location.hash = ""; // a printer page or the Projects page: the hashchange closes it
  else closePrinterDashboard();
}

function _syncPrinterRoute() {
  if (!location.hash.startsWith("#printer=")) {
    closePrinterDashboard();
    return;
  }
  const printerId = decodeURIComponent(location.hash.slice("#printer=".length));
  if (printers[printerId]) _showPrinterDashboard(printerId);
}

window.addEventListener("hashchange", _syncPrinterRoute);

function renderPrinterDetail(printer) {
  const detail = document.getElementById("printer-detail");
  if (_activePrinterId !== printer.id || detail.hidden) return;

  const activeInput = detail.contains(document.activeElement) ? document.activeElement : null;
  const activeInputId = activeInput?.id;
  const activeInputValue = activeInput?.value;
  const detailCamera = detail.querySelector(".detail-camera img");
  const previousCamera = (detailCamera?.dataset.printerId === printer.id ? detailCamera : null) ||
    document.querySelector(`#card-${CSS.escape(printer.id)} .card-camera img`);
  const status = getPrintStatus(printer);
  const sc = statusClass(status);
  const connected = printer.connected;
  const idle = status === "idle";
  const controlsEnabled = connected && idle && printer.printer_type === "cc1";
  const coords = _printerCoords(printer);
  const nozzle = Number(printer.status?.TempOfNozzle ?? printer.status?.NozzleTemp ?? 0);
  const nozzleTarget = Number(printer.status?.TempTargetNozzle ?? printer.status?.NozzleTempTarget ?? 0);
  const bed = Number(printer.status?.TempOfHotbed ?? printer.status?.BedTemp ?? 0);
  const bedTarget = Number(printer.status?.TempTargetHotbed ?? printer.status?.BedTempTarget ?? 0);
  const chamber = Number(printer.status?.TempOfBox ?? printer.status?.ChamberTemp ?? 0);
  const modelFan = _fanSpeed(printer, "ModelFan");
  const auxiliaryFan = _fanSpeed(printer, "AuxiliaryFan");
  const chamberFan = _fanSpeed(printer, "BoxFan");
  const lightOn = getLightOn(printer);
  const printing = isActivelyPrinting(printer);
  const paused = isPaused(printer);
  const detailProgress = getProgress(printer);
  const detailFilename = getFilename(printer);
  const detailRemaining = Number(printer.status?.PrintInfo?.RemainTime ?? 0);
  const speed = Number(printer.status?.PrintInfo?.PrintSpeedPct ?? 100);
  const cameraUrl = connected && printer.camera_url ? `/api/camera/${encodeURIComponent(printer.id)}` : null;
  const disableMotion = controlsEnabled ? "" : "disabled";
  const disableConnected = connected && printer.printer_type === "cc1" ? "" : "disabled";
  const stepButton = value => `<button class="detail-step ${_detailJogStep === value ? "active" : ""}" aria-pressed="${_detailJogStep === value}" onclick="setJogStep(${value})">${value} mm</button>`;
  const fanControl = (key, label, value) => `
    <div class="detail-control-row">
      <div class="detail-control-label"><span>${label}</span><strong id="detail-${key}-value">${value}%</strong></div>
      <input class="detail-slider" type="range" min="0" max="100" step="5" value="${value}" aria-label="${label}"
             oninput="document.getElementById('detail-${key}-value').textContent=this.value+'%'"
             onchange="setPrinterFan('${escAttr(printer.id)}','${key}',+this.value)" ${disableConnected}>
    </div>`;

  // Cockpit layout lives in printer-cockpit.js; the original markup below is the fallback if it fails to load.
  const protoHtml = window.cockpitPrinterHtml?.({
    printer, status, sc, connected, controlsEnabled, coords, nozzle, nozzleTarget, bed, bedTarget, chamber,
    modelFan, auxiliaryFan, chamberFan, lightOn, printing, paused, detailProgress, detailFilename,
    detailRemaining, speed, cameraUrl, disableMotion, disableConnected, jogStep: _detailJogStep,
  });
  detail.innerHTML = protoHtml ?? `
    <div class="detail-nav">
      <button class="detail-back" onclick="returnToPrinterGrid()" aria-label="Back to all printers">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m15 18-6-6 6-6"/></svg>
        All printers
      </button>
      <div class="detail-nav-title">
        <div class="status-dot ${sc}"></div>
        <div><h1>${escHtml(printer.name)}</h1><p>${escHtml(printer.ip)}${printer.attrs?.FirmwareVersion ? ` · firmware ${escHtml(printer.attrs.FirmwareVersion)}` : ""}</p></div>
      </div>
      <span class="status-badge ${sc}">${status}</span>
    </div>

    <div class="detail-layout">
      <div class="detail-column">
        <section class="detail-panel detail-motion-panel">
          <div class="detail-panel-heading"><div><span class="detail-eyebrow">Motion</span><h2>Printer control</h2></div><span class="detail-safety ${controlsEnabled ? "ready" : ""}">${controlsEnabled ? "Ready to move" : connected ? `Locked while ${status}` : "Printer offline"}</span></div>
          <div class="detail-coordinates">
            <div><span>X</span><strong>${_coordText(coords.x)}</strong><small>mm</small></div>
            <div><span>Y</span><strong>${_coordText(coords.y)}</strong><small>mm</small></div>
            <div><span>Z</span><strong>${_coordText(coords.z)}</strong><small>mm</small></div>
          </div>
          <div class="detail-step-picker" role="group" aria-label="Jog distance">${[0.1, 1, 10].map(stepButton).join("")}</div>
          <div class="motion-layout">
            <div class="z-jog">
              <button onclick="jogPrinter('${escAttr(printer.id)}','Z',1)" ${disableMotion} title="Move Z positive">Z+</button>
              <button class="motion-home" onclick="homePrinter('${escAttr(printer.id)}','Z')" ${disableMotion} title="Home Z" aria-label="Home Z">⌂</button>
              <button onclick="jogPrinter('${escAttr(printer.id)}','Z',-1)" ${disableMotion} title="Move Z negative">Z−</button>
            </div>
            <div class="xy-jog" role="group" aria-label="X and Y movement controls">
              <button class="jog-y-plus" onclick="jogPrinter('${escAttr(printer.id)}','Y',1)" ${disableMotion} title="Move Y positive">Y+</button>
              <button class="jog-x-minus" onclick="jogPrinter('${escAttr(printer.id)}','X',-1)" ${disableMotion} title="Move X negative">X−</button>
              <button class="motion-home jog-home" onclick="homePrinter('${escAttr(printer.id)}','XY')" ${disableMotion} title="Home X and Y" aria-label="Home X and Y">⌂</button>
              <button class="jog-x-plus" onclick="jogPrinter('${escAttr(printer.id)}','X',1)" ${disableMotion} title="Move X positive">X+</button>
              <button class="jog-y-minus" onclick="jogPrinter('${escAttr(printer.id)}','Y',-1)" ${disableMotion} title="Move Y negative">Y−</button>
            </div>
            <div class="home-stack">
              <button onclick="homePrinter('${escAttr(printer.id)}','X')" ${disableMotion}>Home X</button>
              <button onclick="homePrinter('${escAttr(printer.id)}','Y')" ${disableMotion}>Home Y</button>
              <button class="home-all" onclick="homePrinter('${escAttr(printer.id)}','XYZ')" ${disableMotion}>Home all</button>
            </div>
          </div>
          <p class="detail-help">Motion controls are locked whenever a print is active. Start with 0.1 mm or 1 mm and keep the chamber clear.</p>
        </section>

        <section class="detail-panel detail-environment-panel">
          <div class="detail-panel-heading"><div><span class="detail-eyebrow">Airflow & light</span><h2>Environment</h2></div></div>
          <div class="fan-grid">
            ${fanControl("model", "Model fan", modelFan)}
            ${fanControl("auxiliary", "Side fan", auxiliaryFan)}
            ${fanControl("chamber", "Chamber fan", chamberFan)}
            <div class="detail-light-control">
              <span>Chamber lighting</span>
              <button class="detail-toggle ${lightOn ? "active" : ""}" onclick="printerAction('${escAttr(printer.id)}','${lightOn ? "light_off" : "light_on"}')" ${connected ? "" : "disabled"} aria-pressed="${lightOn}" aria-label="Chamber lighting"><span></span></button>
            </div>
          </div>
          <div class="detail-speed-row">
            <div class="detail-control-label"><span>Print speed</span><strong id="detail-speed-value">${speed}%</strong></div>
            <input class="detail-slider" type="range" min="10" max="200" step="5" value="${speed}" aria-label="Print speed"
                   ${printing || paused ? "" : 'title="Available while a print is running"'}
                   oninput="document.getElementById('detail-speed-value').textContent=this.value+'%'"
                   onchange="setSpeed('${escAttr(printer.id)}',+this.value)" ${printing || paused ? "" : "disabled"}>
          </div>
        </section>

      </div>

      <div class="detail-column detail-column-monitor">
        <section class="detail-panel detail-camera-panel">
          <div class="detail-panel-heading"><div><span class="detail-eyebrow">Live view</span><h2>Monitoring</h2></div><span class="live-chip${cameraUrl ? "" : " offline"}" id="detail-live-chip">${cameraUrl ? "● Live" : "○ No signal"}</span></div>
          <div class="detail-camera"><div class="camera-placeholder" style="display:flex"><span>${connected ? "Camera unavailable" : "Printer offline"}</span></div></div>
        </section>

        <section class="detail-panel detail-temperature-panel">
          <div class="detail-panel-heading"><div><span class="detail-eyebrow">Thermal</span><h2>Temperature</h2></div></div>
          <div class="detail-temperature-list">
            <div class="detail-temperature-row"><div><span>Nozzle</span><strong class="${tempColor(nozzle)}">${nozzle.toFixed(1)} °C</strong></div><div class="temperature-target"><input id="detail-nozzle-target" type="number" min="0" max="300" value="${Math.round(nozzleTarget)}" aria-label="Nozzle target temperature (°C)" ${disableConnected}><span>°C</span><button onclick="setPrinterTemperature('${escAttr(printer.id)}','nozzle','detail-nozzle-target')" aria-label="Apply nozzle target" ${disableConnected}>Apply</button></div></div>
            <div class="detail-temperature-row"><div><span>Heated bed</span><strong class="${tempColor(bed)}">${bed.toFixed(1)} °C</strong></div><div class="temperature-target"><input id="detail-bed-target" type="number" min="0" max="110" value="${Math.round(bedTarget)}" aria-label="Heated bed target temperature (°C)" ${disableConnected}><span>°C</span><button onclick="setPrinterTemperature('${escAttr(printer.id)}','bed','detail-bed-target')" aria-label="Apply heated bed target" ${disableConnected}>Apply</button></div></div>
            <div class="detail-temperature-row"><div><span>Chamber</span><strong class="${tempColor(chamber)}">${chamber.toFixed(1)} °C</strong></div><span class="temperature-readonly">Sensor only</span></div>
          </div>
          <p class="detail-help">Set a target to 0 °C to turn that heater off.</p>
        </section>

        <section class="detail-panel detail-files-panel">
          <div class="detail-panel-heading"><div><span class="detail-eyebrow">Jobs</span><h2>Printer files</h2></div><button class="btn btn-primary" onclick="openFileBrowser('${escAttr(printer.id)}')">Browse & start print</button></div>
          <p class="detail-help">Select a file already on this printer, review its print options, and start it from the existing protected workflow.</p>
          ${(printing || paused) ? `
            <div class="detail-active-job">
              <div class="detail-active-job-heading">
                <div><span class="detail-job-label">Current print</span><strong title="${escAttr(detailFilename)}">${escHtml(detailFilename || "Print in progress")}</strong></div>
                <span class="detail-job-state ${paused ? "paused" : "printing"}">${paused ? "Paused" : "Printing"}</span>
              </div>
              <div class="detail-job-progress" role="progressbar" aria-label="Print progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${detailProgress ?? 0}"><span style="width:${detailProgress ?? 0}%"></span></div>
              <div class="detail-job-meta"><span>${detailProgress == null ? "Progress unavailable" : `${detailProgress}% complete`}</span><span>${detailRemaining > 0 ? `${formatTime(detailRemaining)} remaining` : "Time remaining unavailable"}</span></div>
            </div>
            <div class="detail-job-actions">
              ${printing ? `<button class="btn btn-secondary detail-job-primary" onclick="printerAction('${escAttr(printer.id)}','pause')">Pause print</button>` : `<button class="btn btn-primary detail-job-primary" onclick="printerAction('${escAttr(printer.id)}','resume')">Resume print</button>`}
              <button class="btn btn-danger detail-job-stop" onclick="confirmStop('${escAttr(printer.id)}')">Stop print</button>
            </div>` : ""}
        </section>
      </div>
    </div>`;

  const camera = detail.querySelector(".detail-camera");
  const placeholder = camera.querySelector(".camera-placeholder");
  if (cameraUrl) {
    const img = previousCamera || document.createElement("img");
    if (!previousCamera || img.dataset.printerId !== printer.id) img.src = cameraUrl;
    img.alt = `${printer.name} camera feed`;
    img.dataset.printerId = printer.id;
    _watchCameraStream(img, cameraUrl);
    const chip = detail.querySelector("#detail-live-chip");
    const setLive = state => {   // "live" | "connecting" | "off"
      chip.textContent = state === "live" ? "● Live" : state === "connecting" ? "○ Connecting…" : "○ No signal";
      chip.classList.toggle("offline", state !== "live");
    };
    setLive("connecting");
    img.addEventListener("load", () => { placeholder.style.display = "none"; setLive("live"); }, { once: true });
    img.addEventListener("error", () => { img.style.display = "none"; placeholder.style.display = "flex"; setLive("off"); }, { once: true });
    camera.insertBefore(img, camera.firstChild);
    if (img.naturalWidth) { placeholder.style.display = "none"; setLive("live"); }
  }

  if (activeInputId) {
    const restored = document.getElementById(activeInputId);
    if (restored) {
      restored.value = activeInputValue;
      restored.focus();
    }
  }
}

function setJogStep(step) {
  _detailJogStep = step;
  if (_activePrinterId && printers[_activePrinterId]) renderPrinterDetail(printers[_activePrinterId]);
}

function jogPrinter(printerId, axis, direction) {
  send({ action: "move_axis", printer_id: printerId, axis, step: _detailJogStep * direction });
}

function homePrinter(printerId, axis) {
  const label = axis === "XYZ" ? "all axes" : axis === "XY" ? "the X and Y axes" : `the ${axis} axis`;
  if (!confirm(`Home ${label}? Make sure the build chamber is clear before continuing.`)) return;
  send({ action: "home_axes", printer_id: printerId, axis });
}

function setPrinterFan(printerId, fan, speed) {
  send({ action: "set_fan", printer_id: printerId, fan, speed });
}

function setPrinterTemperature(printerId, target, inputId) {
  const input = document.getElementById(inputId);
  if (!input || input.value === "") return;
  const value = Number(input.value);
  if (!Number.isFinite(value) || !input.checkValidity()) {
    input.reportValidity();
    return;
  }
  if (value > 0 && !confirm(`Set the ${target} target to ${value} °C?`)) return;
  send({ action: "set_temperature", printer_id: printerId, target, value });
}

function syncEmptyState() {
  const empty = document.getElementById("empty-state");
  const grid  = document.getElementById("printer-grid");
  const has   = Object.keys(printers).length > 0;
  if (_activePrinterId) {
    empty.style.display = "none";
    grid.style.display = "none";
  } else {
    empty.style.display = has ? "none" : "";
    grid.style.display  = has ? ""     : "none";
  }
}

// ─── User actions ──────────────────────────────────────────────────────────────
const _lightPending = {}; // printerId → { on: bool, until: ms timestamp }

function getLightOn(printer) {
  const p = _lightPending[printer.id];
  if (p && Date.now() < p.until) return p.on;
  return !!printer.status?.LightStatus?.SecondLight;
}

function printerAction(id, action) {
  if (action === "light_on" || action === "light_off") {
    _lightPending[id] = { on: action === "light_on", until: Date.now() + 3000 };
    if (printers[id]) renderPrinter(printers[id]);
    if (_activePrinterId === id && printers[id]) renderPrinterDetail(printers[id]);
  }
  send({ action, printer_id: id });
}

function setSpeed(id, speed) {
  send({ action: "set_speed", printer_id: id, speed });
}

function confirmStop(id) {
  if (confirm("Stop the current print? This cannot be undone.")) {
    send({ action: "stop", printer_id: id });
  }
}

function removePrinter(id) {
  if (confirm("Remove this printer?")) {
    send({ action: "remove_printer", printer_id: id });
  }
}

function openPrinterSettings(id) {
  const p = printers[id];
  if (!p) return;
  _editingPrinterId = id;

  document.getElementById("input-name").value = p.name || "";
  document.getElementById("input-ip").value = p.ip || "";
  document.getElementById("input-ip").required = true;
  document.getElementById("input-name").required = true;
  inputType.value = p.printer_type || "cc1";
  inputType.style.display = "none";
  document.getElementById("input-type-readonly").style.display = "";
  const typeLabels = {
    cc1:       "CC1 – Centauri Carbon 1 (WebSocket/SDCP)",
    cc2:       "CC2 – Centauri Carbon 2 (MQTT)",
    prusa:     "Prusa – PrusaLink HTTP API",
    moonraker: "Klipper – Moonraker HTTP API",
  };
  document.getElementById("input-type-readonly").textContent = typeLabels[p.printer_type] || typeLabels.cc1;
  const needsKey = p.printer_type === "cc2" || p.printer_type === "prusa" || p.printer_type === "moonraker";
  const keyLabel = _printerCredentialLabel(p.printer_type);
  const keyHint  = p.printer_type === "moonraker"
    ? "API key (optional)"
    : p.printer_type === "prusa" ? "API key from PrusaLink settings" : "12345";
  inputAccessCode.value = "";
  inputAccessCode.placeholder = p.has_access_code ? "Leave blank to keep current" : keyHint;
  inputAccessCode.required = ["cc2", "prusa"].includes(p.printer_type) && !p.has_access_code;
  document.getElementById("label-access-code-text").textContent = keyLabel;
  labelAccessCode.style.display = needsKey ? "flex" : "none";

  document.getElementById("printer-panel-title").textContent = p.name || "Edit Printer";
  document.getElementById("printer-discover-section").style.display = "none";
  document.getElementById("printer-panel-divider").style.display = "none";
  document.getElementById("printer-form-label").textContent = "Printer details";
  document.getElementById("btn-modal-confirm").innerHTML = "Save Changes";
  document.getElementById("btn-remove-printer").style.display = "";

  openPrinters();
  document.getElementById("input-name").focus();
}

// ─── Sign out ─────────────────────────────────────────────────────────────────
async function signOut() {
  await fetch("/api/logout", { method: "POST" }).catch(() => {});
  location.replace("/login");
}
document.getElementById("btn-signout")?.addEventListener("click", signOut);
document.getElementById("btn-signout-nav")?.addEventListener("click", signOut);

const mobileMoreButton = document.getElementById("btn-mobile-more");
const mobileMoreMenu = document.getElementById("mobile-more-menu");
function closeMobileMore() {
  if (!mobileMoreMenu || !mobileMoreButton) return;
  mobileMoreMenu.hidden = true;
  mobileMoreButton.setAttribute("aria-expanded", "false");
}
mobileMoreButton?.addEventListener("click", event => {
  event.stopPropagation();
  const opening = mobileMoreMenu.hidden;
  mobileMoreMenu.hidden = !opening;
  mobileMoreButton.setAttribute("aria-expanded", String(opening));
  if (opening) mobileMoreMenu.querySelector('[role="menuitem"]')?.focus();
});
mobileMoreMenu?.addEventListener("keydown", event => {
  const items = [...mobileMoreMenu.querySelectorAll('[role="menuitem"]')];
  const index = items.indexOf(document.activeElement);
  if (event.key === "Escape") {
    event.preventDefault();
    closeMobileMore();
    mobileMoreButton?.focus();
  } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    items[(index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
  }
});
document.getElementById("btn-printers-nav")?.addEventListener("click", returnToPrinterGrid);
document.getElementById("btn-mobile-history")?.addEventListener("click", () => {
  closeMobileMore();
  mobileMoreButton?.focus();
  document.getElementById("btn-history")?.click();
});
document.getElementById("btn-mobile-settings")?.addEventListener("click", () => {
  closeMobileMore();
  mobileMoreButton?.focus();
  document.getElementById("btn-app-settings")?.click();
});
document.getElementById("btn-mobile-signout")?.addEventListener("click", signOut);
document.addEventListener("click", event => {
  if (!mobileMoreMenu?.hidden && !mobileMoreMenu.contains(event.target) && event.target !== mobileMoreButton) closeMobileMore();
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape") closeMobileMore();
});

const sidePanelFocus = new WeakMap();
const operatorDialogFocus = new WeakMap();
function openOperatorDialog(modal) {
  operatorDialogFocus.set(modal, document.activeElement);
  modal.classList.add("open");
  const title = modal.querySelector("h3");
  if (title) { title.tabIndex = -1; title.focus(); }
}
function closeOperatorDialog(modal) {
  if (!modal.classList.contains("open")) return;
  modal.classList.remove("open");
  const opener = operatorDialogFocus.get(modal);
  operatorDialogFocus.delete(modal);
  if (opener?.isConnected) opener.focus();
}
document.addEventListener("keydown", event => {
  const modal = [...document.querySelectorAll(".operator-dialog.open")].at(-1);
  if (!modal) return;
  if (event.key === "Escape") {
    event.preventDefault(); event.stopImmediatePropagation();
    closeOperatorDialog(modal);
  } else if (event.key === "Tab") {
    const items = [...modal.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex="0"]')]
      .filter(element => element.getClientRects().length);
    const first = items[0], last = items.at(-1);
    if (!first) return;
    if (event.shiftKey && (document.activeElement === first || !items.includes(document.activeElement))) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && (document.activeElement === last || !items.includes(document.activeElement))) {
      event.preventDefault(); first.focus();
    }
    event.stopImmediatePropagation();
  }
}, true);
let activeSidePanel = null;
const sidePanelFocusable = 'button:not([disabled]), a[href], input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

window.openSidePanel = function openSidePanel(panel, opener = document.activeElement) {
  if (!panel) return;
  if (activeSidePanel && activeSidePanel !== panel) window.closeSidePanel(activeSidePanel, false);
  sidePanelFocus.set(panel, opener instanceof HTMLElement ? opener : null);
  panel.inert = false;
  panel.setAttribute("aria-hidden", "false");
  panel.classList.add("open");
  document.getElementById("panel-backdrop")?.classList.add("open");
  document.body.classList.add("panel-open");
  activeSidePanel = panel;
  requestAnimationFrame(() => panel.querySelector(sidePanelFocusable)?.focus());
};

window.closeSidePanel = function closeSidePanel(panel, restoreFocus = true) {
  if (!panel) return;
  panel.classList.remove("open");
  panel.setAttribute("aria-hidden", "true");
  panel.inert = true;
  if (activeSidePanel === panel) activeSidePanel = null;
  if (!document.querySelector(".side-panel.open")) {
    document.getElementById("panel-backdrop")?.classList.remove("open");
    document.body.classList.remove("panel-open");
  }
  const opener = sidePanelFocus.get(panel);
  sidePanelFocus.delete(panel);
  if (restoreFocus && opener?.isConnected) opener.focus();
};

document.querySelectorAll(".side-panel").forEach(panel => {
  panel.inert = true;
  panel.setAttribute("aria-hidden", "true");
});

document.addEventListener("keydown", event => {
  if (event.key !== "Tab" || !activeSidePanel) return;
  const focusable = [...activeSidePanel.querySelectorAll(sidePanelFocusable)]
    .filter(element => element.getClientRects().length && getComputedStyle(element).visibility !== "hidden");
  if (!focusable.length) {
    event.preventDefault();
    activeSidePanel.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

// ─── App settings ─────────────────────────────────────────────────────────────
const _settingsModal          = document.getElementById("modal-app-settings");
const _settingsMenu           = document.getElementById("settings-menu");
const _settingsPwPage         = document.getElementById("settings-change-password");
const _settingsNotifPage      = document.getElementById("settings-notifications");
const _settingsPrintersPage   = document.getElementById("settings-printers");
const _settingsPrinterEditPage = document.getElementById("settings-printer-edit");
const _settingsPageReturnTargets = new WeakMap();
let _settingsCurrentPage = _settingsMenu;

function _settingsFeedback(message, state = "info") {
  const feedback = document.getElementById("settings-feedback");
  if (!feedback) return;
  feedback.hidden = !message;
  feedback.dataset.state = state;
  feedback.textContent = message;
}

const _allSettingsPages = () => [_settingsPwPage, _settingsNotifPage, _settingsPrintersPage, _settingsPrinterEditPage];

function _openSettings() {
  _allSettingsPages().forEach(p => p && (p.style.display = "none"));
  _settingsMenu.style.display = "";
  _settingsCurrentPage = _settingsMenu;
  const tog = document.getElementById("toggle-light-mode");
  if (tog) tog.checked = document.body.classList.contains("light-mode");
  if (_settingsModal) openOperatorDialog(_settingsModal);
  _settingsFeedback("");
  _settingsMenu.querySelector("h3")?.focus();
}
function _showSettingsPage(pageEl) {
  _settingsPageReturnTargets.set(pageEl, document.activeElement);
  _settingsMenu.style.display = "none";
  _allSettingsPages().forEach(p => p && (p.style.display = "none"));
  pageEl.style.display = "";
  _settingsCurrentPage = pageEl;
  pageEl.querySelector("h3")?.focus();
}
function _backToSettingsMenu() {
  const returnTarget = _settingsPageReturnTargets.get(_settingsCurrentPage);
  _allSettingsPages().forEach(p => p && (p.style.display = "none"));
  _settingsMenu.style.display = "";
  _settingsCurrentPage = _settingsMenu;
  if (returnTarget?.isConnected) returnTarget.focus();
  else _settingsMenu.querySelector("h3")?.focus();
}

function _renderSettingsPrinters() {
  const list = document.getElementById("settings-printer-list");
  if (!list) return;
  const items = Object.values(printers);
  if (!items.length) {
    list.innerHTML = '<p class="settings-printer-empty">No printers added yet.</p>';
    return;
  }
  list.innerHTML = items.map(p => `
    <div class="settings-printer-row">
      <div class="settings-printer-info">
        <span class="settings-printer-name">${escHtml(p.name)}</span>
        <span class="settings-printer-meta">${escHtml(p.ip)} &nbsp;·&nbsp; ${p.printer_type?.toUpperCase() || "CC1"}</span>
      </div>
      <button class="btn btn-secondary btn-sm" onclick="_settingsEditPrinter('${escAttr(p.id)}')">Edit</button>
    </div>
  `).join("");
}

let _settingsEditingPrinterId = null;

function _settingsEditPrinter(id) {
  _settingsEditingPrinterId = id || null;
  const p = id ? printers[id] : null;
  const isNew = !p;

  document.getElementById("settings-printer-edit-title").textContent = isNew ? "Add Printer" : "Edit Printer";
  document.getElementById("settings-edit-name").value = p?.name || "";
  document.getElementById("settings-edit-name").required = !isNew;
  document.getElementById("settings-edit-ip").value = p?.ip || "";
  document.getElementById("settings-edit-ip").required = true;

  const typeSelect = document.getElementById("settings-edit-type");
  const typeReadonly = document.getElementById("settings-edit-type-readonly");
  if (isNew) {
    typeSelect.value = "cc1";
    typeSelect.style.display = "";
    typeReadonly.style.display = "none";
  } else {
    typeSelect.value = p.printer_type || "cc1";
    typeSelect.style.display = "none";
    typeReadonly.style.display = "";
    const typeLabels = {
      cc1:       "CC1 – Centauri Carbon 1 (WebSocket/SDCP)",
      cc2:       "CC2 – Centauri Carbon 2 (MQTT)",
      prusa:     "PrusaLink (Experimental) – HTTP API",
      moonraker: "Klipper (Experimental) – Moonraker HTTP API",
    };
    typeReadonly.textContent = typeLabels[p.printer_type] || typeLabels.cc1;
  }

  const curType = p ? p.printer_type : typeSelect.value;
  const needsKey = curType === "cc2" || curType === "prusa" || curType === "moonraker";
  document.getElementById("settings-edit-access-code-label").style.display = needsKey ? "" : "none";
  document.getElementById("settings-edit-access-code-text").textContent = _printerCredentialLabel(curType);
  const ac = document.getElementById("settings-edit-access-code");
  ac.value = "";
  ac.placeholder = (!isNew && p?.has_access_code)
    ? "Leave blank to keep current"
    : curType === "moonraker" ? "API key (optional)"
    : curType === "prusa" ? "API key from PrusaLink settings" : "12345";
  ac.required = ["cc2", "prusa"].includes(curType) && (isNew || !p?.has_access_code);
  ac.removeAttribute("aria-invalid");
  document.getElementById("settings-edit-access-code-error").hidden = true;

  document.getElementById("btn-settings-printer-remove").style.display = isNew ? "none" : "";
  document.getElementById("btn-settings-printer-save").textContent = isNew ? "Add Printer" : "Save";

  _showSettingsPage(_settingsPrinterEditPage);
}

document.getElementById("btn-app-settings")?.addEventListener("click", _openSettings);
document.getElementById("btn-app-settings-cancel")?.addEventListener("click", () => closeOperatorDialog(_settingsModal));
_settingsModal?.addEventListener("click", e => {
  if (e.target === _settingsModal) closeOperatorDialog(_settingsModal);
});

// Light mode toggle
(function () {
  const toggle = document.getElementById("toggle-light-mode");
  if (!toggle) return;
  toggle.checked = document.body.classList.contains("light-mode");
  toggle.addEventListener("change", () => {
    document.body.classList.toggle("light-mode", toggle.checked);
    localStorage.setItem("theme", toggle.checked ? "light" : "dark");
  });
})();

// Printers sub-page
document.getElementById("btn-settings-goto-printers")?.addEventListener("click", () => {
  _renderSettingsPrinters();
  _showSettingsPage(_settingsPrintersPage);
});
document.getElementById("btn-settings-back-printers")?.addEventListener("click", _backToSettingsMenu);
document.getElementById("btn-settings-add-printer")?.addEventListener("click", () => {
  _settingsEditPrinter(null);
});
document.getElementById("btn-settings-scan-printers")?.addEventListener("click", () => {
  send({ action: "discover" });
  toast("Scanning for printers…");
});

// Printer edit sub-page
document.getElementById("btn-settings-back-printer-edit")?.addEventListener("click", () => {
  _renderSettingsPrinters();
  _showSettingsPage(_settingsPrintersPage);
});
document.getElementById("settings-edit-type")?.addEventListener("change", function () {
  const needsKey = this.value === "cc2" || this.value === "prusa" || this.value === "moonraker";
  document.getElementById("settings-edit-access-code-label").style.display = needsKey ? "" : "none";
  document.getElementById("settings-edit-access-code-text").textContent = _printerCredentialLabel(this.value);
  document.getElementById("settings-edit-access-code").placeholder =
    this.value === "moonraker" ? "API key (optional)"
    : this.value === "prusa" ? "API key from PrusaLink settings" : "12345";
  document.getElementById("settings-edit-access-code").required = ["cc2", "prusa"].includes(this.value)
    && (!_settingsEditingPrinterId || !printers[_settingsEditingPrinterId]?.has_access_code);
});
document.getElementById("btn-settings-printer-save")?.addEventListener("click", () => {
  const name = document.getElementById("settings-edit-name").value.trim();
  const ip   = document.getElementById("settings-edit-ip").value.trim();
  const access_code = document.getElementById("settings-edit-access-code").value.trim();
  const ipInput = document.getElementById("settings-edit-ip");
  const ipError = document.getElementById("settings-edit-ip-error");
  if (!ip) {
    ipInput.setAttribute("aria-invalid", "true");
    ipError.textContent = "Enter the printer IP address before saving.";
    ipError.hidden = false;
    ipInput.focus();
    _settingsFeedback("Printer was not saved. Fix the highlighted field.", "error");
    return;
  }
  ipInput.removeAttribute("aria-invalid");
  ipError.hidden = true;
  const credentialInput = document.getElementById("settings-edit-access-code");
  const credentialError = document.getElementById("settings-edit-access-code-error");
  if (credentialInput.required && !credentialInput.value.trim()) {
    credentialInput.setAttribute("aria-invalid", "true");
    credentialError.textContent = "Enter the credential required by this printer type.";
    credentialError.hidden = false;
    credentialInput.focus();
    _settingsFeedback("Printer was not saved. Fix the highlighted field.", "error");
    return;
  }
  credentialInput.removeAttribute("aria-invalid");
  credentialError.hidden = true;
  _settingsFeedback("");
  if (_settingsEditingPrinterId) {
    send({ action: "update_printer", printer_id: _settingsEditingPrinterId, name, ip, access_code });
  } else {
    const printer_type = document.getElementById("settings-edit-type").value;
    send({ action: "add_printer", ip, name: name || undefined, printer_type, access_code });
  }
  _renderSettingsPrinters();
  _showSettingsPage(_settingsPrintersPage);
});
document.getElementById("btn-settings-printer-remove")?.addEventListener("click", () => {
  if (!_settingsEditingPrinterId) return;
  const name = printers[_settingsEditingPrinterId]?.name || "this printer";
  if (!confirm(`Remove ${name}? This disconnects it and removes its saved printer settings. Existing Jobs and Prints are kept.`)) return;
  send({ action: "remove_printer", printer_id: _settingsEditingPrinterId });
  _renderSettingsPrinters();
  _showSettingsPage(_settingsPrintersPage);
});

// Password sub-page
document.getElementById("btn-settings-goto-password")?.addEventListener("click", () => {
  document.getElementById("settings-new-password").value = "";
  document.getElementById("settings-confirm-password").value = "";
  _settingsFeedback("");
  _showSettingsPage(_settingsPwPage);
});
document.getElementById("btn-settings-back")?.addEventListener("click", _backToSettingsMenu);
document.getElementById("btn-app-settings-save")?.addEventListener("click", async () => {
  const pw  = document.getElementById("settings-new-password").value;
  const pw2 = document.getElementById("settings-confirm-password").value;
  if (pw.length < 8)  { _settingsFeedback("Password must be at least 8 characters.", "error"); return; }
  if (pw !== pw2)     { _settingsFeedback("Passwords do not match.", "error"); return; }
  const button = document.getElementById("btn-app-settings-save");
  button.disabled = true;
  button.textContent = "Saving…";
  _settingsFeedback("Saving password…", "info");
  try {
    const r = await fetch("/api/change-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: pw }),
    });
    if (r.ok) {
      _settingsFeedback("Password updated.", "ok");
      document.getElementById("settings-new-password").value = "";
      document.getElementById("settings-confirm-password").value = "";
    } else {
      const d = await r.json().catch(() => ({}));
      _settingsFeedback(d.error || "Failed to update password. Check the connection and try again.", "error");
    }
  } catch (err) {
    _settingsFeedback(`Failed to update password: ${err.message}. Check the connection and try again.`, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Save";
  }
});

// ─── Push notifications ───────────────────────────────────────────────────────

function _urlBase64ToUint8Array(b64) {
  const pad = "=".repeat((4 - b64.length % 4) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}

async function _subscribePush() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return null;
  try {
    const reg = await _pushWorkerReady();
    const keyResp = await fetch("/api/push-public-key");
    if (!keyResp.ok) { console.warn("Push: could not get public key", keyResp.status); return null; }
    const { publicKey } = await keyResp.json();
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: _urlBase64ToUint8Array(publicKey),
    });
    const r = await fetch("/api/push-subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sub.toJSON()),
    });
    if (!r.ok) { console.warn("Push: server rejected subscription", r.status); return null; }
    return sub;
  } catch (e) {
    console.warn("Push subscribe failed:", e);
    return null;
  }
}

async function _unsubscribePush() {
  if (!("serviceWorker" in navigator)) return;
  try {
    const reg = await navigator.serviceWorker.getRegistration();
    if (!reg) return;
    const sub = await reg.pushManager.getSubscription();
    if (sub) {
      await fetch("/api/push-unsubscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ endpoint: sub.endpoint }),
      });
      await sub.unsubscribe();
    }
  } catch (e) {
    console.warn("Push unsubscribe failed:", e);
  }
}

async function _pushWorkerReady() {
  let timeout;
  try {
    return await Promise.race([
      navigator.serviceWorker.ready,
      new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error("Push registration timed out")), 10000); }),
    ]);
  } finally { clearTimeout(timeout); }
}

// Notifications sub-page UI
async function _populateNotifForm() {
  const save = document.getElementById("btn-notif-save");
  const retry = document.getElementById("btn-notif-load-retry");
  const fields = [..._settingsNotifPage.querySelectorAll("input")];
  save.disabled = true;
  fields.forEach(field => field.disabled = true);
  retry.hidden = true;
  _settingsNotifPage.setAttribute("aria-busy", "true");
  _settingsFeedback("Loading notification settings…", "info");
  let s;
  try {
    const resp = await fetch("/api/notification-settings", { signal: AbortSignal.timeout(15000) });
    if (!resp.ok) throw new Error(`Request failed (${resp.status})`);
    s = await resp.json();
    if (!s || typeof s !== "object" || Array.isArray(s)) throw new Error("Invalid settings response");
  } catch (error) {
    _settingsFeedback(`Could not load notification settings: ${error.message}. Check the connection and retry before saving.`, "error");
    retry.hidden = false;
    _settingsNotifPage.setAttribute("aria-busy", "false");
    return;
  }
  const set = (id, val) => {
    const el = document.getElementById(id);
    if (el) el[typeof val === "boolean" ? "checked" : "value"] = val;
  };
  set("notif-finished-on",               s.finished?.enabled        ?? false);
  set("notif-layer-on",                  s.layer?.enabled           ?? false);
  set("notif-layer-number",              s.layer?.layer             ?? 1);
  set("notif-nozzle-idle-on",            s.nozzle_idle?.enabled     ?? false);
  set("notif-nozzle-idle-threshold",     s.nozzle_idle?.threshold   ?? 50);
  set("notif-nozzle-printing-on",        s.nozzle_printing?.enabled ?? false);
  set("notif-nozzle-printing-threshold", s.nozzle_printing?.threshold ?? 260);
  set("notif-spool-low-on",              s.spool_low?.enabled       ?? false);
  set("notif-spool-low-threshold",       s.spool_low?.threshold     ?? 100);
  _syncNotifParams();
  save.disabled = false;
  fields.forEach(field => field.disabled = false);
  _settingsNotifPage.setAttribute("aria-busy", "false");
  _settingsFeedback("Notification settings loaded.", "info");
}
document.getElementById("btn-notif-load-retry")?.addEventListener("click", _populateNotifForm);

function _syncNotifParams() {
  const show = (paramId, checkId) => {
    const param = document.getElementById(paramId);
    const cb    = document.getElementById(checkId);
    if (param && cb) param.style.display = cb.checked ? "" : "none";
  };
  show("notif-layer-param",           "notif-layer-on");
  show("notif-nozzle-idle-param",     "notif-nozzle-idle-on");
  show("notif-nozzle-printing-param", "notif-nozzle-printing-on");
  show("notif-spool-low-param",       "notif-spool-low-on");
}

["notif-layer-on","notif-nozzle-idle-on","notif-nozzle-printing-on","notif-spool-low-on"].forEach(id =>
  document.getElementById(id)?.addEventListener("change", _syncNotifParams));

document.getElementById("btn-settings-goto-notifications")?.addEventListener("click", () => {
  _populateNotifForm();
  _showSettingsPage(_settingsNotifPage);
});
document.getElementById("btn-settings-back-notif")?.addEventListener("click", _backToSettingsMenu);
document.getElementById("btn-notif-save")?.addEventListener("click", async () => {
  const gb = id => document.getElementById(id)?.checked ?? false;
  const gv = id => parseFloat(document.getElementById(id)?.value) || 0;
  const s = {
    finished:        { enabled: gb("notif-finished-on") },
    layer:           { enabled: gb("notif-layer-on"),           layer:     gv("notif-layer-number") },
    nozzle_idle:     { enabled: gb("notif-nozzle-idle-on"),     threshold: gv("notif-nozzle-idle-threshold") },
    nozzle_printing: { enabled: gb("notif-nozzle-printing-on"), threshold: gv("notif-nozzle-printing-threshold") },
    spool_low:       { enabled: gb("notif-spool-low-on"),       threshold: gv("notif-spool-low-threshold") },
  };
  const anyEnabled = Object.values(s).some(v => v.enabled);
  const thresholdIds = ["notif-layer-number", "notif-nozzle-idle-threshold", "notif-nozzle-printing-threshold", "notif-spool-low-threshold"];
  const invalidThreshold = thresholdIds.map(id => document.getElementById(id)).find(input =>
    input.offsetParent !== null && !input.checkValidity());
  if (invalidThreshold) {
    _settingsFeedback(`${invalidThreshold.labels?.[0]?.textContent || "Notification threshold"} is outside its allowed range.`, "error");
    invalidThreshold.focus();
    return;
  }
  const button = document.getElementById("btn-notif-save");
  button.disabled = true;
  button.textContent = "Saving…";
  _settingsFeedback("Saving notification settings…", "info");
  try {
    if (anyEnabled) {
      if (!("Notification" in window)) {
        _settingsFeedback("This browser does not support notifications. Disable notification rules or use a supported browser.", "error");
        return;
      }
      const perm = await Notification.requestPermission();
      if (perm !== "granted") {
        _settingsFeedback("Notification permission was not granted. Allow it in browser settings, then save again.", "error");
        return;
      }
      const sub = await _subscribePush();
      if (!sub) {
        _settingsFeedback("Settings can be saved, but push registration failed. Notifications may not arrive while Spooler is closed; check the browser service worker and Spooler connection.", "warn");
      }
    } else {
      await _unsubscribePush();
    }
    const r = await fetch("/api/notification-settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(s),
    });
    if (r.ok) {
      _settingsFeedback("Notification settings saved.", "ok");
      toast("Notification settings saved");
      _backToSettingsMenu();
    } else {
      const d = await r.json().catch(() => ({}));
      _settingsFeedback(d.error || "Failed to save notification settings. Check the connection and retry.", "error");
    }
  } catch (err) {
    _settingsFeedback(`Could not save notification settings: ${err.message}. Check the connection and retry.`, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Save";
  }
});

document.getElementById("btn-notif-test")?.addEventListener("click", async () => {
  if (!("Notification" in window)) { _settingsFeedback("This browser does not support notifications.", "error"); return; }
  const perm = await Notification.requestPermission();
  if (perm !== "granted") { _settingsFeedback("Allow notifications in browser settings first, then send the test again.", "error"); return; }
  const sub = await _subscribePush();
  if (!sub) { _settingsFeedback("Could not register push subscription. Check browser permission and Spooler connectivity.", "error"); return; }
  try {
    const r = await fetch("/api/push-test", { method: "POST" });
    if (r.ok) {
      _settingsFeedback("Test notification sent. Check the device where this browser is signed in.", "ok");
    toast("Test notification sent — check your phone");
    } else {
      const d = await r.json().catch(() => ({}));
      _settingsFeedback(d.error || "Failed to send test notification. Check the connection and subscription.", "error");
    }
  } catch (err) {
    _settingsFeedback(`Failed to send test notification: ${err.message}. Check the connection and retry.`, "error");
  }
});

// ─── Printers panel ────────────────────────────────────────────────────────────
const printersPanel = document.getElementById("panel-printers");

const inputType       = document.getElementById("input-type");
const labelAccessCode = document.getElementById("label-access-code");
const inputAccessCode = document.getElementById("input-access-code");

let _editingPrinterId = null;

function _printerCredentialLabel(type) {
  if (type === "cc2") return "MQTT Password (required)";
  if (type === "prusa") return "PrusaLink API Key (required)";
  return "Moonraker API Key (optional)";
}


function resetPrinterForm() {
  _editingPrinterId = null;
  document.getElementById("input-ip").value = "";
  document.getElementById("input-ip").required = true;
  document.getElementById("input-ip").removeAttribute("aria-invalid");
  document.getElementById("input-ip-error").hidden = true;
  document.getElementById("input-name").value = "";
  document.getElementById("input-name").required = false;
  inputType.value = "cc1";
  inputType.style.display = "";
  document.getElementById("input-type-readonly").style.display = "none";
  inputAccessCode.value = "";
  inputAccessCode.required = false;
  inputAccessCode.removeAttribute("aria-invalid");
  document.getElementById("input-access-code-error").hidden = true;
  inputAccessCode.placeholder = "12345";
  labelAccessCode.style.display = "none";

  document.getElementById("printer-panel-title").textContent = "Add Printer";
  document.getElementById("printer-discover-section").style.display = "";
  document.getElementById("printer-panel-divider").style.display = "";
  document.getElementById("printer-form-label").textContent = "Add manually";
  document.getElementById("btn-modal-confirm").innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
    <path d="M12 5v14M5 12h14"/>
  </svg> Add Printer`;
  document.getElementById("btn-remove-printer").style.display = "none";
}

inputType.addEventListener("change", () => {
  const needsKey = inputType.value === "cc2" || inputType.value === "prusa" || inputType.value === "moonraker";
  labelAccessCode.style.display = needsKey ? "flex" : "none";
  document.getElementById("label-access-code-text").textContent =
    inputType.value === "cc2" ? "MQTT Password (required)"
    : inputType.value === "prusa" ? "PrusaLink API Key (required)" : "Moonraker API Key (optional)";
  document.getElementById("input-access-code").placeholder =
    inputType.value === "moonraker" ? "API key (optional)"
    : inputType.value === "prusa" ? "API key from PrusaLink settings" : "12345";
  inputAccessCode.required = ["cc2", "prusa"].includes(inputType.value) && !_editingPrinterId;
});
document.getElementById("input-ip")?.addEventListener("input", event => {
  if (!event.target.value.trim()) return;
  event.target.removeAttribute("aria-invalid");
  document.getElementById("input-ip-error").hidden = true;
});
document.getElementById("settings-edit-ip")?.addEventListener("input", event => {
  if (!event.target.value.trim()) return;
  event.target.removeAttribute("aria-invalid");
  document.getElementById("settings-edit-ip-error").hidden = true;
});

const openPrinters = () => {
  window.openSidePanel(printersPanel, document.activeElement);
};
const closePrinters = () => {
  window.closeSidePanel(printersPanel);
  resetPrinterForm();
};

document.getElementById("btn-add")?.addEventListener("click", () => {
  resetPrinterForm();
  openPrinters();
});
const headerMoreButton = document.getElementById("btn-header-more");
const headerMoreMenu = document.getElementById("header-more-menu");
function closeHeaderMore({ restoreFocus = false } = {}) {
  if (!headerMoreMenu || headerMoreMenu.hidden) return;
  headerMoreMenu.hidden = true;
  headerMoreButton?.setAttribute("aria-expanded", "false");
  if (restoreFocus) headerMoreButton?.focus();
}
headerMoreButton?.addEventListener("click", () => {
  const opening = headerMoreMenu.hidden;
  headerMoreMenu.hidden = !opening;
  headerMoreButton.setAttribute("aria-expanded", String(opening));
  if (opening) headerMoreMenu.querySelector('[role="menuitem"]')?.focus();
});
headerMoreMenu?.addEventListener("click", event => {
  const action = event.target.closest("[role=menuitem]");
  if (!action) return;
  closeHeaderMore({ restoreFocus: true });
  if (action.id === "btn-header-discover") document.getElementById("btn-discover")?.click();
  if (action.id === "btn-header-add-printer") document.getElementById("btn-add")?.click();
});
headerMoreMenu?.addEventListener("keydown", event => {
  const items = [...headerMoreMenu.querySelectorAll('[role="menuitem"]')];
  const index = items.indexOf(document.activeElement);
  if (event.key === "Escape") {
    event.preventDefault();
    closeHeaderMore({ restoreFocus: true });
  } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    items[(index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
  }
});
document.addEventListener("click", event => {
  if (headerMoreMenu && !headerMoreMenu.hidden && !event.target.closest(".header-more-actions")) closeHeaderMore();
});
document.getElementById("btn-printers-close").addEventListener("click", closePrinters);

function _triggerDiscover(btn) {
  send({ action: "discover" });
  btn.disabled = true;
  btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
    <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
  </svg> Scanning…`;
  setTimeout(() => {
    btn.disabled = false;
    btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
    </svg> Scan for Printers`;
  }, 7000);
}
document.getElementById("btn-discover")?.addEventListener("click", e => {
  openPrinters();
  _triggerDiscover(e.currentTarget);
});

document.getElementById("btn-discover-panel")?.addEventListener("click", e => _triggerDiscover(e.currentTarget));

document.getElementById("btn-modal-confirm").addEventListener("click", () => {
  const ipInput = document.getElementById("input-ip");
  const ip          = ipInput.value.trim();
  const name        = document.getElementById("input-name").value.trim();
  const access_code = inputAccessCode.value.trim();
  const ipError = document.getElementById("input-ip-error");
  if (!ip) {
    ipInput.setAttribute("aria-invalid", "true");
    ipError.textContent = "Enter the printer IP address before saving.";
    ipError.hidden = false;
    ipInput.focus();
    return;
  }
  ipInput.removeAttribute("aria-invalid");
  ipError.hidden = true;
  const credentialRequired = ["cc2", "prusa"].includes(inputType.value) && !_editingPrinterId;
  const credentialError = document.getElementById("input-access-code-error");
  if (credentialRequired && !access_code) {
    inputAccessCode.setAttribute("aria-invalid", "true");
    credentialError.textContent = "Enter the access credential required by this printer type.";
    credentialError.hidden = false;
    inputAccessCode.focus();
    return;
  }
  inputAccessCode.removeAttribute("aria-invalid");
  credentialError.hidden = true;
  if (_editingPrinterId) {
    if (!name) {
      document.getElementById("input-name").setAttribute("aria-invalid", "true");
      toast("Enter a printer name", true);
      document.getElementById("input-name").focus();
      return;
    }
    send({ action: "update_printer", printer_id: _editingPrinterId, name, ip, access_code });
  } else {
    send({ action: "add_printer", ip, name: name || undefined, printer_type: inputType.value, access_code });
  }
  closePrinters();
});

document.getElementById("btn-remove-printer").addEventListener("click", () => {
  if (!_editingPrinterId) return;
  const name = printers[_editingPrinterId]?.name || "this printer";
  if (confirm(`Remove ${name}? This disconnects it and removes its saved printer settings. Existing Jobs and Prints are kept.`)) {
    send({ action: "remove_printer", printer_id: _editingPrinterId });
    closePrinters();
  }
});

// ─── File browser ─────────────────────────────────────────────────────────────
function openFileBrowser(printerId) {
  _currentFilePrinterId = printerId;
  _fileBrowserOpener = document.activeElement;
  const p = printers[printerId];
  document.getElementById("files-modal-title").textContent = `Files – ${p?.name || printerId}`;
  document.getElementById("modal-files").classList.add("open");
  document.getElementById("files-modal-title").focus();
  requestPrinterFiles(printerId);
}

function closeFileBrowser() {
  clearTimeout(_fileListTimer);
  _fileListTimer = null;
  document.getElementById("modal-files").classList.remove("open");
  _currentFilePrinterId = null;
  if (_fileBrowserOpener?.isConnected) _fileBrowserOpener.focus();
  _fileBrowserOpener = null;
}

function requestPrinterFiles(printerId = _currentFilePrinterId) {
  if (!printerId || printerId !== _currentFilePrinterId) return;
  clearTimeout(_fileListTimer);
  const printer = printers[printerId];
  if (!printer?.connected) {
    renderFileList([], "Printer offline. Reconnect it, then retry to load files.");
    return;
  }
  document.getElementById("files-list").replaceChildren();
  const loading = document.getElementById("files-loading");
  loading.textContent = `Loading files from ${printer.name || "printer"}…`;
  loading.style.display = "";
  send({ action: "list_files", printer_id: printerId });
  _fileListTimer = setTimeout(() => {
    if (_currentFilePrinterId === printerId) renderFileList([], "File list request timed out. Check the printer connection and retry.");
  }, 10000);
}

function renderFileList(files, error) {
  clearTimeout(_fileListTimer);
  _fileListTimer = null;
  _currentFileList = files || [];
  document.getElementById("files-loading").style.display = "none";
  const list = document.getElementById("files-list");

  if (error) {
    const offline = /offline|not connected/i.test(error);
    const timedOut = /timed out|timeout/i.test(error);
    const title = offline ? "Printer offline" : timedOut ? "File list timed out" : "Could not load printer files";
    list.innerHTML = `<div class="files-state ${offline ? "offline" : "error"}"><strong>${title}</strong><p role="status">${escHtml(error)}</p><button class="btn btn-secondary" type="button" data-action="retry-files">Retry</button></div>`;
    list.querySelector('[data-action="retry-files"]').addEventListener("click", () => requestPrinterFiles());
    return;
  }
  if (!files || files.length === 0) {
    list.innerHTML = '<p class="files-empty" role="status">This printer has no files available.</p>';
    return;
  }

  const ul = document.createElement("ul");
  ul.className = "files-list-items";

  for (const f of files) {
    const li = document.createElement("li");
    li.className = "file-item" + (f.is_dir ? " file-item-dir" : "");

    const nameEl = document.createElement("div");
    nameEl.className = "file-name";
    nameEl.textContent = f.name || f.path || "";
    nameEl.title = f.path || "";
    li.appendChild(nameEl);

    const metaRow = document.createElement("div");
    metaRow.className = "file-meta-row";

    const sizeEl = document.createElement("span");
    sizeEl.className = "file-size";
    sizeEl.textContent = f.is_dir ? "" : formatFileSize(f.size);
    metaRow.appendChild(sizeEl);

    if (!f.is_dir) {
      const filePath = f.path;
      const fileName = f.name || filePath.split("/").pop() || filePath;

      const actions = document.createElement("div");
      actions.className = "file-actions";

      const btn = document.createElement("button");
      btn.className = "btn btn-primary btn-sm";
      btn.textContent = "Print";
      btn.addEventListener("click", () => {
        const meta = _currentFileList.find(x => x.path === filePath) || {};
        openPrintOpts(filePath, fileName, meta);
      });
      actions.appendChild(btn);

      if (printers[_currentFilePrinterId]?.printer_type === "cc1") {
        const queueBtn = document.createElement("button");
        queueBtn.className = "btn btn-secondary btn-sm";
        queueBtn.textContent = "Queue";
        queueBtn.title = "Add this file to the print queue";
        queueBtn.addEventListener("click", () => {
          const meta = _currentFileList.find(x => x.path === filePath) || { path: filePath };
          queuePrinterFile(_currentFilePrinterId, meta);
        });
        actions.appendChild(queueBtn);
      }

      const delBtn = document.createElement("button");
      delBtn.className = "btn btn-danger btn-sm";
      delBtn.textContent = "Delete";
      delBtn.addEventListener("click", () => {
        if (confirm(`Delete "${fileName}"?`)) {
          send({ action: "delete_file", printer_id: _currentFilePrinterId, filename: filePath });
          toast(`Deleting: ${fileName}`);
          setTimeout(() => {
            document.getElementById("files-list").innerHTML = "";
            document.getElementById("files-loading").style.display = "";
            send({ action: "list_files", printer_id: _currentFilePrinterId });
          }, 600);
        }
      });
      actions.appendChild(delBtn);
      metaRow.appendChild(actions);
    }

    li.appendChild(metaRow);
    ul.appendChild(li);
  }

  list.innerHTML = "";
  list.appendChild(ul);
}

function formatFileSize(bytes) {
  if (!bytes) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1073741824) return `${(bytes / 1048576).toFixed(1)} MB`;
  return `${(bytes / 1073741824).toFixed(2)} GB`;
}

// ─── Print Options modal (CC1 + CC2) ──────────────────────────────────────────
let _printOptsFile = null;
let _printOptsName = null;

let _printOptsFilename = null; // bare filename used as key for file_info matching

function _applyFileInfo(msg) {
  // Only update if the modal is still open for the same printer+file
  if (!_printOptsFilename || msg.printer_id !== _currentFilePrinterId) return;
  if (msg.filename !== _printOptsFilename) return;
  const modal = document.getElementById("modal-print-opts");
  if (!modal.classList.contains("open")) return;

  // Thumbnail
  if (msg.thumbnail_b64) {
    const thumbImg  = document.getElementById("print-opts-thumb");
    const thumbWrap = document.getElementById("print-opts-thumb-wrap");
    thumbImg.src = `data:image/png;base64,${msg.thumbnail_b64}`;
    thumbImg.style.display = "";
    thumbWrap.style.display = "";
  }

  // Stats (fill in if present and currently showing --)
  const statsEl = document.getElementById("print-opts-stats");
  if (msg.print_time || msg.layers || msg.filament_g) {
    statsEl.style.display = "";
    if (msg.print_time)
      document.getElementById("print-opts-stat-time-val").textContent = formatTimeLong(msg.print_time);
    if (msg.filament_g != null)
      document.getElementById("print-opts-stat-filament-val").textContent =
        `${parseFloat(msg.filament_g).toFixed(2)}g`;
    if (msg.layers != null)
      document.getElementById("print-opts-stat-layers-val").textContent = msg.layers;
  }
}

function openPrintOpts(filePath, fileName, meta = {}) {
  _printOptsFile = filePath;
  _printOptsName = fileName;
  _printOptsFilename = fileName; // used for file_info matching
  const printerType  = printers[_currentFilePrinterId]?.printer_type;
  const isCC2        = printerType === "cc2";
  const isPrusa      = printerType === "prusa";
  const isMoonraker  = printerType === "moonraker";
  const isHttpPrinter = isPrusa || isMoonraker;

  // Stats bar
  const statsEl = document.getElementById("print-opts-stats");
  const hasStats = meta.print_time || meta.layers || meta.filament_g || meta.filament_mm;
  statsEl.style.display = hasStats ? "" : "none";
  if (hasStats) {
    document.getElementById("print-opts-stat-time-val").textContent =
      meta.print_time ? formatTimeLong(meta.print_time) : "--";

    let filamentText = "--";
    if (meta.filament_g != null)
      filamentText = `${parseFloat(meta.filament_g).toFixed(2)}g`;
    else if (meta.filament_mm != null)
      filamentText = `${Math.round(meta.filament_mm)}mm`;
    document.getElementById("print-opts-stat-filament-val").textContent = filamentText;

    document.getElementById("print-opts-stat-layers-val").textContent =
      meta.layers != null ? meta.layers : "--";
  }

  document.getElementById("print-opts-filename").textContent = fileName;
  document.getElementById("print-opt-timelapse").checked = false;
  document.getElementById("print-opt-leveling").checked  = false;
  _setPrintPlate(0);


  // CC1 and CC2 support leveling/timelapse/plate; Prusa/Moonraker handle these natively
  document.getElementById("print-opts-cc1-only").style.display = isHttpPrinter ? "none" : "";

  // Load thumbnail
  const thumbImg  = document.getElementById("print-opts-thumb");
  const thumbWrap = document.getElementById("print-opts-thumb-wrap");
  thumbImg.style.display = "none";
  thumbImg.src = "";
  if (isHttpPrinter) {
    thumbWrap.style.display = "none";
  } else if (isCC2) {
    // CC2: thumbnail arrives async via WS file_info message; request it now
    thumbWrap.style.display = "none";
    send({ action: "get_file_info", printer_id: _currentFilePrinterId, filename: fileName });
  } else {
    const bare = fileName.endsWith(".png") ? fileName : fileName + ".png";
    thumbImg.onload  = () => { thumbImg.style.display = ""; };
    thumbImg.onerror = () => { thumbImg.style.display = "none"; thumbWrap.style.display = "none"; };
    thumbWrap.style.display = "";
    thumbImg.src = `/api/thumbnail/${encodeURIComponent(_currentFilePrinterId)}/${encodeURIComponent(bare)}`;
  }

  document.getElementById("modal-print-opts").classList.add("open");
}

function closePrintOpts() {
  document.getElementById("modal-print-opts").classList.remove("open");
  _printOptsFile = null;
  _printOptsName = null;
  _printOptsFilename = null;
}

function _setPrintPlate(plateId) {
  document.getElementById("print-plate-textured").className =
    "btn print-plate-btn " + (plateId === 0 ? "btn-primary" : "btn-secondary");
  document.getElementById("print-plate-smooth").className =
    "btn print-plate-btn " + (plateId === 1 ? "btn-primary" : "btn-secondary");
  document.getElementById("print-plate-textured").dataset.selected = plateId === 0 ? "1" : "";
  document.getElementById("print-plate-smooth").dataset.selected   = plateId === 1 ? "1" : "";
}

document.getElementById("print-plate-textured").addEventListener("click", () => _setPrintPlate(0));
document.getElementById("print-plate-smooth").addEventListener("click",   () => _setPrintPlate(1));

document.getElementById("btn-print-opts-cancel").addEventListener("click", closePrintOpts);
document.getElementById("modal-print-opts").addEventListener("click", e => {
  if (e.target === document.getElementById("modal-print-opts")) closePrintOpts();
});

document.getElementById("btn-print-opts-send").addEventListener("click", () => {
  if (!_printOptsFile) return;
  const opts = {
    timelapse:    document.getElementById("print-opt-timelapse").checked,
    leveling:     document.getElementById("print-opt-leveling").checked,
    smooth_plate: document.getElementById("print-plate-smooth").dataset.selected === "1",
  };
  send({
    action:     "start_print",
    printer_id: _currentFilePrinterId,
    filename:   _printOptsFile,
    print_opts: opts,
  });
  closePrintOpts();
  closeFileBrowser();
  toast(`Starting: ${_printOptsName}`);
});

document.getElementById("btn-files-close").addEventListener("click", closeFileBrowser);
document.getElementById("btn-files-refresh").addEventListener("click", () => {
  requestPrinterFiles();
});
document.getElementById("modal-files").addEventListener("click", (e) => {
  if (e.target === document.getElementById("modal-files")) closeFileBrowser();
});

// Printed OK · Bed cleared: pre-fills the outcome from what the printer reported (only a completed print is
// confirmed here; anything else is corrected in History), then releases the next job if auto-start is on.
async function confirmBedCleared(printerId) {
  try {
    // A print that ended and was never answered gets the outcome prompt (Printed OK · Bed cleared, or Failed…).
    if ((await OutcomePrompt.awaiting()).some(a => a.printer_id === printerId && a.outcome_unconfirmed)) {
      OutcomePrompt.open(printerId);
      return;
    }
    const body = {};
    const latest = await (await fetch(`/api/prints?printer_id=${encodeURIComponent(printerId)}&limit=1`)).json();
    const last = latest.prints && latest.prints[0];
    if (last && last.outcome === "completed" && last.outcome_unconfirmed) {
      body.print_id = last.id;
      body.outcome = "completed";
    }
    const resp = await fetch(`/api/printers/${encodeURIComponent(printerId)}/bed-cleared`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const result = await resp.json();
    if (!resp.ok) { toast(result.error || "Could not confirm", true); return; }
    if (result.started) toast(`Bed cleared. Starting ${result.started.file_name}`);
    else if (result.start_error) toast(`Bed cleared, but nothing started: ${result.start_error}`, true);
    else toast("Bed cleared. Printer is ready");
  } catch (e) {
    toast("Could not confirm Bed cleared", true);
  }
}

// ─── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg, isError = false) {
  const area = document.getElementById("toast-area");
  const el = document.createElement("div");
  el.className = "toast" + (isError ? " error" : "");
  el.textContent = msg;
  area.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

function toastAction(msg, btnLabel, onClick, duration = 8000) {
  const area = document.getElementById("toast-area");
  const el = document.createElement("div");
  el.className = "toast toast-action";
  const span = document.createElement("span");
  span.textContent = msg;
  const btn = document.createElement("button");
  btn.className = "toast-btn";
  btn.textContent = btnLabel;
  btn.addEventListener("click", () => { el.remove(); onClick(); });
  el.appendChild(span);
  el.appendChild(btn);
  area.appendChild(el);
  setTimeout(() => el.remove(), duration);
}

// ─── Helpers ───────────────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}
function escAttr(s) {
  return String(s ?? "").replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}

// ─── History panel ────────────────────────────────────────────────────────────
// Print history (loadHistory, openHistory, closeHistory) lives in history.js.
const historyBackdrop = document.getElementById("panel-backdrop");
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeHistory(); closeSpools(); closePrinters(); closeFileBrowser(); closePrintOpts(); } });

// ─── Spoolman / Spools ────────────────────────────────────────────────────────
let _spoolLoadState = "loading";
let _spoolLoadMessage = "Loading Spool inventory…";
let _spoolFetchSequence = 0;
async function fetchSpools() {
  const sequence = ++_spoolFetchSequence;
  _spoolLoadState = "loading";
  _spoolLoadMessage = "Loading Spool inventory…";
  renderSpoolPanel();
  try {
    const r = await fetch(`${SPOOLMAN_URL}/spool`, { signal: AbortSignal.timeout(15000) });
    if (!r.ok) throw new Error(`Spoolman returned ${r.status}`);
    const inventory = await r.json();
    if (!Array.isArray(inventory)) throw new Error("Invalid inventory response");
    if (sequence !== _spoolFetchSequence) return;
    spools = inventory;
    _spoolLoadState = "ready";
    _spoolLoadMessage = `${spools.length} Spools · Inventory checked ${new Date().toLocaleTimeString()}.`;
    Object.values(printers).forEach(p => renderPrinter(p));
  } catch (error) {
    if (sequence !== _spoolFetchSequence) return;
    _spoolLoadState = "error";
    _spoolLoadMessage = `Could not load Spoolman inventory${error.name === "TimeoutError" ? " before the request timed out" : ""}. Check the Spoolman connection and retry.${spools.length ? " Showing previously loaded inventory; assignments may be outdated." : ""}`;
  }
  renderSpoolPanel();
  if (document.getElementById("modal-spool-picker").classList.contains("open")) renderPickerList(currentPickerPrinterId);
  return _spoolLoadState === "ready";
}

document.getElementById("btn-spools-retry").addEventListener("click", fetchSpools);

function renderSpoolPanel() {
  const list  = document.getElementById("spools-list");
  const empty = document.getElementById("spools-empty");
  const status = document.getElementById("spools-status");
  status.hidden = false;
  status.dataset.state = _spoolLoadState;
  status.textContent = _spoolLoadMessage;
  document.getElementById("btn-spools-retry").hidden = _spoolLoadState !== "error";
  if (!spools.length) {
    list.innerHTML = "";
    empty.style.display = _spoolLoadState === "ready" ? "" : "none";
    return;
  }
  empty.style.display = "none";
  list.innerHTML = spools.map(s => {
    const pct        = spoolPct(s);
    const remaining  = spoolRemaining(s);
    const total      = spoolTotal(s);
    const color      = spoolColorHex(s);
    const material   = s.filament?.material || "?";
    const vendor     = s.filament?.vendor?.name || "";
    const colorName  = s.filament?.name || "";
    const assignedTo = spoolAssignedTo(s);
    const printerName = assignedTo ? (printerByLocation(assignedTo)?.name || assignedTo) : null;
    const isEmpty    = s.remaining_weight != null && remaining === 0;
    const isLow      = s.remaining_weight != null && !isEmpty && total > 0 && pct < 10;
    const barColor   = isEmpty ? "var(--red)" : isLow ? "var(--yellow)" : null;

    return `<div class="spool-card${isEmpty ? " spool-empty" : isLow ? " spool-low" : ""}">
      <div class="spool-card-swatch" style="background:${escAttr(color)}"></div>
      <div class="spool-card-body">
        <div class="spool-card-top">
          <div class="spool-card-vendor">${escHtml(vendor || "Unknown brand")}</div>
          <span class="spool-material-badge">${escHtml(material)}</span>
        </div>
        <div class="spool-card-colorname">${escHtml(colorName || "—")}</div>
        <div class="spool-weight-row">
          <span>${spoolQuantityText(s)} · ${total} g initial</span>
          <span class="spool-weight-pct">${pct}%${isEmpty ? ' <span class="spool-tag empty">Empty</span>' : isLow ? ' <span class="spool-tag low">Low</span>' : ""}</span>
        </div>
        <div class="spool-bar-wrap">
          <div class="spool-bar-fill" style="width:${pct}%${barColor ? ";background:" + barColor : ""}"></div>
        </div>
        <div class="spool-card-footer">
          ${printerName ? `<span class="spool-printer-badge">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>
            </svg>
            ${escHtml(printerName)}
          </span>` : "<span></span>"}
          <button class="btn btn-sm btn-secondary" onclick="deleteSpool('${escAttr(s.id)}')">Remove</button>
        </div>
      </div>
    </div>`;
  }).join("");
}

function openSpoolPicker(printerId) {
  currentPickerPrinterId = printerId;
  currentPickerTrayId    = null;
  const printer = printers[printerId];
  document.getElementById("spool-picker-title").textContent =
    `Assign Spool – ${printer?.name || printerId}`;
  renderPickerList(printerId);
  openOperatorDialog(document.getElementById("modal-spool-picker"));
}

function openTrayPicker(printerId, trayId) {
  currentPickerPrinterId = printerId;
  currentPickerTrayId    = trayId;
  const printer = printers[printerId];
  document.getElementById("spool-picker-title").textContent =
    `Link Spool – ${printer?.name || printerId} · Slot ${trayId + 1}`;
  renderPickerList(printerId);
  openOperatorDialog(document.getElementById("modal-spool-picker"));
}

function renderPickerList(printerId) {
  const list   = document.getElementById("spool-picker-list");
  const isTray = currentPickerTrayId != null;
  const feedback = document.getElementById("spool-picker-feedback");
  feedback.hidden = _spoolLoadState === "ready";
  feedback.textContent = _spoolLoadMessage;

  // Classify each spool relative to the current context
  const currentLinked = isTray
    ? (trayMap[printerId] || {})[String(currentPickerTrayId)]
    : spools.find(s => printerByLocation(spoolAssignedTo(s))?.id === printerId)?.id;

  function spoolRow(s, onClickFn, isSelected) {
    const loc          = spoolAssignedTo(s);
    const locPrinter   = printerByLocation(loc);
    const otherPrinter = loc && locPrinter?.id !== printerId ? locPrinter : null;
    const activePrinter = getSpoolActivePrinter(s.id);  // printer currently printing with this spool
    const inUse        = activePrinter != null;
    const pct          = spoolPct(s);
    const badge        = inUse
      ? `<span class="spool-pick-badge in-use">Active · ${escHtml(activePrinter.name)}</span>`
      : otherPrinter
        ? `<span class="spool-pick-badge elsewhere">On · ${escHtml(otherPrinter.name)}</span>`
        : "";
    if (inUse) {
      return `<div class="spool-pick-item spool-pick-blocked" title="Currently loaded on ${escAttr(activePrinter.name)} — unload filament before reassigning">
        <div class="spool-dot" style="background:${escAttr(spoolColorHex(s))};opacity:.4"></div>
        <div class="spool-pick-info">
          <div class="spool-pick-name" style="opacity:.5">${escHtml(spoolName(s))}</div>
          <div class="spool-pick-meta">${escHtml(s.filament?.material || "")} · ${spoolQuantityText(s)} ${badge} · Unload filament before reassigning.</div>
        </div>
      </div>`;
    }
    return `<button type="button" aria-pressed="${isSelected}" class="spool-pick-item${isSelected ? " selected" : ""}${otherPrinter ? " spool-pick-elsewhere" : ""}"
                 ${_spoolLoadState !== "ready" ? "disabled" : ""} onclick="${onClickFn}">
      <div class="spool-dot" style="background:${escAttr(spoolColorHex(s))}"></div>
      <div class="spool-pick-info">
        <div class="spool-pick-name">${escHtml(spoolName(s))}</div>
        <div class="spool-pick-meta">${escHtml(s.filament?.material || "")} · ${spoolQuantityText(s)} ${badge}${s.remaining_weight === 0 ? " · Empty; replace before printing" : ""}</div>
      </div>
      ${isSelected ? '<span class="spool-check">✓</span>' : ""}
    </button>`;
  }

  const noneLabel  = isTray ? "None – unlink" : "None – unassign";
  const noneClick  = isTray
    ? `linkTray('${escAttr(printerId)}', ${currentPickerTrayId}, null)`
    : `assignSpool('${escAttr(printerId)}', null)`;
  const noneSelected = currentLinked == null;

  const rows = spools.map(s => {
    const isSelected = s.id === currentLinked || (!isTray && spoolAssignedTo(s) === printerId);
    const click = isTray
      ? `linkTray('${escAttr(printerId)}', ${currentPickerTrayId}, ${s.id})`
      : `assignSpool('${escAttr(printerId)}', '${escAttr(s.id)}')`;
    return spoolRow(s, click, isSelected);
  });

  list.innerHTML = `
    ${_spoolLoadState === "error" ? '<button class="btn btn-secondary" onclick="fetchSpools()">Retry inventory</button>' : ""}
    <button type="button" aria-pressed="${noneSelected}" class="spool-pick-item${noneSelected ? " selected" : ""}" ${_spoolLoadState !== "ready" ? "disabled" : ""} onclick="${noneClick}">
      <div class="spool-dot" style="background:var(--border)"></div>
      <div class="spool-pick-info"><div class="spool-pick-name">${noneLabel}</div></div>
      ${noneSelected ? '<span class="spool-check">✓</span>' : ""}
    </button>
    ${rows.join("")}
  `;
}

async function assignSpool(printerId, spoolId) {
  const loc = printerLocation(printerId);
  const feedback = document.getElementById("spool-picker-feedback");
  feedback.hidden = false;
  feedback.textContent = "Saving Spool assignment…";
  document.querySelectorAll("#spool-picker-list button").forEach(button => button.disabled = true);
  try {
    // Unassign existing spool on this printer if it's different
    const cur = spools.find(s => printerByLocation(spoolAssignedTo(s))?.id === printerId);
    if (cur && String(cur.id) !== String(spoolId)) {
      const response = await fetch(`${SPOOLMAN_URL}/spool/${cur.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ location: "" }),
      });
      if (!response.ok) throw new Error("Could not unassign the previous Spool");
    }
    if (spoolId) {
      const response = await fetch(`${SPOOLMAN_URL}/spool/${spoolId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ location: loc }),
      });
      if (!response.ok) throw new Error("Could not assign the selected Spool");
    }
    closeOperatorDialog(document.getElementById("modal-spool-picker"));
    await fetchSpools();
    toast(spoolId ? "Spool assigned" : "Spool unassigned");
  } catch (e) {
    await fetchSpools();
    feedback.hidden = false;
    feedback.textContent = `${e.message}. Check the inventory shown here before retrying; a previous assignment may already have been removed.`;
    feedback.dataset.state = "error";
    toast("Failed to assign spool", true);
  }
}

function linkTray(printerId, trayId, spoolId) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({
      action:     "link_tray",
      printer_id: printerId,
      tray_id:    trayId,
      spool_id:   spoolId,
    }));
  }
  closeOperatorDialog(document.getElementById("modal-spool-picker"));
  toast(spoolId != null ? `Slot ${trayId + 1} linked` : `Slot ${trayId + 1} unlinked`);
}

async function deleteSpool(spoolId) {
  if (!confirm("Remove this spool?")) return;
  try {
    // Find the filament/vendor IDs before deleting the spool
    const spool = spools.find(s => s.id == spoolId);
    const filamentId = spool?.filament?.id;
    const vendorId   = spool?.filament?.vendor?.id;

    const r = await fetch(`${SPOOLMAN_URL}/spool/${spoolId}`, { method: "DELETE" });
    if (!r.ok) throw new Error(`${r.status}`);

    // Clean up the filament (may fail if shared – that's fine)
    if (filamentId) {
      await fetch(`${SPOOLMAN_URL}/filament/${filamentId}`, { method: "DELETE" });
    }
    // Clean up the vendor if it has no remaining filaments
    if (vendorId) {
      const vf = await fetch(`${SPOOLMAN_URL}/filament?vendor_id=${vendorId}`);
      if (vf.ok && (await vf.json()).length === 0) {
        await fetch(`${SPOOLMAN_URL}/vendor/${vendorId}`, { method: "DELETE" });
      }
    }

    await fetchSpools();
    toast("Spool removed");
  } catch (e) {
    toast("Failed to remove spool: " + e.message, true);
  }
}

// Spools side panel
const spoolsPanel = document.getElementById("panel-spools");
const btnSpools   = document.getElementById("btn-spools");

const openSpools = () => {
  window.openSidePanel(spoolsPanel, document.activeElement);
  btnSpools.classList.add("active");
  fetchSpools();
};
const closeSpools = () => {
  window.closeSidePanel(spoolsPanel);
  btnSpools.classList.remove("active");
};

btnSpools.addEventListener("click", openSpools);
document.getElementById("btn-spools-close").addEventListener("click", closeSpools);

document.getElementById("btn-import-filaments").addEventListener("click", async () => {
  const btn = document.getElementById("btn-import-filaments");
  btn.disabled = true;
  btn.textContent = "Importing…";
  try {
    const r = await fetch("/api/import-filaments", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ brand: "ELEGOO" }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.status);
    if (d.created > 0) {
      alert(`✓ Imported ${d.created} ELEGOO filaments into Spoolman.\n\nYou can now create spools from these in Spoolman UI or when adding a spool here.`);
    } else {
      alert(`All ${d.total} ELEGOO filaments are already in Spoolman (${d.skipped} skipped).`);
    }
  } catch (e) {
    alert("Import failed: " + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
    </svg> Import Elegoo`;
  }
});

// Close all panels on backdrop click
historyBackdrop.addEventListener("click", () => { closeHistory(); closeSpools(); closePrinters(); });

// Add Spool modal
const addSpoolModal = document.getElementById("modal-add-spool");

// "Other…" toggle for brand and material dropdowns
document.getElementById("spool-input-brand").addEventListener("change", (e) => {
  document.getElementById("spool-input-brand-other").style.display =
    e.target.value === "__other__" ? "" : "none";
});
document.getElementById("spool-input-material").addEventListener("change", (e) => {
  document.getElementById("spool-input-material-other").style.display =
    e.target.value === "__other__" ? "" : "none";
  document.getElementById("spool-input-material-other").required = e.target.value === "__other__";
  // Auto-fill density when a known material is selected
  const density = _materialDensityMap[e.target.value];
  if (density) document.getElementById("spool-input-density").value = density;
});
document.getElementById("spool-input-hex").addEventListener("input", e => {
  document.getElementById("spool-color-hex-value").textContent = `Hex color ${e.target.value.toUpperCase()}`;
});

function getSpoolSelectValue(selectId, otherId) {
  const sel = document.getElementById(selectId).value;
  if (sel === "__other__") return document.getElementById(otherId).value.trim();
  return sel;
}

document.getElementById("btn-add-spool").addEventListener("click", () => {
  loadFilamentMeta();
  openOperatorDialog(addSpoolModal);
});
document.getElementById("btn-spool-catalogue-retry").addEventListener("click", () => loadFilamentMeta());
document.getElementById("btn-add-spool-cancel").addEventListener("click", () => {
  closeOperatorDialog(addSpoolModal);
});
addSpoolModal.addEventListener("click", (e) => {
  if (e.target === addSpoolModal) closeOperatorDialog(addSpoolModal);
});

document.getElementById("btn-add-spool-confirm").addEventListener("click", async () => {
  const brand    = getSpoolSelectValue("spool-input-brand", "spool-input-brand-other");
  const material = getSpoolSelectValue("spool-input-material", "spool-input-material-other");
  const color    = document.getElementById("spool-input-color").value.trim();
  const hex      = (document.getElementById("spool-input-hex").value || "#888888").replace(/^#/, "");
  const weight   = parseFloat(document.getElementById("spool-input-weight").value);
  const diameter = parseFloat(document.getElementById("spool-input-diameter").value);
  const density  = parseFloat(document.getElementById("spool-input-density").value);
  const feedback = document.getElementById("spool-form-feedback");

  if (!material) {
    feedback.hidden = false;
    feedback.dataset.state = "error";
    feedback.textContent = "Choose a material or enter one under Other.";
    (document.getElementById("spool-input-material").value === "__other__"
      ? document.getElementById("spool-input-material-other") : document.getElementById("spool-input-material")).focus();
    return;
  }
  if (!Number.isFinite(weight) || weight < 1) {
    feedback.hidden = false;
    feedback.dataset.state = "error";
    feedback.textContent = "Enter a total weight of at least 1 g.";
    document.getElementById("spool-input-weight").focus();
    return;
  }
  if (!Number.isFinite(diameter) || diameter <= 0 || !Number.isFinite(density) || density <= 0) {
    feedback.hidden = false;
    feedback.dataset.state = "error";
    feedback.textContent = "Diameter and density must both be greater than zero.";
    (!Number.isFinite(diameter) || diameter <= 0 ? document.getElementById("spool-input-diameter") : document.getElementById("spool-input-density")).focus();
    return;
  }
  feedback.hidden = true;

  try {
    // Step 1: find or create vendor
    let vendorId = null;
    if (brand) {
      const vr = await fetch(`${SPOOLMAN_URL}/vendor?name=${encodeURIComponent(brand)}`);
      if (!vr.ok) throw new Error("Failed to fetch vendors");
      const vendors = await vr.json();
      if (vendors.length > 0) {
        vendorId = vendors[0].id;
      } else {
        const cv = await fetch(`${SPOOLMAN_URL}/vendor`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: brand }),
        });
        if (!cv.ok) throw new Error(await cv.text());
        vendorId = (await cv.json()).id;
      }
    }

    // Step 2: create filament
    const filamentBody = { material, weight, color_hex: hex, density, diameter };
    if (color) filamentBody.name = color;
    if (vendorId) filamentBody.vendor_id = vendorId;
    const fr = await fetch(`${SPOOLMAN_URL}/filament`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(filamentBody),
    });
    if (!fr.ok) throw new Error(await fr.text());
    const filament = await fr.json();

    // Step 3: create spool
    const sr = await fetch(`${SPOOLMAN_URL}/spool`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filament_id: filament.id, initial_weight: weight }),
    });
    if (!sr.ok) throw new Error(await sr.text());

    closeOperatorDialog(addSpoolModal);
    // Reset form
    document.getElementById("spool-input-ean").value = "";
    document.getElementById("spool-input-brand").value = "";
    document.getElementById("spool-input-material").value = "";
    document.getElementById("spool-input-brand-other").style.display = "none";
    document.getElementById("spool-input-brand-other").value = "";
    document.getElementById("spool-input-material-other").style.display = "none";
    document.getElementById("spool-input-material-other").value = "";
    document.getElementById("spool-input-color").value = "";
    document.getElementById("spool-input-hex").value = "#888888";
    document.getElementById("spool-input-weight").value = "1000";
    document.getElementById("spool-input-diameter").value = "1.75";
    document.getElementById("spool-input-density").value = "1.24";
    document.getElementById("spool-color-hex-value").textContent = "Hex color #888888";
    feedback.hidden = true;
    await fetchSpools();
    toast("Spool added");
  } catch (e) {
    feedback.hidden = false;
    feedback.dataset.state = "error";
    feedback.textContent = `Could not save Spool: ${e.message}. Your entries are still here. Check the Spoolman connection, then retry.`;
    toast("Failed to add spool: " + e.message, true);
  }
});

// ─── EAN lookup + barcode scanner ─────────────────────────────────────────────

async function lookupEan(ean) {
  if (!ean) return;
  document.getElementById("spool-input-ean").value = ean;
  try {
    const r = await fetch(`/api/lookup-ean?ean=${encodeURIComponent(ean)}`);
    if (r.status === 404) { toast(`EAN ${ean} not found in database`, true); return; }
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();

    const brand    = d.manufacturer || "";
    const material = d.material     || "";
    const color    = d.color_name   || "";
    const hex      = d.color_hex ? "#" + d.color_hex.replace(/^#/, "") : "#888888";
    const weight   = d.weight ?? 1000;

    // Brand dropdown
    const bSel = document.getElementById("spool-input-brand");
    const bOther = document.getElementById("spool-input-brand-other");
    if ([...bSel.options].some(o => o.value === brand)) {
      bSel.value = brand; bOther.style.display = "none";
    } else if (brand) {
      bSel.value = "__other__"; bOther.value = brand; bOther.style.display = "";
    }

    // Material dropdown
    const mSel = document.getElementById("spool-input-material");
    const mOther = document.getElementById("spool-input-material-other");
    if ([...mSel.options].some(o => o.value === material)) {
      mSel.value = material; mOther.style.display = "none";
    } else if (material) {
      mSel.value = "__other__"; mOther.value = material; mOther.style.display = "";
    }

    document.getElementById("spool-input-color").value  = color;
    document.getElementById("spool-input-hex").value    = hex;
    document.getElementById("spool-input-weight").value = weight;

    toast(`Found: ${d.name || [brand, material, color].filter(Boolean).join(" ")}`);
  } catch (e) {
    toast("Lookup failed: " + e.message, true);
  }
}

// EAN field: lookup on Enter
document.getElementById("spool-input-ean").addEventListener("keydown", (e) => {
  if (e.key === "Enter") lookupEan(e.target.value.trim());
});
// Lookup on paste after short delay (scanner keyboards send paste then Enter)
document.getElementById("spool-input-ean").addEventListener("input", (e) => {
  const v = e.target.value.trim();
  if (v.length >= 8 && /^\d+$/.test(v)) {
    clearTimeout(e.target._eanTimer);
    e.target._eanTimer = setTimeout(() => lookupEan(v), 300);
  }
});

// Camera scanner
let _liveScanner = null;

function _setScannerError(msg, opts = {}) {
  const { sub, photo = false } = opts;
  document.getElementById("scanner-region").style.display = "none";
  document.getElementById("scanner-hint").style.display = "none";
  document.getElementById("scanner-error-msg").textContent = msg;
  if (sub !== undefined) document.getElementById("scanner-error-sub").textContent = sub;
  document.getElementById("scanner-error").style.display = "flex";
  document.getElementById("btn-scanner-photo").style.display = photo ? "" : "none";
}

function _resetScannerError() {
  document.getElementById("scanner-region").style.display = "";
  document.getElementById("scanner-hint").style.display = "";
  document.getElementById("scanner-error").style.display = "none";
  document.getElementById("scanner-error-sub").textContent = "Enter the EAN code manually instead.";
  document.getElementById("btn-scanner-photo").style.display = "none";
}

// The barcode library comes from a CDN. It loads when the scanner is first used, never at startup, so a slow or
// blocked CDN only affects barcode scanning.
let _qrLibPromise = null;
function loadQrLib() {
  if (window.Html5Qrcode) return Promise.resolve();
  if (!_qrLibPromise) {
    _qrLibPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js";
      const timer = setTimeout(() => reject(new Error("The barcode scanner couldn't be loaded")), 8000);
      script.onload = () => { clearTimeout(timer); resolve(); };
      script.onerror = () => { clearTimeout(timer); reject(new Error("The barcode scanner couldn't be loaded")); };
      document.head.appendChild(script);
    }).catch(err => { _qrLibPromise = null; throw err; });
  }
  return _qrLibPromise;
}

async function openScanner() {
  _resetScannerError();
  document.getElementById("modal-scanner").classList.add("open");

  if (!window.isSecureContext) {
    _setScannerError("Live scanner requires HTTPS", {
      sub: "Tap 'Take Photo' to scan a barcode with your camera, or enter the code manually.",
      photo: true
    });
    return;
  }

  // Guard against start() hanging indefinitely (e.g. PC with no camera attached)
  let timedOut = false;
  const giveUpTimer = setTimeout(() => {
    timedOut = true;
    const s = _liveScanner;
    _liveScanner = null;
    s?.stop().catch(() => {}).finally(() => { try { s.clear(); } catch {} });
    _setScannerError("No camera found on this device");
  }, 5000);

  try {
    await loadQrLib();
    _liveScanner = new Html5Qrcode("scanner-region", { verbose: false });
    await _liveScanner.start(
      { facingMode: "environment" },
      { fps: 10, qrbox: { width: 280, height: 100 } },
      (text) => { clearTimeout(giveUpTimer); closeScanner(); lookupEan(text); },
      () => {}
    );
    clearTimeout(giveUpTimer);
  } catch (e) {
    clearTimeout(giveUpTimer);
    if (timedOut) return;
    const s = _liveScanner;
    _liveScanner = null;
    s?.stop().catch(() => {}).finally(() => { try { s.clear(); } catch {} });
    _setScannerError(e.name === "NotFoundError" ? "No camera found on this device" : "Camera not available");
  }
}

function closeScanner() {
  document.getElementById("modal-scanner").classList.remove("open");
  if (_liveScanner) {
    const s = _liveScanner;
    _liveScanner = null;
    s.stop()
      .catch(() => {})
      .finally(() => { try { s.clear(); } catch {} });
  }
}

// File input: decode image from camera photo
document.getElementById("barcode-file-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  try {
    await loadQrLib();
    const reader = new Html5Qrcode("barcode-scan-canvas", { verbose: false });
    const text = await reader.scanFile(file, false);
    lookupEan(text);
  } catch {
    toast("No barcode found in image – try again", true);
  }
});

document.getElementById("btn-scan-ean").addEventListener("click", openScanner);
document.getElementById("btn-scanner-cancel").addEventListener("click", closeScanner);
document.getElementById("btn-scanner-photo").addEventListener("click", () => {
  closeScanner();
  document.getElementById("barcode-file-input").click();
});
document.getElementById("modal-scanner").addEventListener("click", (e) => {
  if (e.target === document.getElementById("modal-scanner")) closeScanner();
});

// Spool picker cancel
document.getElementById("btn-spool-picker-cancel").addEventListener("click", () => {
  closeOperatorDialog(document.getElementById("modal-spool-picker"));
});
document.getElementById("modal-spool-picker").addEventListener("click", (e) => {
  if (e.target === document.getElementById("modal-spool-picker"))
    closeOperatorDialog(document.getElementById("modal-spool-picker"));
});

// ─── Changelog ────────────────────────────────────────────────────────────────
let _changelog = [];

async function loadChangelog() {
  try {
    const r = await fetch("/changelog.json?v=" + Date.now());
    const data = await r.json();
    if (!Array.isArray(data) || data.length === 0) return;
    _changelog = data;
    const badge = document.getElementById("version-badge");
    if (badge) badge.textContent = "v" + _changelog[0].version;
  } catch (_) {}
}

function openChangelog() {
  const body = document.getElementById("changelog-body");
  if (!body) return;
  body.innerHTML = _changelog.map(entry => `
    <div class="cl-entry">
      <div class="cl-entry-header">
        <span class="cl-version">v${entry.version}</span>
        <span class="cl-date">${entry.date}</span>
      </div>
      <ul class="cl-list">
        ${entry.changes.map(c => `<li>${escHtml(c)}</li>`).join("")}
      </ul>
    </div>`).join("");
  document.getElementById("modal-changelog")?.classList.add("open");
}

document.getElementById("version-badge")?.addEventListener("click", openChangelog);
document.getElementById("btn-changelog-close")?.addEventListener("click", () =>
  document.getElementById("modal-changelog")?.classList.remove("open"));
document.getElementById("modal-changelog")?.addEventListener("click", e => {
  if (e.target.id === "modal-changelog")
    e.target.classList.remove("open");
});

// ─── Boot ──────────────────────────────────────────────────────────────────────
fetch("/api/auth-status")
  .then(r => r.json())
  .then(({ spoolman_url }) => {
    if (spoolman_url) document.getElementById("btn-spoolman-ui").href = spoolman_url;
  })
  .catch(() => {});
loadChangelog();
connect();

// On startup: if notifications are enabled but the subscription was cleared by the
// browser (e.g. after cache purge), silently re-subscribe so notifications keep working.
(async () => {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return;
  try {
    const resp = await fetch("/api/notification-settings");
    if (!resp.ok) return;
    const s = await resp.json();
    if (!s || !Object.values(s).some(v => v?.enabled)) return;
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (!sub) await _subscribePush();
  } catch (_) {}
})();
