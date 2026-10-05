/* Spooler – Quick print modal: a model (or a G-code file) goes from a file to a queued Job in one dialog.
   Loaded after app.js (uses its globals: printers, getPrintStatus, toast, toastAction, openQueue) and before import.js.

   Entry points (window.QuickPrint):
     QuickPrint.pick({ printerId })            open the file chooser, then the modal (the header and printer-card buttons)
     QuickPrint.open({ file, printerId })      upload a File as a hidden quick Project and open the modal on it
     QuickPrint.open({ projectId, printerId }) open the modal on an existing quick Project (reprint with changes)
     QuickPrint.refresh()                      reload intake, preview and estimate after the Project changed (lay flat, rotate, scale fix)
     QuickPrint.close()  QuickPrint.isOpen()  QuickPrint.current()   -> { projectId, intake, project, choices, phase } or null
   Events on document (detail is { projectId, ... }):
     quickprint:open, quickprint:intake (after every intake load: add placement buttons or notice actions here),
     quickprint:routed (a multi-object or multi-plate 3MF became a normal Project), quickprint:queued, quickprint:close
   Slots left empty in the markup for later tickets, found with document.querySelector inside the modal:
     [data-qp-slot="placement"]      under the badges, left column (lay flat, rotate)
     [data-qp-slot="notice-actions"] beside the blocked/scale notice (scale fix, Open as project)
     [data-qp-slot="more"]           in the options row (More settings)
     [data-qp-slot="more-panel"]     below the options, for a panel that opens inside the modal */
"use strict";

