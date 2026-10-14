/* Spooler workspace – the import review for 3MF files. A 3MF is staged and inspected first (POST /api/projects/import-review);
   nothing in any Project changes until the review is confirmed. A file with nothing to disclose is confirmed straight away. One
   that loses features opens this dialog, naming every affected Model, Object or Plate and what is lost; Continue with losses
   commits exactly what was reviewed, Cancel throws the staging away. Blocking problems (an unmatched material or printer,
   unreadable settings) have no Continue: they must be resolved first. The same dialog serves the New project page, Add models
   and the Projects page. */

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const SCOPES = { model: "Model", object: "Object", plate: "Plate", project: "Project" };

export const isReviewed = file => /\.3mf$/i.test(file?.name || "");

/** The losses grouped by what they affect: [{ scope, name, items: [loss] }], in report order */
export function groupLosses(losses) {
  const groups = new Map();
  for (const loss of losses) {
    const key = `${loss.scope}:${loss.id}:${loss.name}`;
    if (!groups.has(key)) groups.set(key, { scope: loss.scope, name: loss.name, items: [] });
    groups.get(key).items.push(loss);
  }
  return [...groups.values()];
}

function material(side) {
  return [side?.filament || side?.printer, side?.material && `(${side.material})`, side?.nozzle && `${side.nozzle} mm nozzle`].filter(Boolean).join(" ");
}

function differencesTable(rows) {
  return rows.length ? `<table class="import-diff"><thead><tr><th>Setting</th><th>In the file</th><th>With this preset</th></tr></thead><tbody>${
    rows.map(d => `<tr><td>${esc(d.key)}</td><td>${esc(d.before)}</td><td>${esc(d.after)}</td></tr>`).join("")}</tbody></table>` : "";
}

/** The ways out of an unmatched material: the file's own configuration, or a named preset (with what would differ) */
function materialChoices(blocker) {
  const { imported, presets } = blocker.resolutions;
  const own = imported.available
    ? `<label><input type="radio" name="material" value="imported" checked> Use the file's own ${esc(imported.material)} settings
         <span class="slicer-hint">(kept as an immutable snapshot, ${esc(imported.nozzle)} mm nozzle)</span></label>`
    : `<label><input type="radio" name="material" value="imported" disabled> The file's own settings can't be used
         <span class="slicer-hint">${esc(imported.reason)}</span></label>`;
  const options = presets.map(p => `<option value="${esc(p.preset)}">${esc(p.preset)} (${esc(p.material)}${p.same_material ? "" : " — a different material"})</option>`).join("");
  return `<fieldset class="import-resolution"><legend>Choose the material</legend>${own}
    <label><input type="radio" name="material" value="preset"${imported.available ? "" : " checked"}> Use a preset</label>
    <select aria-label="Material preset" data-role="preset">${options}</select>
    <div data-role="preset-diff"></div>
    <label data-role="accept" hidden><input type="checkbox" data-role="accept-box"> Print this in <span data-role="accept-material"></span>, not ${esc(blocker.requested.material || "the file's material")}</label>
    <button type="button" class="btn btn-primary" data-act="use-material">Use this material</button></fieldset>`;
}

function printerChoices(blocker) {
  return `<fieldset class="import-resolution"><legend>Choose the printer</legend>
    <select aria-label="Printer preset" data-role="printer">${blocker.resolutions.printers.map(p => `<option value="${esc(p.printer)}">${esc(p.printer)} (${esc(p.nozzle)} mm nozzle)</option>`).join("")}</select>
    <button type="button" class="btn btn-primary" data-act="use-printer">Use this printer</button></fieldset>`;
}

function conflictChoices() {
  return `<div class="import-resolution"><button type="button" class="btn btn-secondary" data-act="separate">Import as a separate Project</button>
    <button type="button" class="btn btn-secondary" data-act="convert">Convert to this Project's printer and bed</button></div>`;
}

function blockerHtml(blocker) {
  const detail = blocker.requested && blocker.proposed
    ? `<dl class="import-substitution"><dt>The file asks for</dt><dd>${esc(material(blocker.requested))}</dd><dt>Spooler would use</dt><dd>${esc(material(blocker.proposed))}</dd></dl>`
    : "";
  const choices = blocker.code === "unmatched-material" && blocker.resolutions ? materialChoices(blocker)
    : blocker.code === "unmatched-printer" && blocker.resolutions ? printerChoices(blocker)
    : blocker.options ? conflictChoices() : "";
  return `<li data-blocker="${esc(blocker.code)}"><strong>${esc(blocker.code.replace(/-/g, " "))}</strong>: ${esc(blocker.message)}${detail}${choices}</li>`;
}

