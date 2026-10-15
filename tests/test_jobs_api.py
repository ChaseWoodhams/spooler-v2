import asyncio
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import database
import jobs
import state
from http_handler import SPHandler
from tests.test_jobs import GCODE, FakePrinter

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class JobsApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        database.configure(self.dir / "spooler.db")
        jobs.init(self.dir)
        self._saved = (state.printers, state.loop)
        self.printer = FakePrinter()
        state.printers = {"p1": self.printer}

        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.loop_thread.start()
        state.loop = self.loop

        self.uploads = []
        for target, value in ((jobs, {"uploader": lambda ip, path, name, progress: self.uploads.append(name)}),):
            patcher = mock.patch.multiple(target, **value)
            patcher.start()
            self.addCleanup(patcher.stop)
        from tests.test_jobs import isolate_preflight
        isolate_preflight(self, self.dir)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SPHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

        async def cancel_start_tasks():
            for task in list(jobs._start_tasks.values()):
                task.cancel()

        asyncio.run_coroutine_threadsafe(cancel_start_tasks(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop_thread.join(5)
        self.loop.close()
        state.printers, state.loop = self._saved
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        self._tmp.cleanup()

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {"Content-Type": "application/octet-stream" if raw is not None else "application/json"}
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with _opener.open(request, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def upload(self, filename, printer_id="p1"):
        return self.call("POST", f"/api/jobs/upload?filename={filename}&printer_id={printer_id}", raw=GCODE)

    def test_queue_workflow(self):
        status, a = self.upload("cube.gcode")
        self.assertEqual((status, a["state"], a["estimate_s"]), (200, "queued", 3723))
        _, b = self.upload("plate.gcode")

        self.assertEqual(self.call("POST", "/api/jobs/reorder", {"order": [b["id"], a["id"]]})[0], 200)
        _, listing = self.call("GET", "/api/jobs")
        self.assertEqual([j["id"] for j in listing["jobs"]], [b["id"], a["id"]])

        status, edited = self.call("PATCH", f"/api/jobs/{a['id']}", {"notes": "first plate", "print_opts": {"leveling": True}})
        self.assertEqual((status, edited["notes"], edited["print_opts"]["leveling"]), (200, "first plate", True))

        status, started = self.call("POST", f"/api/jobs/{a['id']}/start", {})
        self.assertEqual((status, started["state"]), (200, "starting"))
        deadline = time.monotonic() + 3
        while not self.printer.started and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.printer.started, [("/local/cube.gcode", {"timelapse": False, "leveling": True, "smooth_plate": False})])

        status, refused = self.call("POST", f"/api/jobs/{b['id']}/start", {})
        self.assertEqual(status, 409)
        self.assertIn("already running", refused["error"])

        self.printer.connected = False
        status, finished = self.call("POST", f"/api/jobs/{a['id']}/finish", {"outcome": "failed"})
        self.assertEqual((status, finished["state"]), (200, "failed"))
        self.assertEqual(self.call("POST", f"/api/jobs/{a['id']}/requeue")[1]["state"], "queued")
        self.assertEqual(self.call("DELETE", f"/api/jobs/{b['id']}")[0], 200)

        _, events = self.call("GET", f"/api/jobs/{a['id']}/events")
        self.assertIn("requeued", [e["event"] for e in events["events"]])

    def test_errors_are_reported_with_status_codes(self):
        self.assertEqual(self.upload("cube.stl")[0], 400)
        self.assertEqual(self.call("PATCH", "/api/jobs/999", {"notes": "x"})[0], 404)
        self.assertEqual(self.call("POST", "/api/jobs/reorder", raw=b"not json")[0], 400)
        self.assertEqual(self.call("POST", "/api/jobs/1/explode", {})[0], 404)

    def test_print_history_routes(self):
        import print_history

        print_history.init()
        for code, layer in ((13, 10), (9, 100)):
            self.printer.set_status(code, "benchy.gcode", layer, 100)
            print_history.on_printer_status(self.printer)

        status, listing = self.call("GET", "/api/prints?q=benchy&outcome=completed")
        self.assertEqual((status, listing["total"], listing["summary"]["success_rate"]), (200, 1, 1.0))
        print_id = listing["prints"][0]["id"]

        status, updated = self.call("PATCH", f"/api/prints/{print_id}", {"notes": "great first layer"})
        self.assertEqual((status, updated["notes"]), (200, "great first layer"))
        status, detail = self.call("GET", f"/api/prints/{print_id}")
        self.assertEqual((status, detail["job"], detail["layer"]), (200, None, 100))
        status, job = self.call("POST", f"/api/prints/{print_id}/repeat", {})
        self.assertEqual((status, job["printer_file"], job["state"]), (200, "/local/benchy.gcode", "queued"))

        status, unconfirmed = self.call("GET", "/api/prints?unconfirmed=1")
        self.assertEqual((status, unconfirmed["total"]), (200, 1))
        self.assertEqual(self.call("POST", f"/api/prints/{print_id}/outcome", {"outcome": "failed"})[0], 400)
        status, fixed = self.call("POST", f"/api/prints/{print_id}/outcome",
                                  {"outcome": "failed", "failure_reasons": ["spaghetti"]})
        self.assertEqual((status, fixed["outcome"], fixed["failure_reasons"], fixed["outcome_unconfirmed"]),
                         (200, "failed", ["spaghetti"], False))
        self.assertEqual(self.call("GET", "/api/prints?unconfirmed=1")[1]["total"], 0)
        status, reasons = self.call("GET", "/api/failure-reasons")
        self.assertEqual((status, len(reasons["failure_reasons"])), (200, 9))
        self.assertEqual(reasons["cancel_reasons"], reasons["failure_reasons"] + ["cancelled_on_purpose"])
        self.assertEqual(set(reasons["labels"]), set(reasons["cancel_reasons"]))  # the one list of labels the UI renders
        self.assertEqual(reasons["labels"]["adhesion"], "Adhesion / came loose")
        self.assertEqual(reasons["labels"]["cancelled_on_purpose"], "Cancelled on purpose")

        self.assertEqual(self.call("GET", "/api/prints?outcome=exploded")[0], 400)
        self.assertEqual(self.call("GET", "/api/prints?since=yesterday")[0], 400)
        self.assertEqual(self.call("GET", "/api/prints/999")[0], 404)

        with _opener.open(self.base + "/api/prints/export.csv?q=benchy", timeout=10) as resp:
            self.assertIn("text/csv", resp.headers["Content-Type"])
            body = resp.read().decode("utf-8-sig")
        self.assertIn("benchy.gcode", body)
        self.assertIn("great first layer", body)

    def test_preflight_preview_and_nozzle_routes(self):
        _, job = self.upload("cube.gcode")
        status, report = self.call("GET", f"/api/jobs/{job['id']}/preflight")
        self.assertEqual((status, report["status"], report["can_start"]), (200, "pass", True))
        self.assertEqual(len(report["preview"]["layers"]), 1)
        with _opener.open(self.base + f"/api/jobs/{job['id']}/toolpath", timeout=10) as resp:
            self.assertEqual(resp.headers["Content-Type"], "application/octet-stream")
            self.assertGreater(len(resp.read()), 0)

        self.assertEqual(self.call("GET", "/api/printers/p1/hardware"), (200, {"nozzle_diameter": None, "nozzle_material": None}))
        status, hardware = self.call("POST", "/api/printers/p1/hardware", {"nozzle_diameter": 0.6})
        self.assertEqual((status, hardware["nozzle_diameter"]), (200, 0.6))
        self.assertEqual(self.call("POST", "/api/printers/p1/hardware", {"nozzle_diameter": 0.5})[0], 400)
        self.assertFalse(self.call("GET", f"/api/jobs/{job['id']}/preflight?printer_id=p1")[1]["can_start"])
        status, refused = self.call("POST", f"/api/jobs/{job['id']}/start", {})
        self.assertEqual(status, 409)
        self.assertIn("Preflight failed", refused["error"])

        _, stored = self.call("POST", "/api/jobs", {"printer_id": "p1", "printer_file": "/local/benchy.gcode"})
        self.assertEqual(self.call("GET", f"/api/jobs/{stored['id']}/toolpath")[0], 404)
        self.assertEqual(self.call("POST", f"/api/jobs/{stored['id']}/start", {})[0], 409)  # needs acknowledgement
        status, started = self.call("POST", f"/api/jobs/{stored['id']}/start", {"acknowledge_warnings": True})
        self.assertEqual((status, started["state"]), (200, "starting"))

    def test_the_standalone_slice_endpoints_are_retired(self):
        import slicer
        from tests.test_slicer import cube_stl, fake_runner, make_fake_orca

        slicer.configure(self.dir, orca_path=make_fake_orca(self.dir / "tools"))
        self.addCleanup(slicer.configure)
        slicer.init()
        patcher = mock.patch.object(slicer, "runner", fake_runner())
        patcher.start()
        self.addCleanup(patcher.stop)

        status, info = self.call("GET", "/api/slicer")
        self.assertEqual((status, info["available"], len(info["bundles"])), (200, True, 1))
        bundle_id = info["bundles"][0]["id"]
        status, presets = self.call("GET", "/api/slicer/presets")
        self.assertEqual((status, presets["machines"]), (200, ["Elegoo Centauri Carbon 0.4 nozzle"]))
        status, same = self.call("POST", "/api/slicer/profiles", presets["defaults"])
        self.assertEqual((status, same["id"]), (200, bundle_id))

        # The standalone slice door is closed: Import (POST /api/projects/upload) is the only way in
        query = f"filename=cube.stl&bundle_id={bundle_id}&bed=smooth&leveling=1&printer_id=p1&notes=bracket%20v2"
        self.assertEqual(self.call("POST", f"/api/slices/upload?{query}", raw=cube_stl())[0], 404)
        with self.assertRaises(urllib.error.HTTPError) as gone:  # a page request, so the 404 isn't JSON
            _opener.open(self.base + "/api/slices", timeout=10)
        self.assertEqual(gone.exception.code, 404)
        self.assertEqual(self.call("POST", "/api/slices/1/retry", {})[0], 404)
        self.assertEqual(self.call("DELETE", "/api/slices/1")[0], 404)
        self.assertEqual(jobs.list_jobs(), [])


if __name__ == "__main__":
    unittest.main()
