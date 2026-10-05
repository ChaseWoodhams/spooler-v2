"""History for quick prints: the model name and thumbnail on each Print, one-click Reprint and Open as project,
tested through the backend HTTP API."""
import json
import unittest
import urllib.error
import urllib.request

import database
import jobs
from tests import test_plate_status

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class QuickHistoryTests(unittest.TestCase):
    def setUp(self):
        self.plates = test_plate_status.PlateStatusTests("test_print_without_a_project_is_unaffected_and_listed")
        self.plates.setUp()
        self.addCleanup(self.plates.tearDown)

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.plates.base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with _opener.open(request, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def make_quick(self):
        with database.transaction() as conn:
            conn.execute("UPDATE projects SET quick = 1, name = 'cube' WHERE id = ?", (self.plates.project_id,))

    def printed(self, plate_id=1, end=9):
        name = f"plate{plate_id}.gcode"
        job = self.plates.plate_job(plate_id)
        self.plates.run_job(job, name)
        self.plates.push(end, name, 50, 50)
        return job

    def entry(self):
        return self.call("GET", "/api/prints")[1]["prints"][0]

    def test_a_quick_print_carries_its_model_name_and_a_thumbnail_of_its_plate(self):
        self.make_quick()
        self.printed(1)
        p = self.entry()
        pid = self.plates.project_id
        self.assertEqual((p["quick"], p["project_name"]), (True, "cube"))
        self.assertEqual(p["thumbnail"], f"/api/projects/{pid}/plates/1/preview.png")

    def test_a_normal_print_is_not_quick_and_a_print_without_a_project_has_no_thumbnail(self):
        self.printed(4)
        self.assertEqual((self.entry()["quick"], self.entry()["project_name"]), (False, "SEAT"))
        self.plates.push(13, "loose.gcode", 1, 50)
        self.plates.push(9, "loose.gcode", 50, 50)
        p = self.entry()
        self.assertEqual((p["quick"], p["project_name"], p["thumbnail"], p["reprint_available"]), (False, None, None, False))

    def test_reprint_requeues_the_same_job_unchanged(self):
        self.make_quick()
        job = self.printed(1)
        p = self.entry()
        self.assertTrue(p["reprint_available"])
        status, queued = self.call("POST", f"/api/prints/{p['id']}/reprint", {})
        self.assertEqual(status, 200, queued)
        self.assertEqual((queued["id"], queued["state"]), (job["id"], "queued"))
        self.assertEqual(self.entry()["job_id"], job["id"])

    def test_a_job_still_in_the_queue_cannot_be_reprinted_again(self):
        self.make_quick()
        self.printed(1)
        pid = self.entry()["id"]
        self.assertEqual(self.call("POST", f"/api/prints/{pid}/reprint", {})[0], 200)
        self.assertFalse(self.entry()["reprint_available"])
        self.assertEqual(self.call("POST", f"/api/prints/{pid}/reprint", {})[0], 409)

    def test_with_the_hidden_project_gone_reprint_and_open_as_project_are_unavailable(self):
        self.make_quick()
        self.printed(1)
        with database.transaction() as conn:
            conn.execute("DELETE FROM projects WHERE id = ?", (self.plates.project_id,))
        p = self.entry()
        self.assertEqual((p["quick"], p["project_available"], p["reprint_available"], p["thumbnail"]), (True, False, False, None))
        status, error = self.call("POST", f"/api/prints/{p['id']}/reprint", {})
        self.assertEqual(status, 409, error)
        self.assertEqual(self.call("POST", f"/api/projects/{self.plates.project_id}/open-as-project", {})[0], 404)
        self.assertEqual(len(self.call("GET", "/api/prints")[1]["prints"]), 1)      # still in History

    def test_with_the_gcode_gone_reprint_is_unavailable(self):
        self.make_quick()
        self.printed(1)
        for stored in (self.plates.dir / "jobs").rglob("*.gcode"):
            stored.unlink()
        p = self.entry()
        self.assertFalse(p["reprint_available"])
        self.assertEqual(self.call("POST", f"/api/prints/{p['id']}/reprint", {})[0], 410)

    def test_open_as_project_leaves_the_print_in_history_as_a_normal_project_print(self):
        self.make_quick()
        self.printed(1)
        pid = self.plates.project_id
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/open-as-project", {})[0], 200)
        p = self.entry()
        self.assertEqual((p["quick"], p["project_id"], p["project_available"]), (False, pid, True))