function resolutionHtml(report) {
  const applied = report.resolution || {};
  const lines = [];
  if (applied.material) lines.push(applied.material.choice === "imported"
    ? `Material: the file's own ${esc(applied.material.material)} settings (${esc(applied.material.name)}).`
    : `Material: ${esc(applied.material.preset)}${applied.material.material_change ? ` — a change to ${esc(applied.material.material)} that you accepted` : ""}.`);
  if (applied.printer) lines.push(`Printer: ${esc(applied.printer)}.`);
  if (applied.conflicts) lines.push("Converted to this Project's printer and bed.");
  const left = (report.differences || []).map(d => d.keys ? `${d.keys.length} settings not in the presets (${d.keys.slice(0, 4).join(", ")}${d.keys.length > 4 ? ", …" : ""})` : `${d.key} — ${d.reason}`);
  return (lines.length ? `<section class="import-resolved"><h4>Your choices</h4><ul>${lines.map(l => `<li>${l}</li>`).join("")}</ul></section>` : "")
    + (left.length ? `<details class="import-differences"><summary>${left.length === 1 ? "1 difference remains" : `${left.length} differences remain`} — fidelity is not complete</summary><ul>${left.map(l => `<li>${esc(l)}</li>`).join("")}</ul></details>` : "");
}

function render(review) {
  const { report } = review;
  const groups = groupLosses(report.losses);
  const losses = groups.map(group => `<section class="import-group"><h4>${SCOPES[group.scope] || group.scope} “${esc(group.name)}”</h4><ul>${
    group.items.map(loss => `<li data-code="${esc(loss.code)}"><strong>${esc(loss.feature)}</strong> — ${esc(loss.effect)}</li>`).join("")}</ul></section>`).join("");
  const blocked = report.blockers.length
    ? `<section class="import-blockers" role="alert"><h4>Resolve before importing</h4><ul>${report.blockers.map(blockerHtml).join("")}</ul>
       <p class="slicer-hint">A generic Continue with losses cannot approve these. Choose how to resolve each, or cancel.</p></section>` : "";
  const notes = review.notes.length ? `<details class="import-notes"><summary>${review.notes.length === 1 ? "1 note" : `${review.notes.length} notes`}</summary><ul>${review.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul></details>` : "";
  return `<h3 id="import-review-title">Review import of ${esc(review.filename)}</h3>
    <p class="slicer-hint">Nothing in your Project has changed. ${report.losses.length
      ? "These features are not carried into Spooler, and no Slice will include them."
      : ""}</p><p data-role="error" role="alert" class="slicer-error" hidden></p>${blocked}${losses}${resolutionHtml(report)}${notes}
    <div class="slicer-dialog-actions">
      <button type="button" class="btn btn-secondary" data-act="cancel">${report.blockers.length ? "Cancel import" : "Cancel"}</button>
      ${report.blockers.length ? "" : `<button type="button" class="btn btn-primary" data-act="continue">${report.losses.length ? "Continue with losses" : "Import"}</button>`}
    </div>`;
}

/** Show the report. `resolve(resolution)` re-stages the review with an explicit choice and returns the new review.
    Resolves to { action: "continue" | "cancel" | "separate", review } with the latest review (its binding is what Continue commits). */
export function showReview(review, resolve = null) {
  return new Promise(done => {
    const dialog = document.createElement("dialog");
    dialog.id = "dlg-import-review";
    dialog.className = "slicer-dialog slicer-dialog-wide";
    dialog.setAttribute("aria-labelledby", "import-review-title");
    let answer = "cancel";
    const draw = () => {
      dialog.innerHTML = render(review);
      const select = dialog.querySelector('[data-role="preset"]');
      if (select) {
        const show = () => {
          const preset = review.report.blockers.find(b => b.code === "unmatched-material").resolutions.presets.find(p => p.preset === select.value);
          dialog.querySelector('[data-role="preset-diff"]').innerHTML = differencesTable(preset.differences);
          dialog.querySelector('[data-role="accept"]').hidden = preset.same_material;
          dialog.querySelector('[data-role="accept-material"]').textContent = preset.material;
        };
        select.addEventListener("change", () => { dialog.querySelector('input[value="preset"]').checked = true; show(); });
        show();
      }
      dialog.querySelector('[data-act="cancel"]').focus();   // the safe choice first
    };
    const attempt = async resolution => {
      try {
        review = await resolve(resolution);
        draw();
      } catch (err) {
        const error = dialog.querySelector('[data-role="error"]');
        error.textContent = err.message;
        error.hidden = false;
      }
    };
    dialog.addEventListener("click", e => {
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (!act) return;
      if (act === "use-material") {
        const kind = dialog.querySelector('input[name="material"]:checked')?.value;
        attempt(kind === "preset"
          ? { material: { choice: "preset", preset: dialog.querySelector('[data-role="preset"]').value, accept_material_change: dialog.querySelector('[data-role="accept-box"]').checked } }
          : { material: { choice: "imported" } });
      } else if (act === "use-printer") attempt({ printer: dialog.querySelector('[data-role="printer"]').value });
      else if (act === "convert") attempt({ conflicts: "convert" });
      else { answer = act; dialog.close(); }
    });
    dialog.addEventListener("close", () => { dialog.remove(); done({ action: answer, review }); });
    document.body.appendChild(dialog);
    draw();
    dialog.showModal();
    dialog.querySelector('[data-act="cancel"]').focus();
  });
}

