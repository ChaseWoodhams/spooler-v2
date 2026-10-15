/* When a control that is blocked but still focusable (aria-disabled) is pressed, bring the reason it already shows
   (the element named by aria-describedby) into view and emphasise it for a moment, so the click is never silent. */
const FLASH_MS = 1600;

export function showWhyBlocked(control) {
  const id = (control?.getAttribute("aria-describedby") || "").split(/\s+/)[0];
  const note = id ? document.getElementById(id) : null;
  if (!note) return;
  const calm = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  note.scrollIntoView({ block: "nearest", behavior: calm ? "auto" : "smooth" });
  note.classList.remove("reason-flash");
  void note.offsetWidth;   // restart the animation when pressed again
  note.classList.add("reason-flash");
  clearTimeout(note._flashTimer);
  note._flashTimer = setTimeout(() => note.classList.remove("reason-flash"), FLASH_MS);
}
