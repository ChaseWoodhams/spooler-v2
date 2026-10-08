"""Imports never double-run: the background workers claim an importing model atomically, and a crash releases the claim."""
import asyncio
import json
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import database
import jobs
import projects
import slicer
import splits
from tests.test_slicer import make_fake_orca

GEOMETRY = not splits.missing_modules()
FIXTURES = Path(__file__).parent / "fixtures" / "daz_shifter"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class ImportClaimTests(unittest.TestCase):
    def setUp(self):
        from http_handler import SPHandler
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        database.configure(self.dir / "spooler.db")
        jobs.init(self.dir)
        slicer.configure(self.dir, orca_path=make_fake_orca(self.dir / "tools"))
        slicer.init()
        splits.configure(self.dir)
        projects.configure(self.dir)
        projects.init()
        patcher = mock.patch("http_handler._auth_ok", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SPHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        slicer.configure()
        splits.configure()
        projects.configure()
        self._tmp.cleanup()

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        with _opener.open(request, timeout=30) as resp:
            return resp.status, json.loads(resp.read())

    def upload(self, project_id, files):
        for path in files:
            query = urllib.parse.urlencode({"filename": path.name})
            status, _ = self.call("POST", f"/api/projects/{project_id}/models/upload?{query}", raw=path.read_bytes())
            self.assertEqual(status, 200)

    def run_workers(self, slots=4):
        async def loop(slot):
            while await projects.process_next(slot):
                pass

        async def main():
            await asyncio.gather(*(loop(slot) for slot in range(slots)))
        asyncio.run(main())

    def new_project(self, files):
        project_id = self.call("POST", "/api/projects", {"name": "Daz shifter"})[1]["id"]
        self.upload(project_id, files)
        return project_id

    def states(self, project_id):
        return {m["file"]: (m["state"], m["error"]) for m in self.call("GET", f"/api/projects/{project_id}")[1]["models"]}

    def test_the_whole_daz_shifter_set_imports_and_every_model_is_ready(self):
        files = sorted(FIXTURES.glob("*.stl"))
        self.assertEqual(len(files), 19)
        project_id = self.new_project(files)
        self.run_workers(slots=4)
        states = self.states(project_id)
        self.assertEqual(len(states), 19)
        self.assertEqual({file: s for file, s in states.items() if s != ("ready", None)}, {})
        for model_id in range(1, 20):
            self.assertFalse((projects.model_dir(project_id, model_id) / "error.json").exists())

    def test_no_model_is_analysed_by_two_workers_at_once(self):
        project_id = self.new_project(sorted(FIXTURES.glob("*.stl"))[:6])
        real, lock = projects._run_import, threading.Lock()
        running, runs, overlaps = set(), [], []

        def watched(pid, model_id):
            with lock:
                runs.append(model_id)
                if model_id in running:
                    overlaps.append(model_id)
                running.add(model_id)
            try:
                time.sleep(0.2)  # long enough for another worker to ask for work meanwhile
                return real(pid, model_id)
            finally:
                with lock:
                    running.discard(model_id)

        with mock.patch.object(projects, "_run_import", watched):
            self.run_workers(slots=4)
        self.assertEqual(overlaps, [])
        self.assertEqual(sorted(runs), [1, 2, 3, 4, 5, 6])  # each exactly once
        self.assertTrue(all(state == ("ready", None) for state in self.states(project_id).values()))

    def test_a_model_claimed_when_spooler_stopped_is_imported_after_restart(self):
        files = sorted(FIXTURES.glob("*.stl"))[:1]
        project_id = self.new_project(files)
        self.assertEqual(projects._claim()[:3], ("import", project_id, 1))  # a worker takes it, then Spooler dies
        self.assertIsNone(projects._claim())  # while claimed, nobody else gets it
        projects.init()                       # restart
        self.assertEqual(self.states(project_id), {files[0].name: ("importing", None)})
        self.run_workers(slots=1)
        self.assertEqual(self.states(project_id), {files[0].name: ("ready", None)})


if __name__ == "__main__":
    unittest.main()
