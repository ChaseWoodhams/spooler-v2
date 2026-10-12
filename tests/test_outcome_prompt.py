"""Outcome prompt, push and reminders, tested through the backend HTTP API with Web Push mocked."""
import http.server
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import jobs
import outcome_prompt
import print_history
import snapshots
from tests import test_plate_status
from tests.test_jobs import GCODE

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
FRAME = b"\xff\xd8\xff\xe0fakejpeg\xff\xd9"
HEAD = b"--frame" + bytes([13, 10]) + b"Content-Type: image/jpeg" + bytes([13, 10, 13, 10])
TAIL = bytes([255, 216]) + b"tail"


class OutcomePromptTests(unittest.TestCase):
    def setUp(self):
        self.plates = test_plate_status.PlateStatusTests("test_print_without_a_project_is_unaffected_and_listed")
        self.plates.setUp()
        self.addCleanup(self.plates.tearDown)
        self.printer = self.plates.printer
        self.sent = []
        self.no_camera = mock.patch.object(snapshots, "grab_frame", lambda url, timeout=8: None)
        for patcher in (mock.patch.object(outcome_prompt, "send_push", lambda title, body, data=None: self.sent.append((title, body, data))),
                        mock.patch.object(outcome_prompt, "run_in_background", lambda fn, *args: fn(*args)),
                        self.no_camera):
            patcher.start()
            self.addCleanup(patcher.stop)
        outcome_prompt.init()
        self.addCleanup(outcome_prompt.shutdown)

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.plates.base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with _opener.open(request, timeout=10) as resp:
                return resp.status, resp.read() if path.endswith("/snapshot") else json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def plate_print(self, plate_id=4, end=9, name=None):
        name = name or f"plate{plate_id}.gcode"
        self.plates.slice_plate(plate_id)
        job = self.plates.plate_job(plate_id)
        self.plates.run_job(job, name)
        self.plates.push(end, name, 50, 50)
        return job

    def prompts(self):
        return self.call("GET", "/api/outcome-prompts")[1]["awaiting"]

    # push when a Print closes

    def test_push_names_printer_project_and_plate_gives_outcome_and_times_and_deep_links(self):
        self.plate_print(4)
        self.assertEqual(len(self.sent), 1)
        title, body, data = self.sent[0]
        self.assertEqual(title, "Printer 1 finished SEAT · Plate 4")
        self.assertIn("completed", body)
        self.assertRegex(body, r"\d.* of .*\d")  # actual versus estimated time
        self.assertEqual(data["url"], f"/#projects={self.plates.project_id}&printer=p1&plate=4")
        self.assertEqual(data["printer_id"], "p1")
        self.assertEqual(data["actions"], [{"action": "bed-cleared", "title": "Printed OK · Bed cleared"}])
        self.assertEqual(data["bed_cleared"], {"url": "/api/printers/p1/bed-cleared",
                                               "body": {"print_id": data["print_id"], "outcome": "completed"}})

    def test_push_for_a_failed_print_has_no_printed_ok_button(self):
        self.plate_print(5, end=8)
        title, body, data = self.sent[0]
        self.assertIn("cancelled", body)
        self.assertEqual(data["actions"], [])

    def test_push_for_a_print_with_no_project_names_the_file_and_opens_the_printer(self):
        self.plates.push(13, "loose.gcode", 1, 50)
        self.plates.push(9, "loose.gcode", 50, 50)
        title, _, data = self.sent[0]
        self.assertEqual(title, "Printer 1 finished loose.gcode")
        self.assertEqual(data["url"], "/#printer=p1")

    def test_a_print_is_pushed_once_when_it_closes_not_on_every_status(self):
        self.plate_print(4)
        self.plates.push(0, "plate4.gcode", 50, 50)
        self.plates.push(9, "plate4.gcode", 50, 50)
        self.assertEqual(len(self.sent), 1)

    # quick prints: no visible project to name or open

    def make_quick(self):
        """Turn the fixture project into a Quick print's hidden project, named for its model."""
        with outcome_prompt.database.transaction() as conn:
            conn.execute("UPDATE projects SET quick = 1, name = 'cube' WHERE id = ?", (self.plates.project_id,))

    def test_a_quick_print_pushes_the_model_and_printer_and_opens_the_printer_not_the_hidden_project(self):
        self.make_quick()
        self.plate_print(1)
        title, body, data = self.sent[0]
        self.assertEqual(title, "Printer 1 finished cube")
        self.assertNotIn("Plate", title)
        self.assertEqual(data["url"], "/#printer=p1")
        self.assertEqual(data["tag"], f"print-{data['print_id']}")

    def test_a_quick_print_awaits_with_the_model_name_and_no_plate_name(self):
        self.make_quick()
        self.plate_print(1)
        (awaiting,) = self.prompts()
        self.assertEqual((awaiting["project_name"], awaiting["plate_name"]), ("cube", None))
        self.assertIs(awaiting["quick"], True)
        self.assertEqual(awaiting["url"], "/#printer=p1")

    def test_a_normal_print_still_carries_its_project_and_plate(self):
        self.plate_print(4)
        (awaiting,) = self.prompts()
        self.assertEqual((awaiting["project_id"], awaiting["plate_name"], awaiting["quick"]), (self.plates.project_id, "Plate 4", False))

    def test_the_queue_marks_a_quick_job_and_the_dashboard_drops_its_project_id(self):
        self.make_quick()
        job = self.plates.plate_job(1)
        listed = {j["id"]: j for j in self.call("GET", "/api/jobs")[1]["jobs"]}
        self.assertIs(listed[job["id"]]["project_quick"], True)
        self.plate_print(4)  # a closed print, so the printer shows under Needs you with the queued job next
        (waiting,) = self.call("GET", "/api/dashboard")[1]["needs_you"]["printers"]
        self.assertEqual(waiting["next_job"]["id"], job["id"])
        self.assertIsNone(waiting["next_job"]["project_id"])
        self.assertIsNone(waiting["next_job"]["plate_id"])

    def test_a_normal_job_keeps_its_project_in_the_queue_and_dashboard(self):
        job = self.plates.plate_job(2)
        listed = {j["id"]: j for j in self.call("GET", "/api/jobs")[1]["jobs"]}
        self.assertEqual((listed[job["id"]]["project_id"], listed[job["id"]]["project_quick"]), (self.plates.project_id, False))
        self.plate_print(4)
        (waiting,) = self.call("GET", "/api/dashboard")[1]["needs_you"]["printers"]
        self.assertEqual(waiting["next_job"]["project_id"], self.plates.project_id)

    # the set of Prints awaiting an outcome

    def test_a_closed_print_awaits_until_the_user_answers(self):
        self.plate_print(4)
        (awaiting,) = self.prompts()
        self.assertEqual((awaiting["printer_id"], awaiting["project_name"], awaiting["plate_name"], awaiting["reported_outcome"]),
                         ("p1", "SEAT", "Plate 4", "completed"))
        self.assertTrue(awaiting["outcome_unconfirmed"])
        self.call("POST", "/api/printers/p1/bed-cleared", {"print_id": awaiting["print_id"], "outcome": "completed"})
        self.assertEqual(self.prompts(), [])

    def test_a_confirmed_outcome_still_awaits_bed_cleared_while_a_job_is_queued(self):
        self.plate_print(4)
        print_id = self.prompts()[0]["print_id"]
        self.assertEqual(self.call("POST", f"/api/prints/{print_id}/outcome", {"outcome": "completed"})[0], 200)
        self.assertEqual(self.prompts(), [])  # nothing queued, so nothing waits on the bed
        self.plates.plate_job(6)
        (awaiting,) = self.prompts()
        self.assertFalse(awaiting["outcome_unconfirmed"])
        self.assertTrue(awaiting["bed_cleared_needed"])

    def test_old_prints_that_never_had_a_prompt_do_not_await(self):
        self.plates.push(13, "old.gcode", 1, 50)
        self.plates.push(9, "old.gcode", 50, 50)
        with outcome_prompt.database.transaction() as conn:
            conn.execute("UPDATE prints SET outcome_unconfirmed = 0")
        self.plates.plate_job(2)
        self.assertEqual(len(self.prompts()), 1)  # queued job and no Bed cleared: waits
        self.printer.connected = True
        self.call("POST", "/api/printers/p1/auto-start", {"enabled": False})
        self.call("POST", "/api/printers/p1/bed-cleared", {})
        self.assertEqual(self.prompts(), [])

    # reminders

    def test_reminders_repeat_after_a_delay_while_the_printer_awaits(self):
        self.plate_print(4)
        ended = self.prompts()[0]["ended_at"]
        delay = outcome_prompt.REMINDER_DELAY_S
        self.assertEqual(outcome_prompt.send_due_reminders(now=ended + delay - 1), 0)
        self.assertEqual(outcome_prompt.send_due_reminders(now=ended + delay + 1), 1)
        title, body, data = self.sent[-1]
        self.assertTrue(title.startswith("Reminder: Printer 1"))
        self.assertEqual(data["url"], self.sent[0][2]["url"])
        self.assertEqual(outcome_prompt.send_due_reminders(now=ended + delay + 2), 0)
        self.assertEqual(outcome_prompt.send_due_reminders(now=ended + 2 * delay + 5), 1)
        self.assertEqual(len(self.sent), 3)

    def test_reminders_stop_once_the_user_answers(self):
        self.plate_print(4)
        ended = self.prompts()[0]["ended_at"]
        self.call("POST", "/api/printers/p1/bed-cleared", {"outcome": "completed"})
        self.assertEqual(outcome_prompt.send_due_reminders(now=ended + 10 * outcome_prompt.REMINDER_DELAY_S), 0)
        self.assertEqual(len(self.sent), 1)

    def test_a_reminder_never_assumes_the_bed_is_clear(self):
        self.plate_print(4)
        ended = self.prompts()[0]["ended_at"]
        outcome_prompt.send_due_reminders(now=ended + 99 * outcome_prompt.REMINDER_DELAY_S)
        self.assertFalse(self.call("GET", "/api/printers/p1/bed-cleared")[1]["valid"])

    # the Failed... sheet: one submit does it all

    def test_failed_sheet_logs_the_failure_frees_the_bed_and_requeues_the_plate(self):
        self.plate_print(4)
        print_id = self.prompts()[0]["print_id"]
        self.call("POST", "/api/printers/p1/auto-start", {"enabled": False})
        status, result = self.call("POST", "/api/printers/p1/bed-cleared", {
            "print_id": print_id, "outcome": "failed", "failure_reasons": ["spaghetti", "adhesion"],
            "notes": "corner lifted", "reprint": True})
        self.assertEqual(status, 200)
        self.assertEqual((result["print"]["outcome"], result["print"]["failure_reasons"]), ("failed", ["spaghetti", "adhesion"]))
        self.assertTrue(result["valid"])
        self.assertEqual((result["reprint"]["state"], result["reprint_error"]), ("queued", None))
        self.assertEqual(self.plates.states()[4], "queued")

    def test_failed_sheet_needs_a_reason_and_changes_nothing_without_one(self):
        self.plate_print(4)
        print_id = self.prompts()[0]["print_id"]
        status, error = self.call("POST", "/api/printers/p1/bed-cleared", {"print_id": print_id, "outcome": "failed", "reprint": True})
        self.assertEqual(status, 400)
        self.assertFalse(self.call("GET", "/api/printers/p1/bed-cleared")[1]["valid"])
        self.assertEqual(len(self.prompts()), 1)

    def test_failed_sheet_reprint_reports_a_missing_gcode_but_still_records_the_failure(self):
        self.plate_print(4)
        print_id = self.prompts()[0]["print_id"]
        self.call("POST", "/api/printers/p1/auto-start", {"enabled": False})
        for stored in (self.plates.dir / "jobs").rglob("*.gcode"):
            stored.unlink()
        status, result = self.call("POST", "/api/printers/p1/bed-cleared", {
            "print_id": print_id, "outcome": "failed", "failure_reasons": ["clog"], "reprint": True})
        self.assertEqual(status, 200)
        self.assertEqual(result["print"]["outcome"], "failed")
        self.assertIsNone(result["reprint"])
        self.assertIn("G-code", result["reprint_error"])

    def test_reprint_flag_on_a_print_with_no_plate_is_reported_not_fatal(self):
        self.plates.push(13, "loose.gcode", 1, 50)
        self.plates.push(9, "loose.gcode", 50, 50)
        status, result = self.call("POST", "/api/printers/p1/bed-cleared", {
            "outcome": "failed", "failure_reasons": ["other"], "reprint": True})
        self.assertEqual((status, result["reprint"]), (200, None))
        self.assertIn("plate", result["reprint_error"])

    # end-of-print snapshot

    def test_end_snapshot_is_stored_on_the_print_and_shown_in_the_prompt(self):
        self.printer.camera_url = "http://10.0.0.5:8080/?action=stream"
        with mock.patch.object(snapshots, "grab_frame", lambda url, timeout=8: FRAME if "10.0.0.5" in url else None):
            self.plate_print(4)
        (awaiting,) = self.prompts()
        self.assertEqual(awaiting["snapshot"], f"/api/prints/{awaiting['print_id']}/snapshot")
        self.assertEqual(self.call("GET", awaiting["snapshot"]), (200, FRAME))
        self.assertTrue(self.call("GET", f"/api/prints/{awaiting['print_id']}")[1]["end_snapshot"])

    def test_a_printer_without_a_camera_or_with_a_dead_one_just_has_no_snapshot(self):
        self.plate_print(4)  # no camera_url on the fake printer
        self.assertIsNone(self.prompts()[0]["snapshot"])
        self.assertEqual(self.call("GET", f"/api/prints/{self.prompts()[0]['print_id']}/snapshot")[0], 404)
        self.printer.camera_url = "http://10.0.0.5/stream"
        with mock.patch.object(snapshots, "grab_frame", mock.Mock(side_effect=OSError("down"))):
            self.plate_print(5)
        self.assertEqual(len(self.sent), 2)  # the push still went out

    def test_the_end_snapshot_is_cut_from_a_real_multipart_camera_stream(self):
        stream = HEAD + FRAME + HEAD + TAIL

        class Camera(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                self.wfile.write(stream)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Camera)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.no_camera.stop()
        self.printer.camera_url = f"http://127.0.0.1:{server.server_port}/?action=stream"
        self.plate_print(4)
        (awaiting,) = self.prompts()
        self.assertEqual(self.call("GET", awaiting["snapshot"]), (200, FRAME))


if __name__ == "__main__":
    unittest.main()
