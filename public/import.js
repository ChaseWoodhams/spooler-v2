/* Import from the dashboard: the Import button (a dropped file opens the Quick print modal instead) takes an STL, OBJ, 3MF or G-code file, makes a Project of it
   and opens it on the project stepper. The Projects page does the same in slicer/main.js. */
(() => {
  const input = document.getElementById("import-file");
  const button = document.getElementById("btn-import");
  if (!input || !button) return;
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
    button.disabled = true;
    const label = button.querySelector("span");
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/projects/upload?${new URLSearchParams({ filename: file.name, scale: "1", up: "z" })}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = e => { if (e.lengthComputable && label) label.textContent = `${Math.round(e.loaded / e.total * 100)}%`; };
    xhr.onload = () => {
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch (_) { /* not JSON */ }
      if (xhr.status === 200 && body.id) {
        if (typeof closeQueue === "function") closeQueue(); // importing from the Queue panel
        location.hash = `#projects=${body.id}`; // the Project opens inside the app shell, on the stepper
        reset();
        return;
      }
      if (xhr.status === 401) { location.replace("/login"); return; }
      say(body.error || `Import failed (HTTP ${xhr.status})`, true);
      reset();
    };
    xhr.onerror = () => { say("Import failed: the server couldn't be reached", true); reset(); };
    xhr.send(file);

    function reset() {
      button.disabled = false;
      if (label) label.textContent = "Import";
    }
  }

  // Several files or a folder: one project holding every model
  const folderButton = document.getElementById("btn-import-folder");
  const folderInput = document.getElementById("import-folder");
  async function importBatch(files, folder) {
    const models = files.filter(f => FolderImport.MODEL.test(f.name));
    if (!models.length) { say("There are no STL, OBJ or 3MF files to import", true); return; }
    button.disabled = true;
    if (folderButton) folderButton.disabled = true;
    const label = button.querySelector("span");
    try {
      const result = await FolderImport.toNewProject(models, {
        name: folder,
        onProgress: (done, total) => { if (label && done < total) label.textContent = `${done + 1}/${total}`; },
      });
      if (result.failed.length) say(FolderImport.summary(result, models.length), true);
      if (typeof closeQueue === "function") closeQueue();
      location.hash = `#projects=${result.id}`;
    } catch (err) {
      say(err.message, true);
    } finally {
      button.disabled = false;
      if (folderButton) folderButton.disabled = false;
      if (label) label.textContent = "Import";
    }
  }

  button.addEventListener("click", () => input.click());
  folderButton?.addEventListener("click", () => folderInput.click());
  folderInput?.addEventListener("change", () => {
    const { files, folder } = FolderImport.picked(folderInput.files);
    folderInput.value = "";
    importBatch(files, folder);
  });
  document.getElementById("btn-queue-import")?.addEventListener("click", () => input.click()); // the Queue panel's Import button
  input.addEventListener("change", () => {
    const files = [...input.files];
    input.value = "";
    if (files.length > 1) importBatch(files, ""); else importFile(files[0]);
  });

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
      if (window.QuickPrint) window.QuickPrint.open({ file: files[0] }); // a dropped file is a Quick print; the Import button still makes a Project
      else importFile(files[0]);
    });
  });
})();
