/* confirmDialog({ title, body, confirmLabel, cancelLabel, danger, opener }) -> Promise<boolean>
   The in-app replacement for the browser's native yes/no box. Built on modal.js (openModal / closeModal), so it has dialog semantics,
   a focus trap and Escape = Cancel. Initial focus is on Cancel, so a stray Enter never confirms something destructive.
   title         the question, naming the object ("Stop the print on Voxel?")
   body          what happens, as a string (blank lines split paragraphs) or an array of strings
   confirmLabel  the verb on the confirm button ("Stop print", "Delete file"), never "OK"
   danger        the confirm button is red
   Resolves true on confirm, false on Cancel, Escape or when another confirmDialog replaces it.
   The element is built per call and removed again, inside the open native <dialog> if there is one (the Projects page
   is a showModal() dialog, which makes everything outside it inert and paints over a plain overlay). */
(() => {
  let current = null;   // { el, finish } of the dialog on screen

  function confirmDialog({ title, body = "", confirmLabel = "Confirm", cancelLabel = "Cancel", danger = false, opener } = {}) {
    if (current) current.finish(false);

    const el = document.createElement("div");
    el.className = "modal confirm-dialog";
    const box = document.createElement("div");
    box.className = "modal-box";
    const heading = document.createElement("h3");
    heading.textContent = title || "Are you sure?";
    box.appendChild(heading);
    for (const text of [].concat(body).flatMap(part => String(part ?? "").split(/\n\s*\n/)).filter(Boolean)) {
      const p = document.createElement("p");
      p.className = "confirm-body";
      p.textContent = text;
      box.appendChild(p);
    }
    const actions = document.createElement("div");
    actions.className = "modal-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn btn-secondary";
    cancel.textContent = cancelLabel;
    const ok = document.createElement("button");
    ok.type = "button";
    ok.className = `btn btn-primary${danger ? " btn-danger" : ""}`;
    ok.textContent = confirmLabel;
    actions.append(cancel, ok);
    box.appendChild(actions);
    el.appendChild(box);

    const hosts = document.querySelectorAll("dialog[open]");
    (hosts[hosts.length - 1] || document.body).appendChild(el);

    return new Promise(resolve => {
      let answer = false, done = false;
      const finish = value => {
        if (done) return;
        done = true;
        answer = value;
        closeModal(el);
      };
      current = { el, finish };
      ok.addEventListener("click", () => finish(true));
      cancel.addEventListener("click", () => finish(false));
      el.addEventListener("mousedown", e => { if (e.target === el) finish(false); });
      openModal(el, {
        opener: opener || document.activeElement,
        focus: cancel,
        onClose: () => {
          done = true;
          if (current && current.el === el) current = null;
          el.remove();
          resolve(answer);
        },
      });
    });
  }

  window.confirmDialog = confirmDialog;
})();
