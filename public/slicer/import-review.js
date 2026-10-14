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

function blockerHtml(blocker) {
  const detail = blocker.requested && blocker.proposed
    ? `<dl class="import-substitution"><dt>The file asks for</dt><dd>${esc(material(blocker.requested))}</dd><dt>Spooler would use</dt><dd>${esc(material(blocker.proposed))}</dd></dl>`
    : "";
  return `<li><strong>${esc(blocker.code.replace(/-/g, " "))}</strong>: ${esc(blocker.message)}${detail}</li>`;
}

function render(review) {
  const { report } = review;
  const groups = groupLosses(report.losses);
  const losses = groups.map(group => `<section class="import-group"><h4>${SCOPES[group.scope] || group.scope} “${esc(group.name)}”</h4><ul>${
    group.items.map(loss => `<li data-code="${esc(loss.code)}"><strong>${esc(loss.feature)}</strong> — ${esc(loss.effect)}</li>`).join("")}</ul></section>`).join("");
  const blocked = report.blockers.length
    ? `<section class="import-blockers" role="alert"><h4>Resolve before importing</h4><ul>${report.blockers.map(blockerHtml).join("")}</ul>
       <p class="slicer-hint">A generic Continue with losses cannot approve these. Choose a compatible preset, then import again.</p></section>` : "";
  const notes = review.notes.length ? `<details class="import-notes"><summary>${review.notes.length === 1 ? "1 note" : `${review.notes.length} notes`}</summary><ul>${review.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul></details>` : "";
  return `<h3 id="import-review-title">Review import of ${esc(review.filename)}</h3>
    <p class="slicer-hint">Nothing in your Project has changed. ${report.losses.length
      ? "These features are not carried into Spooler, and no Slice will include them."
      : ""}</p>${blocked}${losses}${notes}
    <div class="slicer-dialog-actions">
      <button type="button" class="btn btn-secondary" data-act="cancel">${report.blockers.length ? "Cancel import" : "Cancel"}</button>
      ${report.blockers.length ? "" : '<button type="button" class="btn btn-primary" data-act="continue">Continue with losses</button>'}
    </div>`;
}

/** Show the report; resolves true for Continue with losses, false for Cancel (or Escape) */
export function showReview(review) {
  return new Promise(resolve => {
    const dialog = document.createElement("dialog");
    dialog.id = "dlg-import-review";
    dialog.className = "slicer-dialog slicer-dialog-wide";
    dialog.setAttribute("aria-labelledby", "import-review-title");
    dialog.innerHTML = render(review);
    let answer = false;
    dialog.addEventListener("click", e => {
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (!act) return;
      answer = act === "continue";
      dialog.close();
    });
    dialog.addEventListener("close", () => { dialog.remove(); resolve(answer); });
    document.body.appendChild(dialog);
    dialog.showModal();
    dialog.querySelector('[data-act="cancel"]').focus();   // the safe choice first
  });
}

/** Stage `file` for `target` (a Project id, or none for a new Project), review it, and commit it when accepted.
    host: { api(method, path, body), upload(url, file, onProgress) }. Resolves to the committed result (the Project, with
    import_notes and import_record), or null when the review was cancelled and nothing changed. */
export async function importWithReview({ file, host, target = null, draftToken = null, query = {}, onProgress = () => {} }) {
  const params = new URLSearchParams({ filename: file.name, scale: "1", up: "z", ...query });
  if (target != null) {
    params.set("target", target);
    if (draftToken != null) params.set("draft_token", draftToken);
  }
  const review = await host.upload(`/api/projects/import-review?${params}`, file, onProgress);
  const accepted = review.required || review.blocked ? await showReview(review) : true;
  if (!accepted) {
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
