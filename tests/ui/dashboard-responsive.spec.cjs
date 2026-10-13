const { chromium } = require("playwright");
const { readFile } = require("node:fs/promises");
const http = require("node:http");
const path = require("node:path");
const viewports = [
  [320, 640], [360, 800], [390, 844], [430, 932],
  [667, 375], [844, 390], [768, 1024], [1024, 768], [1440, 1000],
];

(async () => {
  const root = path.resolve(__dirname, "../..");
  const server = http.createServer(async (request, response) => {
    try {
      const pathname = new URL(request.url, "http://localhost").pathname;
      if (pathname === "/config.js") {
        response.setHeader("Content-Type", "text/javascript");
        return response.end("window.SPOOLER_WS_PORT = 1;");
      }
      const filename = path.join(root, "public", pathname === "/" ? "index.html" : pathname);
      const content = await readFile(filename);
      response.setHeader("Content-Type", filename.endsWith(".js") ? "text/javascript" : filename.endsWith(".css") ? "text/css" : "text/html");
      response.end(content);
    } catch { response.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const baseUrl = `http://127.0.0.1:${server.address().port}`;
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    for (const [width, height] of viewports) {
      const page = await browser.newPage({ viewport: { width, height }, serviceWorkers: "block" });
      await page.routeWebSocket(/.*/, ws => ws.close());
      await page.route("**/api/**", route => route.fulfill({ json: {} }));
      await page.goto(baseUrl);
      const result = await page.evaluate(() => ({
        viewportWidth: document.documentElement.clientWidth,
        bodyWidth: document.body.scrollWidth,
        actions: ["btn-quick-print", "btn-new-project"].map(id => {
          const element = document.getElementById(id);
          const { left, right } = element.getBoundingClientRect();
          return { id, reachable: getComputedStyle(element).display !== "none" && left >= 0 && right <= document.documentElement.clientWidth };
        }),
        hasOverflowMenu: getComputedStyle(document.getElementById("btn-header-more")).display !== "none",
      }));
      let menuWorks = true;
      if (width <= 720) {
        const more = page.getByRole("button", { name: "More actions" });
        await more.focus();
        await page.keyboard.press("Enter");
        menuWorks = await more.getAttribute("aria-expanded") === "true"
          && await page.getByRole("menuitem", { name: "Discover printers" }).isVisible()
          && await page.getByRole("menuitem", { name: "Add Printer" }).isVisible()
          && await page.evaluate(() => document.activeElement.id === "btn-header-discover");
        await page.keyboard.press("ArrowDown");
        menuWorks = menuWorks && await page.evaluate(() => document.activeElement.id === "btn-header-add-printer");
        await page.keyboard.press("Escape");
        menuWorks = menuWorks && await more.getAttribute("aria-expanded") === "false"
          && await page.evaluate(() => document.activeElement.id === "btn-header-more");
      }
      if (width <= 640) {
        const navMore = page.locator("#btn-mobile-more");
        await navMore.focus();
        await page.keyboard.press("Enter");
        menuWorks = menuWorks && await navMore.getAttribute("aria-expanded") === "true"
          && await page.evaluate(() => document.activeElement.id === "btn-mobile-history");
        await page.keyboard.press("Escape");
        menuWorks = menuWorks && await navMore.getAttribute("aria-expanded") === "false"
          && await page.evaluate(() => document.activeElement.id === "btn-mobile-more");
        const bottomNav = await page.locator(".sidenav").evaluate(element => {
          const rect = element.getBoundingClientRect();
          return rect.left >= 0 && rect.right <= innerWidth && rect.bottom <= innerHeight;
        });
        menuWorks = menuWorks && bottomNav;
      }
      const failed = result.bodyWidth > result.viewportWidth || result.actions.some(action => !action.reachable)
        || (width <= 720 && (!result.hasOverflowMenu || !menuWorks));
      result.overflowMenuWorks = width <= 720 ? menuWorks : undefined;
      console.log(`${width}x${height} ${failed ? "FAIL" : "PASS"}`, JSON.stringify(result));
      await page.close();
      if (failed) process.exitCode = 1;
    }

    const page = await browser.newPage({ viewport: { width: 390, height: 844 }, serviceWorkers: "block" });
    page.on("pageerror", error => console.error("Browser error:", error.message));
    await page.routeWebSocket(/.*/, ws => ws.close());
    let catalogueAttempts = 0;
    await page.route("**/api/**", route => {
      const url = route.request().url();
      if (url.includes("/api/backup")) return route.fulfill({ json: {
        name: "spooler-backup-test.zip", path: "temporary-test-backup.zip", size: 2048,
        created_at: Date.now() / 1000, errors: [],
      } });
      if (url.includes("/api/filament-meta")) {
        catalogueAttempts++;
        return catalogueAttempts === 1
          ? route.fulfill({ status: 503, json: { error: "catalogue unavailable" } })
          : route.fulfill({ json: { brands: ["Fixture brand"], materials: [{ name: "PLA", density: 1.24 }] } });
      }
      if (url.includes("/api/spoolman/api/v1/spool")) return route.fulfill({ json: [] });
      return route.fulfill({ json: {} });
    });
    page.on("response", response => { if (response.url().includes("/api/backup")) console.log("Backup fixture response", response.status()); });
    await page.goto(baseUrl);
    await page.evaluate(() => {
      eval("_wsConnected = true; _wsAttempts = 0;");
      applySystemStatus({ generated_at: Date.now() / 1000, spooler: { loop_lag_ms: 1, started_at: Date.now() / 1000, browser_clients: 1 },
        spoolman: { ok: true, latency_ms: 1, pending_deductions: 0 }, printers: [],
        backups: { running: false, latest: null, overdue: true, count: 0, keep: 14, interval_hours: 24, dir: "temporary" } });
    });
    await page.getByRole("button", { name: /System health/ }).click();
    const healthTimestampVisible = await page.locator("#system-health-updated").isVisible();
    await page.getByRole("button", { name: "Back up now" }).click();
    await page.waitForFunction(() => document.getElementById("backup-feedback")?.textContent.includes("spooler-backup-test.zip"), null, { timeout: 3000 });
    const backupFeedback = await page.locator("#backup-feedback").innerText();
    const backupResultPersistent = await page.locator("#backup-feedback").isVisible()
      && /recovery|restore/i.test(backupFeedback);
    console.log(`Health timestamp ${healthTimestampVisible ? "PASS" : "FAIL"}; backup recovery feedback ${backupResultPersistent ? "PASS" : "FAIL"}`);
    if (!healthTimestampVisible || !backupResultPersistent) process.exitCode = 1;

    await page.locator("#btn-system-health-close").click();
    await page.locator("#btn-mobile-more").click();
    await page.locator("#btn-mobile-settings").click();
    await page.locator("#btn-settings-goto-password").click();
    const passwordTitleFocused = await page.evaluate(() => document.activeElement.textContent.includes("Change password"));
    await page.locator("#btn-settings-back").click();
    const settingsContextRestored = await page.evaluate(() => document.activeElement.id === "btn-settings-goto-password");
    console.log(`Settings page focus ${passwordTitleFocused && settingsContextRestored ? "PASS" : "FAIL"}`);
    if (!passwordTitleFocused || !settingsContextRestored) process.exitCode = 1;
    await page.locator("#btn-settings-goto-password").click();
    await page.locator("#settings-new-password").fill("short");
    await page.locator("#settings-confirm-password").fill("short");
    await page.locator("#btn-app-settings-save").click();
    const passwordFeedback = await page.locator("#settings-feedback").innerText().catch(() => "");
    const passwordRejectedClearly = /at least 8 characters/i.test(passwordFeedback);
    console.log(`Password validation ${passwordRejectedClearly ? "PASS" : "FAIL"}`);
    if (!passwordRejectedClearly) process.exitCode = 1;
    await page.locator("#modal-app-settings").click({ position: { x: 2, y: 2 } });

    const printerUiResults = await page.evaluate(() => {
      window.__sentActions = [];
      send = message => window.__sentActions.push(message);
      printers.fixture = { id: "fixture", name: "Fixture printer", ip: "192.0.2.10", connected: false,
        printer_type: "cc1", status: {}, camera_url: null };
      openFileBrowser("fixture");
      const offlineDoesNotRequest = window.__sentActions.length === 0
        && document.getElementById("files-list").innerText.includes("offline");
      printers.fixture.connected = true;
      renderFileList([], "File list request timed out.");
      const canRetry = !!document.querySelector("#files-list button[data-action='retry-files']");
      return { offlineDoesNotRequest, canRetry };
    });
    if (printerUiResults.canRetry) {
      await page.locator("#files-list button[data-action='retry-files']").click();
      printerUiResults.retryTargetsSamePrinter = await page.evaluate(() => window.__sentActions.at(-1)?.printer_id === "fixture");
      await page.locator("#btn-files-close").click();
    }
    const cameraToggle = await page.evaluate(() => {
      document.getElementById("printer-grid").style.display = "grid";
      renderPrinter(printers.fixture);
      return !!document.querySelector("#card-fixture .camera-view-toggle");
    });
    if (cameraToggle) {
      await page.locator("#card-fixture .camera-view-toggle").click();
      printerUiResults.cameraExpands = await page.locator("#card-fixture .card-camera").evaluate(el => !el.classList.contains("camera-compact"));
    }
    const printerUiPass = printerUiResults.offlineDoesNotRequest && printerUiResults.canRetry
      && printerUiResults.retryTargetsSamePrinter && cameraToggle && printerUiResults.cameraExpands;
    console.log(`Printer monitoring recovery ${printerUiPass ? "PASS" : "FAIL"}`, JSON.stringify(printerUiResults));
    if (!printerUiPass) process.exitCode = 1;

    await page.evaluate(() => openPrinterDashboard("fixture"));
    await page.waitForFunction(() => typeof window.cockpitPrinterHtml === "function");
    await page.evaluate(() => renderPrinterDetail(printers.fixture));
    await page.getByRole("tab", { name: "Motion", exact: true }).focus();
    await page.keyboard.press("ArrowRight");
    const temperatureTab = page.getByRole("tab", { name: "Temperatures", exact: true });
    const temperatureTabOperable = await temperatureTab.getAttribute("aria-selected") === "true"
      && await page.getByRole("tabpanel", { name: "Temperatures", exact: true }).isVisible();
    const explicitTemperatureUnits = await page.getByRole("spinbutton", { name: "Nozzle target temperature (°C)" }).count() === 1;
    const printerDetailFits = await page.evaluate(() => document.body.scrollWidth <= innerWidth);
    console.log(`Printer detail keyboard/units/phone ${temperatureTabOperable && explicitTemperatureUnits && printerDetailFits ? "PASS" : "FAIL"}`);
    if (!temperatureTabOperable || !explicitTemperatureUnits || !printerDetailFits) process.exitCode = 1;
    await page.getByRole("button", { name: "All printers", exact: false }).click();

    await page.locator("#btn-spools").click();
    await page.locator("#btn-add-spool").click();
    await page.waitForFunction(() => document.getElementById("spool-catalogue-status")?.dataset.state === "error");
    const catalogueErrorVisible = await page.locator("#spool-catalogue-status").isVisible().catch(() => false);
    await page.locator("#spool-input-weight").fill("750");
    await page.locator("#spool-input-color").fill("Blue");
    const retryVisible = await page.locator("#btn-spool-catalogue-retry").isVisible().catch(() => false);
    if (retryVisible) await page.locator("#btn-spool-catalogue-retry").click();
    const catalogueRecovered = await page.waitForFunction(() =>
      !!document.querySelector("#spool-input-brand option[value='Fixture brand']"), null, { timeout: 3000 }).then(() => true).catch(() => false);
    const spoolFormPreserved = await page.locator("#spool-input-weight").inputValue() === "750"
      && await page.locator("#spool-input-color").inputValue() === "Blue";
    const spoolCataloguePass = catalogueErrorVisible && retryVisible && catalogueRecovered && spoolFormPreserved;
    console.log(`Spool catalogue retry ${spoolCataloguePass ? "PASS" : "FAIL"}`, JSON.stringify({ catalogueErrorVisible, retryVisible, catalogueRecovered, spoolFormPreserved, catalogueAttempts }));
    if (!spoolCataloguePass) process.exitCode = 1;
    await page.locator("#btn-add-spool-cancel").click();
    await page.route("**/api/spoolman/api/v1/spool", route => route.fulfill({ status: 503, json: { error: "Fixture Spoolman unavailable" } }));
    await page.locator("#btn-spools-close").click();
    await page.locator("#btn-spools").click();
    await page.waitForFunction(() => document.getElementById("spools-status")?.dataset.state === "error", null, { timeout: 3000 });
    const inventoryFailureClear = /Spoolman|connection/i.test(await page.locator("#spools-status").innerText())
      && await page.getByRole("button", { name: "Retry inventory" }).isVisible()
      && !await page.locator("#spools-empty").isVisible();
    console.log(`Spool dependency failure ${inventoryFailureClear ? "PASS" : "FAIL"}`);
    if (!inventoryFailureClear) process.exitCode = 1;

    // Toasts: announced, clear of the bottom nav, errors persist and can be dismissed, action slot works
    await page.evaluate(() => { window.__undone = 0; toast("Saved fixture"); toast("Fixture failed to save", true); toastAction("Removed fixture", "Undo", () => window.__undone++); });
    await page.waitForTimeout(400);  // let the entrance animation finish
    const toastBox = await page.locator("#toast-area").evaluate(area => {
      const nav = document.querySelector(".sidenav").getBoundingClientRect();
      const boxes = [...area.querySelectorAll(".toast")].map(t => t.getBoundingClientRect());
      return { live: area.getAttribute("role") === "status" && area.getAttribute("aria-live") === "polite",
        clear: boxes.length >= 3 && boxes.every(b => b.bottom <= nav.top && b.left >= 0 && b.right <= innerWidth) };
    });
    await page.locator(".toast-btn", { hasText: "Undo" }).focus();
    await page.keyboard.press("Enter");
    const undone = await page.evaluate(() => window.__undone === 1 && !document.querySelector(".toast-action"));
    await page.waitForTimeout(5000);
    const successGone = await page.locator(".toast", { hasText: "Saved fixture" }).count() === 0;
    const errorToast = page.locator(".toast.error");
    const errorPersists = await errorToast.count() === 1 && await errorToast.getAttribute("role") === "alert";
    await errorToast.getByRole("button", { name: "Dismiss" }).click();
    const errorDismissed = await errorToast.count() === 0;
    const toastPass = toastBox.live && toastBox.clear && undone && successGone && errorPersists && errorDismissed;
    console.log(`Toasts ${toastPass ? "PASS" : "FAIL"}`, JSON.stringify({ toastBox, undone, successGone, errorPersists, errorDismissed }));
    if (!toastPass) process.exitCode = 1;
    await page.close();

    // Accessible names and landmarks: no two buttons in one list share a name; one aria-current in the nav
    for (const [width, height] of [[1098, 507], [390, 844]]) {
      const a11y = await browser.newPage({ viewport: { width, height }, serviceWorkers: "block" });
      await a11y.routeWebSocket(/.*/, ws => ws.close());
      await a11y.route("**/api/**", route => route.fulfill({ json: {} }));
      await a11y.goto(baseUrl);
      const found = await a11y.evaluate(() => {
        const mk = (id, name) => ({ id, name, ip: "192.0.2." + id.length, connected: true, printer_type: "cc1", status: {}, camera_url: null });
        printers.alpha = mk("alpha", "Alpha");
        printers.bravo = mk("bravo", "Bravo");
        document.getElementById("printer-grid").style.display = "grid";
        renderPrinter(printers.alpha); renderPrinter(printers.bravo);
        _renderSettingsPrinters?.();
        const dupes = [];
        const lists = [...document.querySelectorAll("#printer-grid, #settings-printers-list, .settings-printer-list, nav.sidenav")];
        for (const list of lists) {
          const rows = [...list.querySelectorAll("button, a[href]")].filter(el => el.getClientRects().length);
          const names = rows.map(el => (el.getAttribute("aria-label") || el.textContent).trim().replace(/\s+/g, " "));
          const seen = new Set();
          for (const n of names) { if (seen.has(n) && !/^(Camera|Expand|Collapse)/i.test(n)) dupes.push(n); seen.add(n); }
        }
        const nav = document.querySelector("nav.sidenav");
        return { dupes, current: nav.querySelectorAll("[aria-current=page]").length,
          navLabel: nav.getAttribute("aria-label"), h1: [...document.querySelectorAll("h1")].filter(el => el.getClientRects().length).length,
          navNames: [...nav.querySelectorAll(".sidenav-item")].map(el => el.getAttribute("aria-label")),
          skip: document.querySelector(".skip-link")?.getAttribute("href") === "#main" && !!document.getElementById("main") };
      });
      await a11y.locator("#btn-slicer").click();
      await a11y.waitForFunction(() => document.getElementById("btn-slicer").classList.contains("active"));
      const projectsCurrent = await a11y.evaluate(() => [...document.querySelectorAll("nav.sidenav [aria-current=page]")].map(el => el.id));
      const a11yPass = found.dupes.length === 0 && found.current === 1 && found.navLabel === "Main" && found.h1 === 1 && found.skip
        && found.navNames.every(Boolean) && projectsCurrent.join() === "btn-slicer";
      console.log(`${width}x${height} Accessible names ${a11yPass ? "PASS" : "FAIL"}`, JSON.stringify({ found, projectsCurrent }));
      if (!a11yPass) process.exitCode = 1;
      await a11y.close();
    }

    // Hit targets at 768 px with a coarse pointer: every listed control is at least 44 px each way, and nothing overflows.
    for (const [width, height] of [[768, 1024], [1098, 507]]) {
      const context = await browser.newContext({ viewport: { width, height }, hasTouch: true, serviceWorkers: "block" });
      const hit = await context.newPage();
      await hit.routeWebSocket(/.*/, ws => ws.close());
      await hit.route("**/api/**", route => route.fulfill({ json: {} }));
      await hit.goto(baseUrl);
      await hit.evaluate(() => {
        printers.fixture = { id: "fixture", name: "Fixture printer", ip: "192.0.2.10", connected: true,
          printer_type: "cc1", status: {}, camera_url: null };
        document.getElementById("printer-grid").style.display = "grid";
        renderPrinter(printers.fixture);
        openFileBrowser("fixture");
        renderFileList([{ name: "part.gcode", size: 2048, mtime: 1 }]);
      });
      const measure = selectors => hit.evaluate(list => list.flatMap(selector =>
        [...document.querySelectorAll(selector)].filter(el => el.getClientRects().length).map(el => {
          const r = el.getBoundingClientRect();
          return { selector, w: Math.round(r.width), h: Math.round(r.height), text: (el.textContent || el.id).trim().slice(0, 20) };
        })), selectors);
      const mainSelectors = [".sidenav-item", ".card-settings-btn", ".card-remove-btn", ".card-files-btn", ".footer-version", ".file-actions .btn", "#files-list .btn-sm"];
      const small = (await measure(mainSelectors)).filter(item => item.h < 44 || (!item.selector.includes("sidenav") && item.selector.includes("btn") && item.selector !== ".file-actions .btn" && item.selector !== "#files-list .btn-sm" && item.w < 44));
      const sawFileButtons = (await measure([".file-actions .btn"])).length > 0;
      await hit.locator("#btn-files-close").click();
      await hit.evaluate(() => { window.__nav = true; });
      await hit.locator("#btn-app-settings, #btn-mobile-more").first().click().catch(() => {});
      const back = await hit.locator("#btn-settings-goto-password").click({ timeout: 1500 }).then(() => measure([".settings-back-btn"])).catch(() => []);
      small.push(...back.filter(item => item.w < 44 || item.h < 44));
      const overflow = await hit.evaluate(() => document.body.scrollWidth > document.documentElement.clientWidth);
      const pass = !small.length && sawFileButtons && !overflow;
      console.log(`Hit targets ${width}px coarse ${pass ? "PASS" : "FAIL"}`, JSON.stringify({ small, sawFileButtons, overflow }));
      if (!pass) process.exitCode = 1;
      await context.close();
    }

    {
      const context = await browser.newContext({ viewport: { width: 768, height: 1024 }, hasTouch: true, serviceWorkers: "block" });
      const hit = await context.newPage();
      await hit.addInitScript(() => { try { localStorage.removeItem("slicer.project"); } catch (_) { /* storage blocked */ } });
      await hit.route("**/api/**", route => {
        const p = new URL(route.request().url()).pathname;
        return route.fulfill({ json: p === "/api/projects" ? { projects: [{ id: 7, name: "Garden chair", plates: 3, objects: 16, models: 4, sliced: 3,
          slicing: 0, queued: 0, printing: 0, busy: false, split_id: null, updated_at: 1_800_000_300, created_at: 1 }] }
          : p === "/api/splits" ? { splits: [] } : p === "/api/printers" ? [] : {} });
      });
      await hit.goto(`${baseUrl}/slicer.html`);
      await hit.locator("#project-list .slicer-project-row").first().waitFor({ timeout: 8000 });
      const rows = await hit.evaluate(() => [...document.querySelectorAll("#project-list .slicer-project-row .btn")].map(el => {
        const r = el.getBoundingClientRect();
        return { text: el.textContent.trim(), w: Math.round(r.width), h: Math.round(r.height) };
      }));
      const overflow = await hit.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
      const pass = rows.length >= 2 && rows.every(item => item.h >= 44) && !overflow;
      console.log(`Projects list hit targets 768px ${pass ? "PASS" : "FAIL"}`, JSON.stringify({ rows, overflow }));
      if (!pass) process.exitCode = 1;
      await context.close();
    }
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exit(1); });
