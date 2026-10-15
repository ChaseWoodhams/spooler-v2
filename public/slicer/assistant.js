// Assistant panel (optional): docks on the right of the project page. This shell shows availability and the empty
// thread (what the assistant can and can't do, the Purpose summary, example prompts). Asking, Proposal cards and the
// review hand-off attach to the same panel in later tickets through hooks.ask and the thread area.
// Nothing here runs until the person opens the panel, so the page loads and works the same without an assistant.
const OPEN_KEY = "slicer.assistant.open";
const CHIPS = ["Make these parts stronger", "Fit more on this plate", "Why is this plate so slow?"];
const PURPOSE_NAMES = { general: "General", functional: "Functional", display: "Display", draft: "Draft" };
const MAX_LISTED = 5;

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const remembered = () => { try { return localStorage.getItem(OPEN_KEY) === "1"; } catch { return false; } };
const remember = open => { try { localStorage.setItem(OPEN_KEY, open ? "1" : "0"); } catch { /* private window: fine */ } };

export const assistantPanel = {
  hooks: null,
  open: false,
  status: null,        // null = not asked yet; else {available, reason, purpose, checking?}
  loading: false,
  fetched: null,       // "<project id>:<revision>" the status was read for
  sequence: 0,

  // hooks: { api(method, path), project() -> record | null, ask?(text) }
  attach(hooks) {
    this.hooks = hooks;
    this.panel = document.getElementById("assistant-panel");
    this.button = document.getElementById("btn-assistant");
    this.button.addEventListener("click", () => this.setOpen(!this.open));
    this.panel.addEventListener("click", event => this.click(event));
    this.panel.addEventListener("input", event => { if (event.target.classList.contains("assistant-input")) this.draftChanged(); });
    this.panel.addEventListener("keydown", event => {
      if (event.key === "Escape") { this.setOpen(false); this.button.focus(); }
    });
    if (remembered()) this.setOpen(true, { focus: false });
  },

  setOpen(open, { focus = true } = {}) {
    this.open = open;
    remember(open);
    document.body.classList.toggle("assistant-open", open);
    this.panel.hidden = !open;
    this.button.setAttribute("aria-expanded", String(open));
    if (open) { this.refresh(); if (focus) this.focusStart(); }
  },

  focusStart() {
    (this.panel.querySelector(".assistant-input:not(:disabled)") || this.panel.querySelector("button"))?.focus();
  },

  // Called whenever the page re-renders: read the (cached, cheap) status again when the saved project changed.
  update() {
    if (!this.open) return;
    const project = this.hooks.project();
    if (project && this.fetched !== `${project.id}:${project.revision}` && !this.loading) this.refresh();
  },

  async refresh({ fresh = false } = {}) {
    const project = this.hooks.project();
    if (!project) return;
    const ticket = ++this.sequence;
    const key = `${project.id}:${project.revision}`;
    this.loading = true;
    this.render();
    try {
      const status = await this.hooks.api("GET", `/api/projects/${project.id}/assistant/status${fresh ? "?fresh=1" : ""}`);
      if (ticket !== this.sequence) return;
      this.status = status;
    } catch (error) {
      if (ticket !== this.sequence) return;
      this.status = { available: false, reason: `The assistant could not be reached: ${error.message}.`, purpose: this.status?.purpose || null };
    }
    this.loading = false;
    this.fetched = key;
    this.render();
    if (this.status.checking) setTimeout(() => this.open && this.refresh(), 1500);     // the check outlasted the wait: read it again
  },

  click(event) {
    const target = event.target.closest("button");
    if (!target || !this.panel.contains(target)) return;
    const action = target.dataset.action;
    if (action === "close") { this.setOpen(false); this.button.focus(); }
    else if (action === "recheck") this.refresh({ fresh: true });
    else if (action === "chip") {
      const input = this.panel.querySelector(".assistant-input");
      input.value = target.dataset.text;
      this.draftChanged();
      input.focus();
    } else if (action === "send") this.send();
  },

  draftChanged() {
    const send = this.panel.querySelector('[data-action="send"]');
    const input = this.panel.querySelector(".assistant-input");
    if (send && input) send.disabled = !this.hooks.ask || !input.value.trim();
  },

  send() {
    const input = this.panel.querySelector(".assistant-input");
    const text = input.value.trim();
    if (text && this.hooks.ask) this.hooks.ask(text);
  },

  purposeHtml(purpose) {
    if (!purpose) return "";
    const name = value => PURPOSE_NAMES[value] || "General";
    const listed = (purpose.models || []).filter(m => m.value !== "general" || m.note);
    const line = m => `<li><b>${esc(m.name)}</b>: ${esc(name(m.value))}${m.note ? ` <i>"${esc(m.note)}"</i>` : ""}</li>`;
    const note = purpose.default.note ? ` <i>"${esc(purpose.default.note)}"</i>` : "";
    const rest = listed.length
      ? `<ul>${listed.slice(0, MAX_LISTED).map(line).join("")}</ul>${listed.length > MAX_LISTED ? `<div class="slicer-muted">and ${listed.length - MAX_LISTED} more</div>` : ""}`
      : (purpose.models || []).length ? `<div class="slicer-muted">Every model follows the project default.</div>` : "";
    return `<div class="assistant-purpose"><div class="slicer-muted">Purpose it will be given</div>
      <div>Project default: <b>${esc(name(purpose.default.value))}</b>${note}</div>${rest}</div>`;
  },

  render() {
    if (!this.panel) return;
    const draft = this.panel.querySelector(".assistant-input")?.value || "";
    const status = this.status;
    const available = !!status?.available;
    let thread;
    if (!status || status.checking) {
      thread = `<div class="slicer-muted" role="status">Checking the assistant…</div>`;
    } else if (!status.available) {
      thread = `<div class="assistant-notice warn" role="status"><b>AI unavailable</b>
          <div class="slicer-muted">${esc(status.reason || "The assistant did not answer.")}</div>
          <div class="slicer-muted">Everything else works as normal.</div>
          <div class="assistant-actions"><button type="button" class="btn btn-secondary btn-sm" data-action="recheck">Check again</button></div>
        </div>`;
    } else {
      thread = `<div class="assistant-empty">
          <p>Ask about settings or layout for this project. I can propose changes; you review and accept them. I can't touch printers.</p>
          ${this.purposeHtml(status.purpose)}
          <div class="assistant-chips" role="group" aria-label="Example prompts">${CHIPS.map(c => `<button type="button" class="assistant-chip" data-action="chip" data-text="${esc(c)}">${esc(c)}</button>`).join("")}</div>
        </div>`;
    }
    this.panel.innerHTML = `
      <div class="assistant-head"><h3>Assistant</h3><span class="slicer-badge">optional</span><span class="assistant-grow"></span>
        <button type="button" class="btn btn-secondary btn-sm" data-action="close" aria-label="Close assistant">Close</button></div>
      <div class="assistant-thread" aria-live="polite">${thread}</div>
      <div class="assistant-compose">
        <label class="sr-only" for="assistant-input">Ask the assistant</label>
        <textarea id="assistant-input" class="assistant-input" rows="2" maxlength="2000" placeholder="${available ? "Ask the assistant…" : "AI unavailable"}" ${available ? "" : "disabled"}></textarea>
        <button type="button" class="btn btn-primary btn-sm" data-action="send" disabled>Send</button>
      </div>`;
    const input = this.panel.querySelector(".assistant-input");
    if (draft && available) { input.value = draft; this.draftChanged(); }
  },
};
