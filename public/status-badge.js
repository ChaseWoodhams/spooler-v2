/* Status badge: the one component for a Print's Outcome (plus "printing"). Every state has an icon shape AND a text
   label, so colour is never the only cue, and its colours (--st-* in style.css) are not shared with connection states.
   The labels are the Outcome vocabulary from GLOSSARY.md: "completed" is shown as "Completed" everywhere (the combined
   button "Printed OK · Bed cleared" is the action, not the Outcome).
   Public: OUTCOME_LABEL, OUTCOME_KIND, statusIcon(kind), statusBadge(kind, label?, title?), outcomeBadge(outcome, unconfirmed?) -> HTML. */
"use strict";

const OUTCOME_LABEL = { completed: "Completed", failed: "Failed", cancelled: "Cancelled", unknown: "Outcome unknown",
  unconfirmed: "Unconfirmed", printing: "Printing" };
// Outcome value from the API → badge kind (a Print with no Outcome yet is still printing)
const OUTCOME_KIND = { completed: "completed", failed: "failed", cancelled: "cancelled", unknown: "unknown" };
const _STATUS_ICON = {
  completed:   '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  failed:      '<path d="m6 6 12 12M18 6 6 18"/>',
  cancelled:   '<circle cx="12" cy="12" r="8"/><path d="m6.5 17.5 11-11"/>',
  unconfirmed: '<circle cx="12" cy="12" r="8" stroke-dasharray="3 3"/><path d="M12 8v4.5"/><circle cx="12" cy="16" r=".6"/>',
  unknown:     '<path d="M9.5 9.5a2.5 2.5 0 1 1 3.6 2.2c-.7.4-1.1.9-1.1 1.8"/><circle cx="12" cy="17" r=".6"/>',
  printing:    '<path d="M6 5v14l12-7z"/>',
};

function statusIcon(kind) {
  return `<svg class="sb-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${_STATUS_ICON[kind] || ""}</svg>`;
}

function statusBadge(kind, label, title) {
  const text = label || OUTCOME_LABEL[kind] || kind;
  const icon = statusIcon(kind);
  const tip = title ? ` title="${String(title).replace(/"/g, "&quot;")}"` : "";
  return `<span class="status-badge outcome-badge sb-${kind}"${tip}>${icon}<span>${String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;")}</span></span>`;
}

function outcomeBadge(outcome) {
  return statusBadge(outcome ? OUTCOME_KIND[outcome] || "unknown" : "printing");
}
function unconfirmedBadge() {
  return statusBadge("unconfirmed", null, "Reported by the printer, not confirmed by you");
}
