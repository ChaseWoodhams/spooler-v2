"""Holds, carried preflight warnings, blocked unattended starts and the start snapshot, through the backend HTTP API."""
import time
import unittest
import urllib.request
from unittest import mock

import database
import jobs
import print_history
from tests import test_jobs_api


class HoldsApiTests(unittest.TestCase):
    def setUp(self):
        self.api = test_jobs_api.JobsApiTests("test_queue_workflow")
        self.api.setUp()
        self.addCleanup(self.api.tearDown)
        print_history.init()
        self.printer = self.api.printer
        self.notices = []
        patcher = mock.patch.object(jobs, "notifier", lambda title, body: self.notices.append((title, body)))
        patcher.start()
        self.addCleanup(patcher.stop)

    # helpers

    def queue(self, name="cube.gcode", opts=None):
        job = self.api.upload(name)[1]
        if opts:
            job = self.api.call("PATCH", f"/api/jobs/{job['id']}", {"print_opts": opts})[1]
        return job

    def push(self, code, filename="", layer=0, total=0):
        self.printer.status = {"CurrentStatus": [0], "PrintInfo": {
            "Status": code, "Filename": filename, "CurrentLayer": layer, "TotalLayer": total, "CurrentTicks": 0}}
        jobs.on_printer_status(self.printer)
        print_history.on_printer_status(self.printer)

    def wait_started(self):
        deadline = time.monotonic() + 3
        while not self.printer.started and time.monotonic() < deadline:
            time.sleep(0.02)

    def print_job(self, job, end_code):
        """Start the job, let it print and end the print with a printer status code (9 completed, 8 cancelled)."""
        status, _ = self.api.call("POST", f"/api/jobs/{job['id']}/start", {})
        self.assertEqual(status, 200)
        self.wait_started()
        self.printer.started.clear()
        self.push(13, job["file_name"], 1, 50)
        self.push(end_code, job["file_name"], 50, 50)

    def hold(self, job, **target):
        return self.api.call("POST", f"/api/jobs/{job['id']}/holds", target)

    def start_via_bed_cleared(self):
        return self.api.call("POST", "/api/printers/p1/bed-cleared", {})[1]

    def events(self, job):
        return self.api.call("GET", f"/api/jobs/{job['id']}/events")[1]["events"]

    def job(self, job_id):
        return next(j for j in self.api.call("GET", "/api/jobs")[1]["jobs"] if j["id"] == job_id)

    def finish_a_print(self):
        self.push(13, "old.gcode", 1, 50)
        self.push(9, "old.gcode", 50, 50)

    # holds

    def test_a_held_job_waits_until_the_other_job_completes(self):
        first, held = self.queue("a.gcode"), self.queue("b.gcode")
        status, result = self.hold(held, after_job_id=first["id"])
        self.assertEqual(status, 200)
        self.assertEqual([(h["after_job_id"], h["satisfied"]) for h in result["holds"]], [(first["id"], False)])
        self.assertTrue(self.job(held["id"])["held"])

        status, refused = self.api.call("POST", f"/api/jobs/{held['id']}/start", {})
        self.assertEqual(status, 409)
        self.assertIn("Waiting for", refused["error"])

        self.print_job(first, 9)
        job = self.job(held["id"])
        self.assertFalse(job["held"])
        self.assertTrue(job["holds"][0]["satisfied"])
        self.assertEqual(self.api.call("POST", f"/api/jobs/{held['id']}/start", {})[0], 200)

    def test_a_cancelled_job_does_not_release_a_hold(self):
        first, held = self.queue("a.gcode"), self.queue("b.gcode")
        self.hold(held, after_job_id=first["id"])
        self.print_job(first, 8)  # cancelled
        self.assertTrue(self.job(held["id"])["held"])
        self.assertEqual(self.api.call("POST", f"/api/jobs/{held['id']}/start", {})[0], 409)

    def test_bed_cleared_skips_held_jobs_and_releases_the_next_free_one(self):
        first, held, free = self.queue("a.gcode"), self.queue("b.gcode"), self.queue("c.gcode")
        self.hold(held, after_job_id=first["id"])
        self.api.call("POST", "/api/jobs/reorder", {"order": [held["id"], free["id"], first["id"]]})
        self.finish_a_print()
        result = self.start_via_bed_cleared()
        self.assertEqual((result["started"]["id"], result["start_error"]), (free["id"], None))

    def test_a_plate_hold_waits_for_the_plate_to_print_ok(self):
        with database.transaction() as conn:
            project = conn.execute("INSERT INTO projects (name, data, models, slices, created_at, updated_at) "
                                   "VALUES ('P', '{}', '[]', '{}', 1, 1)").lastrowid
        plate1, plate2 = self.queue("p1.gcode"), self.queue("p2.gcode")
        with database.transaction() as conn:
            for job, plate in ((plate1, 1), (plate2, 2)):
                conn.execute("UPDATE jobs SET project_id = ?, plate_id = ? WHERE id = ?", (project, plate, job["id"]))
        status, result = self.hold(plate2, after_project_id=project, after_plate_id=1)
        self.assertEqual((status, result["holds"][0]["satisfied"]), (200, False))
        self.print_job(plate1, 8)  # cancelled
        self.assertTrue(self.job(plate2["id"])["held"])
        self.api.call("POST", f"/api/jobs/{plate1['id']}/requeue")
        self.print_job(plate1, 9)  # printed again, this time completed
        self.assertFalse(self.job(plate2["id"])["held"])

    def test_holds_can_be_removed_and_are_validated(self):
        first, held = self.queue("a.gcode"), self.queue("b.gcode")
        self.assertEqual(self.hold(held, after_job_id=held["id"])[0], 400)
        self.assertEqual(self.hold(held, after_job_id=9999)[0], 404)
        self.assertEqual(self.hold(held)[0], 400)
        self.assertEqual(self.hold(held, after_project_id=1)[0], 400)
        _, result = self.hold(held, after_job_id=first["id"])
        hold_id = result["holds"][0]["id"]
        status, result = self.api.call("DELETE", f"/api/jobs/{held['id']}/holds/{hold_id}")
        self.assertEqual((status, result["holds"], result["held"]), (200, [], False))

    def test_a_longer_cycle_of_holds_is_refused(self):
        a, b, c = self.queue("a.gcode"), self.queue("b.gcode"), self.queue("c.gcode")
        self.assertEqual(self.hold(a, after_job_id=b["id"])[0], 200)
        self.assertEqual(self.hold(b, after_job_id=c["id"])[0], 200)
        status, refused = self.hold(c, after_job_id=a["id"])
        self.assertEqual(status, 409)
        self.assertIn("circle", refused["error"])
        self.assertEqual(self.job(c["id"])["holds"], [])
        self.assertFalse(self.job(c["id"])["held"])  # c can still start, so a printer is never left with nothing startable

    def test_a_cycle_through_a_plate_hold_is_refused(self):
        with database.transaction() as conn:
            project = conn.execute("INSERT INTO projects (name, data, models, slices, created_at, updated_at) "
                                   "VALUES ('P', '{}', '[]', '{}', 1, 1)").lastrowid
        plate1, plate2 = self.queue("p1.gcode"), self.queue("p2.gcode")
        with database.transaction() as conn:
            for job, plate in ((plate1, 1), (plate2, 2)):
                conn.execute("UPDATE jobs SET project_id = ?, plate_id = ? WHERE id = ?", (project, plate, job["id"]))
        self.assertEqual(self.hold(plate2, after_project_id=project, after_plate_id=1)[0], 200)
        status, refused = self.hold(plate1, after_job_id=plate2["id"])  # plate 1 would wait for plate 2's job
        self.assertEqual(status, 409)
        self.assertIn("circle", refused["error"])
        status, refused = self.hold(plate1, after_project_id=project, after_plate_id=2)
        self.assertEqual(status, 409)

    # carried warnings and blocked unattended starts

    def acknowledge(self, job):
        return self.api.call("POST", f"/api/jobs/{job['id']}/acknowledge-warnings", {})

    def test_warnings_acknowledged_at_queue_time_carry_through_to_auto_start(self):
        job = self.queue(opts={"smooth_plate": True})  # sliced for the textured plate: a warning
        status, result = self.acknowledge(job)
        self.assertEqual(status, 200)
        self.assertEqual(len(result["acknowledged"]), 1)
        self.finish_a_print()
        result = self.start_via_bed_cleared()
        self.assertEqual((result["started"]["id"], result["start_error"]), (job["id"], None))
        self.assertEqual(self.notices, [])

    def test_unacknowledged_warnings_block_an_automatic_start_and_notify(self):
        job = self.queue(opts={"smooth_plate": True})
        self.finish_a_print()
        result = self.start_via_bed_cleared()
        self.assertIsNone(result["started"])
        self.assertIn("warnings", result["start_error"])
        self.assertEqual(self.job(job["id"])["state"], "queued")
        self.assertEqual(self.printer.started, [])
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(self.events(job)[-1]["event"], "start_blocked")

    def test_a_new_warning_since_queueing_blocks_the_start(self):
        job = self.queue(opts={"smooth_plate": True})
        self.acknowledge(job)
        spool = {"spool": {"id": 7, "filament": {"material": "PETG"}}}
        self.finish_a_print()
        with mock.patch("preflight.loaded_spool", lambda printer: spool):
            result = self.start_via_bed_cleared()
        self.assertIsNone(result["started"])
        self.assertIn("PETG", result["start_error"])
        self.assertEqual(self.printer.started, [])
        self.assertEqual(len(self.notices), 1)

    def test_an_accepted_warning_whose_number_changed_is_still_accepted(self):
        job = self.queue()
        low = lambda grams: {"spool": {"id": 7, "remaining_weight": grams, "filament": {"material": "PLA"}}}
        with mock.patch("preflight.loaded_spool", lambda printer: low(1)):
            _, result = self.acknowledge(job)
        self.assertEqual(len(result["acknowledged"]), 1)  # Spool #7 has 1 g left; this print needs about N g
        self.finish_a_print()
        with mock.patch("preflight.loaded_spool", lambda printer: low(2)):  # the grams left changed since queueing
            result = self.start_via_bed_cleared()
        self.assertEqual((result["started"]["id"], result["start_error"]), (job["id"], None))
        self.assertEqual(self.notices, [])

    def test_a_different_warning_still_blocks_when_another_one_was_accepted(self):
        job = self.queue()
        low = {"spool": {"id": 7, "remaining_weight": 1, "filament": {"material": "PLA"}}}
        with mock.patch("preflight.loaded_spool", lambda printer: low):
            self.acknowledge(job)
        self.finish_a_print()
        petg = {"spool": {"id": 7, "remaining_weight": 1, "filament": {"material": "PETG"}}}
        with mock.patch("preflight.loaded_spool", lambda printer: petg):
            result = self.start_via_bed_cleared()
        self.assertIsNone(result["started"])
        self.assertIn("PETG", result["start_error"])
        self.assertNotIn("g left", result["start_error"])

    def test_changed_gcode_blocks_the_start(self):
        job = self.queue()
        stored = self.api.dir / job["stored_file"]
        self.finish_a_print()
        stored.write_bytes(stored.read_bytes() + b"; edited\n")
        result = self.start_via_bed_cleared()
        self.assertIsNone(result["started"])
        self.assertIn("changed", result["start_error"])
        self.assertEqual(len(self.notices), 1)

    def test_an_offline_printer_blocks_the_start_and_notifies(self):
        self.queue()
        self.finish_a_print()
        self.printer.connected = False
        result = self.start_via_bed_cleared()
        self.assertIsNone(result["started"])
        self.assertIn("offline", result["start_error"])
        self.assertEqual(len(self.notices), 1)

    # snapshot at start

    def test_a_snapshot_is_saved_on_the_timeline_when_the_printer_has_a_camera(self):
        self.printer.camera_url = "http://10.0.0.5:8080/?action=stream"
        taken = []
        job = self.queue()
        with mock.patch.object(jobs, "snapshotter", lambda url: taken.append(url) or b"\xff\xd8jpeg\xff\xd9"):
            self.assertEqual(self.api.call("POST", f"/api/jobs/{job['id']}/start", {})[0], 200)
            self.wait_started()
        self.assertEqual(taken, ["http://10.0.0.5:8080/?action=stream"])
        self.assertIn("snapshot", [e["event"] for e in self.events(job)])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"{self.api.base}/api/jobs/{job['id']}/snapshot", timeout=5) as resp:
            self.assertEqual((resp.status, resp.headers["Content-Type"], resp.read()),
                             (200, "image/jpeg", b"\xff\xd8jpeg\xff\xd9"))

    def test_without_a_camera_or_when_it_fails_the_start_goes_ahead(self):
        job = self.queue()
        self.assertEqual(self.api.call("POST", f"/api/jobs/{job['id']}/start", {})[0], 200)  # FakePrinter has no camera
        self.wait_started()
        self.assertNotIn("snapshot", [e["event"] for e in self.events(job)])

        self.printer.camera_url = "http://10.0.0.5/"
        second = self.queue("two.gcode")
        self.printer.connected = True
        self.api.call("POST", f"/api/jobs/{job['id']}/finish", {"outcome": "failed"})
        self.printer.started.clear()

        def broken(url):
            raise OSError("no camera")
        with mock.patch.object(jobs, "snapshotter", broken):
            self.assertEqual(self.api.call("POST", f"/api/jobs/{second['id']}/start", {})[0], 200)
            self.wait_started()
        self.assertEqual(len(self.printer.started), 1)
        self.assertNotIn("snapshot", [e["event"] for e in self.events(second)])


if __name__ == "__main__":
    unittest.main()
