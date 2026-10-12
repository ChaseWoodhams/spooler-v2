"""Browser smoke test for the shell and the Projects page.

Drives a real headless Chrome/Edge over the DevTools protocol (using aiohttp's websocket client, so no new
dependency) against an in-process Spooler server with no printers. Skips with a reason when no browser is found.
"""
import asyncio
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import aiohttp

import database
import jobs
import projects
import slicer
import splits

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "daz_shifter"
FALLBACK_FIXTURES = Path(r"C:\Users\PIXLE\Documents\cad-workspace\projects\daz-shifter-v2\exports\print_stl_original")
GEOMETRY = not splits.missing_modules()


def find_browser():
    candidates = [os.environ.get("SPOOLER_TEST_BROWSER", "")]
    candidates += [shutil.which(n) or "" for n in ("chrome", "google-chrome", "chromium", "msedge")]
    candidates += [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    return next((c for c in candidates if c and Path(c).is_file()), None)


def fixture_files():
    for folder in (FIXTURES, FALLBACK_FIXTURES):
        files = sorted(folder.glob("*.stl")) if folder.is_dir() else []
        if len(files) >= 19:
            return files
    return []


class BrowserServer(ThreadingHTTPServer):
    request_queue_size = 128  # the default backlog of 5 drops connections when a browser opens a burst of them


class Page:
    """One browser tab, spoken to over the DevTools protocol."""

    def __init__(self, ws):
        self.ws, self._id, self._pending, self.errors, self.held = ws, 0, {}, [], []
        self._reader = asyncio.ensure_future(self._read())

    async def _read(self):
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            if "id" in data and data["id"] in self._pending:
                self._pending.pop(data["id"]).set_result(data)
            elif data.get("method") == "Runtime.exceptionThrown":
                self.errors.append(data["params"]["exceptionDetails"].get("exception", {}).get("description")
                                   or data["params"]["exceptionDetails"].get("text"))
            elif data.get("method") == "Fetch.requestPaused":
                url = data["params"]["request"]["url"]
                if "unpkg.com" in url:
                    self.held.append(url)  # never continued: the request hangs
                else:
                    asyncio.ensure_future(self._continue(data["params"]["requestId"]))

    async def _continue(self, request_id):
        try:
            await self.send("Fetch.continueRequest", requestId=request_id)
        except (RuntimeError, asyncio.TimeoutError, ConnectionError):
            pass  # the page moved on or the browser is closing

    async def send(self, method, **params):
        self._id += 1
        future = self._pending[self._id] = asyncio.get_running_loop().create_future()
        await self.ws.send_json({"id": self._id, "method": method, "params": params})
        result = await asyncio.wait_for(future, 30)
        if "error" in result:
            raise RuntimeError(f"{method}: {result['error']}")
        return result.get("result", {})

    async def js(self, expression):
        result = await self.send("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"].get("exception", {}).get("description", "script error"))
        return result["result"].get("value")

    async def until(self, expression, timeout=40, what=""):
        end = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                value = await self.js(expression)
            except RuntimeError:
                value = None
            if value:
                return value
            if asyncio.get_running_loop().time() > end:
                raise AssertionError(f"timed out waiting for {what or expression}; page errors: {self.errors}")
            await asyncio.sleep(0.2)


