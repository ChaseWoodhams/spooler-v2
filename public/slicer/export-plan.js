/* Spooler workspace – the Orca export plan. GET /api/projects/{id}/export-plan says what one Orca project file can faithfully
   carry; the download is bound to that plan's revision and fingerprint, so a Project that changed since it was shown asks for a
   new plan instead of exporting something else. The faithful download never chooses a lossy or substitute file; the lossy
   whole-Project 3MF is a separate, explicitly acknowledged choice (see lossyHtml). */

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const show = value => Array.isArray(value) ? value.join("; ") : value === null || value === undefined ? "(not set)" : String(value);

export function planHtml(plan, { error = null } = {}) {
  const materials = plan.plates.map(p => `<tr><td>${esc(p.name)}</td><td>${esc(p.material.type || "?")} · ${esc(p.material.preset || "")}${p.material.colour ? ` · ${esc(p.material.colour)}` : ""}</td><td>${p.objects}</td></tr>`).join("");
  const table = plan.plates.length ? `<table class="import-diff"><thead><tr><th>Plate</th><th>Material</th><th>Objects</th></tr></thead><tbody>${materials}</tbody></table>` : "";
  const blockers = plan.blockers.length ? `<section data-role="blockers"><h4>Resolve before exporting</h4><ul>${
    plan.blockers.map(b => `<li data-code="${esc(b.code)}">${esc(b.message)}</li>`).join("")}</ul></section>` : "";
  const differences = plan.differences.length ? `<section data-role="differences"><h4>Why one file cannot keep these settings</h4>
      <p class="slicer-hint">Plates are set up differently. An Orca project file has one machine, process and filament configuration, and Spooler will not copy a Plate setting onto Objects to hide that.</p>
      <table class="import-diff"><thead><tr><th>Setting</th><th>Values per Plate</th></tr></thead><tbody>${
        plan.differences.map(d => `<tr><td>${esc(d.key)}</td><td>${d.plates.map(p => `${esc(p.name)}: ${esc(show(p.value))}`).join(" · ")}</td></tr>`).join("")}</tbody></table></section>` : "";
  const objects = plan.faithful && plan.object_overrides.length ? `<section data-role="objects"><h4>Object settings carried</h4><ul>${
    plan.object_overrides.map(o => `<li>${esc(o.name)}: ${esc(o.key)} = ${esc(show(o.value))}</li>`).join("")}</ul></section>` : "";
  const limits = plan.limitations.length ? `<section data-role="limitations"><h4>Not reproduced by an Orca file</h4><ul>${
    plan.limitations.map(l => `<li data-code="${esc(l.code)}">${esc(l.message)} (${l.plates.map(esc).join(", ")})</li>`).join("")}</ul></section>` : "";
  const lead = plan.faithful
    ? `<p>One Orca project file keeps this Project's Plates, placement, material and settings. It is named <strong>${esc(plan.filename)}</strong>.</p>`
    : `<p><strong>A single Orca file cannot faithfully keep this Project.</strong> Nothing has been exported.</p>`;
  return `<h3 id="export-plan-title">Export to Orca</h3>${lead}
    <p data-role="error" role="alert" class="slicer-error"${error ? "" : " hidden"}>${esc(error || "")}</p>${blockers}${differences}${table}${objects}${limits}${lossyTeaserHtml(plan)}
    <p class="slicer-hint">${esc(plan.label)}</p>
    <div class="slicer-dialog-actions">
      <button type="button" class="btn btn-secondary" data-act="close">Close</button>
      <button type="button" class="btn btn-secondary" data-act="replan">Plan again</button>
      ${plan.faithful ? '<button type="button" class="btn btn-primary" data-act="download">Download Orca 3MF</button>' : ""}
    </div>`;
}

const CATEGORIES = { material: "Material", bed: "Bed", "plate-settings": "Plate settings", machine: "Machine" };
const materialText = m => `${m.type || "?"} · ${m.preset || ""}`;

