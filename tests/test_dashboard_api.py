"""The dashboard's Needs you list and active project rows, tested through the backend HTTP API."""
import json
import unittest
import urllib.request

from tests import test_plate_status

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.ps = test_plate_status.PlateStatusTests("test_print_from_a_plate_keeps_its_project_and_plate_after_the_job_is_deleted")
        self.ps.setUp()
        self.addCleanup(self.ps.doCleanups)  # unpatch the auth check the fixture installed
        self.addCleanup(self.ps.tearDown)

    def dashboard(self):
        return self.ps.get("/api/dashboard")

    def post(self, path, body):
        request = urllib.request.Request(self.ps.base + path, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        with _opener.open(request, timeout=10) as resp:
            return json.loads(resp.read())

    def finish_plate(self, plate_id):
        job = self.ps.plate_job(plate_id)
        self.ps.run_job(job, f"plate{plate_id}.gcode")
        self.ps.push(9, f"plate{plate_id}.gcode", 50, 50)
        return job

    def test_a_quiet_farm_needs_nothing(self):
        board = self.dashboard()
        self.assertEqual(board["needs_you"], {"printers": [], "prints": []})
        self.assertEqual(board["projects"], [])

    def test_a_printer_whose_print_ended_waits_on_bed_cleared_with_the_next_job(self):
        self.finish_plate(4)
        queued = self.ps.plate_job(5)
        (waiting,) = self.dashboard()["needs_you"]["printers"]
        self.assertEqual((waiting["printer_id"], waiting["printer_name"], waiting["kind"]), ("p1", "Printer 1", "bed_cleared"))
        self.assertEqual(waiting["print"]["file_name"], "plate4.gcode")
        self.assertTrue(waiting["print"]["outcome_unconfirmed"])
        self.assertEqual(waiting["next_job"]["id"], queued["id"])

    def test_the_waiting_printers_print_is_not_listed_twice_as_unconfirmed(self):
        self.finish_plate(4)
        self.assertEqual(self.dashboard()["needs_you"]["prints"], [])

    def test_confirming_bed_cleared_without_an_outcome_leaves_the_print_unconfirmed(self):
        self.finish_plate(4)
        self.post("/api/printers/p1/auto-start", {"enabled": False})
        self.post("/api/printers/p1/bed-cleared", {})
        needs = self.dashboard()["needs_you"]
        self.assertEqual(needs["printers"], [])
        self.assertEqual([p["file_name"] for p in needs["prints"]], ["plate4.gcode"])

    def test_confirming_the_outcome_with_bed_cleared_clears_everything(self):
        self.finish_plate(4)
        print_id = self.ps.prints()[0]["id"]
        self.post("/api/printers/p1/bed-cleared", {"print_id": print_id, "outcome": "completed"})
        self.assertEqual(self.dashboard()["needs_you"], {"printers": [], "prints": []})

    def test_a_cleared_printer_with_a_waiting_job_needs_a_start_when_auto_start_is_off(self):
        self.finish_plate(4)
        queued = self.ps.plate_job(5)
        self.post("/api/printers/p1/auto-start", {"enabled": False})
        print_id = self.ps.prints()[0]["id"]
        self.post("/api/printers/p1/bed-cleared", {"print_id": print_id, "outcome": "completed"})
        (waiting,) = self.dashboard()["needs_you"]["printers"]
        self.assertEqual((waiting["printer_id"], waiting["kind"], waiting["next_job"]["id"]), ("p1", "start_job", queued["id"]))

    def test_a_printer_that_is_printing_needs_nothing(self):
        self.ps.run_job(self.ps.plate_job(4), "plate4.gcode")
        self.assertEqual(self.dashboard()["needs_you"]["printers"], [])

    def test_an_old_unconfirmed_print_on_a_ready_printer_is_listed_on_its_own(self):
        self.finish_plate(4)
        print_id = self.ps.prints()[0]["id"]
        self.post("/api/printers/p1/bed-cleared", {"print_id": print_id, "outcome": "completed"})
        self.finish_plate(5)
        self.post("/api/printers/p1/bed-cleared", {})
        needs = self.dashboard()["needs_you"]
        self.assertEqual([p["file_name"] for p in needs["prints"]], ["plate5.gcode"])

    def test_prints_and_jobs_with_no_project_still_show_on_the_dashboard_and_in_history(self):
        import io
        # A file queued from the printer file browser is a plain Job: no project, no plate
        queued = self.post("/api/jobs", {"printer_id": "p1", "printer_file": "/local/benchy.gcode"})
        self.assertEqual((queued["project_id"], queued["plate_id"]), (None, None))
        loose = test_plate_status.jobs.create_upload_job(io.BytesIO(test_plate_status.GCODE), len(test_plate_status.GCODE),
                                                         "loose.gcode", "p1")
        self.ps.run_job(loose, "loose.gcode")
        self.ps.push(9, "loose.gcode", 50, 50)

        (waiting,) = self.dashboard()["needs_you"]["printers"]
        self.assertEqual(waiting["print"]["file_name"], "loose.gcode")
        self.assertEqual(waiting["next_job"]["id"], queued["id"])
        (record,) = self.ps.prints()
        self.assertEqual((record["file_name"], record["project_id"], record["plate_id"]), ("loose.gcode", None, None))

    def test_active_project_rows_show_progress_and_leave_out_untouched_projects(self):
        self.assertEqual(self.dashboard()["projects"], [])
        self.ps.slice_plate(4)
        self.finish_plate(4)
        (row,) = self.dashboard()["projects"]
        self.assertEqual((row["id"], row["name"], row["printed_ok"], row["total"]), (self.ps.project_id, "SEAT", 1, 26))
        self.assertEqual(row["percent"], 4)


if __name__ == "__main__":
    unittest.main()
