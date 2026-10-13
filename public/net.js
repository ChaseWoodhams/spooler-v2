// Network resilience for every page: loaded first, before any script that fetches.
//
// 1. Reads (GET/HEAD) that fail to connect (a refused or dropped connection, Spooler busy or restarting) are retried a few
//    times with backoff instead of surfacing "Failed to fetch". An HTTP error response is Spooler's real answer and is
//    returned at once. Writes are never retried: the browser cannot tell whether a failed POST reached Spooler, and
//    repeating it could slice or queue something twice.
// 2. The app shell notices when Spooler's UI files have changed (an update, or a restart onto new code) and reloads
//    itself when nothing is in progress, so a stale page never runs against a newer server.
(function () {
  const RETRY_DELAYS_MS = [400, 1200, 3000];
  const nativeFetch = window.fetch.bind(window);
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

  window.fetch = async function (input, init) {
    const method = (init?.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (method !== "GET" && method !== "HEAD") return nativeFetch(input, init);
    const signal = init?.signal || (input instanceof Request ? input.signal : null);
    for (let attempt = 0; ; attempt++) {
      try {
        return await nativeFetch(input instanceof Request ? input.clone() : input, init);
      } catch (err) {
        if (attempt >= RETRY_DELAYS_MS.length || err.name === "AbortError" || err.name === "TimeoutError" || signal?.aborted) throw err;
      }
      await sleep(RETRY_DELAYS_MS[attempt]);
      if (signal?.aborted) throw new DOMException("The operation was aborted.", "AbortError");
    }
  };

  // Bounded wait: fetchWithTimeout(url, opts, { timeoutMs, retries }). Each attempt is cut off after timeoutMs (a stalled
  // loopback or a busy Spooler never leaves a panel waiting for ever); only reads (GET/HEAD) are retried, writes never.
  // A timeout rejects with name "TimeoutError" and a plain-language message.
  window.fetchWithTimeout = async function (url, opts = {}, { timeoutMs = 15000, retries = 0 } = {}) {
    const method = (opts.method || "GET").toUpperCase();
    const attempts = (method === "GET" || method === "HEAD" ? retries : 0) + 1;
    for (let attempt = 1; ; attempt++) {
      const timer = AbortSignal.timeout(timeoutMs);
      const signal = opts.signal ? AbortSignal.any([opts.signal, timer]) : timer;
      try {
        return await window.fetch(url, { ...opts, signal });
      } catch (err) {
        if (opts.signal?.aborted) throw err;
        if (attempt >= attempts) {
          if (!timer.aborted) throw err;
          const error = new Error("Spooler did not answer in time");
          error.name = "TimeoutError";
          throw error;
        }
      }
    }
  };

  // One loading / error / empty block for every panel: spinner or message, plain copy, and an in-place Retry.
  // stateBlock(container, "loading"|"error"|"empty", "Plain copy", onRetry?, retryLabel?) replaces the container's content;
  // stateBlock(container) with no kind clears it. Loading fades in after a beat so a quick answer never flashes it.
  window.stateBlock = function (container, kind, message, onRetry, retryLabel = "Retry") {
    if (!container) return null;
    container.textContent = "";
    if (!kind) return null;
    const block = document.createElement("div");
    block.className = "state-block";
    block.dataset.state = kind;
    block.setAttribute("role", kind === "error" ? "alert" : "status");
    if (kind === "loading") {
      const spinner = document.createElement("span");
      spinner.className = "state-spinner";
      spinner.setAttribute("aria-hidden", "true");
      block.appendChild(spinner);
    }
    const text = document.createElement("p");
    text.textContent = message || "";
    block.appendChild(text);
    if (kind === "error" && onRetry) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "btn btn-secondary btn-sm state-retry";
      retry.textContent = retryLabel;
      retry.addEventListener("click", onRetry);
      block.appendChild(retry);
    }
    container.appendChild(block);
    return block;
  };

  // ── UI updates (app shell only; an embedded Projects page is reloaded with it) ──
  if (window.self !== window.top) return;
  const CHECK_EVERY_MS = 30000;
  let pending = false;

  // True while reloading would lose something: an open dialog or Quick print, or a project edit not yet saved.
  function busy() {
    if (document.querySelector("dialog[open], .qp-scrim")) return true;
    const frame = document.getElementById("projects-frame");
    try { return !!frame?.contentWindow?.spoolerBusy?.(); } catch (_) { return false; }
  }

  function offerReload() {
    if (document.getElementById("ui-update-banner")) return;
    const banner = document.createElement("button");
    banner.id = "ui-update-banner";
    banner.type = "button";
    banner.className = "ui-update-banner";
    banner.textContent = "Spooler was updated — reload to use the new version";
    banner.addEventListener("click", () => location.reload());
    document.body.appendChild(banner);
  }

  async function checkBuild() {
    if (!window.SPOOLER_BUILD) return;
    let build;
    try {
      const response = await nativeFetch("/api/build", { cache: "no-store" });
      if (!response.ok) return;
      build = (await response.json()).build;
    } catch (_) { return; }   // offline or restarting: the next check, or the reconnect, tries again
    if (!build || build === window.SPOOLER_BUILD) return;
    pending = true;
    if (busy()) offerReload();
    else location.reload();
  }

  // An update found while busy reloads as soon as the work is done.
  setInterval(() => {
    if (pending && !busy()) location.reload();
    else if (!pending && !document.hidden) checkBuild();
  }, CHECK_EVERY_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) checkBuild(); });
  window.addEventListener("online", checkBuild);
  window.spoolerCheckBuild = checkBuild;
})();