/** The pointer to the lossy alternative, shown with the plan. It only offers the review; nothing is exported from here. */
export function lossyTeaserHtml(plan) {
  const lossy = plan.lossy;
  if (!lossy?.available) return "";
  return `<section data-role="lossy-offer"><h4>Convenient alternative: one lossy file</h4>
      <p class="slicer-hint">A single Orca file can still be written, but it would change what some Plates print. Review exactly what would differ before choosing it.</p>
      <button type="button" class="btn btn-secondary" data-act="lossy-review">Review the lossy alternative…</button></section>`;
}

/** The review of what the lossy file changes, with the acknowledgement it needs. */
export function lossyHtml(plan, { error = null, acknowledged = false } = {}) {
  const lossy = plan.lossy;
  const plates = `<section data-role="lossy-plates"><h4>Material per Plate</h4><table class="import-diff"><thead><tr><th>Plate</th><th>Your material</th><th>The file would contain</th></tr></thead><tbody>${
    lossy.plates.map(p => `<tr data-material-changed="${p.material_changed}"><td>${esc(p.name)}</td><td>${esc(materialText(p.material))}</td><td>${esc(materialText(p.written_as))}${p.material_changed ? " <strong>(material changed)</strong>" : ""}</td></tr>`).join("")}</tbody></table></section>`;
  const groups = Object.keys(CATEGORIES).map(category => {
    const rows = lossy.differences.filter(d => d.category === category);
    return rows.length ? `<section data-role="lossy-differences" data-category="${category}"><h4>${CATEGORIES[category]} differences</h4><table class="import-diff"><thead><tr><th>Setting</th><th>Each Plate uses</th><th>The file writes</th></tr></thead><tbody>${
      rows.map(d => `<tr><td>${esc(d.key)}</td><td>${d.plates.map(p => `${esc(p.name)}: ${esc(show(p.value))}${p.kept ? "" : " (lost)"}`).join(" · ")}</td><td>${esc(show(d.written))}</td></tr>`).join("")}</tbody></table></section>` : "";
  }).join("");
  const inherited = lossy.inherited.length ? `<section data-role="lossy-inherited"><h4>Inherited values Spooler cannot check</h4><p class="slicer-hint">These come from locked presets and are carried into the file unchanged; Spooler has no control that validates or edits them.</p><ul>${
    lossy.inherited.map(i => `<li>${esc(i.key)} = ${esc(show(i.value))} (${i.plates.map(esc).join(", ")})</li>`).join("")}</ul></section>` : "";
  const features = lossy.features.length ? `<section data-role="lossy-features"><h4>Advanced Orca features already lost at import</h4><p class="slicer-hint">These were accepted when the Models were imported. The original files stay downloadable with the Project; this export does not bring them back.</p><ul>${
    lossy.features.flatMap(r => r.losses.map(l => `<li>${esc(l.name)}: ${esc(l.feature)}</li>`)).join("")}</ul></section>` : "";
  const bricks = lossy.bricklayers ? `<section data-role="lossy-bricklayers"><h4>BrickLayers</h4><p>${esc(lossy.bricklayers.message)} Affected: ${lossy.bricklayers.plates.map(esc).join(", ")}.</p></section>` : "";
  const objects = lossy.object_overrides.length ? `<section data-role="lossy-objects"><h4>Object settings still carried</h4><ul>${
    lossy.object_overrides.map(o => `<li>${esc(o.name)}: ${esc(o.key)} = ${esc(show(o.value))}</li>`).join("")}</ul></section>` : "";
  const alternatives = `<section data-role="faithful-alternatives"><h4>Faithful alternatives</h4><ul>${
    lossy.faithful_alternatives.map(a => `<li>${a.representation === "single-3mf" ? "One faithful Orca file" : esc(a.representation)}: ${a.available ? "available" : `not available (${esc(a.why || "")})`}</li>`).join("")}
      <li>The actual sliced G-code of each Plate is still a separate download and is what prints.</li></ul></section>`;
  return `<h3 id="export-plan-title">Export one lossy Orca file</h3>
    <p><strong>This file will not keep your Project's settings.</strong> It is one Orca project written with the configuration of <strong>${esc(lossy.basis.plate_name)}</strong> (${esc(materialText(lossy.basis.material))}) for every Plate. Nothing is exported until you acknowledge.</p>
    <p data-role="error" role="alert" class="slicer-error"${error ? "" : " hidden"}>${esc(error || "")}</p>
    ${plates}${groups}${objects}${inherited}${features}${bricks}${alternatives}
    <label class="slicer-check"><input type="checkbox" data-role="acknowledge"${acknowledged ? " checked" : ""}> I understand this file differs from my Project as listed above.</label>
    <div class="slicer-dialog-actions">
      <button type="button" class="btn btn-secondary" data-act="lossy-cancel">Cancel</button>
      <button type="button" class="btn btn-primary" data-act="lossy-download"${acknowledged ? "" : " disabled"}>Download ${esc(lossy.filename)}</button>
    </div>`;
}