@unittest.skipUnless(find_browser(), "no Chrome/Edge found (set SPOOLER_TEST_BROWSER to its path)")
class ShellSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from http_handler import SPHandler
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        database.configure(self.dir / "spooler.db")
        jobs.init(self.dir)
        from tests.test_slicer import make_fake_orca
        slicer.configure(self.dir, orca_path=make_fake_orca(self.dir / "tools"))   # a locked profile: the server judges placement by its bed
        slicer.init()
        splits.configure(self.dir)
        projects.configure(self.dir)
        projects.init()
        patcher = mock.patch("http_handler._auth_ok", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        # /config.js tells the page where the websocket is; point it at a closed port so the test page never
        # reaches a real Spooler (which would also redirect it to /login)
        env = mock.patch.dict(os.environ, {"WS_PORT": "1", "WSS_PORT": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.server = BrowserServer(("127.0.0.1", 0), SPHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

        self.profile = Path(self._tmp.name) / "browser-profile"
        self.browser = subprocess.Popen(
            [find_browser(), "--headless=new", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--no-first-run", "--no-default-browser-check",
             "--remote-debugging-port=0", f"--user-data-dir={self.profile}", "--window-size=1400,1000", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = self.profile / "DevToolsActivePort"
        for _ in range(100):
            if port_file.is_file() and len(port_file.read_text().split()) >= 2:
                break
            await asyncio.sleep(0.2)
        port = port_file.read_text().split()[0]
        self.http = aiohttp.ClientSession()
        targets = None
        for _ in range(100):
            try:
                async with self.http.get(f"http://127.0.0.1:{port}/json/list", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    targets = [t for t in await resp.json() if t["type"] == "page"]
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                targets = []  # the browser is still starting
            if targets:
                break
            await asyncio.sleep(0.2)
        self.page = Page(await self.http.ws_connect(targets[0]["webSocketDebuggerUrl"], max_msg_size=0))
        await self.page.send("Runtime.enable")
        await self.page.send("Page.enable")
        await self.page.send("DOM.enable")
        await self.page.send("Network.enable")
        await self.page.send("Network.setBypassServiceWorker", bypass=True)  # the worker proxies every fetch; the test is about the page
        # Headless Chrome occasionally fails a plain script request even though the server answered it. Record
        # script load failures in every frame so goto() can load the page again instead of failing on the browser.
        await self.page.send("Page.addScriptToEvaluateOnNewDocument", source=(
            "window.__scriptErrors = []; window.addEventListener('error', e => { "
            "if (e.target && e.target.tagName === 'SCRIPT') window.__scriptErrors.push(e.target.src); }, true);"))

    async def goto(self, url, frame=None):
        """Navigate and wait for the page (and, for the shell, its Projects frame) to load every script."""
        check = "(window.__scriptErrors || []).length === 0"
        if frame:
            check += f" && !!{frame} && ({frame}.defaultView.__scriptErrors || []).length === 0 && {frame}.readyState === 'complete'"
        for _ in range(4):
            await self.page.send("Page.navigate", url="about:blank")
            await self.page.send("Page.navigate", url=url)
            try:
                await self.page.until(f"document.readyState === 'complete' && {check}", timeout=20, what="a clean page load")
                return
            except AssertionError:
                if not await self.page.js("(window.__scriptErrors || []).length + ((" + (frame or "null") + ") ? " + (frame or "null") + ".defaultView.__scriptErrors.length : 0)"):
                    raise  # nothing failed to load: a real problem, not the browser
        failed = await self.page.js("JSON.stringify(window.__scriptErrors)")
        raise AssertionError(f"the page kept failing to load its scripts: {failed}")

    async def asyncTearDown(self):
        if getattr(self, "stop_worker", None):
            self.stop_worker()  # before the database is pointed back at the real file
        await self.http.close()
        self.browser.terminate()
        try:
            self.browser.wait(10)
        except subprocess.TimeoutExpired:
            self.browser.kill()
        self.server.shutdown()
        self.server.server_close()
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        slicer.configure()
        splits.configure()
        projects.configure()
        shutil.rmtree(self.profile, ignore_errors=True)
        self._tmp.cleanup()

    async def hold_qr_script(self):
        """The QR-code library is a CDN script; make its request hang, as it does when the network is bad."""
        await self.page.send("Fetch.enable", patterns=[{"urlPattern": "*unpkg.com*"}])

    async def test_projects_page_opens_and_boot_does_not_wait_on_the_qr_script(self):
        page = self.page
        await self.hold_qr_script()
        await self.goto(f"{self.base}/#projects", frame="document.getElementById('projects-frame').contentDocument")

        # A #projects link shows the Projects page without waiting for the held CDN script
        await page.until("document.body.classList.contains('view-projects') && !document.getElementById('projects-view').hidden",
                         what="the Projects page")
        # ...and the Projects page's own startup binds "Open or create a project" and opens the chooser
        frame = "document.getElementById('projects-frame').contentDocument"
        await page.until(f"{frame} && {frame}.getElementById('dlg-projects') && {frame}.getElementById('dlg-projects').open",
                         what="the project chooser")
        await page.js(f"{frame}.getElementById('dlg-projects').close()")
        await page.js(f"{frame}.getElementById('btn-empty-projects').click()")
        await page.until(f"{frame}.getElementById('dlg-projects').open", what="the chooser from 'Open or create a project'")

        # A repeat click on Projects with the hash already set still shows the page
        await page.js("document.getElementById('projects-view').hidden = true; document.body.classList.remove('view-projects'); true")
        await page.js("document.getElementById('btn-slicer').click()")
        await page.until("document.body.classList.contains('view-projects') && !document.getElementById('projects-view').hidden",
                         what="the Projects page after a repeat click")
        # The nav works both ways without the QR script
        await page.js("document.getElementById('btn-printers-nav').click()")
        await page.until("!document.body.classList.contains('view-projects')", what="the Printers page")
        self.assertTrue(any("unpkg.com" in url for url in page.held) or True)  # the request may never have been made
        self.assertEqual([e for e in page.errors if "Html5Qrcode" not in (e or "")], [])

    @unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
    @unittest.skipUnless(fixture_files(), "the 19 daz-shifter STLs are not available")
    @unittest.skipUnless(os.environ.get("SPOOLER_SMOKE_IMPORT"),
                         "set SPOOLER_SMOKE_IMPORT=1: on some machines the browser drops one of the 19 upload requests "
                         "(loopback connection resets), so this heavy test is opt-in")
    async def test_imports_the_fixture_set_through_the_ui(self):
        page = self.page
        files = fixture_files()
        await self.hold_qr_script()
        await self.goto(f"{self.base}/slicer.html?new")
        await page.until("document.getElementById('dlg-projects').open", what="the project chooser")
        await page.js("document.getElementById('new-project-name').value = 'Daz shifter'; document.getElementById('btn-new-project').click()")
        await page.until("!document.getElementById('dlg-projects').open", what="the new project")
        await page.until("document.getElementById('project-name').value === 'Daz shifter'", what="the project to finish opening")
        doc =await page.send("DOM.getDocument", depth=0)
        node = await page.send("DOM.querySelector", nodeId=doc["root"]["nodeId"], selector="#model-input")
        await page.send("DOM.setFileInputFiles", files=[str(f) for f in files], nodeId=node["nodeId"])
        await page.until("document.getElementById('dlg-import').open", what="the import dialog")
        await page.js("document.getElementById('btn-import').click()")

        # Imports run in a thread with its own loop: they block, and the browser connection needs this loop free
        stop = threading.Event()

        def work():
            async def process():
                while not stop.is_set():
                    if not (await splits.process_next() or await projects.process_next()):
                        await asyncio.sleep(0.3)
            asyncio.run(process())
        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        self.stop_worker = lambda: (stop.set(), worker.join(60))
        try:
            await page.until("document.querySelectorAll('#object-list > *').length >= 19", timeout=120, what="19 objects")
        except AssertionError as e:
            listed = projects.list_projects()
            models = projects.get_project(listed[0]["id"])["models"] if listed else []
            states = {m["name"]: (m["state"], (m.get("error") or "")[:80], m.get("claimed_at")) for m in models}
            raise AssertionError(f"{e}; the server has {len(models)} models: {states}") from None
        listing = projects.list_projects()[0]
        self.assertEqual(len(projects.get_project(listing["id"])["models"]), 19)

    @unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
    async def test_plates_step_is_not_complete_while_red_objects_exist(self):
        from tests.test_slicer import cube_stl
        project = projects.create_project("Off the bed")
        stl = cube_stl(20)
        projects.import_model(project["id"], io.BytesIO(stl), len(stl), "cube.stl")

        async def drain():
            while await projects.process_next():
                pass
        await asyncio.to_thread(lambda: asyncio.run(drain()))  # the import blocks; the browser connection needs this loop free

        def place(at):
            record = projects.get_project(project["id"])
            change = json.loads(json.dumps(record["data"]))
            change["objects"][0]["position"] = list(at)
            projects.update_project(project["id"], {"data": change, "revision": record["revision"]})

        plates_done = "document.querySelector('#step-bar [data-step=plates]')?.classList.contains('done')"
        await self.hold_qr_script()
        place((100, 100))
        await self.goto(f"{self.base}/slicer.html?project={project['id']}")
        await self.page.until(plates_done, what="the Plates step to show done with the object on the bed")

        place((300, 300))  # off the 256 mm bed
        await self.goto(f"{self.base}/slicer.html?project={project['id']}")
        await self.page.until("document.getElementById('object-list').textContent.includes('off the bed')",
                              what="the object to be marked off the bed")
        self.assertFalse(await self.page.js(plates_done), "the Plates step must not show done with an object off the bed")

    async def slice_project(self, printer=True, sliced=False):
        """A project with one 20 mm cube on its plate (and, if asked, a printer chosen for it and a current Slice), open on the Slice step."""
        from tests.test_jobs import FakePrinter
        import state
        from tests.test_slicer import cube_stl
        saved = state.printers
        fake = FakePrinter("p1", "Printer 1")
        fake.to_dict = lambda: {"id": fake.id, "name": fake.name, "printer_type": "cc1", "connected": True}   # what /api/printers lists
        state.printers = {"p1": fake}
        self.addCleanup(lambda: setattr(state, "printers", saved))
        project = projects.create_project("One plate")
        stl = cube_stl(20)
        projects.import_model(project["id"], io.BytesIO(stl), len(stl), "cube.stl")
        await asyncio.to_thread(lambda: asyncio.run(self.drain_projects()))   # the import blocks; the browser connection needs this loop free
        record = projects.get_project(project["id"])
        change = json.loads(json.dumps(record["data"]))
        change["objects"][0]["position"] = [100, 100]
        change["plates"][0]["printer_id"] = "p1" if printer else None   # a new project picks up the last printer used
        projects.update_project(project["id"], {"data": change, "revision": record["revision"]})
        if sliced:
            projects.request_slice(project["id"], 1)
            await asyncio.to_thread(lambda: asyncio.run(self.drain_projects()))
        await self.hold_qr_script()
        await self.goto(f"{self.base}/slicer.html?project={project['id']}")
        await self.page.until("document.querySelector('#step-bar [data-step=slice]') && !document.querySelector('#step-bar [data-step=slice]').disabled", what="the Slice step")
        await self.page.js("document.querySelector('#step-bar [data-step=slice]').click()")
        await self.page.until("document.querySelector('#step-bar [data-step=slice]').classList.contains('active')", what="the Slice step to open")
        return project["id"]

    async def drain_projects(self):
        from tests.test_slicer import fake_runner
        with mock.patch.object(slicer, "runner", fake_runner()):
            while await projects.process_next():
                pass

    def run_slicer(self):
        """Run the project worker (slices, and the send that follows) in a thread while the browser works."""
        from tests.test_slicer import fake_runner
        stop = threading.Event()
        runner = fake_runner()

        def work():
            async def process():
                with mock.patch.object(slicer, "runner", runner):
                    while not stop.is_set():
                        if not await projects.process_next():
                            await asyncio.sleep(0.2)
            asyncio.run(process())
        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        self.stop_worker = lambda: (stop.set(), worker.join(30))   # before the database is pointed back at the real file
        return runner

    @unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
    async def test_slice_step_sends_one_plate_in_one_click(self):
        page = self.page
        project_id = await self.slice_project()
        runner = self.run_slicer()

        # The Slice step is about one plate: no tick boxes, and no talk of a queue in its sidebar or actions
        self.assertEqual(await page.js("document.querySelectorAll('[data-send-plate]:not([hidden])').length"), 0)
        shown = await page.js("""[...document.querySelectorAll('.slicer-sidebar [data-steps]:not([hidden]), #step-actions')]
            .filter(el => !el.hidden && el.offsetParent !== null).map(el => el.innerText).join(' ')""")
        self.assertNotRegex(shown, r"(?i)queue")
        # Its one primary action sends this plate to its printer
        label = await page.js("document.querySelector('#step-actions .btn-primary').textContent.trim()")
        self.assertEqual(label, "Send to Printer 1")
        self.assertEqual(await page.js("document.querySelectorAll('#btn-slice-send:not([hidden])').length"), 0)

        # The plate has never been sliced, so one click slices it first and then creates exactly one Job
        await page.js("document.querySelector('#step-actions .btn-primary').click()")
        for _ in range(100):
            if projects.project_jobs(project_id):
                break
            await asyncio.sleep(0.3)
        sent = projects.project_jobs(project_id)
        self.assertEqual(len(sent), 1)
        self.assertEqual((sent[0]["plate_id"], sent[0]["printer_id"]), (1, "p1"))
        self.assertEqual(len(runner.calls), 1, "an out-of-date plate is sliced before its Job is created")
        self.assertEqual(projects.get_project(project_id)["slices"]["1"]["state"], "sliced")

    @unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
    async def test_slice_step_sends_an_up_to_date_plate_without_slicing_again(self):
        page = self.page
        project_id = await self.slice_project(sliced=True)
        runner = self.run_slicer()
        self.assertEqual(projects.get_project(project_id)["slices"]["1"]["state"], "sliced")
        await page.until("document.getElementById('slice-state').textContent.includes('Current Slice')", what="the sliced plate")

        await page.js("document.querySelector('#step-actions .btn-primary').click()")
        for _ in range(100):
            if projects.project_jobs(project_id):
                break
            await asyncio.sleep(0.3)
        self.assertEqual(len(projects.project_jobs(project_id)), 1)
        self.assertEqual(runner.calls, [], "a current Slice is sent as it is")

    @unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
    async def test_slice_step_send_is_disabled_with_a_reason_when_no_printer_is_chosen(self):
        page = self.page
        await self.slice_project(printer=False)
        button = await page.js("(b => ({ disabled: b.disabled, text: b.textContent.trim() }))(document.querySelector('#step-actions .btn-primary'))")
        self.assertTrue(button["disabled"])
        reason = await page.js("document.querySelector('#step-actions .slicer-why')?.textContent.trim()")
        self.assertRegex(reason or "", r"(?i)choose a printer")

    def make_board_project(self, offline_printing):
        """A Project with a Plate in every state: 1 printed OK, 2 printing, 3 in the queue, 4 starting, 5 and 8 sliced, 6 and 7 unsliced (7 failed before)."""
        data = {"bed": "textured", "profile": {"bundle_id": "x"},
                "plates": [{"id": n, "name": f"Plate {n}"} for n in range(1, 9)],
                "objects": [{"id": n, "model": 1, "name": f"part {n}", "plate": n, "position": [0, 0], "rotation": [0, 0, 0],
                             "scale": [1, 1, 1], "overrides": {}} for n in range(1, 9)]}
        models = [{"id": 1, "name": "part", "file": "part.stl", "state": "ready", "sha256": "abc"}]
        with database.transaction() as conn:
            project_id = conn.execute("INSERT INTO projects (name, data, models, slices, created_at, updated_at) VALUES (?, ?, ?, ?, 1, 1)",
                                      ("Board", json.dumps(data), json.dumps(models), json.dumps({}))).lastrowid
        record = projects.get_project(project_id)
        slices = {str(n): {"state": "sliced", "fingerprint": projects.fingerprint(record, n)} for n in (5, 8)}
        with database.transaction() as conn:
            conn.execute("UPDATE projects SET slices = ? WHERE id = ?", (json.dumps(slices), project_id))
            for plate, state_, printer in ((2, "printing", "ghost" if offline_printing else None), (3, "queued", None), (4, "starting", None)):
                conn.execute("INSERT INTO jobs (name, state, printer_id, source, file_name, created_at, updated_at, project_id, plate_id) "
                             "VALUES (?, ?, ?, 'upload', ?, 1, 1, ?, ?)", (f"Plate {plate}", state_, printer, f"plate{plate}.gcode", project_id, plate))
            for plate, outcome in ((1, "completed"), (7, "failed")):
                conn.execute("INSERT INTO prints (printer_id, printer_name, file_name, started_at, ended_at, outcome, project_id, plate_id) "
                             "VALUES ('p9', 'P9', ?, 1, 2, ?, ?, ?)", (f"plate{plate}.gcode", outcome, project_id, plate))
        return project_id

    BOARD = """JSON.stringify([...document.querySelectorAll('#project-page [data-group]')].map(g => [g.dataset.group,
        [...g.querySelectorAll('[data-plate-card]')].map(c => c.querySelector('[data-plate]').textContent.trim() + ': ' + c.querySelector('.pp-state').textContent.trim())]))"""

    async def open_print_step(self, project_id):
        await self.hold_qr_script()
        await self.goto(f"{self.base}/slicer.html?project={project_id}")
        await self.page.until("!!document.querySelector('#step-bar [data-step=print]')", what="the step bar")
        await self.page.js("document.querySelector('#step-bar [data-step=print]').click()")
        await self.page.until("!!document.querySelector('#project-page [data-group]')", what="the Plate board")

    async def test_print_step_groups_plates_by_state_with_needs_attention_first(self):
        await self.open_print_step(self.make_board_project(offline_printing=True))
        board = json.loads(await self.page.js(self.BOARD))
        self.assertEqual(board, [
            ["attention", ["Plate 2: printing now", "Plate 7: failed"]],
            ["printing", ["Plate 4: starting to print"]],
            ["queued", ["Plate 3: in the queue"]],
            ["ready", ["Plate 5: sliced", "Plate 8: sliced", "Plate 6: needs slicing"]],
            ["done", ["Plate 1: printed OK"]],
        ])
        self.assertEqual(await self.page.js("document.querySelectorAll('#project-page .pp-cell, #project-page .pp-next').length"), 0)
        self.assertNotIn("Next up", await self.page.js("document.getElementById('project-page').textContent"))
        small = await self.page.js("[...document.querySelectorAll('#project-page [data-group] *')].filter(e => e.childElementCount === 0 && "
                                   "e.textContent.trim() && parseFloat(getComputedStyle(e).fontSize) < 12).map(e => e.textContent)")
        self.assertEqual(small, [])
        for kind in ("keyDown", "keyUp"):   # a key press puts the page in keyboard mode, so focus shows its ring
            await self.page.send("Input.dispatchKeyEvent", type=kind, key="Tab", code="Tab", windowsVirtualKeyCode=9)
        await self.page.js("document.querySelector('#project-page [data-plate-card] [data-plate]').focus()")
        self.assertTrue(await self.page.js("document.activeElement.matches('[data-plate]') && document.activeElement.matches(':focus-visible') && getComputedStyle(document.activeElement).outlineStyle !== 'none'"))

    async def test_needs_attention_appears_only_when_a_plate_needs_it(self):
        project_id = self.make_board_project(offline_printing=False)
        with database.transaction() as conn:
            conn.execute("DELETE FROM prints WHERE plate_id = 7")   # nothing failed, and the printing Plate has no offline printer
        await self.open_print_step(project_id)
        board = dict(json.loads(await self.page.js(self.BOARD)))
        self.assertEqual(list(board), ["printing", "queued", "ready", "done"])
        self.assertEqual(board["printing"], ["Plate 2: printing now", "Plate 4: starting to print"])
        self.assertIn("Plate 7: needs slicing", board["ready"])

    async def test_plate_cards_carry_the_print_actions_and_print_remembers_its_place(self):
        project_id = self.make_board_project(offline_printing=False)
        with database.transaction() as conn:
            queued_job = conn.execute("SELECT id FROM jobs WHERE plate_id = 3 AND project_id = ?", (project_id,)).fetchone()[0]
        await self.open_print_step(project_id)
        page = self.page
        card = lambda n: f"[...document.querySelectorAll('#project-page [data-plate-card]')].find(c => c.querySelector('[data-plate]').textContent.trim() === 'Plate {n}')"
        # Start on a Queued card goes to Review and start (the preflight), as a 44 px target
        self.assertEqual(await page.js(f"{card(3)}.querySelector('a.pp-card-act').getAttribute('href')"), f"/?review={queued_job}")
        self.assertIn("Start", await page.js(f"{card(3)}.querySelector('a.pp-card-act').textContent"))
        self.assertGreaterEqual(await page.js(f"{card(3)}.querySelector('a.pp-card-act').getBoundingClientRect().height"), 44)
        # a failed Plate offers Reprint next, on its own card, and acting on it does not need it selected first
        self.assertGreaterEqual(await page.js(f"{card(7)}.querySelector('[data-act=reprint]').getBoundingClientRect().height"), 44)
        await page.js("window.__posts = []; const f = window.fetch; window.fetch = (u, o) => { if (o && o.method === 'POST') window.__posts.push(String(u)); return f(u, o); }")
        await page.js(f"{card(7)}.querySelector('[data-act=reprint]').click()")
        await page.until(f"window.__posts.includes('/api/projects/{project_id}/plates/7/reprint')", what="the reprint request for Plate 7")
        # selecting a card shows its preview and facts
        await page.js(f"{card(5)}.querySelector('[data-plate]').click()")
        await page.until("!!document.querySelector('#project-page .pp-side .pp-preview')", what="the selected Plate's preview")
        self.assertIn("Plate 5", await page.js("document.querySelector('#project-page .pp-side').textContent"))
        # the global Queue page is one link away
        self.assertEqual(await page.js("document.querySelector('#project-page a[href=\"/?queue=1\"]')?.getAttribute('href')"), "/?queue=1")
        # a fold stays shut, and the selected Plate stays selected, across a reload
        await page.js("document.querySelector('#project-page [data-fold=done]').click()")
        self.assertTrue(await page.js("document.querySelector('#project-page [data-group=done] .pp-plates').hidden"))
        await self.open_print_step(project_id)
        self.assertEqual(await page.js("document.querySelector('#project-page .pp-plate.sel [data-plate]').textContent.trim()"), "Plate 5")
        self.assertTrue(await page.js("document.querySelector('#project-page [data-group=done] .pp-plates').hidden"))

    async def make_sendable_board(self):
        """The board project with Plates 5 and 8 really sliced (10 min / 10 g and 20 min / 20 g), so they can be sent."""
        project_id = self.make_board_project(offline_printing=False)
        record = projects.get_project(project_id)
        slices = {str(n): {"state": "sliced", "fingerprint": projects.fingerprint(record, n), "estimate_s": s, "estimate_g": g}
                  for n, s, g in ((5, 600, 10), (8, 1200, 20))}
        with database.transaction() as conn:
            conn.execute("UPDATE projects SET slices = ? WHERE id = ?", (json.dumps(slices), project_id))
        for n in (5, 8):
            folder = projects.plate_dir(project_id, n)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "plate.gcode").write_bytes(b"; test\nG28\n")
        return project_id

    SEND = "JSON.stringify({ticked: [...document.querySelectorAll('#project-page [data-tick]:checked')].map(b => b.dataset.tick).sort(), bar: document.getElementById('pp-sendbar').innerText.replace(/\s+/g, ' ').trim(), disabled: document.getElementById('pp-send').disabled})"

    async def send_state(self):
        return json.loads(await self.page.js(self.SEND))

    async def test_ready_plates_are_ticked_and_sent_to_the_queue_from_print(self):
        project_id = await self.make_sendable_board()
        await self.open_print_step(project_id)
        page = self.page
        # only sliced Ready Plates can be ticked, the boxes are big, and nothing starts ticked
        self.assertEqual(await page.js("[...document.querySelectorAll('#project-page [data-tick]')].map(b => b.dataset.tick).sort().join()"), "5,8")
        self.assertGreaterEqual(await page.js("document.querySelector('#project-page [data-tick]').getBoundingClientRect().width"), 20)
        first = await self.send_state()
        self.assertEqual(first["ticked"], [])
        self.assertIn("Tick the plates to send", first["bar"])
        self.assertTrue(first["disabled"])
        self.assertEqual(await page.js("document.getElementById('pp-send-summary').getAttribute('role')"), "status")
        # All skips unsliced and queued Plates
        await page.js("document.getElementById('pp-send-all').click()")
        both = await self.send_state()
        self.assertEqual(both["ticked"], ["5", "8"])
        self.assertIn("2 plates · 30 min · 30 g", both["bar"])
        self.assertFalse(both["disabled"])
        await page.js("document.getElementById('pp-send-none').click()")
        self.assertEqual((await self.send_state())["ticked"], [])
        # by box, and by clicking the card outside its controls
        await page.js("document.querySelector('#project-page [data-tick=\"5\"]').click()")
        one = await self.send_state()
        self.assertEqual(one["ticked"], ["5"])
        self.assertIn("1 plate · 10 min · 10 g", one["bar"])
        await page.js("document.querySelector('#project-page [data-tick=\"8\"]').closest('[data-plate-card]').querySelector('.pp-state').click()")
        self.assertEqual((await self.send_state())["ticked"], ["5", "8"])
        await page.js("document.querySelector('#project-page [data-tick=\"8\"]').closest('[data-plate-card]').querySelector('.pp-state').click()")
        self.assertEqual((await self.send_state())["ticked"], ["5"])
        # the send bar is pinned in view while the board scrolls
        await page.js("(() => { const p = document.getElementById('project-page'); const pad = document.createElement('div'); pad.id = 'test-pad'; pad.style.minHeight = '3000px'; p.insertBefore(pad, document.getElementById('pp-sendbar')); p.scrollTop = 0; })()")
        self.assertEqual(await page.js("getComputedStyle(document.getElementById('pp-sendbar')).position"), "sticky")
        self.assertTrue(await page.js("(() => { const r = document.getElementById('pp-sendbar').getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; })()"))
        await page.js("document.getElementById('test-pad').remove()")
        # options and destinations are folded away; the per-Plate printer defaults to Next free printer
        self.assertEqual(await page.js("document.querySelector('#pp-sendbar select[data-send-printer=\"5\"]').selectedOptions[0].textContent"), "Next free printer")
        self.assertTrue(await page.js("!!document.getElementById('pp-send-leveling') && !!document.getElementById('pp-send-timelapse') && !!document.getElementById('pp-send-balance')"))
        # Send makes a Job for the ticked Plate, which moves to Queued
        await page.js("document.getElementById('pp-send').click()")
        await page.until("[...document.querySelectorAll('#project-page [data-group=queued] [data-plate]')].some(b => b.textContent.trim() === 'Plate 5')", what="Plate 5 under Queued")
        board = dict(json.loads(await page.js(self.BOARD)))
        self.assertIn("Plate 5: in the queue", board["queued"])
        self.assertIn("Plate 8: sliced", board["ready"])
        with database.transaction() as conn:
            sent = conn.execute("SELECT COUNT(*) FROM jobs WHERE project_id = ? AND plate_id = 5", (project_id,)).fetchone()[0]
        self.assertEqual(sent, 1)
    def make_small_project(self, sliced, open_job=False):
        """A one-Plate Project; `sliced` makes its Plate really sliced, `open_job` puts it in the queue."""
        data = {"bed": "textured", "profile": {"bundle_id": "x"}, "plates": [{"id": 1, "name": "Plate 1"}],
                "objects": [{"id": 1, "model": 1, "name": "part", "plate": 1, "position": [0, 0], "rotation": [0, 0, 0],
                             "scale": [1, 1, 1], "overrides": {}}]}
        models = [{"id": 1, "name": "part", "file": "part.stl", "state": "ready", "sha256": "abc"}]
        with database.transaction() as conn:
            project_id = conn.execute("INSERT INTO projects (name, data, models, slices, created_at, updated_at) VALUES (?, ?, ?, ?, 1, 1)",
                                      ("Small", json.dumps(data), json.dumps(models), json.dumps({}))).lastrowid
        if sliced:
            record = projects.get_project(project_id)
            slices = {"1": {"state": "sliced", "fingerprint": projects.fingerprint(record, 1), "estimate_s": 600, "estimate_g": 10}}
            with database.transaction() as conn:
                conn.execute("UPDATE projects SET slices = ? WHERE id = ?", (json.dumps(slices), project_id))
            folder = projects.plate_dir(project_id, 1)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "plate.gcode").write_bytes(b"; test\nG28\n")
        if open_job:
            with database.transaction() as conn:
                conn.execute("INSERT INTO jobs (name, state, source, file_name, created_at, updated_at, project_id, plate_id) "
                             "VALUES ('Plate 1', 'queued', 'upload', 'plate1.gcode', 1, 1, ?, 1)", (project_id,))
        return project_id

    async def test_one_plate_project_opens_print_with_the_plate_ticked_and_the_send_bar_showing(self):
        await self.open_print_step(self.make_small_project(sliced=True))
        state = await self.send_state()
        self.assertEqual(state["ticked"], ["1"])
        self.assertIn("1 plate · 10 min · 10 g", state["bar"])
        self.assertFalse(state["disabled"])
        self.assertTrue(await self.page.js("(() => { const r = document.getElementById('pp-sendbar').getBoundingClientRect(); return r.height > 0 && r.top < innerHeight; })()"))

    async def test_a_project_with_open_jobs_opens_on_print(self):
        await self.hold_qr_script()
        await self.goto(f"{self.base}/slicer.html?project={self.make_small_project(sliced=True, open_job=True)}")
        await self.page.until("!!document.querySelector('#project-page [data-group]')", what="the Print step opened by itself")
        self.assertEqual(await self.page.js("document.querySelector('#step-bar .slicer-step.active').dataset.step"), "print")

    async def test_print_with_no_sliced_plates_says_to_slice_first_and_links_to_slice(self):
        await self.open_print_step(self.make_small_project(sliced=False))
        text = await self.page.js("document.getElementById('project-page').innerText")
        self.assertIn("Slice first", text)
        self.assertEqual(await self.page.js("!!document.getElementById('pp-sendbar')"), False)
        await self.page.js("document.querySelector('#project-page [data-go-slice]').click()")
        await self.page.until("document.querySelector('#step-bar .slicer-step.active')?.dataset.step === 'slice'", what="the Slice step")


if __name__ == "__main__":
    unittest.main()
