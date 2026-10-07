"""The Print step's plate order (Next up by best available time) and queueing, through the project HTTP API."""
import asyncio
import copy
import io
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
import projects
import slicer
import splits
from tests.test_slicer import cube_stl, fake_runner, make_fake_orca

GEOMETRY = not splits.missing_modules()
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
GREY = {"preset": "Elegoo PETG @ECC", "colour": "grey"}


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class PrintStepOrderTests(unittest.TestCase):
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

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with _opener.open(request, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def settle(self):
        async def work():
            with mock.patch.object(slicer, "runner", fake_runner()):
                while await projects.process_next():
                    pass
        asyncio.run(work())

    def project_with(self, *stls):
        project = projects.create_project("Plan")
        for n, data in enumerate(stls, start=1):
            projects.import_model(project["id"], io.BytesIO(data), len(data), f"part{n}.stl")
        self.settle()
        return projects.get_project(project["id"])

    def save(self, project, change):
        data = copy.deepcopy(project["data"])
        change(data)
        status, saved = self.call("PATCH", f"/api/projects/{project['id']}", {"data": data, "revision": project["revision"]})
        self.assertEqual(status, 200, saved)
        return saved

    def lay(self, project, placements, filaments=None):
        """Objects as [(model, plate, (x, y), pinned)] from id 1; plates are made as needed."""
        def change(data):
            templates = {o["model"]: o for o in data["objects"]}
            data["objects"] = [{**templates[model], "id": i, "plate": plate, "position": list(at), "name": f"Object {i}",
                                **({"pinned": True} if pinned else {})}
                               for i, (model, plate, at, pinned) in enumerate(placements, start=1)]
            for plate in range(len(data["plates"]) + 1, max(p for _, p, _, _ in placements) + 1):
                data["plates"].append({"id": plate, "name": f"Plate {plate}"})
            if filaments:
                data["filaments"] = filaments
        return self.save(project, change)

    def plan(self, project):
        return self.call("POST", f"/api/projects/{project['id']}/plan", {})

    def accept(self, project, **body):
        return self.call("POST", f"/api/projects/{project['id']}/plan/accept", {"revision": project["revision"], **body})

    def two_plates(self):
        """Plate 1 holds a small cube, plate 2 a big one: the big one is longer by the model estimate."""
        project = self.project_with(cube_stl(10), cube_stl(40))
        return self.lay(project, [(1, 1, (50, 50), False), (2, 2, (50, 50), False)])

    def edit_slices(self, project, change):
        with database.transaction() as conn:
            slices = json.loads(conn.execute("SELECT slices FROM projects WHERE id = ?", (project["id"],)).fetchone()["slices"])
            change(slices)
            conn.execute("UPDATE projects SET slices = ? WHERE id = ?", (json.dumps(slices), project["id"]))

    def slice_all(self, project, seconds=None):
        """Slice every plate; `seconds` ({plate id: estimate_s}) replaces the fake slicer's figure for those plates."""
        projects.request_slices(project["id"])
        self.settle()
        self.edit_slices(project, lambda slices: [slices[str(p)].update(estimate_s=s) for p, s in (seconds or {}).items()])

    def status(self, project):
        status, body = self.call("GET", f"/api/projects/{project['id']}/status")
        self.assertEqual(status, 200, body)
        return body

    def test_next_up_orders_unsliced_plates_by_the_model_estimate_and_keeps_plate_names(self):
        body = self.status(self.two_plates())
        self.assertEqual(body["print_order"], [2, 1])
        self.assertEqual({p["id"]: p["name"] for p in body["plates"]}, {1: "Plate 1", 2: "Plate 2"})

    def test_next_up_follows_the_slicers_figures_once_every_plate_is_sliced(self):
        project = self.two_plates()
        self.slice_all(project, {1: 7200, 2: 600})
        body = self.status(project)
        self.assertEqual(body["print_order"], [1, 2])
        self.assertEqual({p["id"]: p["name"] for p in body["plates"]}, {1: "Plate 1", 2: "Plate 2"})

    def test_mixed_sources_are_ranked_by_layers_and_volume_not_by_comparing_seconds(self):
        project = self.two_plates()
        self.slice_all(project, {1: 100000})
        self.edit_slices(project, lambda slices: slices["2"].update(state="unsliced"))   # only plate 2's model estimate remains
        body = self.status(project)
        self.assertEqual([p["time_estimate"]["source"] for p in body["plates"]], ["slicer", "model"])
        self.assertEqual(body["print_order"], [2, 1])

    def test_queue_all_follows_a_manual_reorder(self):
        project = self.two_plates()
        self.slice_all(project)
        status, sent = self.call("POST", f"/api/projects/{project['id']}/queue", {})
        self.assertEqual(status, 200, sent)
        ids = [j["id"] for j in sent["jobs"]]
        self.assertEqual(self.call("POST", "/api/jobs/reorder", {"order": list(reversed(ids))})[0], 200)
        queued = [j["plate_id"] for j in self.call("GET", "/api/jobs")[1]["jobs"]]
        self.assertEqual(queued, [j["plate_id"] for j in reversed(sent["jobs"])])


if __name__ == "__main__":
    unittest.main()