/** Plan the export of `projectId` and show it. host: { api(method, path), toast(message, error), save() } where save() flushes
    pending edits first. Resolves when the dialog closes. */
export async function showExport(host, projectId) {
  await host.save();
  let plan = await host.api("GET", `/api/projects/${projectId}/export-plan`);
  return new Promise(done => {
    const dialog = document.createElement("dialog");
    dialog.id = "dlg-export-plan";
    dialog.className = "slicer-dialog slicer-dialog-wide";
    dialog.setAttribute("aria-labelledby", "export-plan-title");
    let view = "plan";
    const draw = error => {
      if (view === "lossy" && plan.lossy?.available) {
        dialog.innerHTML = lossyHtml(plan, { error });
        dialog.querySelector('[data-act="lossy-cancel"]').focus();   // the safe choice has the focus
      } else {
        view = "plan";
        dialog.innerHTML = planHtml(plan, { error });
        dialog.querySelector('[data-act="close"]').focus();
      }
    };
    const replan = async notice => {
      await host.save();
      plan = await host.api("GET", `/api/projects/${projectId}/export-plan`);
      draw(notice);
    };
    const downloadLossy = async () => {
      const lossy = plan.lossy;
      const acknowledgement = { representation: lossy.representation, revision: plan.revision, fingerprint: plan.fingerprint,
        report_version: lossy.report_version, report_fingerprint: lossy.report_fingerprint, acknowledged: true };
      const response = await fetch(`/api/projects/${projectId}/export-lossy`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(acknowledgement) });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        if (body.issues?.[0]?.code === "export-acknowledgement-stale") { view = "plan"; return replan("The Project changed since you reviewed these losses. Review the new plan; nothing was exported."); }
        throw new Error(body.error || "The export failed");
      }
      const link = document.createElement("a");
      link.href = URL.createObjectURL(await response.blob());
      link.download = lossy.filename;
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 10000);
      host.toast(`Exported ${lossy.filename} (lossy)`);
    };
    const download = async () => {
      const query = new URLSearchParams({ revision: plan.revision, fingerprint: plan.fingerprint });
      const response = await fetch(`/api/projects/${projectId}/export.3mf?${query}`);
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        if (body.issues?.[0]?.code === "export-plan-stale") return replan("The Project changed since this plan was made. Review the new plan before downloading.");
        throw new Error(body.error || "The export failed");
      }
      const link = document.createElement("a");
      link.href = URL.createObjectURL(await response.blob());
      link.download = plan.filename;
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 10000);
      host.toast(`Exported ${plan.filename}`);
    };
    dialog.addEventListener("click", async e => {
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (!act) return;
      try {
        if (act === "close") dialog.close();
        else if (act === "replan") await replan(null);
        else if (act === "download") await download();
        else if (act === "lossy-review") { view = "lossy"; draw(null); }
        else if (act === "lossy-cancel") { view = "plan"; draw(null); }
        else if (act === "lossy-download") await downloadLossy();
      } catch (err) {
        const box = dialog.querySelector('[data-role="error"]');
        if (box) { box.textContent = err.message; box.hidden = false; }
      }
    });
    dialog.addEventListener("change", e => {
      if (e.target.dataset?.role === "acknowledge") dialog.querySelector('[data-act="lossy-download"]').disabled = !e.target.checked;
    });
    dialog.addEventListener("close", () => { dialog.remove(); done(plan); });
    document.body.appendChild(dialog);
    draw(null);
    dialog.showModal();
  });
}