(() => {
  const MODEL = /\.(stl|obj|3mf|gcode)$/i;
  const LAYER_FALLBACK = [0.08, 0.12, 0.16, 0.2, 0.24, 0.28, 0.32];
  const SIDES = { textured: "Textured PEI (Side A)", smooth: "Smooth PEI (Side B)" };
  const POLL_MS = 900;
  const OPTION_KEYS = ["layer_height", "infill", "walls", "supports", "brim", "brim_width"];

  let S = null;           // the open session, or null
  let picker = null;      // the hidden file input
  let pickerPrinter = null;

  const esc = text => String(text ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const say = (text, error = false) => (typeof toast === "function" ? toast(text, error) : console[error ? "error" : "log"](text));
  const emit = (name, detail = {}) => document.dispatchEvent(new CustomEvent(`quickprint:${name}`, { detail: { projectId: S?.id ?? null, ...detail } }));
  const $ = selector => S?.root.querySelector(selector);

  function duration(seconds) {
    const minutes = Math.max(1, Math.round(seconds / 60));
    const h = Math.floor(minutes / 60), m = minutes % 60;
    return h ? `${h} h ${m} min` : `${m} min`;
  }
  const mm = n => `${Math.round(n * 10) / 10}`.replace(/\.0$/, "");

  async function api(method, path, body) {
    const response = await fetch(path, {
      method, headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if (response.status === 401) { location.replace("/login"); throw new Error("Signed out"); }
    let data = {};
    try { data = await response.json(); } catch (_) { /* not JSON */ }
    if (!response.ok) {
      const error = new Error(data.error || `Request failed (HTTP ${response.status})`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  // ─── Printers (live from app.js) ────────────────────────────────────────────
  function printerList() {
    const all = typeof printers === "object" && printers ? Object.values(printers) : [];
    return all.map(p => {
      const status = typeof getPrintStatus === "function" ? getPrintStatus(p) : (p.connected ? "idle" : "offline");
      const unsupported = p.printer_type && p.printer_type !== "cc1";
      const state = !p.connected ? "offline" : ["idle", "complete", "cancelled"].includes(status) ? "idle" : "busy";
      return { id: p.id, name: p.name, state, status, usable: p.connected && !unsupported, unsupported };
    });
  }
  const printerLabel = p => `${p.name}: ${p.unsupported ? "not supported yet" : p.state === "busy" ? `busy (${p.status})` : p.state}`;

  // ─── Opening ────────────────────────────────────────────────────────────────
  function pick({ printerId = null } = {}) {
    if (S) { say("A Quick print is already open", true); return; }
    if (!picker) {
      picker = document.createElement("input");
      picker.type = "file";
      picker.accept = ".stl,.obj,.3mf,.gcode";
      picker.hidden = true;
      picker.addEventListener("change", () => {
        const file = picker.files[0];
        picker.value = "";
        if (file) open({ file, printerId: pickerPrinter });
      });
      document.body.appendChild(picker);
    }
    pickerPrinter = printerId;
    picker.click();
  }

  async function open({ file = null, projectId = null, printerId = null } = {}) {
    if (S) { say("Finish or close the current Quick print first", true); return; }
    if (file && !MODEL.test(file.name)) { say("Choose an STL, OBJ or 3MF model, or a G-code file", true); return; }
    S = {
      id: projectId, file, root: null, phase: file ? "upload" : "check", project: null, intake: null, bundles: [], spools: [], spoolError: null,
      layerHeights: LAYER_FALLBACK, choices: { printer: printerId, preset: "", spool: "", bed: "textured", leveling: false, timelapse: false },
      values: {}, dirty: new Set(), ack: false, error: null, review: null, timers: [], preview: null, xhr: null, uploadPct: 0,
      formBuilt: false, estimate: null, closed: false,
    };
    const session = S;
    buildShell();
    emit("open", { printerId });
    try {
      if (file) await upload(file);
      if (session.closed || session.phase === "routed") return;
      await Promise.all([loadProject(), loadChoices(), loadLists()]);
      if (session.closed) return;
      await waitForIntake();
    } catch (err) {
      if (S === session && !session.closed) fail(err.message);
    }
  }

  function upload(file) {
    return new Promise((resolve, reject) => {
      const session = S;
      const xhr = new XMLHttpRequest();
      session.xhr = xhr;
      xhr.open("POST", `/api/projects/upload?${new URLSearchParams({ filename: file.name, scale: "1", up: "z", quick: "1" })}`);
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.upload.onprogress = e => { if (e.lengthComputable) { session.uploadPct = Math.round(e.loaded / e.total * 100); renderStatus(); } };
      xhr.onload = () => {
        let body = {};
        try { body = JSON.parse(xhr.responseText); } catch (_) { /* not JSON */ }
        if (xhr.status === 401) { location.replace("/login"); return; }
        if (session.closed) { resolve(); return; }
        if (xhr.status !== 200 || !body.id) { reject(new Error(body.error || `The upload failed (HTTP ${xhr.status})`)); return; }
        session.id = body.id;
        if (body.routed || body.quick === false) {   // a multi-object or multi-plate 3MF: a normal Project, not a quick print
          const detail = { project: body, routed: body.routed || { to: "project" } };
          session.phase = "routed";
          session.routed = detail.routed;
          emit("routed", detail);
          renderStatus();
          resolve();
          return;
        }
        resolve();
      };
      xhr.onerror = () => reject(new Error("The upload failed: the server couldn't be reached"));
      xhr.onabort = () => resolve();
      session.phase = "upload";
      renderStatus();
      xhr.send(file);
    });
  }

  async function loadProject() {
    const project = await api("GET", `/api/projects/${S.id}`);
    if (S) S.project = project;
    return project;
  }

  async function loadChoices(preset) {
    const info = await api("GET", `/api/projects/${S.id}/quick-options${preset ? `?preset=${encodeURIComponent(preset)}` : ""}`);
    if (!S) return;
    if (info.layer_heights?.length) S.layerHeights = info.layer_heights;
    for (const key of OPTION_KEYS) if (!S.dirty.has(key) && info.options[key] !== undefined) S.values[key] = info.options[key];
    if (!S.dirty.has("brim_width") && info.options.brim_width === undefined && S.values.brim_width === undefined) S.values.brim_width = 5;
    if (!preset) {
      const c = S.choices;
      c.preset = info.preset || "";
      c.bed = info.bed || "textured";
      c.spool = info.spool_id ?? "";
      c.leveling = !!info.leveling;
      c.timelapse = !!info.timelapse;
      if (!c.printer) c.printer = info.printer_id || null;
    }
    S.optionsInfo = info;
  }

  async function loadLists() {
    const [bundles, spools] = await Promise.allSettled([api("GET", "/api/slicer"), api("GET", "/api/slicer/spools")]);
    if (!S) return;
    S.bundles = bundles.status === "fulfilled" ? bundles.value.bundles || [] : [];
    if (spools.status === "fulfilled") S.spools = spools.value.spools || [];
    else S.spoolError = spools.reason?.message || "Spoolman isn't reachable";
  }

  // ─── Intake ─────────────────────────────────────────────────────────────────
  async function waitForIntake() {
    const session = S;
    for (;;) {
      const intake = await api("GET", `/api/projects/${session.id}/intake`);
      if (S !== session) return;
      session.intake = intake;
      if (intake.state !== "analysing") break;
      session.phase = "check";
      renderStatus();
      await new Promise(resolve => session.timers.push(setTimeout(resolve, POLL_MS)));
      if (S !== session) return;
    }
    if (session.intake.state === "failed" || session.intake.state === "none") { fail(session.intake.reason || "The file couldn't be read"); return; }
    enterEdit();
  }

  /** Reload intake and the project (so the preview, badges, notice and estimate follow a change made elsewhere). */
  async function refresh() {
    if (!S || !S.id) return null;
    const session = S;
    const [intake, project] = await Promise.all([api("GET", `/api/projects/${session.id}/intake`), api("GET", `/api/projects/${session.id}`)]);
    if (S !== session) return null;
    session.intake = intake;
    session.project = project;
    if (session.phase === "edit") renderIntake();
    return intake;
  }

  function fail(message) {
    if (!S) return;
    S.phase = "error";
    S.error = message;
    renderStatus();
  }

  // ─── Shell and phases ───────────────────────────────────────────────────────
  function buildShell() {
    const root = document.createElement("div");
    root.className = "qp-scrim";
    root.innerHTML = `<div class="qp-card" role="dialog" aria-modal="true" aria-labelledby="qp-title" tabindex="-1">
      <div class="qp-head"><h2 id="qp-title">Quick print</h2><button type="button" class="qp-x" data-qp-act="close" aria-label="Close">&times;</button></div>
      <div class="qp-body" data-qp="body"></div>
      <div class="qp-foot" data-qp="foot"></div>
    </div>`;
    document.body.appendChild(root);
    S.root = root;
    S.previouslyFocused = document.activeElement;
    root.addEventListener("click", onClick);
    root.addEventListener("change", onChange);
    root.addEventListener("input", onInput);
    root.addEventListener("keydown", trapTab);
    document.addEventListener("keydown", onEscape);
    renderStatus();
    root.querySelector(".qp-card").focus();
  }

  function onEscape(e) { if (e.key === "Escape" && S) { e.preventDefault(); close(); } }
  function trapTab(e) {
    if (e.key !== "Tab") return;
    const items = [...S.root.querySelectorAll("button, select, input, a[href], [tabindex]:not([tabindex='-1'])")].filter(el => !el.disabled && el.offsetParent !== null);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && (document.activeElement === first || document.activeElement === S.root.querySelector(".qp-card"))) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  function title() { return S.intake?.file || S.file?.name || S.project?.name || ""; }

  function setHead() {
    const name = title();
    $("#qp-title").textContent = name ? `Quick print: ${name}` : "Quick print";
  }

  /** The non-form phases: uploading, checking, slicing, review, queueing and errors replace the body. */
  function renderStatus() {
    if (!S || !S.root) return;
    const body = $('[data-qp="body"]'), foot = $('[data-qp="foot"]');
    setHead();
    S.formBuilt = false;
    stopPrinterWatch();
    disposePreview();
    const phase = S.phase;
    if (phase === "upload") {
      body.innerHTML = `<div class="qp-status"><div>Uploading ${esc(S.file?.name)}…</div><div class="qp-bar"><i style="width:${S.uploadPct}%"></i></div></div>`;
      foot.innerHTML = `<span class="qp-sp"></span><button type="button" class="btn btn-secondary" data-qp-act="close">Cancel</button>`;
    } else if (phase === "check") {
      body.innerHTML = `<div class="qp-status"><div>Checking the model: reading it, repairing small holes and measuring it…</div><div class="qp-bar qp-bar-busy"><i></i></div></div>`;
      foot.innerHTML = `<span class="qp-sp"></span><button type="button" class="btn btn-secondary" data-qp-act="close">Close</button>`;
    } else if (phase === "slicing") {
      const entry = S.project?.slices?.["1"] || {};
      const stage = { preparing: "Preparing", slicing: "Slicing", "post-processing": "Post-processing", analysing: "Analysing" }[entry.stage] || (entry.state === "queued" ? "Waiting for the slicer" : "Slicing");
      body.innerHTML = `<div class="qp-status"><div><strong data-qp="stage">${esc(stage)}</strong> ${esc(title())}<span class="qp-muted" data-qp="elapsed"></span></div>
        <div class="qp-bar qp-bar-busy"><i></i></div>
        <p class="qp-muted">It goes to the queue when it's done. Closing this window leaves the slice running but queues nothing.</p></div>`;
      foot.innerHTML = `<span class="qp-sp"></span><button type="button" class="btn btn-secondary" data-qp-act="close">Close</button>`;
    } else if (phase === "review") {
      renderReview(body, foot);
    } else if (phase === "queueing") {
      body.innerHTML = `<div class="qp-status"><div>Adding it to the queue…</div><div class="qp-bar qp-bar-busy"><i></i></div></div>`;
      foot.innerHTML = "";
    } else if (phase === "routed") {
      renderRouted(body, foot);
    } else if (phase === "error") {
      body.innerHTML = `<div class="qp-notice qp-bad" role="alert">${esc(S.error)}</div>`;
      foot.innerHTML = `<span class="qp-sp"></span>${S.intake?.state === "ready" ? `<button type="button" class="btn btn-secondary" data-qp-act="back">Back</button>` : ""}<button type="button" class="btn btn-primary" data-qp-act="close">Close</button>`;
    }
  }

  /** A multi-object or multi-plate 3MF isn't a quick print: say so, then open it as a normal Project. */
  function renderRouted(body, foot) {
    const reason = S.routed?.reason || "This file holds more than one object or plate";
    body.innerHTML = `<div class="qp-status"><div class="qp-notice" role="status"><strong>This 3MF opens as a project.</strong>
      <div>${esc(reason)}. A Quick print handles one object on one plate, so the file has been imported as a Project where you can arrange, slice and print every plate.</div></div></div>`;
    foot.innerHTML = `<span class="qp-sp"></span><button type="button" class="btn btn-secondary" data-qp-act="close">Not now</button>
      <button type="button" class="btn btn-primary" data-qp-act="open-project">Open project</button>`;
  }
  function openRoutedProject() {
    const id = S?.id;
    if (!id) return;
    close({ silent: true });
    location.hash = `#projects=${id}`;
  }

  function renderReview(body, foot) {
    const r = S.review || { warnings: [] };
    const name = printerName();
    if (S.intake?.kind === "gcode") { renderGcodeReview(body, foot); return; }
    const est = exactEstimate();
    body.innerHTML = `<div class="qp-status"><div><strong>Sliced.</strong> ${est ? esc(est) : ""}</div>
      <div class="qp-notice qp-warn" role="alert">${r.warnings.map(w => `<div class="qp-warning"><strong>${esc(w.title)}</strong>${w.detail ? `<div class="qp-muted">${esc(w.detail)}</div>` : ""}</div>`).join("")}</div>
      ${S.error ? `<div class="qp-notice qp-bad" role="alert">${esc(S.error)}</div>` : ""}
      <label class="qp-check"><input type="checkbox" data-qp-act="ack" ${S.ack ? "checked" : ""}> I've seen these warnings</label></div>`;
    foot.innerHTML = `<span class="qp-muted">Queues on ${esc(name)}</span><span class="qp-sp"></span>
      <button type="button" class="btn btn-secondary" data-qp-act="back">Back</button>
      <button type="button" class="btn btn-primary" data-qp-act="queue" ${S.ack ? "" : "disabled"}>Queue print</button>`;
  }

  const CHECK_MARK = { pass: "OK", info: "Note", warn: "Warning", fail: "Blocked" };
  function renderGcodeReview(body, foot) {
    const r = S.review || { warnings: [], checks: [] };
    const warned = !!r.warnings?.length;
    const rows = (r.checks?.length ? r.checks : r.warnings || []).map(c => `<div class="qp-warning"><strong>${esc(CHECK_MARK[c.level] || c.level)}: ${esc(c.title)}</strong>${c.detail ? `<div class="qp-muted">${esc(c.detail)}</div>` : ""}</div>`).join("");
    body.innerHTML = `<div class="qp-status"><div><strong>Preflight for ${esc(title())}</strong> on ${esc(printerName())}. Slicing was skipped: the file is already sliced.</div>
      <div class="qp-notice ${warned ? "qp-warn" : ""}" role="status">${rows || "No checks to report."}</div>
      ${S.error ? `<div class="qp-notice qp-bad" role="alert">${esc(S.error)}</div>` : ""}
      ${warned ? `<label class="qp-check"><input type="checkbox" data-qp-act="ack" ${S.ack ? "checked" : ""}> I've seen these warnings</label>` : ""}</div>`;
    foot.innerHTML = `<span class="qp-muted">Queues on ${esc(printerName())}</span><span class="qp-sp"></span>
      <button type="button" class="btn btn-secondary" data-qp-act="back">Back</button>
      <button type="button" class="btn btn-primary" data-qp-act="queue" ${S.ack ? "" : "disabled"}>Queue print</button>`;
  }

  function printerName() { return printerList().find(p => p.id === S.choices.printer)?.name || "the printer"; }
  function exactEstimate() {
    const entry = S.project?.slices?.["1"];
    if (entry?.state === "sliced" && !entry.outdated && entry.estimate_s) return `${duration(entry.estimate_s)} · ${Math.round(entry.estimate_g || 0)} g`;
    return "";
  }

  // ─── The form (preview and badges left; printer, preset, spool, side, options right) ─────
  function enterEdit() {
    S.phase = "edit";
    S.error = null;
    buildForm();
    renderIntake();
    startPrinterWatch();
  }

  /** A preset's own value the dialog can't express (manual supports, an inner brim): shown, never sent unless the user picks another. */
  const presetChoice = current => (current === "preset" ? [["preset", "Preset's own", true]] : []);
  function selectOptions(list, current) {
    return list.map(([value, label, disabled]) => `<option value="${esc(value)}" ${String(value) === String(current) ? "selected" : ""} ${disabled ? "disabled" : ""}>${esc(label)}</option>`).join("");
  }

  function gcodeNote() {
    return `<div class="qp-notice" data-qp="gcode-note" role="status"><strong>Already sliced.</strong> This G-code goes to the printer as it is: no slicing, and the preset and print options are fixed in the file. Choose the printer, spool and plate side it was sliced for; the preflight check comes next.</div>`;
  }

  function buildForm() {
    const body = $('[data-qp="body"]'), foot = $('[data-qp="foot"]');
    const v = S.values, c = S.choices;
    const gcode = S.intake.kind === "gcode";
    setHead();
    const spoolList = [["", S.spoolError ? "Spoolman isn't reachable: no spool" : "No spool: use the preset's values"],
      ...S.spools.map(sp => [sp.id, `${sp.name}${sp.remaining_weight != null ? ` · ${Math.round(sp.remaining_weight)} g` : ""}`])];
    if (c.spool && !S.spools.some(sp => String(sp.id) === String(c.spool))) spoolList.push([c.spool, `Spool #${c.spool}`]);
    const presetList = S.bundles.map(b => [b.id, b.label || b.id]);
    if (!presetList.length) presetList.push(["", "No preset available"]);
    const layers = S.layerHeights.includes(v.layer_height) ? S.layerHeights : [...S.layerHeights, v.layer_height].sort((a, b) => a - b);
    body.innerHTML = `<div class="qp-cols">
      <div class="qp-left">
        <div class="qp-preview" data-qp="preview"><span class="qp-muted">Loading the preview…</span></div>
        <div class="qp-badges" data-qp="badges"></div>
        <div class="qp-slot" data-qp-slot="placement"></div>
      </div>
      <div class="qp-right">
        ${gcode ? gcodeNote() : ""}
        <div class="qp-notice" data-qp="notice" hidden><span data-qp="notice-text"></span> <span class="qp-slot" data-qp-slot="notice-actions"></span></div>
        <div data-qp="error"></div>
        <label class="qp-f">Printer<select data-qp-k="printer"></select></label>
        <div class="qp-grid3">
          ${gcode ? "" : `<label class="qp-f">Preset<select data-qp-k="preset">${selectOptions(presetList, c.preset)}</select></label>`}
          <label class="qp-f">Spool<select data-qp-k="spool">${selectOptions(spoolList, c.spool)}</select></label>
          <label class="qp-f">Plate side<select data-qp-k="bed">${selectOptions(Object.entries(SIDES), c.bed)}</select></label>
        </div>
        ${gcode ? "" : `<fieldset class="qp-options"><legend>Options</legend>
          <div class="qp-grid3">
            <label class="qp-f">Layer height (mm)<select data-qp-k="layer_height">${selectOptions(layers.map(n => [n, String(n)]), v.layer_height)}</select></label>
            <label class="qp-f">Infill (%)<input type="number" min="0" max="100" step="5" inputmode="numeric" value="${esc(v.infill)}" data-qp-k="infill"></label>
            <label class="qp-f">Walls<input type="number" min="1" max="20" step="1" inputmode="numeric" value="${esc(v.walls)}" data-qp-k="walls"></label>
            <label class="qp-f">Supports<select data-qp-k="supports">${selectOptions([["off", "Off"], ["normal", "Normal"], ["tree", "Tree"], ...presetChoice(v.supports)], v.supports)}</select></label>
            <label class="qp-f">Brim<select data-qp-k="brim">${selectOptions([["off", "Off"], ["auto", "Auto"], ["custom", "Custom"], ...presetChoice(v.brim)], v.brim)}</select></label>
            <label class="qp-f" data-qp="brim-width" ${v.brim === "custom" ? "" : "hidden"}>Brim width (mm)<input type="number" min="1" max="20" step="1" inputmode="decimal" value="${esc(v.brim_width ?? 5)}" data-qp-k="brim_width"></label>
          </div>
        </fieldset>`}
        <div class="qp-row">
          <label class="qp-check"><input type="checkbox" data-qp-k="leveling" ${c.leveling ? "checked" : ""}> Bed leveling</label>
          <label class="qp-check"><input type="checkbox" data-qp-k="timelapse" ${c.timelapse ? "checked" : ""}> Timelapse</label>
          <span class="qp-slot" data-qp-slot="more"></span>
        </div>
        <div class="qp-slot" data-qp-slot="more-panel"></div>
      </div>
    </div>`;
    foot.innerHTML = `<span class="qp-muted" data-qp="estimate"></span><span class="qp-sp"></span>
      <button type="button" class="btn btn-primary" data-qp-act="go" data-qp="go" disabled>Slice &amp; print</button>`;
    S.formBuilt = true;
    syncPrinterSelect(true);
  }

  /** What changes when the intake does: badges, notice, estimate, preview and whether Slice & print can go. */
  function renderIntake() {
    if (!S || !S.formBuilt) return;
    const i = S.intake;
    setHead();
    const badges = [];
    if (i.kind === "gcode") {
      badges.push(`<span class="qp-badge">G-code, already sliced</span>`);
      const est = i.estimate || {};
      if (est.seconds) badges.push(`<span class="qp-badge">${esc(duration(est.seconds))}</span>`);
      if (est.grams != null) badges.push(`<span class="qp-badge">${esc(est.grams)} g</span>`);
    } else {
      badges.push(`<span class="qp-badge" title="Width × depth × height">${i.size.map(mm).join(" × ")} mm</span>`,
        `<span class="qp-badge">${Number(i.volume_cm3).toLocaleString(undefined, { maximumFractionDigits: 1 })} cm³</span>`);
      badges.push(!i.fits ? `<span class="qp-badge qp-bad">Too big for the bed</span>` : `<span class="qp-badge qp-ok">Fits the bed</span>`);
      if (!i.scale_ok) badges.push(`<span class="qp-badge qp-warn">Scale looks wrong</span>`);
      badges.push(i.repaired
        ? `<span class="qp-badge qp-warn" title="${esc((i.repairs || []).join("; "))}">Repaired: ${esc(i.repairs[0])}${i.repairs.length > 1 ? ` +${i.repairs.length - 1}` : ""}</span>`
        : `<span class="qp-badge qp-ok">No repairs needed</span>`);
    }
    $('[data-qp="badges"]').innerHTML = badges.join("");
    const notice = $('[data-qp="notice"]');
    notice.hidden = !i.reason;
    notice.className = `qp-notice ${!i.fits || !i.scale_ok ? "qp-warn" : ""}`;
    $('[data-qp="notice-text"]').textContent = i.reason ? `${i.reason}.` : "";
    const exact = exactEstimate();
    $('[data-qp="estimate"]').textContent = exact || (i.estimate && i.estimate.grams != null
      ? `${i.estimate.rough === false ? "" : "≈ "}${i.estimate.seconds ? duration(i.estimate.seconds) : "?"} · ${i.estimate.grams} g${i.estimate.rough === false ? " (from the file)" : " (rough, before slicing)"}` : "");
    updateGo();
    loadPreview();
    emit("intake", { intake: i, project: S.project });
  }

  function syncPrinterSelect(force = false) {
    const select = $('[data-qp-k="printer"]');
    if (!select) return;
    const list = printerList();
    const signature = JSON.stringify(list.map(p => [p.id, p.state, p.usable]));
    if (!force && signature === S.printerSignature) return;
    S.printerSignature = signature;
    const c = S.choices;
    const wanted = list.find(p => p.id === c.printer && p.usable);
    if (!wanted) c.printer = (list.find(p => p.usable && p.state === "idle") || list.find(p => p.usable))?.id || null;
    const options = list.length ? [...(c.printer ? [] : [["", "Choose a printer"]]), ...list.map(p => [p.id, printerLabel(p), !p.usable])] : [["", "No printers"]];
    select.innerHTML = selectOptions(options, c.printer ?? "");
    const none = !list.some(p => p.usable);
    const text = $('[data-qp="error"]');
    if (text && !S.error) text.innerHTML = none ? `<div class="qp-notice qp-warn" role="alert">No printer is available. Connect or add a Centauri Carbon printer to print.</div>` : "";
    updateGo();
  }
  function startPrinterWatch() { stopPrinterWatch(); S.printerTimer = setInterval(() => syncPrinterSelect(), 2000); }
  function stopPrinterWatch() { if (S?.printerTimer) { clearInterval(S.printerTimer); S.printerTimer = null; } }

  function blockedReason() {
    const i = S.intake;
    if (!i || i.state !== "ready") return "The model isn't ready yet";
    if (!i.can_print) return i.reason || "The model can't be printed yet";
    if (!S.choices.printer) return printerList().some(p => p.usable) ? "Choose a printer" : "No printer is available";
    if (!S.choices.preset && i.kind !== "gcode") return "Choose a preset";
    return null;
  }
  function updateGo() {
    const go = $('[data-qp="go"]');
    if (!go) return;
    const reason = blockedReason();
    go.disabled = !!reason;
    go.title = reason || "";
    go.textContent = S.intake?.kind === "gcode" ? "Queue print" : "Slice & print";
  }

  // ─── Form events ────────────────────────────────────────────────────────────
  function onClick(e) {
    const act = e.target.closest("[data-qp-act]")?.dataset.qpAct;
    if (act === "close") close();
    else if (act === "go") go();
    else if (act === "back") { S.phase = "edit"; S.error = null; S.review = null; S.ack = false; buildForm(); renderIntake(); startPrinterWatch(); }
    else if (act === "queue") queue();
    else if (act === "open-project") openRoutedProject();
  }
  function onChange(e) {
    if (e.target.dataset.qpAct === "ack") { S.ack = e.target.checked; renderReviewButtons(); return; }
    const key = e.target.dataset.qpK;
    if (!key) return;
    applyField(key, e.target);
  }
  function onInput(e) {
    const key = e.target.dataset.qpK;
    if (key && e.target.type === "number") applyField(key, e.target);
  }
  function renderReviewButtons() {
    const button = $('[data-qp-act="queue"]');
    if (button) button.disabled = !S.ack;
  }
  function applyField(key, input) {
    const c = S.choices, v = S.values;
    const number = () => (input.value === "" ? NaN : Number(input.value));
    if (key === "printer") c.printer = input.value || null;
    else if (key === "preset") { c.preset = input.value; loadChoices(c.preset).then(() => S && S.formBuilt && fillOptions()).catch(err => say(err.message, true)); }
    else if (key === "spool") c.spool = input.value;
    else if (key === "bed") c.bed = input.value;
    else if (key === "leveling" || key === "timelapse") c[key] = input.checked;
    else {
      S.dirty.add(key);
      if (key === "layer_height") v[key] = Number(input.value);
      else if (key === "infill" || key === "walls" || key === "brim_width") v[key] = number();
      else v[key] = input.value;
      if (key === "brim") { const w = $('[data-qp="brim-width"]'); if (w) w.hidden = v.brim !== "custom"; }
    }
    updateGo();
  }
  /** After a preset change the options the user hasn't touched show the new preset's values. */
  function fillOptions() {
    for (const key of OPTION_KEYS) {
      const input = $(`[data-qp-k="${key}"]`);
      if (!input || S.dirty.has(key) || S.values[key] === undefined) continue;
      if (S.values[key] === "preset" && input.tagName === "SELECT" && ![...input.options].some(o => o.value === "preset")) input.insertAdjacentHTML("beforeend", selectOptions(presetChoice("preset"), ""));
      input.value = S.values[key];
    }
    const w = $('[data-qp="brim-width"]');
    if (w) w.hidden = S.values.brim !== "custom";
  }

  // ─── Slice & print ──────────────────────────────────────────────────────────
  function requestBody(extra = {}) {
    const c = S.choices, v = S.values, options = {};
    for (const key of ["layer_height", "infill", "walls", "supports"]) if (S.dirty.has(key) && v[key] !== "preset") options[key] = v[key];
    if ((S.dirty.has("brim") || S.dirty.has("brim_width")) && v.brim !== "preset") {
      options.brim = v.brim;
      if (v.brim === "custom") options.brim_width = v.brim_width;
    }
    const body = { printer_id: c.printer, bed: c.bed, spool_id: c.spool === "" ? null : Number(c.spool), leveling: c.leveling, timelapse: c.timelapse, ...extra };
    if (c.preset) body.preset = c.preset;
    if (Object.keys(options).length) body.options = options;
    return body;
  }

  function validateFields() {
    const v = S.values;
    if (S.dirty.has("infill") && (!Number.isInteger(v.infill) || v.infill < 0 || v.infill > 100)) return "Infill must be a whole percentage from 0 to 100";   // an untouched preset value (15.5) stays as it is
    if (S.dirty.has("walls") && (!Number.isInteger(v.walls) || v.walls < 1 || v.walls > 20)) return "Walls must be a whole number from 1 to 20";
    if (v.brim === "custom" && !(v.brim_width > 0 && v.brim_width <= 20)) return "Brim width must be between 0 and 20 mm";
    return null;
  }

  function showFormError(message) {
    const box = $('[data-qp="error"]');
    if (box) box.innerHTML = message ? `<div class="qp-notice qp-bad" role="alert">${esc(message)}</div>` : "";
    S.error = message || null;
  }

  async function go() {
    if (blockedReason()) return;
    const problem = validateFields();
    if (problem) { showFormError(problem); return; }
    showFormError(null);
    if (S.intake.kind === "gcode") { await reviewGcode(); return; }
    const session = S;
    session.phase = "slicing";
    session.sliceStarted = Date.now();
    renderStatus();
    try {
      const result = await api("POST", `/api/projects/${session.id}/print`, requestBody({ review: true }));
      if (S !== session) return;
      session.project = result;
      pollSlice(result.review);
    } catch (err) {
      sliceError(session, err.message);
    }
  }

  function sliceError(session, message) {
    if (S !== session) return;
    session.phase = "edit";
    buildForm();
    renderIntake();
    startPrinterWatch();
    showFormError(message);
  }

  /** Follow the slice until it is sliced, then fetch the preflight report (and queue straight away when it is clean). */
  async function pollSlice(review) {
    const session = S;
    for (;;) {
      if (review && review.state !== "slicing") return reviewed(session, review);
      await new Promise(resolve => session.timers.push(setTimeout(resolve, POLL_MS)));
      if (S !== session) return;
      try {
        const project = await api("GET", `/api/projects/${session.id}`);
        if (S !== session) return;
        session.project = project;
        const entry = project.slices?.["1"] || {};
        const elapsed = $('[data-qp="elapsed"]');
        if (elapsed) elapsed.textContent = ` · ${Math.round((Date.now() - session.sliceStarted) / 1000)} s`;
        const stage = $('[data-qp="stage"]');
        if (stage) stage.textContent = { preparing: "Preparing", slicing: "Slicing", "post-processing": "Post-processing", analysing: "Analysing" }[entry.stage] || (entry.state === "queued" ? "Waiting for the slicer" : "Slicing");
        if (entry.state === "failed") return sliceError(session, `Slicing failed: ${entry.error || "unknown error"}`);
        if (entry.state === "sliced" && !entry.outdated) {
          const result = await api("POST", `/api/projects/${session.id}/print`, requestBody({ review: true }));
          if (S !== session) return;
          session.project = result;
          return reviewed(session, result.review);
        }
      } catch (err) {
        return sliceError(session, err.message);
      }
    }
  }

  function reviewed(session, review) {
    if (S !== session) return;
    session.review = review;
    if (review.can_start === false) {
      const failed = (review.checks || review.warnings || []).filter(c => c.level === "fail").map(c => c.title + (c.detail ? `: ${c.detail}` : ""));
      return sliceError(session, `Sliced, but it can't be printed: ${failed.join("; ") || "a preflight check failed"}`);
    }
    if (!review.warnings?.length) return queue();
    session.phase = "review";
    session.ack = false;
    renderStatus();
  }

  async function queue() {
    const session = S;
    session.phase = "queueing";
    renderStatus();
    try {
      await api("POST", `/api/projects/${session.id}/print`, requestBody({ acknowledge_warnings: !!session.review?.warnings?.length && session.ack }));
      if (S !== session) return;
      await queued(session);
    } catch (err) {
      if (S !== session) return;
      session.phase = session.review?.warnings?.length ? "review" : "edit";
      if (session.phase === "review") { session.error = err.message; renderStatus(); }
      else sliceError(session, err.message);
    }
  }

  /** G-code is already sliced: fetch the preflight for the chosen printer and show it before anything is queued. */
  async function reviewGcode() {
    const session = S;
    session.phase = "queueing";
    renderStatus();
    try {
      const result = await api("POST", `/api/projects/${session.id}/print`, requestBody({ review: true }));
      if (S !== session) return;
      session.project = result;
      session.review = result.review;
      const failed = (result.review.checks || []).filter(c => c.level === "fail");
      if (result.review.can_start === false) return sliceError(session, `It can't be printed: ${failed.map(c => c.title + (c.detail ? `: ${c.detail}` : "")).join("; ") || "a preflight check failed"}`);
      session.phase = "review";
      session.ack = !result.review.warnings?.length;   // only warnings need to be acknowledged
      renderStatus();
    } catch (err) { sliceError(session, err.message); }
  }

  async function queued(session) {
    const name = printerName();
    const projectId = session.id;
    const c = session.choices;
    api("PATCH", "/api/defaults", { printer: c.printer, profile: c.preset || null, spool: c.spool === "" ? null : Number(c.spool), plate_side: c.bed,
      options: { leveling: c.leveling, timelapse: c.timelapse } }).catch(() => {});   // best effort, like the rest of the app
    emit("queued", { printerId: c.printer, printerName: name });
    close({ silent: true });
    const message = `Queued on ${name}. It starts when you confirm the bed is cleared.`;
    if (typeof toastAction === "function" && typeof openQueue === "function") toastAction(message, "Open queue", () => openQueue());
    else say(message);
    if (typeof refreshQueueSoon === "function") refreshQueueSoon();
    if (typeof loadDashboard === "function") loadDashboard();
    void projectId;
  }

  // ─── Preview (three.js, loaded on demand) ───────────────────────────────────
  async function loadPreview() {
    const session = S;
    const host = $('[data-qp="preview"]');
    if (!host || !session.intake || session.intake.kind === "gcode") { if (host) host.innerHTML = `<span class="qp-muted">Already sliced: no model preview</span>`; return; }
    const splitId = session.project?.split_id;
    const color = !session.intake.fits ? 0xf87171 : !session.intake.scale_ok ? 0xfbbf24 : 0xff6b2b;
    try {
      if (!session.preview) {
        if (!splitId) throw new Error("no preview yet");
        const THREE = await import("three");
        const { OrbitControls } = await import("three/addons/OrbitControls.js");
        const { smoothGeometry } = await import("/mesh-shading.js");
        const response = await fetch(`/api/splits/${splitId}/preview`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const meshes = parseMeshes(await response.arrayBuffer());
        if (S !== session || !session.formBuilt) return;
        host.innerHTML = "";
        const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
        renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
        host.appendChild(renderer.domElement);
        const scene = new THREE.Scene();
        const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 100000);
        camera.up.set(0, 0, 1);
        scene.add(new THREE.HemisphereLight(0xffffff, 0x404452, 1.2));
        const key = new THREE.DirectionalLight(0xffffff, 1.6);
        key.position.set(0.5, -1.1, 1.8);
        scene.add(key);
        const holder = new THREE.Group();
        const material = new THREE.MeshStandardMaterial({ color, roughness: 0.6, metalness: 0.05 });
        const box = new THREE.Box3();
        for (const mesh of meshes) {
          const geometry = smoothGeometry(mesh.positions, mesh.index);
          holder.add(new THREE.Mesh(geometry, material));
          geometry.computeBoundingBox();
          box.union(geometry.boundingBox);
        }
        const centre = box.getCenter(new THREE.Vector3());
        holder.children.forEach(m => m.position.sub(centre));
        const turn = new THREE.Group();     // scale and rotation as the server places them (about X, then Y, then Z)
        turn.add(holder);
        scene.add(turn);
        const floor = new THREE.GridHelper(1, 10, 0x6b7280, 0x3a3d50);
        floor.rotation.x = Math.PI / 2;
        scene.add(floor);
        const controls = new OrbitControls(camera, renderer.domElement);
        controls.enableDamping = false;
        const draw = () => renderer.render(scene, camera);
        controls.addEventListener("change", draw);
        const resize = () => {
          const w = host.clientWidth, h = host.clientHeight;
          if (!w || !h) return;
          renderer.setSize(w, h, false);
          camera.aspect = w / h;
          camera.updateProjectionMatrix();
          draw();
        };
        const observer = new ResizeObserver(resize);
        observer.observe(host);
        session.preview = { THREE, renderer, scene, camera, turn, floor, material, controls, draw, resize, observer, fitted: false };
      }
      const p = session.preview;
      if (!p) return;
      const { THREE } = p, intake = session.intake;
      p.material.color.setHex(color);
      const [rx, ry, rz] = (intake.rotation || [0, 0, 0]).map(d => d * Math.PI / 180);
      p.turn.scale.setScalar(Number(intake.scale) || 1);
      p.turn.rotation.set(rx, ry, rz, "ZYX");
      p.turn.position.set(0, 0, 0);
      p.turn.updateMatrixWorld(true);
      const box = new THREE.Box3().setFromObject(p.turn);
      const size = box.getSize(new THREE.Vector3()), centre = box.getCenter(new THREE.Vector3());
      p.turn.position.set(-centre.x, -centre.y, -box.min.z);        // sit on the floor, centred
      const span = Math.max(size.x, size.y, size.z, 1e-3);
      p.floor.scale.setScalar(span * 2);
      p.floor.position.set(0, 0, 0);
      const radius = Math.hypot(size.x, size.y, size.z) / 2;
      const distance = Math.max(radius, 1e-3) * 3.2;
      p.controls.target.set(0, 0, size.z / 2);
      p.camera.position.set(distance * 0.45, -distance * 0.8, size.z / 2 + distance * 0.55);
      p.camera.near = distance / 100;
      p.camera.far = distance * 100;
      p.camera.updateProjectionMatrix();
      p.controls.update();
      p.resize();
      p.draw();
    } catch (err) {
      if (S === session && host.isConnected && !session.preview) host.innerHTML = `<span class="qp-muted">The preview isn't available (${esc(err.message)})</span>`;
    }
  }

  function parseMeshes(buffer) {
    if (String.fromCharCode(...new Uint8Array(buffer, 0, 4)) !== "SPV1") throw new Error("unexpected preview format");
    const view = new DataView(buffer), meshes = [];
    let offset = 8;
    for (let i = 0, count = view.getUint32(4, true); i < count; i++) {
      const vertices = view.getUint32(offset, true), triangles = view.getUint32(offset + 4, true);
      offset += 8;
      const positions = new Float32Array(buffer, offset, vertices * 3);
      offset += vertices * 12;
      const index = new Uint32Array(buffer, offset, triangles * 3);
      offset += triangles * 12;
      meshes.push({ positions, index });
    }
    return meshes;
  }

  function disposePreview() {
    const p = S?.preview;
    if (!p) return;
    p.observer.disconnect();
    p.controls.dispose();
    p.renderer.dispose();
    p.renderer.forceContextLoss?.();
    p.scene.traverse(o => { o.geometry?.dispose?.(); });
    p.material.dispose();
    S.preview = null;
  }

  // ─── More settings (ticket 12): the full settings panel, for this print only ─────────────────────
  // Precedence. The Quick project's data.profile.process is the one home of this print's overrides, as on the project page.
  //  - The five option fields are a view over it. Edits in them stay local (S.dirty) and go with the print request.
  //  - An edit in the panel first flushes those pending field edits into data.profile.process (one PATCH, together with the
  //    panel change), clears their dirty flags, then re-reads the fields from quick-options. Per setting the later edit
  //    wins, and neither side overwrites the other: print-time `options` only hold field edits made since the last flush.
  //  - While the panel is open it shows pending field edits as changed settings (pendingOverlay), so both views agree.
  //  - The panel only writes the project's data.profile.process. Presets and the locked bundle are only read (the panel's
  //    base is the bundle's flattened process settings). Any change alters the project fingerprint, so a slice is Outdated.
  const PANEL_NOTE = "These changes apply to this print only. Presets are not changed.";
  const QUICK_OF = { layer_height: ["layer_height"], sparse_infill_density: ["infill"], wall_loops: ["walls"], enable_support: ["supports"],
    support_type: ["supports"], brim_type: ["brim", "brim_width"], brim_width: ["brim", "brim_width"] };
  let settingsKit = null;

  function loadSettingsKit() {
    if (!settingsKit) {
      settingsKit = Promise.all([import("/slicer/settings.js?v=4"), fetch("/orca/settings.json").then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })])
        .then(([mod, schema]) => ({ SettingsPanel: mod.SettingsPanel, schema }))
        .catch(err => { settingsKit = null; throw new Error(`Couldn't load the settings panel (${err.message})`); });
    }
    return settingsKit;
  }

  /** The dialog fields' unsaved edits as the Orca keys they would write (mirrors the server's _quick_overrides). */
  function pendingOverlay() {
    const v = S.values, d = S.dirty, process = {}, covers = [];
    if (d.has("layer_height") && Number.isFinite(v.layer_height)) { process.layer_height = `${v.layer_height}`; covers.push("layer_height"); }
    if (d.has("infill") && Number.isInteger(v.infill) && v.infill >= 0 && v.infill <= 100) { process.sparse_infill_density = `${v.infill}%`; covers.push("infill"); }
    if (d.has("walls") && Number.isInteger(v.walls) && v.walls >= 1 && v.walls <= 20) { process.wall_loops = String(v.walls); covers.push("walls"); }
    if (d.has("supports") && ["off", "normal", "tree"].includes(v.supports)) {
      process.enable_support = v.supports === "off" ? "0" : "1";
      if (v.supports !== "off") process.support_type = v.supports === "tree" ? "tree(auto)" : "normal(auto)";
      covers.push("supports");
    }
    if (d.has("brim") || d.has("brim_width")) {
      if (v.brim === "off") { process.brim_type = "no_brim"; covers.push("brim", "brim_width"); }
      else if (v.brim === "auto") { process.brim_type = "auto_brim"; covers.push("brim", "brim_width"); }
      else if (v.brim === "custom" && v.brim_width > 0 && v.brim_width <= 20) { process.brim_type = "outer_only"; process.brim_width = `${v.brim_width}`; covers.push("brim", "brim_width"); }
    }
    return { process, covers };
  }

  function projectProcess() { return S.project?.data?.profile?.process || {}; }

  async function moreBase() {
    const id = S.choices.preset || S.project?.data?.profile?.bundle_id;
    if (!id) return null;
    const m = S.more;
    if (!m.bases[id]) m.bases[id] = await api("GET", `/api/slicer/profiles/${encodeURIComponent(id)}/settings`);
    return m.bases[id];
  }

  function moreMode() { try { return localStorage.getItem("slicer.mode") || "simple"; } catch (_) { return "simple"; } }

  /** Adds the More settings button (and reopens the panel after the form was rebuilt). Runs on every quickprint:intake. */
  function mountMore() {
    if (!S || !S.formBuilt || !S.intake || S.intake.kind === "gcode") return;
    const m = S.more ||= { open: false, panel: null, bases: {}, queue: Promise.resolve() };
    const slot = $('[data-qp-slot="more"]');
    if (!slot) return;
    if (!slot.firstElementChild) slot.innerHTML = `<button type="button" class="btn btn-secondary btn-sm" data-more-act="toggle" aria-expanded="${m.open}">More settings</button>`;
    if (!S.moreBound) {
      S.moreBound = true;
      S.root.addEventListener("click", e => { if (e.target.closest('[data-more-act="toggle"]')) toggleMore(); });
      S.root.addEventListener("change", onMoreFieldEdit);
      S.root.addEventListener("input", e => { if (e.target.type === "number") onMoreFieldEdit(e); });
    }
    if (m.open && !$('[data-more="body"]')) openMore();
  }

  function toggleMore() {
    const m = S.more;
    m.open = !m.open;
    $('[data-more-act="toggle"]')?.setAttribute("aria-expanded", String(m.open));
    if (m.open) openMore();
    else { const host = $('[data-qp-slot="more-panel"]'); if (host) host.innerHTML = ""; m.panel = null; }
  }

  async function openMore() {
    const session = S, m = S.more, host = $('[data-qp-slot="more-panel"]');
    if (!host) return;
    host.innerHTML = `<div class="qp-more"><div class="qp-muted">Loading the settings…</div></div>`;
    try {
      const [kit, base] = await Promise.all([loadSettingsKit(), moreBase()]);
      if (S !== session || !m.open || !host.isConnected) return;
      if (!base) throw new Error("Choose a preset to see its settings");
      host.innerHTML = `<div class="qp-more" data-more="box">
        <div class="qp-more-tools"><input type="search" data-more="search" placeholder="Search settings" aria-label="Search settings">
          <select data-more="mode" aria-label="Setting detail"><option value="simple">Simple</option><option value="advanced">Advanced</option><option value="expert">Expert</option></select></div>
        <div data-more="tabs" hidden></div>
        <div class="qp-more-pages" data-more="pages"></div>
        <div class="qp-more-body" data-more="body"></div>
        <div data-more="error"></div></div>`;
      const q = name => host.querySelector(`[data-more="${name}"]`);
      q("mode").value = moreMode();
      q("mode").addEventListener("change", () => { try { localStorage.setItem("slicer.mode", q("mode").value); } catch (_) { /* storage blocked */ } });
      m.panel = new kit.SettingsPanel({ tabs: q("tabs"), pages: q("pages"), body: q("body"), search: q("search"), mode: q("mode"),
        onChange: (_target, key, value) => commitMore(process => { process[key] = value; }),
        onReset: (_target, keys) => commitMore(process => { for (const key of keys) delete process[key]; }, keys) });
      m.panel.setSchema(kit.schema);
      showMore();
    } catch (err) {
      if (S === session && host.isConnected) host.innerHTML = `<div class="qp-notice qp-bad" role="alert">${esc(err.message)}</div>`;
    }
  }

  async function showMore() {
    const m = S?.more;
    if (!m?.panel || !m.open) return;
    const session = S, panel = m.panel;
    const base = await moreBase().catch(() => null);
    if (S !== session || m.panel !== panel || !base) return;
    panel.show({ tab: "process", tabs: [{ id: "process", label: "Process" }], layout: "process", base: base.process,
      changes: { ...projectProcess(), ...pendingOverlay().process }, target: { kind: "process" }, note: PANEL_NOTE });
  }

  function moreError(message) {
    const box = $('[data-more="error"]');
    if (box) box.innerHTML = message ? `<div class="qp-notice qp-bad" role="alert">${esc(message)}</div>` : "";
  }

  /** Flush pending field edits and apply one panel change to the project, in order, on fresh data (never outdated data). */
  function commitMore(mutate, resetKeys = []) {
    const session = S, m = S.more;
    m.queue = m.queue.then(async () => {
      if (S !== session) return;
      moreError(null);
      const project = await api("GET", `/api/projects/${session.id}`);
      const pending = pendingOverlay();
      const data = project.data;
      const process = { ...data.profile.process, ...pending.process };
      mutate(process);
      data.profile.process = process;
      const saved = await api("PATCH", `/api/projects/${session.id}`, { data, revision: project.revision });
      if (S !== session) return;
      session.project = saved;
      for (const key of pending.covers) session.dirty.delete(key);
      for (const key of resetKeys) for (const quick of QUICK_OF[key] || []) session.dirty.delete(quick);
      if (session.choices.preset) await loadChoices(session.choices.preset);
      if (S !== session) return;
      refreshFields();
      showMore();
    }).catch(err => {
      if (S !== session) return;
      moreError(err.message);
      showMore();
    });
    return m.queue;
  }

  /** Show the values the project now has in the five fields (a value outside the layer-height list gets an entry). */
  function refreshFields() {
    const v = S.values;
    const layer = $('[data-qp-k="layer_height"]');
    if (layer && v.layer_height !== undefined && ![...layer.options].some(o => Number(o.value) === v.layer_height)) {
      const sizes = [...layer.options].map(o => Number(o.value)).concat(v.layer_height).sort((a, b) => a - b);
      layer.innerHTML = selectOptions(sizes.map(n => [n, String(n)]), v.layer_height);
    }
    fillOptions();
    const exact = exactEstimate(), box = $('[data-qp="estimate"]'), i = S.intake;
    if (box) box.textContent = exact || (i?.estimate && i.estimate.grams != null ? `≈ ${i.estimate.seconds ? duration(i.estimate.seconds) : "?"} · ${i.estimate.grams} g (rough, before slicing)` : "");
  }

  /** A field or preset change while the panel is open: the panel follows (pending edits show as changed settings). */
  function onMoreFieldEdit(e) {
    const key = e.target.dataset?.qpK;
    if (!S?.more?.open || !key) return;
    if (key === "preset" || OPTION_KEYS.includes(key)) showMore();
  }

  document.addEventListener("quickprint:intake", () => mountMore());

  // ─── Closing ────────────────────────────────────────────────────────────────
  function close({ silent = false } = {}) {
    if (!S) return;
    const session = S;
    session.closed = true;
    session.xhr?.abort();
    session.timers.forEach(clearTimeout);
    stopPrinterWatch();
    disposePreview();
    document.removeEventListener("keydown", onEscape);
    session.root.remove();
    const projectId = session.id;
    S = null;
    if (!silent) document.dispatchEvent(new CustomEvent("quickprint:close", { detail: { projectId } }));
    try { session.previouslyFocused?.focus?.(); } catch (_) { /* element gone */ }
  }

  // ─── Placement and not-fit handling (ticket 11): fills the placement and notice-actions slots on every intake ──
  async function placementAct(path, failure) {
    const session = S;
    if (!session) return;
    const buttons = () => session.root.querySelectorAll("[data-qp-place], [data-qp-fix]");
    buttons().forEach(b => { b.disabled = true; });
    try {
      let result = await api("POST", `/api/projects/${session.id}/${path}`);
      for (let tries = 0; result.state === "analysing" && tries < 60 && S === session; tries++) {   // a scale fix re-analyses the model
        await new Promise(resolve => setTimeout(resolve, 400));
        result = await api("GET", `/api/projects/${session.id}/intake`);
      }
      if (S === session) await refresh();   // intake, project, badges, notice, estimate and preview follow
    } catch (error) {
      if (S === session) { say(`${failure}: ${error.message}`, true); await refresh().catch(() => {}); }
    } finally {
      buttons().forEach(b => { b.disabled = false; });
    }
  }
  async function openAsProject() {
    const session = S;
    if (!session) return;
    try {
      await api("POST", `/api/projects/${session.id}/open-as-project`);
    } catch (error) { say(`Couldn't open it as a project: ${error.message}`, true); return; }
    close();
    location.hash = `#projects=${session.id}`;   // an unplaced model lands on Model/Split (the stepper's default step)
  }
  function renderPlacementSlots() {
    if (!S || S.phase !== "edit") return;
    const i = S.intake, place = $('[data-qp-slot="placement"]'), actions = $('[data-qp-slot="notice-actions"]');
    if (!i || !place || !actions) return;
    const model = i.state === "ready" && i.kind !== "gcode";
    place.innerHTML = model && !i.placed
      ? `<button type="button" class="btn btn-secondary btn-sm" data-qp-place="lay-flat" title="Put the model on its largest flat face">Lay flat</button>
         <button type="button" class="btn btn-secondary btn-sm" data-qp-place="rotate" title="Turn the model 90 degrees about the vertical axis">Rotate 90°</button>` : "";
    const fixable = model && !i.scale_ok && i.scale_fix, tooBig = model && i.scale_ok && !i.fits;
    actions.innerHTML = (fixable ? `<button type="button" class="btn btn-primary btn-sm" data-qp-fix="scale">${esc(i.scale_fix.label)}</button>` : "")
      + (tooBig || (model && !i.scale_ok) ? `<button type="button" class="btn ${tooBig ? "btn-primary" : "btn-secondary"} btn-sm" data-qp-fix="project" title="Keep this model as a Project and continue in the Model and Split steps">Open as project</button>` : "");
    place.querySelectorAll("[data-qp-place]").forEach(b => b.addEventListener("click", () => placementAct(b.dataset.qpPlace, b.dataset.qpPlace === "rotate" ? "Couldn't rotate" : "Couldn't lay it flat")));
    actions.querySelector('[data-qp-fix="scale"]')?.addEventListener("click", () => placementAct("fix-scale", "Couldn't fix the scale"));
    actions.querySelector('[data-qp-fix="project"]')?.addEventListener("click", openAsProject);
  }
  document.addEventListener("quickprint:intake", renderPlacementSlots);

  window.QuickPrint = {
    open, pick, close, refresh, isOpen: () => !!S,
    current: () => (S ? { projectId: S.id, intake: S.intake, project: S.project, choices: { ...S.choices }, phase: S.phase } : null),
  };

  document.getElementById("btn-quick-print")?.addEventListener("click", () => pick());
})();
