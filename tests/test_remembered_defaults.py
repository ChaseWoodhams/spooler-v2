import json
import shutil
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
import projects
import slicer
import state
from tests.test_jobs import FakePrinter
from tests.test_slicer import make_fake_orca

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class RememberedDefaultsApiTests(unittest.TestCase):
    def setUp(self):
        from http_handler import SPHandler
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        database.configure(self.dir / "spooler.db")
        jobs.init(self.dir)
        slicer.configure(self.dir, orca_path=make_fake_orca(self.dir / "tools"))
        slicer.init()
        projects.configure(self.dir)
        projects.init()
        self._saved_printers = state.printers
        state.printers = {"p1": FakePrinter(), "p2": FakePrinter()}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SPHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        state.printers = self._saved_printers
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        slicer.configure()
        projects.configure()
        self._tmp.cleanup()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with _opener.open(request, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def custom_bundle(self):
        return slicer.derive_bundle(slicer.list_bundles()[0]["id"], {"process": {"wall_loops": "6"}})

    def test_nothing_remembered_yet(self):
        status, defaults = self.call("GET", "/api/defaults")
        self.assertEqual(status, 200)
        self.assertEqual(defaults["defaults"], {"printer": None, "profile": None, "spool": None, "plate_side": None, "options": {}, "pack": None})

    def test_saved_defaults_are_served_back_and_merge(self):
        bundle = self.custom_bundle()
        self.assertEqual(self.call("PATCH", "/api/defaults", {"printer": "p2", "profile": bundle["id"]})[0], 200)
        status, saved = self.call("PATCH", "/api/defaults", {"spool": 7, "plate_side": "smooth", "options": {"leveling": True, "timelapse": False}})
        self.assertEqual(status, 200)
        expected = {"printer": "p2", "profile": bundle["id"], "spool": 7, "plate_side": "smooth",
                    "options": {"leveling": True, "timelapse": False}, "pack": None}
        self.assertEqual(saved["defaults"], expected)
        self.assertEqual(self.call("GET", "/api/defaults")[1]["defaults"], expected)

    def test_clearing_a_default(self):
        self.call("PATCH", "/api/defaults", {"spool": 7, "printer": "p1"})
        _, saved = self.call("PATCH", "/api/defaults", {"spool": None})
        self.assertEqual((saved["defaults"]["spool"], saved["defaults"]["printer"]), (None, "p1"))

    def test_invalid_values_are_refused_and_nothing_is_saved(self):
        for body in ({"plate_side": "glossy"}, {"spool": "abc"}, {"spool": 0}, {"options": "fast"}, {"printer": "nope"},
                     {"profile": "missing"}, {"colour": "red"}):
            with self.subTest(body=body):
                self.assertEqual(self.call("PATCH", "/api/defaults", body)[0], 400)
        self.assertEqual(self.call("GET", "/api/defaults")[1]["defaults"]["plate_side"], None)

    def test_a_new_project_starts_from_the_defaults(self):
        bundle = self.custom_bundle()
        self.call("PATCH", "/api/defaults", {"printer": "p2", "profile": bundle["id"], "spool": 7, "plate_side": "smooth"})
        status, project = self.call("POST", "/api/projects", {"name": "Fresh"})
        self.assertEqual(status, 200)
        data = project["data"]
        self.assertEqual((data["profile"]["bundle_id"], data["profile"]["spool_id"], data["bed"]), (bundle["id"], 7, "smooth"))
        self.assertEqual(data["plates"][0]["printer_id"], "p2")

    def test_without_defaults_a_new_project_takes_the_first_plain_profile(self):
        _, project = self.call("POST", "/api/projects", {"name": "Plain"})
        data = project["data"]
        self.assertEqual(data["profile"]["bundle_id"], slicer.list_bundles()[0]["id"])
        self.assertEqual((data["bed"], data["profile"].get("spool_id")), ("textured", None))
        self.assertIsNone(data["plates"][0].get("printer_id"))

    def test_a_forgotten_printer_or_profile_falls_back(self):
        bundle = self.custom_bundle()
        self.call("PATCH", "/api/defaults", {"printer": "p2", "profile": bundle["id"]})
        state.printers = {"p1": FakePrinter()}
        shutil.rmtree(slicer.profiles_dir() / bundle["id"])
        _, project = self.call("POST", "/api/projects", {"name": "Fallback"})
        self.assertEqual(project["data"]["profile"]["bundle_id"], slicer.list_bundles()[0]["id"])
        self.assertIsNone(project["data"]["plates"][0].get("printer_id"))

    def test_defaults_survive_a_restart(self):
        self.call("PATCH", "/api/defaults", {"plate_side": "smooth"})
        database.configure(self.dir / "spooler.db")  # same file, as after a restart
        self.assertEqual(self.call("GET", "/api/defaults")[1]["defaults"]["plate_side"], "smooth")


if __name__ == "__main__":
    unittest.main()
