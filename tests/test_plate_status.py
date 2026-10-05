"""A Print remembers its project and plate; the backend serves each plate's status and the project's progress."""
import asyncio
import json
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import database
import jobs
import print_history
import state
from http_handler import SPHandler
from tests.test_jobs import GCODE, FakePrinter

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PLATES = 26  # the saved SEAT project: 26 plates, only plate 4 sliced


class PlateStatusTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        database.configure(self.dir / "spooler.db")
        jobs.init(self.dir)
        print_history.init()
        self._saved = (state.printers, state.loop)
        self.printer = FakePrinter()
        state.printers = {"p1": self.printer}
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.loop_thread.start()
        state.loop = self.loop
        for patcher in (mock.patch.object(jobs, "uploader", lambda ip, path, name, progress: None),
                        mock.patch("http_handler._auth_ok", return_value=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SPHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.project_id = self.make_project()

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

    # fixtures

    def make_project(self) -> int:
        data = {"bed": "textured", "profile": {"bundle_id": "x"},
                "plates": [{"id": n, "name": f"Plate {n}"} for n in range(1, PLATES + 1)],
                "objects": [{"id": n, "model": 1, "name": f"part {n}", "plate": n, "position": [0, 0], "rotation": [0, 0, 0],
                             "scale": [1, 1, 1], "overrides": {}} for n in range(1, PLATES + 1)]}
        models = [{"id": 1, "name": "seat", "file": "seat.stl", "state": "ready", "sha256": "abc"}]
        with database.transaction() as conn:
            return conn.execute("INSERT INTO projects (name, data, models, slices, created_at, updated_at) VALUES (?, ?, ?, ?, 1, 1)",
                                ("SEAT", json.dumps(data), json.dumps(models), json.dumps({}))).lastrowid

    def slice_plate(self, plate_id, state_="sliced", **extra):
        """Record a finished slice for a plate (the saved SEAT project had plate 4 sliced)."""
        import projects
        record = projects.get_project(self.project_id)
        entry = {"state": state_, "fingerprint": projects.fingerprint(record, plate_id), **extra}
        slices = {**record["slices"], str(plate_id): entry}
        with database.transaction() as conn:
            conn.execute("UPDATE projects SET slices = ? WHERE id = ?", (json.dumps(slices), self.project_id))

    def plate_job(self, plate_id, printer_id="p1"):
        job = jobs.create_upload_job(__import__("io").BytesIO(GCODE), len(GCODE), f"plate{plate_id}.gcode", printer_id)
        with database.transaction() as conn:
            conn.execute("UPDATE jobs SET project_id = ?, plate_id = ? WHERE id = ?", (self.project_id, plate_id, job["id"]))
        return job

    def run_job(self, job, name):
        """Start the job on the printer and let the printer report it working."""
        asyncio.run_coroutine_threadsafe(jobs.start_job(job["id"]), self.loop).result(5)
        for _ in range(200):
            if self.printer.started:
                break
            threading.Event().wait(0.01)
        self.printer.started.clear()
        self.push(13, name, 1, 50)

    def push(self, code, filename="", layer=0, total=0):
        self.printer.status = {"CurrentStatus": [0], "PrintInfo": {
            "Status": code, "Filename": filename, "CurrentLayer": layer, "TotalLayer": total, "CurrentTicks": 0}}
        jobs.on_printer_status(self.printer)
        return print_history.on_printer_status(self.printer)

    def get(self, path):
        with _opener.open(self.base + path, timeout=10) as resp:
            return json.loads(resp.read())

    def call(self, method, path):
        request = urllib.request.Request(self.base + path, method=method)
        with _opener.open(request, timeout=10) as resp:
            return resp.status, json.loads(resp.read())

    def prints(self):
        return self.get("/api/prints")["prints"]

    # the Print-to-plate link

    def test_print_from_a_plate_keeps_its_project_and_plate_after_the_job_is_deleted(self):
        job = self.plate_job(4)
        self.run_job(job, "plate4.gcode")
        self.push(9, "plate4.gcode", 50, 50)
        record = self.prints()[0]
        self.assertEqual((record["project_id"], record["plate_id"], record["job_id"]), (self.project_id, 4, job["id"]))

        self.call("DELETE", f"/api/jobs/{job['id']}")
        record = self.prints()[0]
        self.assertEqual((record["project_id"], record["plate_id"], record["job_id"]), (self.project_id, 4, None))

    def test_print_without_a_project_is_unaffected_and_listed(self):
        self.push(13, "loose.gcode", 1, 50)
        self.push(9, "loose.gcode", 50, 50)
        listed = self.prints()
        self.assertEqual(len(listed), 1)
        self.assertEqual((listed[0]["project_id"], listed[0]["plate_id"], listed[0]["outcome"]), (None, None, "completed"))

    # per-plate status and project progress

    def status(self):
        return self.get(f"/api/projects/{self.project_id}/status")

    def states(self):
        return {p["id"]: p["status"] for p in self.status()["plates"]}

    def test_saved_project_has_every_plate_unsliced_except_the_sliced_one(self):
        self.slice_plate(4)
        status = self.status()
        self.assertEqual(len(status["plates"]), PLATES)
        states = self.states()
        self.assertEqual(states[4], "sliced")
        self.assertEqual({s for n, s in states.items() if n != 4}, {"unsliced"})
        self.assertEqual(status["progress"]["total"], PLATES)
        self.assertEqual(status["progress"]["counts"]["sliced"], 1)
        self.assertEqual(status["progress"]["counts"]["unsliced"], PLATES - 1)
        self.assertEqual(status["progress"]["printed_ok"], 0)

    def test_a_plate_in_the_queue_is_queued_and_one_held_by_another_plate_is_waiting(self):
        for n in (1, 2, 3):
            self.slice_plate(n)
        self.plate_job(1)
        held = self.plate_job(2)
        with database.transaction() as conn:
            conn.execute("INSERT INTO job_holds (job_id, after_project_id, after_plate_id, created_at) VALUES (?, ?, 1, 1)",
                         (held["id"], self.project_id))
        states = self.states()
        self.assertEqual((states[1], states[2], states[3]), ("queued", "waiting", "sliced"))

    def test_a_plate_whose_job_waits_on_another_job_is_waiting_until_that_job_completes(self):
        for n in (1, 2):
            self.slice_plate(n)
        first = self.plate_job(1, printer_id="p1")
        held = self.plate_job(2)
        with database.transaction() as conn:
            conn.execute("INSERT INTO job_holds (job_id, after_job_id, created_at) VALUES (?, ?, 1)", (held["id"], first["id"]))
        self.assertEqual((self.states()[1], self.states()[2]), ("queued", "waiting"))
        self.run_job(first, "plate1.gcode")
        self.push(9, "plate1.gcode", 50, 50)
        self.assertEqual((self.states()[1], self.states()[2]), ("printed_ok", "queued"))

    def test_a_plate_on_the_printer_is_printing_then_printed_ok(self):
        self.slice_plate(5)
        job = self.plate_job(5)
        self.run_job(job, "plate5.gcode")
        self.assertEqual(self.states()[5], "printing")
        self.push(9, "plate5.gcode", 50, 50)
        status = self.status()
        self.assertEqual(self.states()[5], "printed_ok")
        self.assertEqual(status["progress"]["printed_ok"], 1)
        self.assertEqual(status["progress"]["counts"]["printed_ok"], 1)

    def test_a_cancelled_print_is_failed_until_the_plate_is_printed_again(self):
        self.slice_plate(6)
        self.run_job(self.plate_job(6), "plate6.gcode")
        self.push(8, "plate6.gcode", 20, 50)
        self.assertEqual(self.states()[6], "failed")
        self.assertEqual(self.status()["progress"]["counts"]["failed"], 1)
        self.plate_job(6)
        self.assertEqual(self.states()[6], "queued")

    def test_status_survives_deleting_the_job_and_missing_projects_are_404(self):
        self.slice_plate(7)
        job = self.plate_job(7)
        self.run_job(job, "plate7.gcode")
        self.push(9, "plate7.gcode", 50, 50)
        self.call("DELETE", f"/api/jobs/{job['id']}")
        self.assertEqual(self.states()[7], "printed_ok")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/projects/999/status")
        self.assertEqual(caught.exception.code, 404)

    def test_status_carries_what_the_project_page_shows_for_each_plate(self):
        self.slice_plate(1, estimate_s=3600, estimate_g=10.0)
        self.slice_plate(2, estimate_s=7200, estimate_g=20.0)
        self.slice_plate(3)  # sliced, no estimate recorded
        self.run_job(self.plate_job(1), "plate1.gcode")
        self.push(9, "plate1.gcode", 50, 50)  # plate 1 printed OK
        queued = self.plate_job(2)
        plates = {p["id"]: p for p in self.status()["plates"]}
        self.assertEqual((plates[1]["estimate_s"], plates[1]["estimate_g"], plates[1]["parts"]), (3600, 10.0, 1))
        self.assertEqual((plates[2]["job_id"], plates[2]["printer_id"]), (queued["id"], "p1"))
        self.assertIsNone(plates[3]["estimate_s"])
        self.assertIsNone(plates[3]["job_id"])
        self.assertIsNone(plates[4]["estimate_g"])
        filament = self.status()["progress"]["filament_g"]
        self.assertEqual((filament["used"], filament["total"]), (10.0, 30.0))
        self.assertEqual(filament["unknown_plates"], PLATES - 2)  # every plate but 1 and 2 has no estimate yet

    # reprint of a failed plate

    def post(self, path):
        request = urllib.request.Request(self.base + path, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
        try:
            with _opener.open(request, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def fail_plate(self, plate_id, code=8):
        self.slice_plate(plate_id)
        job = self.plate_job(plate_id)
        self.run_job(job, f"plate{plate_id}.gcode")
        self.push(code, f"plate{plate_id}.gcode", 20, 50)
        return job

    def test_reprint_queues_a_failed_plate_again_for_the_same_printer_from_the_stored_gcode(self):
        job = self.fail_plate(8)
        self.assertEqual(self.states()[8], "failed")
        status, body = self.post(f"/api/projects/{self.project_id}/plates/8/reprint")
        self.assertEqual(status, 200)
        self.assertEqual((body["id"], body["state"], body["printer_id"]), (job["id"], "queued", "p1"))
        self.assertEqual(self.states()[8], "queued")

    def test_reprint_with_the_stored_gcode_gone_says_so_and_queues_nothing(self):
        job = self.fail_plate(9)
        for stored in (self.dir / "jobs").rglob("*.gcode"):
            stored.unlink()
        status, body = self.post(f"/api/projects/{self.project_id}/plates/9/reprint")
        self.assertEqual(status, 410)
        self.assertIn("G-code", body["error"])
        self.assertEqual(self.states()[9], "failed")
        self.assertEqual(jobs.get_job(job["id"])["state"], "cancelled")

    def test_reprint_of_a_plate_that_has_not_failed_is_refused(self):
        self.slice_plate(10)
        status, _ = self.post(f"/api/projects/{self.project_id}/plates/10/reprint")
        self.assertEqual(status, 409)
        status, _ = self.post(f"/api/projects/{self.project_id}/plates/999/reprint")
        self.assertEqual(status, 404)

    def test_reprint_when_the_failed_job_was_deleted_says_the_gcode_is_gone(self):
        job = self.fail_plate(11)
        self.call("DELETE", f"/api/jobs/{job['id']}")
        status, body = self.post(f"/api/projects/{self.project_id}/plates/11/reprint")
        self.assertEqual(status, 410)
        self.assertEqual(self.states()[11], "failed")

    # the project's filament total

    def record_grams(self, filename, grams):
        with database.transaction() as conn:
            conn.execute("UPDATE prints SET filament_g = ? WHERE file_name = ?", (grams, filename))

    def test_filament_used_is_what_the_prints_really_consumed_including_failed_ones(self):
        self.slice_plate(1, estimate_g=10.0)
        self.slice_plate(2, estimate_g=20.0)
        self.run_job(self.plate_job(1), "plate1.gcode")
        self.push(9, "plate1.gcode", 50, 50)
        self.record_grams("plate1.gcode", 11.5)  # the printer's own count beats the estimate
        self.run_job(self.plate_job(2), "plate2.gcode")
        self.push(8, "plate2.gcode", 20, 50)
        self.record_grams("plate2.gcode", 6.0)  # a failed plate still used filament
        filament = self.status()["progress"]["filament_g"]
        self.assertEqual((filament["used"], filament["total"]), (17.5, 30.0))

    def test_filament_shows_the_chosen_spool_from_spoolman_and_survives_spoolman_being_down(self):
        self.slice_plate(1, estimate_g=10.0)
        with database.transaction() as conn:
            row = conn.execute("SELECT data FROM projects WHERE id = ?", (self.project_id,)).fetchone()
            data = json.loads(row["data"])
            data["profile"]["spool_id"] = 7
            conn.execute("UPDATE projects SET data = ? WHERE id = ?", (json.dumps(data), self.project_id))
        with mock.patch("spoolman.spool_level", return_value={"name": "Sunlu PLA grey", "remaining_g": 420.0}) as level:
            spool = self.status()["progress"]["filament_g"]["spool"]
        level.assert_called_with(7)
        self.assertEqual(spool, {"id": 7, "name": "Sunlu PLA grey", "remaining_g": 420.0})
        with mock.patch("spoolman.spool_level", return_value=None):
            self.assertEqual(self.status()["progress"]["filament_g"]["spool"], {"id": 7, "name": None, "remaining_g": None})

    def test_filament_has_no_spool_when_the_project_has_none(self):
        self.assertIsNone(self.status()["progress"]["filament_g"]["spool"])

    # the per-plate preview picture

    def add_cube(self, model_id=1):
        import numpy as np
        verts = np.array([(x, y, z) for x in (0, 20) for y in (0, 20) for z in (0, 20)], "<f4")
        tris = np.array([(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
                         (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)], "<u4")
        folder = Path(self.dir) / "projects" / str(self.project_id) / "models" / str(model_id)
        folder.mkdir(parents=True)
        (folder / "preview.bin").write_bytes(b"SPV1" + struct.pack("<I", 1) + struct.pack("<II", len(verts), len(tris)) + verts.tobytes() + tris.tobytes())

    def picture(self, plate_id):
        with _opener.open(f"{self.base}/api/projects/{self.project_id}/plates/{plate_id}/preview.png", timeout=20) as resp:
            return resp.headers["Content-Type"], resp.read()

    def test_a_plate_has_a_preview_picture_of_its_layout_sliced_or_not(self):
        import projects
        projects.configure(self.dir)
        self.add_cube()
        kind, body = self.picture(3)  # plate 3 is not sliced
        self.assertEqual(kind, "image/png")
        self.assertEqual(body[:4], bytes([137]) + b"PNG")
        self.assertGreater(len(body), 300)

    def test_a_preview_for_a_missing_plate_is_404(self):
        import projects
        projects.configure(self.dir)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.picture(999)
        self.assertEqual(caught.exception.code, 404)
