/* Outcome prompt: shown for a printer whose last print has ended. Pre-filled with what the printer reported, with the
   end-of-print snapshot where there is one. "Printed OK · Bed cleared" confirms and frees the bed in one request;
   "Failed…" opens a sheet (reasons, note, Bed cleared ticked, Reprint this plate next) that does it all in one submit.
   Plain script: used by the dashboard (index.html) and the project page (slicer.html).
   Public: OutcomePrompt.open(printerId), OutcomePrompt.awaiting() -> Promise<array of printer ids awaiting>. */
const OutcomePrompt = (() => {
  // The failure reasons come from the backend (GET /api/failure-reasons) so the list lives in one place.
  async function failureReasons() {
    const data = await api("GET", "/api/failure-reasons");
    return data.failure_reasons.map(id => [id, data.labels[id] || id]);
  }
  const esc = s => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const say = (msg, bad) => {
    if (typeof toast === "function") return toast(msg, bad);
    const area = document.getElementById("toast-area");   // the project page keeps its toast in a module, but shares the area
    if (!area) return console[bad ? "error" : "log"](msg);
    const el = document.createElement("div");
    el.className = "toast" + (bad ? " toast-error error" : "");
    el.textContent = msg;
    area.appendChild(el);
    setTimeout(() => el.remove(), 3500);
  };
  const minutes = s => (s ? `${Math.floor(s / 3600) ? Math.floor(s / 3600) + "h " : ""}${String(Math.floor(s / 60) % 60).padStart(2, "0")}m` : "");
  let box = null;

  async function api(method, path, body) {
    const resp = await fetch(path, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
  }

  async function awaiting() {
    try { return (await api("GET", "/api/outcome-prompts")).awaiting; } catch (_) { return []; }
  }

  function close() { box?.remove(); box = null; }

  function show(html) {
    if (!box) {
      box = document.createElement("div");
      box.setAttribute("style", "position:fixed;inset:0;z-index:2000;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.6);padding:12px");
      box.addEventListener("click", e => { if (e.target === box) close(); });
      document.body.appendChild(box);
    }
    box.innerHTML = `<div role="dialog" aria-modal="true" style="background:var(--surface,#16181f);color:var(--text,#e8eaf0);border:1px solid var(--border,#2a2d38);border-radius:12px;padding:18px;width:min(440px,100%);max-height:100%;overflow:auto;display:flex;flex-direction:column;gap:12px">${html}</div>`;
    return box;
  }

  const heading = item => `<h3 style="margin:0">${esc(item.printer_name)} finished ${esc(item.project_name ? (item.plate_name ? `${item.project_name} · ${item.plate_name}` : item.project_name) : item.file_name)}</h3>
    <div style="color:var(--text-muted,#9ca3af)">The printer reported <b>${esc(item.reported_outcome)}</b>${item.outcome_unconfirmed ? " (not confirmed yet)" : ""}
      · ${minutes(item.duration_s) || "under 1m"}${item.estimate_s ? ` of ${minutes(item.estimate_s)} estimated` : ""}</div>
    ${item.snapshot ? `<img src="${esc(item.snapshot)}" alt="Printer camera when the print ended" style="width:100%;border-radius:8px">` : ""}`;

  function question(item) {
    show(`${heading(item)}
      <button class="btn btn-primary" data-op="ok" style="min-height:48px">Printed OK · Bed cleared</button>
      <button class="btn btn-secondary" data-op="failed" style="min-height:48px">Failed…</button>
      <button class="btn btn-secondary btn-sm" data-op="later">Later</button>`);
    box.querySelector('[data-op="later"]').onclick = close;
    box.querySelector('[data-op="failed"]').onclick = () => sheet(item);
    box.querySelector('[data-op="ok"]').onclick = async () => {
      try {
        const r = await api("POST", `/api/printers/${encodeURIComponent(item.printer_id)}/bed-cleared`, { print_id: item.print_id, outcome: "completed" });
        close();
        say(r.started ? `Bed cleared. Starting ${r.started.file_name}` : r.start_error ? `Bed cleared, but nothing started: ${r.start_error}` : "Printed OK. Bed cleared");
      } catch (e) { say(e.message, true); }
    };
  }

  async function sheet(item) {
    let reasonList;
    try { reasonList = await failureReasons(); } catch (e) { say(`Couldn't load the failure reasons: ${e.message}`, true); return; }
    // A quick print has no plate the user knows of: its reprint is the Print itself (the server routes it), worded without "plate"
    const what = item.quick ? "print" : "plate";
    const canReprint = item.quick || !!(item.project_id && item.plate_id);
    show(`${heading(item)}
      <fieldset style="border:1px solid var(--border,#2a2d38);border-radius:8px"><legend>What went wrong? (pick at least one)</legend>
        ${reasonList.map(([id, label]) => `<label style="display:block;padding:4px 0"><input type="checkbox" data-reason value="${id}"> ${esc(label)}</label>`).join("")}</fieldset>
      <label>Note (optional) <textarea data-note rows="2" style="width:100%"></textarea></label>
      <label><input type="checkbox" data-bed checked> Bed cleared</label>
      ${canReprint ? `<label><input type="checkbox" data-reprint> Reprint this ${what} next</label>` : ""}
      <div style="display:flex;gap:8px"><button class="btn btn-primary" data-op="submit" style="flex:1;min-height:48px">Save failure</button>
        <button class="btn btn-secondary" data-op="back">Back</button></div>`);
    box.querySelector('[data-op="back"]').onclick = () => question(item);
    box.querySelector('[data-op="submit"]').onclick = async () => {
      const reasons = [...box.querySelectorAll("[data-reason]:checked")].map(c => c.value);
      if (!reasons.length) { say("Pick at least one failure reason", true); return; }
      const note = box.querySelector("[data-note]").value.trim() || undefined;
      const bed = box.querySelector("[data-bed]").checked, reprint = !!box.querySelector("[data-reprint]")?.checked;
      try {
        let result;
        if (bed) {
          result = await api("POST", `/api/printers/${encodeURIComponent(item.printer_id)}/bed-cleared`,
            { print_id: item.print_id, outcome: "failed", failure_reasons: reasons, notes: note, reprint });
        } else {
          await api("POST", `/api/prints/${item.print_id}/outcome`, { outcome: "failed", failure_reasons: reasons, notes: note });
          result = { reprint_error: null };
          if (reprint) {
            try { await api("POST", item.quick ? `/api/prints/${item.print_id}/reprint` : `/api/projects/${item.project_id}/plates/${item.plate_id}/reprint`, {}); }
            catch (e) { result.reprint_error = e.message; }
          }
        }
        close();
        say(result.reprint_error ? `Failure saved, but the ${what} was not re-queued: ${result.reprint_error}` : "Failure saved" + (reprint ? `. ${item.quick ? "Print" : "Plate"} queued again` : ""),
          !!result.reprint_error);
      } catch (e) { say(e.message, true); }
    };
  }

  async function open(printerId) {
    const item = (await awaiting()).find(a => a.printer_id === printerId);
    if (item) question(item);
    else say("Nothing is waiting for an answer on this printer");
  }

  return { open, awaiting };
})();
