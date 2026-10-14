/* The review dialog for adopting a newer preset or Spoolman properties into a Project: what is validated against,
   the before/after of every setting that would change, what stays as saved, and anything that blocks it. */
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function showValue(value) {
  if (value === undefined) return "not set";
  if (value === null || value === "nil") return "inherited";
  if (Array.isArray(value)) return value.map(showValue).join(", ");
  return String(value);
}

const names = list => list.map(p => p.name).join(", ");

export function blockers(preview) {
  return preview.issues.filter(i => i.resolution_required);
}

export function acknowledgements(preview) {
  return preview.issues.filter(i => i.acknowledgement_required);
}

export function reviewTitle(preview) {
  return preview.source === "spool" ? `Apply spool ${preview.spool.name}`
    : preview.applies_to === "model-filament" ? `Update ${preview.target.preset} for Model ${preview.target.model_id}` : "Refresh the Project's Filament preset";
}

export function reviewHtml(preview, { error = null } = {}) {
  const base = preview.base;
  const rows = preview.changes.map(c => `<tr data-key="${esc(c.key)}"><th scope="row">${esc(c.key)}</th><td>${esc(showValue(c.before))}</td><td>${esc(showValue(c.after))}</td></tr>`).join("");
  const scope = preview.applies_to === "project-default"
    ? "Project default Filament only. Models with their own Filament keep their saved snapshots."
    : "This Model's Filament only, shared by its Copies. The Project default and other Models are unchanged.";
  const alternates = (preview.preserved.alternates || []).map(a => `${esc(a.preset)} (Model ${a.model_id}, ${esc((a.material || []).join(", ") || "material unverified")})`);
  const now = preview.current ? `Now: ${esc(preview.current.name)}, ${esc((preview.current.material || []).join(", "))}. ` : "Now: no verified saved material. ";
  const after = preview.proposed ? `After: ${esc(preview.proposed.name)}, ${esc((preview.proposed.material || []).join(", ") || "unknown")}, ${esc(preview.proposed.nozzle)} mm nozzle.` : "";
  const lines = [
    `<p class="slicer-muted" data-review="base">Checked against ${esc(base.machine)}, ${esc(base.nozzle)} mm nozzle (${esc(base.validated_against)}).</p>`,
    `<p data-review="scope">${esc(scope)}</p>`,
    preview.current || preview.proposed ? `<p data-review="material">${now}${after}</p>` : "",
    preview.unchanged ? '<p data-review="unchanged">The Project already matches; there is nothing to adopt.</p>' : "",
    preview.changes.length ? `<table class="review-changes"><thead><tr><th>Setting</th><th>Before</th><th>After</th></tr></thead><tbody>${rows}</tbody></table>`
      : preview.unchanged ? "" : '<p class="slicer-muted">No setting values change.</p>',
    preview.cleared?.length ? `<p class="slicer-muted" data-review="cleared">Removed from the Project default: ${esc(preview.cleared.join(", "))}</p>` : "",
    preview.skipped?.length ? `<p class="slicer-muted" data-review="skipped">Not applied (the preset does not accept them): ${esc(preview.skipped.map(s => s.key).join(", "))}</p>` : "",
    alternates.length ? `<p class="slicer-muted" data-review="alternates">Unchanged: ${alternates.join("; ")}.</p>` : "",
    preview.preserved.project_overrides?.length ? `<p class="slicer-muted" data-review="overrides">Your Project overrides stay: ${esc(preview.preserved.project_overrides.join(", "))}.</p>` : "",
    preview.affected.plates.length ? `<p class="slicer-muted" data-review="plates">Reaches ${esc(names(preview.affected.plates))}.${preview.affected.outdated_slices.length ? ` Sliced Plates become Outdated: ${preview.affected.outdated_slices.join(", ")}.` : ""}</p>` : "",
    preview.source === "spool" ? `<p class="slicer-muted" data-review="stock">Remaining stock: ${preview.stock.remaining_weight != null ? `${Math.round(preview.stock.remaining_weight)} g` : "unavailable"}. Shown for information; it is not a slicing setting and is not saved.</p>` : "",
  ];
  const issues = preview.issues.map(i => `<li class="review-issue review-${esc(i.severity)}" data-code="${esc(i.code)}">${esc(i.message)}</li>`).join("");
  const checks = acknowledgements(preview).map(i => `<label class="slicer-check"><input type="checkbox" data-accept="${esc(i.code.replace(/-/g, "_"))}"> ${esc(i.message)}. Adopt anyway.</label>`).join("");
  return `${lines.join("")}${issues ? `<ul class="review-issues" role="alert">${issues}</ul>` : ""}${checks}${error ? `<p class="slicer-warn" role="alert" data-review="error">${esc(error)}</p>` : ""}`;
}
