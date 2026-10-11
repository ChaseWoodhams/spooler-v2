/* Drop files on the dashboard: one file opens Quick print, several files or a folder make one Project and open it on the stepper.
   Starting a Project by name (New project) is the Projects page's job, in slicer/main.js. */
(() => {
  const MODEL = /\.(stl|obj|3mf|gcode)$/i;

  function say(text, error = false) {
    const el = document.createElement("div");
    el.className = `toast${error ? " toast-error" : ""}`;
    el.textContent = text;
    (document.getElementById("toast-area") || document.body).appendChild(el);
    setTimeout(() => el.remove(), error ? 6000 : 3500);
  }

  function importFile(file) {
    if (!file) return;
    if (!MODEL.test(file.name)) { say("Choose an STL, OBJ or 3MF model, or a G-code file", true); return; }
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/projects/upload?${new URLSearchParams({ filename: file.name, scale: "1", up: "z" })}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.onload = () => {
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch (_) { /* not JSON */ }
      if (xhr.status === 200 && body.id) {
        if (typeof closeQueue === "function") closeQueue();
        location.hash = `#projects=${body.id}`; // the Project opens inside the app shell, on the stepper
        return;
      }
      if (xhr.status === 401) { location.replace("/login"); return; }
      say(body.error || `Couldn't add the file (HTTP ${xhr.status})`, true);
    };
    xhr.onerror = () => say("Couldn't add the file: the server couldn't be reached", true);
    xhr.send(file);
  }

  // Several files or a folder: one project holding every model
  async function importBatch(files, folder) {
    const models = files.filter(f => FolderImport.MODEL.test(f.name));
    if (!models.length) { say("There are no STL, OBJ or 3MF files to add", true); return; }
    try {
      const result = await FolderImport.toNewProject(models, { name: folder });
      if (result.failed.length) say(FolderImport.summary(result, models.length), true);
      if (typeof closeQueue === "function") closeQueue();
      location.hash = `#projects=${result.id}`;
    } catch (err) {
      say(err.message, true);
    }
  }

  // Drop a file anywhere on the dashboard (not onto a form field or a dialog that handles its own drops)
  let depth = 0;
  const hasFiles = e => [...(e.dataTransfer?.types || [])].includes("Files");
  document.addEventListener("dragenter", e => { if (hasFiles(e)) { depth++; document.body.classList.add("import-dropping"); } });
  document.addEventListener("dragleave", e => { if (hasFiles(e) && --depth <= 0) { depth = 0; document.body.classList.remove("import-dropping"); } });
  document.addEventListener("dragover", e => { if (hasFiles(e)) e.preventDefault(); });
  document.addEventListener("drop", e => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    document.body.classList.remove("import-dropping");
    FolderImport.dropped(e.dataTransfer).then(({ files, folder }) => {
      if (files.length > 1 || folder) { importBatch(files, folder); return; } // a folder or several files make one Project
      if (window.QuickPrint) window.QuickPrint.open({ file: files[0] }); // a dropped file is a Quick print
      else importFile(files[0]);
    });
  });

  // New project (header and Queue panel): the Projects page opens on its New project form
  function newProject() {
    if (typeof closeQueue === "function") closeQueue();
    const frame = document.getElementById("projects-frame");
    if (frame) frame.dataset.route = ""; // reload the form even if it is already showing
    if (location.hash === "#projects=new") window.dispatchEvent(new HashChangeEvent("hashchange"));
    else location.hash = "#projects=new";
  }
  document.getElementById("btn-new-project")?.addEventListener("click", newProject);
  document.getElementById("btn-queue-new-project")?.addEventListener("click", newProject);
})();
