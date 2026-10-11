/* Folder import shared by the dashboard (import.js) and the Projects page (slicer/main.js): reads a dropped folder, and uploads a batch of model files
   into one new project named after the folder. */
window.FolderImport = (() => {
  const MODEL = /\.(stl|obj|3mf)$/i;
  const byPath = (a, b) => (a.path || a.file.name).localeCompare(b.path || b.file.name, undefined, { numeric: true });

  const readEntries = reader => new Promise((resolve, reject) => reader.readEntries(resolve, reject));
  const fileOf = entry => new Promise((resolve, reject) => entry.file(resolve, reject));

  async function walk(entry, out, path) {
    if (entry.isFile) { out.push({ file: await fileOf(entry), path: path + entry.name }); return; }
    const reader = entry.createReader();
    for (let batch = await readEntries(reader); batch.length; batch = await readEntries(reader)) {   // readEntries returns at most ~100 at a time
      for (const child of batch) await walk(child, out, `${path}${entry.name}/`);
    }
  }

  /** Everything dropped, folders expanded: { files: File[], folder: name of the single dropped folder or "" } */
  async function dropped(dataTransfer) {
    // The entries must be taken before the first await: the DataTransfer is cleared once the event handler yields
    const entries = [...(dataTransfer.items || [])].map(i => i.webkitGetAsEntry?.()).filter(Boolean);
    if (!entries.some(e => e.isDirectory)) return { files: [...dataTransfer.files], folder: "" };
    const out = [];
    for (const entry of entries) await walk(entry, out, "");
    const folders = entries.filter(e => e.isDirectory);
    return { files: out.sort(byPath).map(o => o.file), folder: folders.length === 1 && entries.length === 1 ? folders[0].name : "" };
  }

  /** The model files of a folder input's selection, in path order, and the folder's name */
  function picked(fileList) {
    const files = [...fileList].filter(f => MODEL.test(f.name))
      .sort((a, b) => (a.webkitRelativePath || a.name).localeCompare(b.webkitRelativePath || b.name, undefined, { numeric: true }));
    return { files, folder: (files[0]?.webkitRelativePath || "").split("/")[0] };
  }

  function post(url, file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", url);
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.upload.onprogress = e => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
      xhr.onload = () => {
        if (xhr.status === 401) { location.replace("/login"); return; }
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch (_) { /* not JSON */ }
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new Error(data.error || `Upload failed (HTTP ${xhr.status})`));
      };
      xhr.onerror = () => reject(new Error("Upload failed — the connection to Spooler was lost"));
      xhr.send(file);
    });
  }

  /** Creates a project named `name` and uploads each model into it, one at a time.
      onProgress(done, total, fraction of the current file). Resolves { id, added, failed: ["file: reason"] }. */
  async function toNewProject(files, { name, scale = "1", up = "z", onProgress = () => {} }) {
    const models = files.filter(f => MODEL.test(f.name));
    if (!models.length) throw new Error("There are no STL, OBJ or 3MF files to import");
    const response = await fetch("/api/projects", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name || "Imported models" }),
    });
    if (response.status === 401) { location.replace("/login"); throw new Error("Signed out"); }
    const project = await response.json().catch(() => ({}));
    if (!response.ok || !project.id) throw new Error(project.error || `Couldn't create the project (HTTP ${response.status})`);
    const failed = []; let added = 0;
    for (const [i, file] of models.entries()) {
      onProgress(i, models.length, 0);
      try {
        await post(`/api/projects/${project.id}/models/upload?${new URLSearchParams({ filename: file.name, scale, up })}`, file, f => onProgress(i, models.length, f));
        added++;
      } catch (err) {
        failed.push(`${file.name}: ${err.message}`);
        if (/at most \d+ objects/.test(err.message)) { failed.push(`${models.length - i - 1} more skipped`); break; }
      }
    }
    onProgress(models.length, models.length, 1);
    // Every model lands on plate 1 in the same spot: the project opens straight into the Plan plates review (slicer/main.js reads this)
    if (added > 1) { try { sessionStorage.setItem("slicer.autoplan", String(project.id)); } catch (_) { /* storage blocked */ } }
    return { id: project.id, added, failed };
  }

  const summary = (result, total) => result.failed.length
    ? `Added ${result.added} of ${total} files. ${result.failed.slice(0, 3).join(" · ")}${result.failed.length > 3 ? " …" : ""}`
    : `Imported ${result.added} files`;

  return { MODEL, dropped, picked, toNewProject, summary };
})();
