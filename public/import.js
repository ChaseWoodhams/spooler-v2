/* Import from the dashboard: one button (or a dropped file) takes an STL, OBJ, 3MF or G-code file, makes a Project of it
   and opens it on the confirm screen. The Projects page does the same in slicer/main.js. */
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
        location.hash = `#projects=${body.id}&confirm=1`; // the Project opens inside the app shell
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

  button.addEventListener("click", () => input.click());
  input.addEventListener("change", () => { importFile(input.files[0]); input.value = ""; });

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
    importFile(e.dataTransfer.files[0]);
  });
})();
