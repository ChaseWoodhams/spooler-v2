// Assistant panel (optional): docks on the right of the project page. It shows availability, the empty thread (what the
// assistant can and can't do, the Purpose summary, example prompts), and the conversation of this visit: ask, progress
// steps while a run works, then the plain-text answer. The thread lives only in this object: reload or Clear empties it
// and nothing is stored. Each ask sends the earlier turns (capped), because the server keeps no conversation.
// Proposal cards (ticket 15) are thread entries {kind: "proposal", card, status, question}: pending, accepted, dismissed,
// superseded (the project changed) or replaced (a newer Proposal of the same kind). Later tickets add timeout, cancelled and
// usage limit to entryHtml() and finish().
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
const CARD_STATUS = {
  accepted: "Applied. Undo puts it back.",
  dismissed: "Dismissed. Nothing was changed.",
  replaced: "Replaced by a newer proposal of the same kind. Nothing was changed.",
};
const TERMINAL_POLL_MS = 5000;          // how often an open panel looks for Proposals staged from a terminal session
const GIVE_UP_MS = 90000;              // the server stops a run after 60 s; if its answer still has not come, stop waiting (never a spinner forever)
const END_STATES = ["timeout", "cancelled", "usage_limit", "error"];
const END_TITLES = { timeout: "No answer in 60 s", cancelled: "Cancelled", usage_limit: "AI unavailable", error: "The assistant could not answer" };

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
  terminalTimer: null, // looks for Proposals staged by a terminal session (no app run owns them)

  // hooks: { api(method, path, body), project() -> record | null, flush() (save unsaved edits first),
  //          openReview(view, { accept }) (the Plan plates review sheet), applied(project, card) (the page takes the accepted project) }
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
    if (open) { this.refresh(); this.watchTerminal(); if (focus) this.focusStart(); }
    else clearTimeout(this.terminalTimer);
  },

  focusStart() {
    (this.panel.querySelector(".assistant-input:not(:disabled)") || this.panel.querySelector("button"))?.focus();
  },

  // Called whenever the page re-renders: read the (cached, cheap) status again when the saved project changed.
  update() {
    this.supersedeStale();
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
    else if (action === "cancel") this.cancel(target);
    else if (action === "retry") this.retry();
    else if (action === "clear") this.clear();
    else if (action === "review" || action === "dismiss" || action === "ask-again") {
      const entry = this.entryFor(target.dataset.token);
      if (!entry) return;
      if (action === "review") this.review(entry);
      else if (action === "dismiss") this.dismissCard(entry);
      else this.send(entry.question);
    }
  },

  draftChanged() {
    const send = this.panel.querySelector('[data-action="send"]');
    const input = this.panel.querySelector(".assistant-input");
    if (send && input) send.disabled = this.busy || !input.value.trim();
  },

  // The earlier turns of this visit for the next ask: what was said and answered, newest kept, capped like the server does.
  history() {
    const turns = this.thread.flatMap(entry => {
      if (entry.kind === "user") return [{ role: "user", text: entry.text }];
      if (entry.kind === "answer") return [{ role: "assistant", text: entry.text }];
      if (entry.kind === "proposal") return [{ role: "assistant", text: this.proposalTurn(entry) }];
      return [];
    }).map(turn => ({ ...turn, text: turn.text.slice(0, MAX_TURN_CHARS) }));
    const kept = [];
    let used = 0;
    for (const turn of turns.reverse()) {
      if (kept.length >= MAX_HISTORY_TURNS || used + turn.text.length > MAX_HISTORY_CHARS) break;
      kept.unshift(turn);
      used += turn.text.length;
    }
    return kept;
  },

  // Try again: the failed question goes back out as a fresh ask, without the failed attempt in the thread or the history.
  retry() {
    if (this.busy) return;
    const last = this.thread.at(-1);
    if (!last || !END_STATES.includes(last.kind)) return;
    const question = this.thread.at(-2);
    if (question?.kind !== "user") return;
    this.thread.splice(-2, 2);
    this.send(question.text);
  },

  async cancel(button) {
    const run = this.run;
    if (!run || !this.busy) return;
    if (button) { button.disabled = true; button.textContent = "Cancelling…"; }
    try {
      const view = await this.hooks.api("POST", `/api/projects/${run.project}/assistant/runs/${run.id}/cancel`);
      if (this.run === run && view.state !== "thinking") this.finish(view);
    } catch (error) {                      // polling carries on and shows however the run ends
      if (button) { button.disabled = false; button.textContent = "Cancel"; }
    }
  },

  // `again` is a question to ask once more (Try again, or a Superseded card's "Ask again"); otherwise the composer's text is sent.
  async send(again) {
    const input = this.panel.querySelector(".assistant-input");
    const text = again ?? input?.value.trim();
    if (!text || this.busy || !this.hooks.project() || !this.status?.available) return;
    this.notice = null;
    this.busy = true;
    try {
      await this.hooks.flush?.();           // unsaved edits are saved first, so the Proposal is made for the project as it is
    } catch (error) {
      this.busy = false;
      this.notice = { text: error.message };
      this.renderThread();
      return;
    }
    const project = this.hooks.project();
    const history = this.history();
    if (again === undefined) input.value = "";
    this.thread.push({ kind: "user", text });
    this.renderThread();
    try {
      const run = await this.hooks.api("POST", `/api/projects/${project.id}/assistant/ask`, { message: text, history });
      this.run = { id: run.id, project: project.id, steps: run.steps || [], failures: 0, startedAt: Date.now(), question: text };
    } catch (error) {                       // refused: nothing started, so the question goes back to be sent again
      this.thread.pop();
      this.busy = false;
      this.notice = { text: error.message };
      if (again === undefined) input.value = text;
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
    if (Date.now() - run.startedAt > GIVE_UP_MS) { this.finish({ state: "error", error: "The assistant did not report back in time." }); return; }
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

  // A run ended: answer, proposal, or one of the end states.
  finish(view) {
    const question = this.run?.question;
    this.run = null;
    this.busy = false;
    clearTimeout(this.pollTimer);
    if (view.state === "answer") this.thread.push({ kind: "answer", text: view.answer || "" });
    else if (view.state === "proposal") {
      if (view.answer) this.thread.push({ kind: "answer", text: view.answer });
      for (const card of view.proposals || []) this.addCard(card, question);
    } else {
      const kind = END_STATES.includes(view.state) ? view.state : "error";
      this.thread.push({ kind, text: view.error || "The assistant did not answer.", view });
    }
    this.renderThread();
    this.panel.querySelector(".assistant-input")?.focus({ preventScroll: true });
  },

  // ── Proposals from a terminal session ──
  // The Spooler MCP server started from a terminal stages Proposals for its project without an app run. While the panel is open
  // (and the tab visible) the project's pending ones are listed and shown as cards; each then works like any other card.
  watchTerminal() {
    clearTimeout(this.terminalTimer);
    if (!this.open) return;
    this.loadTerminal().finally(() => {
      if (this.open) this.terminalTimer = setTimeout(() => this.watchTerminal(), TERMINAL_POLL_MS);
    });
  },

  async loadTerminal() {
    const project = this.hooks.project();
    if (!project || document.hidden) return;
    let cards;
    try {
      cards = (await this.hooks.api("GET", `/api/projects/${project.id}/assistant/proposals`)).proposals || [];
    } catch { return; }                       // an unreachable list changes nothing; the next look tries again
    if (this.hooks.project()?.id !== project.id) return;
    let added = false;
    for (const card of cards) {
      if (card.base_revision !== project.revision || this.entryFor(card.token)) continue;
      this.addCard(card, null);
      added = true;
    }
    if (added) this.renderThread();
  },

  // ── Proposal cards ──
  proposalTurn(entry) {
    const outcome = { pending: "waiting for the person's decision", accepted: "accepted", dismissed: "dismissed",
      superseded: "superseded by a change to the project", replaced: "replaced by a newer proposal" }[entry.status];
    return `Proposal "${entry.card.title}" (${entry.card.scope}): ${outcome}`;
  },

  // Only the latest Proposal of each kind stays live: an older pending one of the same kind is replaced.
  addCard(card, question) {
    for (const old of this.thread) {
      if (old.kind === "proposal" && old.status === "pending" && old.card.kind === card.kind) this.settle(old, "replaced");
    }
    this.thread.push({ kind: "proposal", card, status: "pending", question });
  },

  entryFor(token) {
    return this.thread.find(entry => entry.kind === "proposal" && entry.card.token === token);
  },

  // Gives a card its final state and sweeps its staging (best effort: sweeping never changes the project).
  settle(entry, status) {
    entry.status = status;
    this.hooks.api("POST", `/api/projects/${entry.card.project_id}/assistant/proposals/${entry.card.token}/dismiss`, {}).catch(() => {});
  },

  // Called as the page re-renders: a Proposal is only good for the revision it was made at, so any edit supersedes it.
  supersedeStale() {
    const project = this.hooks?.project();
    if (!project) return;
    let changed = false;
    for (const entry of this.thread) {
      if (entry.kind === "proposal" && entry.status === "pending" && entry.card.project_id === project.id && entry.card.base_revision !== project.revision) {
        this.settle(entry, "superseded");
        changed = true;
      }
    }
    if (changed && this.open) this.renderThread();
  },

  async review(entry) {
    if (entry.status !== "pending") return;
    try {
      const view = await this.hooks.api("GET", `/api/projects/${entry.card.project_id}/assistant/proposals/${entry.card.token}`);
      this.hooks.openReview(view, { accept: () => this.accept(entry) });
    } catch (error) {
      if (error.status === 409 || error.status === 404) { this.settle(entry, "superseded"); this.renderThread(); return; }
      this.notice = { text: error.message };
      this.renderThread();
    }
  },

  // Accept: the server applies the stored data as one revision (or refuses with 409: Superseded). Resolves {ok, message}.
  async accept(entry) {
    if (entry.status !== "pending") return { ok: false, message: "This proposal is no longer open." };
    await this.hooks.flush?.();
    let project;
    try {
      project = await this.hooks.api("POST", `/api/projects/${entry.card.project_id}/assistant/proposals/${entry.card.token}/accept`, { revision: this.hooks.project().revision });
    } catch (error) {
      if (error.status === 409 || error.status === 404) {
        entry.status = "superseded";
        this.renderThread();
        return { ok: false, message: "The project changed since this proposal was made, so it was not applied. Ask again from the Assistant." };
      }
      throw error;
    }
    entry.status = "accepted";             // before the page re-renders: a new revision must not read as a supersede
    this.hooks.applied(project, entry.card);
    this.renderThread();
    return { ok: true };
  },

  dismissCard(entry) {
    if (entry.status !== "pending") return;
    this.settle(entry, "dismissed");
    this.renderThread();
  },

  cardHtml(entry) {
    const { card, status } = entry;
    const lines = (card.diff || []).map(line => `<li><b>${esc(line.label)}</b> ${esc(line.text)}</li>`).join("");
    const pinned = (card.pinned || []).length
      ? `<div class="slicer-muted">Pinned, left alone: ${(card.pinned || []).map(p => esc(p.label)).join(", ")}</div>` : "";
    const notes = [...(card.notes || []), ...(card.problems || [])].map(note => `<li>${esc(note)}</li>`).join("");
    let foot;
    if (status === "pending") {
      foot = `<div class="assistant-actions"><button type="button" class="btn btn-primary btn-sm" data-action="review" data-token="${esc(card.token)}">Review changes…</button>
        <button type="button" class="btn btn-secondary btn-sm" data-action="dismiss" data-token="${esc(card.token)}">Dismiss</button></div>`;
    } else if (status === "superseded") {
      foot = `<div class="slicer-muted">The project changed since this was made, so it can't be applied. Nothing was changed.</div>
        ${entry.question ? `<div class="assistant-actions"><button type="button" class="btn btn-secondary btn-sm" data-action="ask-again" data-token="${esc(card.token)}"${this.busy ? " disabled" : ""}>Ask again</button></div>` : ""}`;
    } else foot = `<div class="slicer-muted">${CARD_STATUS[status]}</div>`;
    const badge = status === "superseded" ? ` <span class="slicer-badge">Superseded</span>` : "";
    return `<div class="assistant-card${status === "pending" ? "" : " done"}" data-token="${esc(card.token)}" data-kind="${esc(card.kind)}" data-status="${status}">
        <div class="assistant-card-head"><b>${esc(card.title)}</b>${badge}</div>
        <div class="slicer-muted">${esc(card.scope)}</div>
        <p class="assistant-reason">${esc(card.reason)}</p>
        ${lines ? `<ul class="assistant-diff">${lines}</ul>` : ""}${pinned}
        ${notes ? `<ul class="assistant-notes slicer-muted">${notes}</ul>` : ""}
        ${foot}
      </div>`;
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
  entryHtml(entry, isLast) {
    if (entry.kind === "user") return `<div class="assistant-msg me">${esc(entry.text)}</div>`;
    if (entry.kind === "answer") return `<div class="assistant-msg answer">${esc(entry.text)}</div>`;
    if (entry.kind === "proposal") return this.cardHtml(entry);
    const retry = isLast ? `<div class="assistant-actions"><button type="button" class="btn btn-secondary btn-sm" data-action="retry">Try again</button></div>` : "";
    const quiet = entry.kind === "cancelled" ? "" : " warn";
    const reassure = entry.kind === "error" ? `<div class="slicer-muted">Nothing was changed.</div>` : "";
    return `<div class="assistant-notice${quiet}" role="alert" data-end="${esc(entry.kind)}"><b>${esc(END_TITLES[entry.kind] || END_TITLES.error)}</b>
        <div>${esc(entry.text)}</div>${reassure}${retry}</div>`;
  },

  thinkingHtml() {
    const steps = this.run?.steps || [];
    const list = steps.length
      ? `<ol class="assistant-steps">${steps.map((step, n) => `<li class="${n === steps.length - 1 ? "on" : "done"}">${esc(step.label)}</li>`).join("")}</ol>`
      : `<ol class="assistant-steps"><li class="on">Starting</li></ol>`;
    return `<div class="assistant-notice" role="status"><div class="assistant-working"><span class="assistant-spin" aria-hidden="true"></span><b>Working…</b><span class="assistant-grow"></span>
      <button type="button" class="btn btn-secondary btn-sm" data-action="cancel">Cancel</button></div>${list}
      <div class="slicer-muted">Stops by itself after 60 s.</div></div>`;
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
    return this.thread.map((entry, n) => this.entryHtml(entry, n === this.thread.length - 1 && !this.busy)).join("") + (this.busy ? this.thinkingHtml() : "") + notice;
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
