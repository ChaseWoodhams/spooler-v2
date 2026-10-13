/* Printer detail page — "Cockpit" layout: the camera is the page, HUD overlay, controls in tabs underneath.
 * Promoted from the printer-variants prototype (layouts A/C/D and the switcher were dropped).
 * app.js calls window.cockpitPrinterHtml(ctx) from renderPrinterDetail; if this file is missing it falls back
 * to the original markup. Same handlers as before — nothing new is mutated on the printer.
 *
 * Full screen: the #printer-detail container is what goes fullscreen (not the camera node), because
 * renderPrinterDetail replaces its innerHTML on every status update and that would otherwise exit fullscreen.
 * CSS (.pv-fs) hides everything but the hero. Browsers without the Fullscreen API (iPhone Safari) get the
 * same layout pinned over the viewport instead.
 */
(() => {
  let tab = "motion";                       // active tab (in memory only)

  const detailEl = () => document.getElementById("printer-detail");
  const apiFullscreen = () => document.fullscreenElement || document.webkitFullscreenElement;
  const isFs = () => !!detailEl()?.classList.contains("pv-fs");

  const rerender = () => {
    if (_activePrinterId && printers[_activePrinterId]) renderPrinterDetail(printers[_activePrinterId]);
  };
  window.selectPrinterTab = t => {
    tab = t; rerender();
    document.getElementById(`printer-tab-${t}`)?.focus();
  };
  document.addEventListener("keydown", event => {
    if (!event.target.matches(".pv-tabs [role=tab]")) return;
    const keys = ["motion", "temps", "air", "files"];
    let index = keys.indexOf(tab);
    if (event.key === "ArrowRight") index = (index + 1) % keys.length;
    else if (event.key === "ArrowLeft") index = (index + keys.length - 1) % keys.length;
    else if (event.key === "Home") index = 0;
    else if (event.key === "End") index = keys.length - 1;
    else return;
    event.preventDefault(); window.selectPrinterTab(keys[index]);
  });

  function setFs(on) {
    const el = detailEl();
    if (!el) return;
    el.classList.toggle("pv-fs", on);
    document.body.classList.toggle("pv-fs-lock", on);
    const req = el.requestFullscreen || el.webkitRequestFullscreen;
    const exit = document.exitFullscreen || document.webkitExitFullscreen;
    if (on && req && !apiFullscreen()) Promise.resolve(req.call(el)).catch(() => {}); // on failure the pinned layout still applies
    else if (!on && apiFullscreen() && exit) Promise.resolve(exit.call(document)).catch(() => {});
    rerender();                                                                       // refresh the button label
  }
  window.toggleCameraFullscreen = () => setFs(!isFs());

  // Esc / browser UI leaving fullscreen must drop our class too
  for (const ev of ["fullscreenchange", "webkitfullscreenchange"])
    document.addEventListener(ev, () => { if (!apiFullscreen() && isFs()) setFs(false); });
  // pinned (no Fullscreen API) mode has no native Esc
  addEventListener("keydown", e => { if (e.key === "Escape" && isFs() && !apiFullscreen()) setFs(false); });
  // leaving the printer page always leaves full screen
  addEventListener("hashchange", () => { if (isFs()) setFs(false); });

  // ── shared bits ────────────────────────────────────────────────────────────
  const T = (n) => Number(n).toFixed(1);
  const cam = c => `
    <div class="detail-camera pv-cam" ondblclick="toggleCameraFullscreen()" title="Double-click for full screen"><div class="camera-placeholder" style="display:flex"><span>${c.connected ? "Camera unavailable" : "Printer offline"}</span></div></div>`;
  const chip = c => `<span class="live-chip${c.cameraUrl ? "" : " offline"}" id="detail-live-chip">${c.cameraUrl ? "● Live" : "○ No signal"}</span>`;
  const fsBtn = () => `<button class="pv-fsbtn" onclick="toggleCameraFullscreen()" aria-label="${isFs() ? "Exit full screen" : "Full screen video"}" title="${isFs() ? "Exit full screen" : "Full screen"}">${isFs() ? "⤡" : "⛶"}</button>`;
  const back = `<button class="pv-back" onclick="returnToPrinterGrid()">‹ All printers</button>`;
  const title = c => `<span class="status-dot ${c.sc}"></span><b>${escHtml(c.printer.name)}</b><small>${escHtml(c.printer.ip)}</small>`;
  const id = c => escAttr(c.printer.id);

  const fan = (c, key, label, v) => `
    <label class="pv-slider">
      <span>${label}</span><strong id="detail-${key}-value">${v}%</strong>
      <input id="detail-${key}-slider" type="range" min="0" max="100" step="5" value="${v}" aria-label="${label}"
        oninput="document.getElementById('detail-${key}-value').textContent=this.value+'%'"
        onchange="setPrinterFan('${id(c)}','${key}',+this.value)" ${c.disableConnected}>
    </label>`;
  const speed = c => `
    <label class="pv-slider">
      <span>Print speed</span><strong id="detail-speed-value">${c.speed}%</strong>
      <input id="detail-speed-slider" type="range" min="10" max="200" step="5" value="${c.speed}" aria-label="Print speed"
        oninput="document.getElementById('detail-speed-value').textContent=this.value+'%'"
        onchange="setSpeed('${id(c)}',+this.value)" ${c.printing || c.paused ? "" : "disabled"}>
    </label>`;
  const light = c => `<button class="detail-toggle ${c.lightOn ? "active" : ""}" onclick="printerAction('${id(c)}','${c.lightOn ? "light_off" : "light_on"}')" ${c.connected ? "" : "disabled"} aria-pressed="${c.lightOn}" aria-label="Chamber lighting"><span></span></button>`;
  const steps = c => [0.1, 1, 10].map(v => `<button class="pv-chip ${c.jogStep === v ? "on" : ""}" aria-pressed="${c.jogStep === v}" aria-label="Jog distance ${v} mm" onclick="setJogStep(${v})">${v}</button>`).join("");
  const jog = c => `
    <div class="pv-jog">
      <div class="pv-xy">
        <i></i><button onclick="jogPrinter('${id(c)}','Y',1)" ${c.disableMotion}>Y+</button><i></i>
        <button onclick="jogPrinter('${id(c)}','X',-1)" ${c.disableMotion}>X−</button>
        <button class="home" onclick="homePrinter('${id(c)}','XY')" ${c.disableMotion} aria-label="Home XY">⌂</button>
        <button onclick="jogPrinter('${id(c)}','X',1)" ${c.disableMotion}>X+</button>
        <i></i><button onclick="jogPrinter('${id(c)}','Y',-1)" ${c.disableMotion}>Y−</button><i></i>
      </div>
      <div class="pv-z">
        <button onclick="jogPrinter('${id(c)}','Z',1)" ${c.disableMotion}>Z+</button>
        <button class="home" onclick="homePrinter('${id(c)}','Z')" ${c.disableMotion} aria-label="Home Z">⌂</button>
        <button onclick="jogPrinter('${id(c)}','Z',-1)" ${c.disableMotion}>Z−</button>
      </div>
    </div>`;
  const temp = (c, key, label, val, tgt, max, ro) => `
    <div class="pv-temp">
      <span>${label}</span><strong class="${tempColor(val)}">${T(val)} °C</strong>
      ${ro ? `<em>sensor only</em>` : `
      <span class="pv-tgt">Target <input id="detail-${key}-target" type="number" min="0" max="${max}" value="${Math.round(tgt)}" aria-label="${label} target temperature (°C)" ${c.disableConnected}>°C
      <button aria-label="Apply ${label.toLowerCase()} target" onclick="setPrinterTemperature('${id(c)}','${key}','detail-${key}-target')" ${c.disableConnected}>Set</button></span>`}
    </div>`;
  const pct = c => c.detailProgress ?? 0;
  const eta = c => c.detailRemaining > 0 ? formatTime(c.detailRemaining) : "—";
  const jobBtns = c => c.printing || c.paused ? `
    ${c.printing
      ? `<button class="btn btn-secondary" onclick="printerAction('${id(c)}','pause')">Pause</button>`
      : `<button class="btn btn-primary" onclick="printerAction('${id(c)}','resume')">Resume</button>`}
    <button class="btn btn-danger" onclick="confirmStop('${id(c)}')">Stop</button>` : "";
  const browseBtn = c => `<button class="btn btn-primary" onclick="openFileBrowser('${id(c)}')">Browse & start print</button>`;

  // ── Cockpit ────────────────────────────────────────────────────────────────
  window.cockpitPrinterHtml = c => {
    const tabs = { motion: "Motion", temps: "Temperatures", air: "Air & light", files: "Files" };
    const controlReason = !c.connected ? "Printer offline. Reconnect the printer to use controls." : c.printer.printer_type !== "cc1" ? "Motion, temperature and fan controls are available for Centauri Carbon 1 only." : "";
    const body = {
      motion: `<div class="pv-row-wrap">${jog(c)}<div class="pv-col"><div class="pv-chips">Step ${steps(c)} mm</div>
        <div class="pv-xyz">X ${_coordText(c.coords.x)} · Y ${_coordText(c.coords.y)} · Z ${_coordText(c.coords.z)}</div>
        <button class="pv-chip" onclick="homePrinter('${id(c)}','XYZ')" ${c.disableMotion}>Home all</button>
        <small>${c.controlsEnabled ? "Ready to move" : c.connected ? `Locked while ${c.status}` : "Printer offline"}</small></div></div>`,
      temps: `<div class="pv-col wide"><small>Nozzle target: 0–300 °C · Bed target: 0–110 °C. Zero turns heating off.</small>${temp(c, "nozzle", "Nozzle", c.nozzle, c.nozzleTarget, 300)}${temp(c, "bed", "Bed", c.bed, c.bedTarget, 110)}${temp(c, "chamber", "Chamber", c.chamber, 0, 0, true)}</div>`,
      air: `<div class="pv-col wide">${fan(c, "model", "Model fan", c.modelFan)}${fan(c, "auxiliary", "Side fan", c.auxiliaryFan)}${fan(c, "chamber", "Chamber fan", c.chamberFan)}${speed(c)}
        <small>Fans: 0–100%. Print speed: 10–200%, available during a Print.</small><div class="pv-light"><span>Chamber light</span>${light(c)}</div></div>`,
      files: `<div class="pv-col">${browseBtn(c)}<small>Pick a file already on the printer.</small></div>`,
    };
    return `<div class="pv pv-b">
      <div class="pv-hero">
        ${cam(c)}
        <div class="pv-hud top">${back}<span class="pv-title">${title(c)}</span><span class="status-badge ${c.sc}">${c.status}</span>${chip(c)}${fsBtn()}</div>
        <div class="pv-hud bot">
          <div class="pv-pills"><span>🔥 ${T(c.nozzle)}°<small>/${Math.round(c.nozzleTarget)}</small></span><span>▦ ${T(c.bed)}°<small>/${Math.round(c.bedTarget)}</small></span><span>▢ ${T(c.chamber)}°</span></div>
          ${c.printing || c.paused ? `<div class="pv-hudjob"><b>${escHtml(c.detailFilename || "Print")}</b><div class="pv-bar"><span style="width:${pct(c)}%"></span></div><small>${pct(c)}% · ${eta(c)} left</small><span class="pv-btns">${jobBtns(c)}</span></div>` : `<div class="pv-hudjob idle"><b>Idle</b>${browseBtn(c)}</div>`}
        </div>
      </div>
      <div class="pv-tabs" role="tablist" aria-label="Printer controls">${Object.entries(tabs).map(([k, v]) => `<button id="printer-tab-${k}" role="tab" aria-selected="${tab === k}" aria-controls="printer-panel" tabindex="${tab === k ? 0 : -1}" class="${tab === k ? "on" : ""}" onclick="selectPrinterTab('${k}')">${v}</button>`).join("")}</div>
      <div id="printer-panel" class="pv-tabbody" role="tabpanel" tabindex="0" aria-labelledby="printer-tab-${tab}">${controlReason ? `<p role="status">${controlReason}</p>` : ""}${body[tab]}</div>
    </div>`;
  };

  // ── styles ─────────────────────────────────────────────────────────────────
  const css = `
  .pv{max-width:1200px;margin:0 auto;display:flex;flex-direction:column;gap:14px}
  .pv button{font:inherit;color:inherit;cursor:pointer}
  .pv button:disabled{opacity:.4;cursor:not-allowed}
  .pv-back{background:none;border:0;color:var(--text-muted)}
  .pv-title{display:flex;align-items:center;gap:8px}.pv-title small{color:var(--text-muted)}
  .pv-chip{background:var(--surface2);border:1px solid var(--border);border-radius:99px;padding:4px 12px}
  .pv-chip.on{background:var(--accent-fill);border-color:var(--accent-fill);color:#fff;font-weight:700}.pv-chip.on::before{content:"\\2713\\00a0"}
  .pv-chips{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
  .pv-bar{height:6px;background:var(--surface2);border-radius:4px;overflow:hidden;min-width:80px}.pv-bar span{display:block;height:100%;background:var(--accent)}
  .pv-btns{display:flex;gap:16px 8px;flex-wrap:wrap}.pv-btns .btn-danger{margin-left:24px}
  .pv-row-wrap{display:flex;gap:24px;flex-wrap:wrap;align-items:center}
  .pv-col{display:flex;flex-direction:column;gap:10px}.pv-col.wide{width:100%;max-width:520px}
  .pv-xyz{font-variant-numeric:tabular-nums;color:var(--text-muted)}
  /* jog */
  .pv-jog{display:flex;gap:14px;align-items:center}
  .pv-xy{display:grid;grid-template-columns:repeat(3,48px);grid-auto-rows:48px;gap:4px}
  .pv-z{display:grid;grid-template-rows:repeat(3,48px);gap:4px;width:48px}
  .pv-jog button{background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius-sm)}
  .pv-jog .home{color:var(--accent)}
  /* sliders / temps */
  .pv-slider{display:grid;grid-template-columns:1fr auto;gap:2px 8px}.pv-slider input{grid-column:1/3;accent-color:var(--accent)}
  .pv-temp{display:flex;align-items:center;gap:10px;padding:6px 0}.pv-temp>span:first-child{width:76px;color:var(--text-muted)}.pv-temp strong{font-size:1.25em;min-width:64px}.pv-temp em{color:var(--text-muted)}
  .pv-tgt{display:flex;align-items:center;gap:4px;margin-left:auto}.pv-tgt input{width:60px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:4px 6px}
  .pv-tgt button{background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:3px 10px}
  .pv-light{display:flex;align-items:center;justify-content:space-between}
  /* hero */
  .pv-b .pv-hero{position:relative;border-radius:var(--radius);overflow:hidden;background:#000;min-height:min(62vh,560px)}
  .pv-b .pv-cam{position:absolute;inset:0;border-radius:0;max-height:none;aspect-ratio:auto;cursor:zoom-in}.pv-b .pv-cam img{width:100%;height:100%;object-fit:contain}
  .pv-hud{position:absolute;left:0;right:0;display:flex;align-items:center;gap:12px;padding:12px 16px;z-index:2;color:#fff;flex-wrap:wrap;pointer-events:none}
  .pv-hud>*{pointer-events:auto}
  .pv-hud.top{top:0;background:linear-gradient(#000a,transparent)}.pv-hud.bot{bottom:0;background:linear-gradient(transparent,#000c);justify-content:space-between;align-items:flex-end}
  .pv-hud .pv-back{color:#fff}.pv-hud .live-chip{margin-left:auto}
  .pv-pills{display:flex;gap:8px}.pv-pills span{background:#000a;border-radius:99px;padding:4px 12px;backdrop-filter:blur(6px)}.pv-pills small{opacity:.6}
  .pv-hudjob{display:flex;align-items:center;gap:12px;flex-wrap:wrap;background:#000a;border-radius:var(--radius-sm);padding:8px 12px;backdrop-filter:blur(6px);flex:1;max-width:620px}
  .pv-hudjob .pv-bar{flex:1}.pv-hudjob.idle{flex:0 0 auto}
  .pv-fsbtn{background:#0008;border:1px solid #fff4;border-radius:8px;width:34px;height:34px;font-size:18px;line-height:1;color:#fff;backdrop-filter:blur(6px)}
  .pv-fsbtn:hover{background:#000b}
  .pv-tabs{display:flex;gap:4px;border-bottom:1px solid var(--border)}
  .pv-tabs button{background:none;border:0;border-bottom:2px solid transparent;padding:10px 16px;color:var(--text-muted)}.pv-tabs button.on{color:var(--text);border-color:var(--accent)}
  .pv-tabbody{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:18px;min-height:190px}
  @media(max-width:900px){.pv-b .pv-hero{min-height:56vh}}
  .pv button,.pv input[type=number]{min-height:44px}.pv-slider input{min-height:44px}
  .pv-title{min-width:0}.pv-title b,.pv-hudjob b{overflow-wrap:anywhere}
  @media(max-width:640px){
    .pv-b .pv-hero{min-height:0;display:flex;flex-direction:column;background:var(--surface)}
    .pv-b .pv-cam{position:relative;aspect-ratio:16/9;min-height:150px}
    .pv-hud{position:relative;background:#161a20!important;padding:10px;gap:8px}
    .pv-hud.top{order:-1}.pv-title{flex:1;flex-wrap:wrap}.pv-title small{display:none}
    .pv-hud.bot{align-items:stretch}.pv-pills{flex-wrap:wrap}.pv-hudjob{width:100%;max-width:none}
    .pv-tabs{flex-wrap:wrap}.pv-tabs button{flex:1;padding:10px 8px}
    .pv-tabbody{padding:10px}.pv-temp{flex-wrap:wrap}.pv-tgt{margin-left:0;flex-wrap:wrap}
    .pv-temp>span:first-child{width:65px}.pv-temp strong{min-width:70px}
    .pv-row-wrap{gap:12px}.pv-jog{gap:8px}.pv-pills span{padding:4px 8px}
  }
  /* full screen: only the hero stays */
  body.pv-fs-lock{overflow:hidden}
  #printer-detail.pv-fs{position:fixed;inset:0;z-index:9000;background:#000;padding:0;margin:0;max-width:none;overflow:hidden}
  #printer-detail.pv-fs .pv{max-width:none;height:100%;gap:0}
  #printer-detail.pv-fs .pv-tabs,#printer-detail.pv-fs .pv-tabbody{display:none}
  #printer-detail.pv-fs .pv-hero{flex:1;height:100%;min-height:0;border-radius:0}
  #printer-detail.pv-fs .pv-cam{cursor:zoom-out}`;
  document.head.appendChild(Object.assign(document.createElement("style"), { textContent: css }));
})();
