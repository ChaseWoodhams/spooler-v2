"""Copies of a model (the Objects made from it across all plates), seen through the project HTTP API."""
import asyncio
import copy
import io
import json
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
import splits
from tests.test_slicer import cube_stl, fake_runner, make_fake_orca

GEOMETRY = not splits.missing_modules()
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class CopiesApiTests(unittest.TestCase):
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
            with _opener.open(request, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def project_with_cubes(self, models=1):
        """A project with `models` 20 mm cube models, one object each, all on plate 1."""
        project = projects.create_project("Brackets")
        for _ in range(models):
            data = cube_stl(20)
            projects.import_model(project["id"], io.BytesIO(data), len(data), "cube.stl")

        async def work():
            while await projects.process_next():
                pass
        asyncio.run(work())
        return projects.get_project(project["id"])

    def save(self, project, change):
        data = copy.deepcopy(project["data"])
        change(data)
        status, saved = self.call("PATCH", f"/api/projects/{project['id']}", {"data": data, "revision": project["revision"]})
        self.assertEqual(status, 200, saved)
        return saved

    def layout(self, project, placements):
        """Replace the objects with copies of the first one: placements is [(plate, pinned)] in list order, ids from 1."""
        def change(data):
            template = data["objects"][0]
            data["objects"] = [{**template, "id": i, "plate": plate, "name": f"Cube {i}", **({"pinned": True} if pinned else {})}
                               for i, (plate, pinned) in enumerate(placements, start=1)]
            for plate in range(len(data["plates"]) + 1, max(p for p, _ in placements) + 1):
                data["plates"].append({"id": plate, "name": f"Plate {plate}"})
        return self.save(project, change)

    def set_copies(self, project, object_id, copies, **extra):
        return self.call("POST", f"/api/projects/{project['id']}/copies", {"object": object_id, "copies": copies, **extra})

    def ids(self, record):
        return [o["id"] for o in record["data"]["objects"]]

    def test_raising_copies_adds_objects_of_the_same_model_with_the_same_settings(self):
        project = self.project_with_cubes()
        project = self.save(project, lambda d: d["objects"][0].update(scale=[1.5, 1.5, 1.5], rotation=[0, 0, 90], name="Bracket"))
        status, result = self.set_copies(project, 1, 3)
        self.assertEqual(status, 200, result)
        objects = result["data"]["objects"]
        self.assertEqual(len(objects), 3)
        self.assertEqual(result["copies"], {"model": 1, "count": 3, "asked": 3, "pinned": 0})
        self.assertEqual(len({o["id"] for o in objects}), 3)
        for added in objects[1:]:
            self.assertEqual((added["model"], added["name"], added["scale"], added["rotation"], added["overrides"], added.get("pinned", False)),
                             (1, "Bracket", [1.5, 1.5, 1.5], [0.0, 0.0, 90.0], {}, False))
        self.assertEqual(objects[0], project["data"]["objects"][0])            # the selected object is untouched
        self.assertEqual(self.call("GET", f"/api/projects/{project['id']}")[1]["data"], result["data"])
        # each 30 mm cube clears the others, on the same plate
        self.assertEqual({o["plate"] for o in objects}, {1})
        for i, a in enumerate(objects):
            for b in objects[i + 1:]:
                self.assertGreaterEqual(max(abs(a["position"][0] - b["position"][0]), abs(a["position"][1] - b["position"][1])), 31)

    def test_added_objects_spill_onto_a_new_plate_when_the_plate_is_full(self):
        project = self.project_with_cubes()
        project = self.save(project, lambda d: d["objects"][0].update(scale=[10, 10, 10], position=[128, 128]))   # a 200 mm cube
        status, result = self.set_copies(project, 1, 2)
        self.assertEqual(status, 200, result)
        self.assertEqual([o["plate"] for o in result["data"]["objects"]], [1, 2])
        self.assertEqual([p["id"] for p in result["data"]["plates"]], [1, 2])

    def test_adding_objects_makes_that_plates_slice_outdated(self):
        project = self.project_with_cubes()
        projects.request_slice(project["id"], 1)

        async def work():
            with mock.patch.object(slicer, "runner", fake_runner()):
                while await projects.process_next():
                    pass
        asyncio.run(work())
        self.assertFalse(self.call("GET", f"/api/projects/{project['id']}")[1]["slices"]["1"]["outdated"])
        status, result = self.set_copies(project, 1, 2)
        self.assertEqual(status, 200, result)
        self.assertTrue(result["slices"]["1"]["outdated"])

    def test_lowering_copies_removes_unpinned_objects_last_plate_first(self):
        project = self.project_with_cubes(models=2)
        project = self.layout(project, [(1, False), (1, False), (2, False), (2, False), (3, False)])
        other = {"id": 9, "model": 2, "plate": 3, "name": "Other", "position": [100, 100], "rotation": [0, 0, 0], "scale": [1, 1, 1], "overrides": {}}
        project = self.save(project, lambda d: d["objects"].append(other))
        status, result = self.set_copies(project, 1, 3)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.ids(result), [1, 2, 3, 9])       # plate 3's object went first, then the last on plate 2
        self.assertEqual(result["copies"], {"model": 1, "count": 3, "asked": 3, "pinned": 0})
        status, result = self.set_copies(result, 1, 1)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.ids(result), [1, 9])             # the other model's object is never touched

    def test_lowering_copies_never_removes_a_pinned_object_and_says_why(self):
        project = self.project_with_cubes()
        project = self.layout(project, [(1, False), (1, True), (2, True), (2, False), (3, False)])
        status, result = self.set_copies(project, 1, 1)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.ids(result), [1, 2, 3])
        self.assertEqual(result["copies"], {"model": 1, "count": 3, "asked": 1, "pinned": 2})
        self.assertTrue(all(o.get("pinned") for o in result["data"]["objects"][1:]))

    def test_a_request_for_the_current_number_changes_nothing(self):
        project = self.project_with_cubes()
        project = self.layout(project, [(1, False), (1, False)])
        status, result = self.set_copies(project, 1, 2)
        self.assertEqual(status, 200, result)
        self.assertEqual((result["data"], result["revision"]), (project["data"], project["revision"]))
        self.assertEqual(result["copies"], {"model": 1, "count": 2, "asked": 2, "pinned": 0})

    def test_bad_requests_are_refused(self):
        project = self.project_with_cubes()
        for bad in (0, -1, "three", 1.5, None, True, 10000):
            status, error = self.set_copies(project, 1, bad)
            self.assertEqual(status, 400, bad)
            self.assertIn("copies", error["error"].lower())
        self.assertEqual(self.set_copies(project, 99, 2)[0], 404)
        status, error = self.set_copies(project, 1, 2, revision=project["revision"] + 5)
        self.assertEqual(status, 409, error)
        self.assertEqual(self.ids(self.call("GET", f"/api/projects/{project['id']}")[1]), [1])


if __name__ == "__main__":
    unittest.main()
