"""Bed cleared and per-printer auto-start, tested through the backend HTTP API."""
import time
import unittest

import print_history
from tests import test_jobs_api


class BedClearedApiTests(unittest.TestCase):
    def setUp(self):
        self.api = test_jobs_api.JobsApiTests("test_queue_workflow")
        self.api.setUp()
        self.addCleanup(self.api.tearDown)
        print_history.init()
        self.printer = self.api.printer

    def finish_print(self, name="benchy.gcode"):
        """The printer prints a file and ends it; returns the print's id."""
        for code, layer in ((13, 10), (9, 100)):
            self.printer.set_status(code, name, layer, 100)
            print_history.on_printer_status(self.printer)
        return self.api.call("GET", "/api/prints")[1]["prints"][0]["id"]

    def queue(self, name="cube.gcode"):
        return self.api.upload(name)[1]

    def wait_started(self):
        deadline = time.monotonic() + 3
        while not self.printer.started and time.monotonic() < deadline:
            time.sleep(0.02)
        return self.printer.started

    def test_nothing_starts_without_confirmation(self):
        job = self.queue()
        self.finish_print()
        time.sleep(0.2)
        self.assertEqual(self.printer.started, [])
        self.assertEqual(self.api.call("GET", "/api/jobs")[1]["jobs"][0]["state"], "queued")
        status, bed = self.api.call("GET", "/api/printers/p1/bed-cleared")
        self.assertEqual((status, bed["valid"], bed["auto_start"]), (200, False, True))

    def test_confirming_releases_the_next_queued_job_when_auto_start_is_on(self):
        first, second = self.queue("a.gcode"), self.queue("b.gcode")
        self.finish_print()
        status, result = self.api.call("POST", "/api/printers/p1/bed-cleared", {})
        self.assertEqual((status, result["started"]["id"], result["start_error"]), (200, first["id"], None))
        self.assertEqual([f for f, _ in self.wait_started()], ["/local/a.gcode"])
        self.assertEqual(self.api.call("GET", f"/api/jobs/{second['id']}/events")[1]["events"][-1]["event"], "created")

    def test_with_auto_start_off_confirming_only_marks_the_printer_ready(self):
        job = self.queue()
        self.finish_print()
        status, settings = self.api.call("POST", "/api/printers/p1/auto-start", {"enabled": False})
        self.assertEqual((status, settings["auto_start"]), (200, False))
        status, result = self.api.call("POST", "/api/printers/p1/bed-cleared", {})
        self.assertEqual((status, result["started"], result["valid"]), (200, None, True))
        time.sleep(0.2)
        self.assertEqual(self.printer.started, [])
        # The user then presses Start through the ordinary jobs path.
        self.assertEqual(self.api.call("POST", f"/api/jobs/{job['id']}/start", {})[0], 200)

    def test_confirmation_expires_when_the_next_print_ends(self):
        self.finish_print("one.gcode")
        self.api.call("POST", "/api/printers/p1/auto-start", {"enabled": False})
        self.api.call("POST", "/api/printers/p1/bed-cleared", {})
        self.assertTrue(self.api.call("GET", "/api/printers/p1/bed-cleared")[1]["valid"])
        time.sleep(0.02)
        self.finish_print("two.gcode")
        self.assertFalse(self.api.call("GET", "/api/printers/p1/bed-cleared")[1]["valid"])

    def test_an_offline_printer_is_confirmed_but_nothing_starts(self):
        job = self.queue()
        self.finish_print()
        self.printer.connected = False
        status, result = self.api.call("POST", "/api/printers/p1/bed-cleared", {})
        self.assertEqual((status, result["started"], result["valid"]), (200, None, True))
        self.assertIn("offline", result["start_error"])
        self.assertEqual(self.printer.started, [])

    def test_a_printer_that_is_printing_cannot_be_confirmed(self):
        self.queue()
        self.printer.set_status(13, "benchy.gcode", 10, 100)
        print_history.on_printer_status(self.printer)
        status, result = self.api.call("POST", "/api/printers/p1/bed-cleared", {})
        self.assertEqual(status, 409)
        self.assertEqual(self.printer.started, [])

    def test_unknown_printer_is_not_found(self):
        self.assertEqual(self.api.call("POST", "/api/printers/nope/bed-cleared", {})[0], 404)

    def test_printed_ok_and_bed_cleared_in_one_action_confirms_the_outcome(self):
        print_id = self.finish_print()
        self.api.call("POST", "/api/printers/p1/auto-start", {"enabled": False})
        status, result = self.api.call("POST", "/api/printers/p1/bed-cleared",
                                       {"print_id": print_id, "outcome": "completed"})
        self.assertEqual((status, result["print"]["outcome"], result["print"]["outcome_unconfirmed"]),
                         (200, "completed", False))
        self.assertTrue(result["valid"])
        self.assertEqual(self.api.call("POST", "/api/printers/p1/bed-cleared",
                                       {"print_id": print_id, "outcome": "failed"})[0], 400)


if __name__ == "__main__":
    unittest.main()