/** Stage `file` for `target` (a Project id, or none for a new Project), review it, and commit it when accepted.
    host: { api(method, path, body), upload(url, file, onProgress) }. Resolves to the committed result (the Project, with
    import_notes and import_record), or null when the review was cancelled and nothing changed. A file that can't join `target`
    may be imported as a separate Project instead; the result then carries `separate: true`. */
export async function importWithReview({ file, host, target = null, draftToken = null, query = {}, onProgress = () => {} }) {
  const params = new URLSearchParams({ filename: file.name, scale: "1", up: "z", ...query });
  if (target != null) {
    params.set("target", target);
    if (draftToken != null) params.set("draft_token", draftToken);
  }
  let review = await host.upload(`/api/projects/import-review?${params}`, file, onProgress);
  const resolve = resolution => host.api("POST", `/api/projects/import-review/${review.token}/resolve`,
    draftToken != null ? { ...resolution, draft_token: draftToken } : resolution).then(next => { review = next; return next; });
  let action = "continue";
  if (review.required || review.blocked) ({ action, review } = await showReview(review, resolve));
  if (action === "separate") {
    await host.api("DELETE", `/api/projects/import-review/${review.token}`).catch(() => {});
    const { plate, ...alone } = query;
    const result = await importWithReview({ file, host, query: alone, onProgress });
    return result && { ...result, separate: true };
  }
  if (action !== "continue") {
    await host.api("DELETE", `/api/projects/import-review/${review.token}`).catch(() => {});
    return null;
  }
  const body = { ...review.binding, acknowledged: true };
  if (draftToken != null) body.draft_token = draftToken;
  return host.api("POST", `/api/projects/import-review/${review.token}/confirm`, body);
}

/** The accepted imports of an open Project, newest first (loss reports and downloadable originals) */
export const importRecords = (host, projectId) => host.api("GET", `/api/projects/${projectId}/imports`).then(r => r.imports);

/** The accepted imports of an open Project: the losses accepted with each and a link to the original archive */
export async function showRecords(host, projectId) {
  const records = await importRecords(host, projectId);
  const dialog = document.createElement("dialog");
  dialog.id = "dlg-import-records";
  dialog.className = "slicer-dialog slicer-dialog-wide";
  dialog.setAttribute("aria-labelledby", "import-records-title");
  dialog.innerHTML = `<h3 id="import-records-title">Imported files</h3>${records.length ? records.map(record => `<section class="import-group">
      <h4>${esc(record.filename)} <a href="/api/projects/${projectId}/imports/${record.id}/source" download>Download original</a></h4>
      <p class="slicer-hint">Imported ${esc(new Date(record.accepted_at * 1000).toLocaleString())}${record.losses.length ? " with these losses accepted" : " with nothing lost"}.</p>
      ${groupLosses(record.losses).map(group => `<h5>${SCOPES[group.scope] || group.scope} “${esc(group.name)}”</h5><ul>${
        group.items.map(loss => `<li><strong>${esc(loss.feature)}</strong> — ${esc(loss.effect)}</li>`).join("")}</ul>`).join("")}
    </section>`).join("") : '<p class="slicer-hint">No 3MF has been imported into this Project.</p>'}
    <div class="slicer-dialog-actions"><button type="button" class="btn btn-secondary" data-act="close">Close</button></div>`;
  dialog.addEventListener("click", e => { if (e.target.closest("[data-act='close']")) dialog.close(); });
  dialog.addEventListener("close", () => dialog.remove());
  document.body.appendChild(dialog);
  dialog.showModal();
}
