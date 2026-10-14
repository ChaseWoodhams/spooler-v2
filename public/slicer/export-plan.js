/* Spooler workspace – the Orca export plan. GET /api/projects/{id}/export-plan says what one Orca project file can faithfully
   carry; the download is bound to that plan's revision and fingerprint, so a Project that changed since it was shown asks for a
   new plan instead of exporting something else. Nothing here chooses a lossy or substitute file. */

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
    <p data-role="error" role="alert" class="slicer-error"${error ? "" : " hidden"}>${esc(error || "")}</p>${blockers}${differences}${table}${objects}${limits}
    <p class="slicer-hint">${esc(plan.label)}</p>
    <div class="slicer-dialog-actions">
      <button type="button" class="btn btn-secondary" data-act="close">Close</button>
      <button type="button" class="btn btn-secondary" data-act="replan">Plan again</button>
      ${plan.faithful ? '<button type="button" class="btn btn-primary" data-act="download">Download Orca 3MF</button>' : ""}
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
    const draw = error => {
      dialog.innerHTML = planHtml(plan, { error });
      dialog.querySelector('[data-act="close"]').focus();
    };
    const replan = async notice => {
      await host.save();
      plan = await host.api("GET", `/api/projects/${projectId}/export-plan`);
      draw(notice);
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
      } catch (err) {
        const box = dialog.querySelector('[data-role="error"]');
        if (box) { box.textContent = err.message; box.hidden = false; }
      }
    });
    dialog.addEventListener("close", () => { dialog.remove(); done(plan); });
    document.body.appendChild(dialog);
    draw(null);
    dialog.showModal();
  });
}
