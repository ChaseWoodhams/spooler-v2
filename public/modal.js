/* One dialog helper for every .modal on the dashboard and the project page.
   openModal(el, { opener, onClose, focus })  el is the .modal element (the backdrop). Sets role="dialog", aria-modal and
       aria-labelledby (from the first heading), moves focus in (the heading, or `focus`), and pushes it on the layer stack.
       Calling it again on an open modal only re-labels and re-focuses it (content swapped in place).
   closeModal(el)  removes .open, pops the layer, runs onClose, and restores focus to the opener (if it is still visible),
       otherwise to the layer underneath.
   topModal()  the topmost open modal or null.
   Tab is trapped in, and Escape closes, the topmost layer only. Handled in the capture phase so the page-level Escape
   handlers (which close the panel beneath) never see the key while a modal is open.
   openOperatorDialog / closeOperatorDialog are kept as aliases. */
(() => {
  const FOCUSABLE = 'button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])';
  const layers = [];            // open modals, oldest first
  const state = new WeakMap();  // el -> { opener, onClose }
  let uid = 0;

  const isOpen = el => el.classList.contains("open");
  const visible = el => !!el && el.isConnected && el.getClientRects().length > 0;
  const prune = () => { for (let i = layers.length - 1; i >= 0; i--) if (!layers[i].isConnected || !isOpen(layers[i])) layers.splice(i, 1); };

  function label(el) {
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-modal", "true");
    const heading = el.querySelector("h2, h3");
    if (heading && !el.hasAttribute("aria-label")) {   // an explicit aria-label (Settings, whose heading changes per page) wins
      if (!heading.id) heading.id = `modal-title-${++uid}`;
      el.setAttribute("aria-labelledby", heading.id);
    }
    return heading;
  }

  function focusIn(el, target) {
    const heading = el.querySelector("h2, h3");
    const node = target || heading;
    if (!node) return;
    if (!node.matches(FOCUSABLE)) node.tabIndex = -1;
    node.focus();
  }

  function openModal(el, opts = {}) {
    if (!el) return;
    const heading = label(el);
    if (isOpen(el) && state.has(el)) {          // already open: content was swapped
      if (opts.onClose) state.get(el).onClose = opts.onClose;
      focusIn(el, opts.focus || heading);
      return;
    }
    prune();
    const opener = opts.opener || document.activeElement;
    state.set(el, { opener: opener instanceof HTMLElement ? opener : null, onClose: opts.onClose || null });
    el.classList.add("open");
    layers.push(el);
    focusIn(el, opts.focus || heading);
  }

  function closeModal(el) {
    if (!el || !isOpen(el)) return;
    const { opener, onClose } = state.get(el) || {};
    state.delete(el);
    el.classList.remove("open");
    const at = layers.indexOf(el);
    if (at >= 0) layers.splice(at, 1);
    prune();
    if (onClose) onClose();
    if (visible(opener)) opener.focus();
    else {
      const below = layers.at(-1);
      if (below) focusIn(below, below.contains(document.activeElement) ? document.activeElement : null);
    }
  }

  const topModal = () => { prune(); return layers.at(-1) || null; };

  document.addEventListener("keydown", event => {
    const modal = topModal();
    if (!modal) return;
    if (event.key === "Escape") {
      event.preventDefault(); event.stopImmediatePropagation();
      closeModal(modal);
    } else if (event.key === "Tab") {
      const items = [...modal.querySelectorAll(FOCUSABLE)].filter(visible);
      const first = items[0], last = items.at(-1);
      if (!first) return;
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !items.includes(active))) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (active === last || !items.includes(active))) { event.preventDefault(); first.focus(); }
      event.stopImmediatePropagation();
    }
  }, true);

  window.openModal = openModal;
  window.closeModal = closeModal;
  window.topModal = topModal;
  window.openOperatorDialog = openModal;
  window.closeOperatorDialog = closeModal;
})();
