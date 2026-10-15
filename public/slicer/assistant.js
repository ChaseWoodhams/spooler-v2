// Assistant panel (optional): docks on the right of the project page. It shows availability, the empty thread (what the
// assistant can and can't do, the Purpose summary, example prompts), and the conversation of this visit: ask, progress
// steps while a run works, then the plain-text answer. The thread lives only in this object: reload or Clear empties it
// and nothing is stored. Each ask sends the earlier turns (capped), because the server keeps no conversation.
// Later tickets add entry kinds (Proposal cards, timeout, cancelled, usage limit) to ENTRY_RENDERERS and finish().
// Nothing here runs until the person opens the panel, so the page loads and works the same without an assistant.
const OPEN_KEY = "slicer.assistant.open";
const CHIPS = ["Make these parts stronger", "Fit more on this plate", "Why is this plate so slow?"];
const PURPOSE_NAMES = { general: "General", functional: "Functional", display: "Display", draft: "Draft" };
const MAX_LISTED = 5;
const MAX_HISTORY_TURNS = 12;          // keep in step with assistant_runner.py
const MAX_HISTORY_CHARS = 6000;
const MAX_TURN_CHARS = 1500;
const POLL_FIRST_MS = 400;
const POLL_MS = 900;
const POLL_FAILURES = 3;

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
  thread: [],          // [{kind: "user"|"answer"|"error"|..., text, view?}] in order; never stored anywhere
  busy: false,         // a run is working
  run: null,           // {id, steps: [{label}]} while busy
  notice: null,        // a refused ask: {text}; not part of the thread or the history
  pollTimer: null,

  // hooks: { api(method, path, body), project() -> record | null }
  attach(hooks) {
    this.hooks = hooks;
    this.panel = document.getElementById("assistant-panel");
    this.button = document.getElementById("btn-assistant");
    this.button.addEventListener("click", () => this.setOpen(!this.open));
    this.panel.addEventListener("click", event => this.click(event));
    this.panel.addEventListener("input", event => { if (event.target.classList.contains("assistant-input")) this.draftChanged(); });
    this.panel.addEventListener("keydown", event => {
      if (event.key === "Escape") { this.setOpen(false); this.button.focus(); }
      else if (event.key === "Enter" && !event.shiftKey && event.target.classList.contains("assistant-input")) {
        event.preventDefault();
        this.send();
      }
    });
    document.addEventListener("visibilitychange", () => { if (!document.hidden && this.busy) this.schedulePoll(POLL_FIRST_MS); });
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
    else if (action === "clear") this.clear();
  },

  draftChanged() {
    const send = this.panel.querySelector('[data-action="send"]');
    const input = this.panel.querySelector(".assistant-input");
    if (send && input) send.disabled = this.busy || !input.value.trim();
  },

  // The earlier turns of this visit for the next ask: what was said and answered, newest kept, capped like the server does.
  history() {
    const turns = this.thread.filter(entry => entry.kind === "user" || entry.kind === "answer")
      .map(entry => ({ role: entry.kind === "user" ? "user" : "assistant", text: entry.text.slice(0, MAX_TURN_CHARS) }));
    const kept = [];
    let used = 0;
    for (const turn of turns.reverse()) {
      if (kept.length >= MAX_HISTORY_TURNS || used + turn.text.length > MAX_HISTORY_CHARS) break;
      kept.unshift(turn);
      used += turn.text.length;
    }
    return kept;
  },

  async send() {
    const input = this.panel.querySelector(".assistant-input");
    const project = this.hooks.project();
    const text = input?.value.trim();
    if (!text || this.busy || !project || !this.status?.available) return;
    const history = this.history();
    input.value = "";
    this.notice = null;
    this.busy = true;
    this.thread.push({ kind: "user", text });
    this.renderThread();
    try {
      const run = await this.hooks.api("POST", `/api/projects/${project.id}/assistant/ask`, { message: text, history });
      this.run = { id: run.id, project: project.id, steps: run.steps || [], failures: 0 };
    } catch (error) {                       // refused: nothing started, so the question goes back to be sent again
      this.thread.pop();
      this.busy = false;
      this.notice = { text: error.message };
      input.value = text;
      this.renderThread();
      return;
    }
    this.renderThread();
    this.schedulePoll(POLL_FIRST_MS);
  },

  schedulePoll(delay) {
    clearTimeout(this.pollTimer);
    this.pollTimer = setTimeout(() => this.poll(), delay);
  },

  async poll() {
    const run = this.run;
    if (!run || !this.busy) return;
    if (document.hidden) return;            // resumes on visibilitychange
    try {
      const view = await this.hooks.api("GET", `/api/projects/${run.project}/assistant/runs/${run.id}`);
      if (this.run !== run) return;
      run.failures = 0;
      run.steps = view.steps || [];
      if (view.state === "thinking") { this.renderThread(); this.schedulePoll(POLL_MS); return; }
      this.finish(view);
    } catch (error) {
      if (this.run !== run) return;
      if (++run.failures >= POLL_FAILURES) this.finish({ state: "error", error: `Lost contact with the assistant: ${error.message}` });
      else this.schedulePoll(POLL_MS);
    }
  },

  // A run ended. Later tickets add states here: proposal, timeout, cancelled, usage_limit.
  finish(view) {
    this.run = null;
    this.busy = false;
    if (view.state === "answer") this.thread.push({ kind: "answer", text: view.answer || "" });
    else this.thread.push({ kind: "error", text: view.error || "The assistant did not answer.", view });
    this.renderThread();
    this.panel.querySelector(".assistant-input")?.focus({ preventScroll: true });
  },

  clear() {
    if (this.busy) return;
    this.thread = [];
    this.notice = null;
    this.renderThread();
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

  // One renderer per kind of thread entry. Later tickets add proposal, timeout, cancelled and usage_limit here.
  entryHtml(entry) {
    if (entry.kind === "user") return `<div class="assistant-msg me">${esc(entry.text)}</div>`;
    if (entry.kind === "answer") return `<div class="assistant-msg answer">${esc(entry.text)}</div>`;
    return `<div class="assistant-notice warn" role="alert"><b>The assistant could not answer</b>
        <div>${esc(entry.text)}</div><div class="slicer-muted">Nothing was changed.</div></div>`;
  },

  thinkingHtml() {
    const steps = this.run?.steps || [];
    const list = steps.length
      ? `<ol class="assistant-steps">${steps.map((step, n) => `<li class="${n === steps.length - 1 ? "on" : "done"}">${esc(step.label)}</li>`).join("")}</ol>`
      : `<ol class="assistant-steps"><li class="on">Starting</li></ol>`;
    return `<div class="assistant-notice" role="status"><div class="assistant-working"><span class="assistant-spin" aria-hidden="true"></span><b>Working…</b></div>${list}</div>`;
  },

  threadHtml() {
    const status = this.status;
    if (!status || status.checking) return `<div class="slicer-muted" role="status">Checking the assistant…</div>`;
    if (!status.available) {
      return `<div class="assistant-notice warn" role="status"><b>AI unavailable</b>
          <div class="slicer-muted">${esc(status.reason || "The assistant did not answer.")}</div>
          <div class="slicer-muted">Everything else works as normal.</div>
          <div class="assistant-actions"><button type="button" class="btn btn-secondary btn-sm" data-action="recheck">Check again</button></div>
        </div>`;
    }
    const notice = this.notice ? `<div class="assistant-notice warn" role="alert">${esc(this.notice.text)}</div>` : "";
    if (!this.thread.length && !this.busy) {
      return `<div class="assistant-empty">
          <p>Ask about settings or layout for this project. I can propose changes; you review and accept them. I can't touch printers.</p>
          ${this.purposeHtml(status.purpose)}
          <div class="assistant-chips" role="group" aria-label="Example prompts">${CHIPS.map(c => `<button type="button" class="assistant-chip" data-action="chip" data-text="${esc(c)}">${esc(c)}</button>`).join("")}</div>
        </div>${notice}`;
    }
    return this.thread.map(entry => this.entryHtml(entry)).join("") + (this.busy ? this.thinkingHtml() : "") + notice;
  },

  // Redraws the thread and the controls that depend on it, leaving the composer (draft and focus) alone.
  renderThread() {
    const thread = this.panel?.querySelector(".assistant-thread");
    if (!thread) return;
    const stick = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 40;
    thread.innerHTML = this.threadHtml();
    if (stick || this.busy) thread.scrollTop = thread.scrollHeight;
    const clear = this.panel.querySelector('[data-action="clear"]');
    if (clear) { clear.disabled = this.busy; clear.hidden = !this.thread.length && !this.busy; }
    this.draftChanged();
  },

  render() {
    if (!this.panel) return;
    const draft = this.panel.querySelector(".assistant-input")?.value || "";
    const available = !!this.status?.available;
    this.panel.innerHTML = `
      <div class="assistant-head"><h3>Assistant</h3><span class="slicer-badge">optional</span><span class="assistant-grow"></span>
        <button type="button" class="btn btn-secondary btn-sm" data-action="clear" hidden>Clear</button>
        <button type="button" class="btn btn-secondary btn-sm" data-action="close" aria-label="Close assistant">Close</button></div>
      <div class="assistant-thread" aria-live="polite"></div>
      <div class="assistant-compose">
        <label class="sr-only" for="assistant-input">Ask the assistant</label>
        <textarea id="assistant-input" class="assistant-input" rows="2" maxlength="2000" placeholder="${available ? "Ask the assistant…" : "AI unavailable"}" ${available ? "" : "disabled"}></textarea>
        <button type="button" class="btn btn-primary btn-sm" data-action="send" disabled>Send</button>
      </div>`;
    const input = this.panel.querySelector(".assistant-input");
    if (draft && available) input.value = draft;
    this.renderThread();
  },
};
