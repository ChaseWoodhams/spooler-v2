// Settings > Assistant > Run log: the newest finished Assistant runs, read from /api/assistant/run-log, with a Clear button.
// Self-contained (own section, own fetches); everything is drawn with textContent, so a question or prompt can never become markup.
(function () {
  const list = () => document.getElementById("assistant-runlog-list");
  const status = () => document.getElementById("assistant-runlog-status");
  const say = text => { const el = status(); if (el) el.textContent = text; };

  function field(parent, label, value) {
    if (value === null || value === undefined || value === "") return;
    const row = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = label + ": ";
    row.append(name, document.createTextNode(String(value)));
    parent.append(row);
  }

  function block(parent, label, text) {
    field(parent, label, "(below)");
    const pre = document.createElement("pre");
    pre.className = "assistant-runlog-prompt";
    pre.textContent = text || "";
    parent.append(pre);
  }

  function draw(data) {
    const host = list();
    if (!host) return;
    host.replaceChildren();
    const entries = Array.isArray(data && data.entries) ? data.entries : [];
    say(entries.length ? `${entries.length} of at most ${data.cap} Assistant runs kept.` : "No Assistant runs logged yet.");
    for (const e of entries) {
      const item = document.createElement("li");
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = `${String(e.time || "").replace("T", " ")} · ${e.project_name || "Project " + e.project_id} · ${e.outcome} · ${e.duration_seconds}s · ${e.question}`;
      details.append(summary);
      field(details, "Outcome", e.outcome + (e.reason ? ` (${e.reason})` : ""));
      field(details, "Question", e.question);
      field(details, "Configured model", e.configured_model || "CLI default");
      field(details, "Model reported by the CLI", e.model || "not reported");
      field(details, "Tools called", (e.steps || []).join(", ") || "none");
      field(details, "Proposals made", (e.proposals || []).length || "none");
      field(details, "Tuning in force", JSON.stringify(e.tuning || {}));
      block(details, "Prompt sent", e.prompt);
      block(details, "System prompt sent", e.system_prompt);
      item.append(details);
      host.append(item);
    }
  }

  async function load() {
    try {
      const r = await fetch("/api/assistant/run-log");
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "unavailable");
      draw(d);
    } catch (err) {
      say(`Could not load the run log: ${err.message}.`);
    }
  }

  async function clear() {
    try {
      const r = await fetch("/api/assistant/run-log/clear", { method: "POST" });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "unavailable");
      draw(d);
    } catch (err) {
      say(`Could not clear the run log: ${err.message}.`);
    }
  }

  document.getElementById("btn-settings-goto-assistant")?.addEventListener("click", load);
  document.getElementById("btn-assistant-runlog-clear")?.addEventListener("click", clear);
})();
