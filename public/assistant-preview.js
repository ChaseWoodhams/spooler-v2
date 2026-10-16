// Settings > Assistant > Preview: the exact instructions and per-question prompt the next Assistant run would send for the SAVED settings.
// Free and read only: it asks the server to build the text (POST /api/projects/<id>/assistant/preview) and starts nothing.
// Everything is drawn with textContent, so a name or note can never become markup.
(function () {
  const $ = id => document.getElementById(id);
  const say = text => { const el = $("assistant-preview-status"); if (el) el.textContent = text; };

  async function loadProjects() {
    const select = $("assistant-preview-project");
    if (!select) return;
    try {
      const r = await fetch("/api/projects");
      const list = await r.json();
      if (!r.ok || !Array.isArray(list)) throw new Error("unavailable");
      select.replaceChildren(...list.map(p => { const o = document.createElement("option"); o.value = p.id; o.textContent = p.name || `Project ${p.id}`; return o; }));
      say(list.length ? "" : "No projects yet: the instructions can still be previewed once a project exists.");
    } catch (err) {
      say(`Could not load projects: ${err.message}.`);
    }
  }

  async function show() {
    const project = $("assistant-preview-project").value;
    if (!project) { say("Choose a project first."); return; }
    try {
      const r = await fetch(`/api/projects/${encodeURIComponent(project)}/assistant/preview`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: $("assistant-preview-question").value, history: [] }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "unavailable");
      $("assistant-preview-instructions").textContent = d.instructions;
      $("assistant-preview-prompt").textContent = d.prompt;
      say("Built from the saved settings. Nothing was started or sent.");
    } catch (err) {
      say(`Could not build the preview: ${err.message}.`);
    }
  }

  document.getElementById("btn-settings-goto-assistant")?.addEventListener("click", loadProjects);
  $("btn-assistant-preview")?.addEventListener("click", show);
})();
