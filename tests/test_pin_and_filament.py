"""Pinned objects and per-model filament choice, seen through the project HTTP API."""
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
from tests.test_slicer import cube_stl, make_fake_orca

GEOMETRY = not splits.missing_modules()
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
GREY = {"preset": "Elegoo PETG @ECC", "colour": "grey"}


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class PinAndFilamentApiTests(unittest.TestCase):
    def setUp(self):
        from http_handler import SPHandler
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        database.configure(self.dir / "spooler.db")
        jobs.init(self.dir)
        slicer.configure(self.dir, orca_path=make_fake_orca(self.dir / "tools"))
        slicer.init()
        slicer.save_user_preset("filament", "Elegoo PETG @ECC", "Elegoo PLA @ECC", {"filament_type": ["PETG"]})
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

    def project_with_cubes(self, count=2):
        """A project with `count` cube models, one object each, all on plate 1."""
        project = projects.create_project("Brackets")
        for _ in range(count):
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
        return self.call("PATCH", f"/api/projects/{project['id']}", {"data": data, "revision": project["revision"]})

    def fetch(self, project):
        return self.call("GET", f"/api/projects/{project['id']}")[1]

    def test_an_object_can_be_pinned_and_unpinned(self):
        project = self.project_with_cubes()
        status, saved = self.save(project, lambda d: d["objects"][0].update(pinned=True))
        self.assertEqual(status, 200, saved)
        self.assertEqual([o.get("pinned", False) for o in saved["data"]["objects"]], [True, False])
        self.assertEqual([o.get("pinned", False) for o in self.fetch(project)["data"]["objects"]], [True, False])
        status, saved = self.save(saved, lambda d: d["objects"][0].update(pinned=False))
        self.assertEqual(status, 200, saved)
        self.assertEqual([o.get("pinned", False) for o in self.fetch(project)["data"]["objects"]], [False, False])

    def test_pinned_must_be_true_or_false(self):
        project = self.project_with_cubes(1)
        for bad in ("yes", 1, [], {}):
            status, error = self.save(project, lambda d: d["objects"][0].update(pinned=bad))
            self.assertEqual(status, 400, bad)
            self.assertIn("pinned", error["error"].lower())
        self.assertFalse(self.fetch(project)["data"]["objects"][0].get("pinned", False))

    def test_a_models_filament_choice_can_be_set_and_cleared(self):
        project = self.project_with_cubes()
        status, saved = self.save(project, lambda d: d.update(filaments={"1": GREY, "2": {"preset": "Elegoo PLA @ECC"}}))
        self.assertEqual(status, 200, saved)
        self.assertEqual({key: {k: c[k] for k in ("preset", "colour")} for key, c in self.fetch(project)["data"]["filaments"].items()},
                         {"1": GREY, "2": {"preset": "Elegoo PLA @ECC", "colour": ""}})
        status, saved = self.save(saved, lambda d: d["filaments"].update({"1": None}))   # null clears a model's choice
        self.assertEqual(status, 200, saved)
        self.assertEqual(list(self.fetch(project)["data"]["filaments"]), ["2"])
        status, saved = self.save(saved, lambda d: d.pop("filaments"))                  # absent clears them all
        self.assertEqual(status, 200, saved)
        self.assertFalse(self.fetch(project)["data"].get("filaments"))

    def test_filament_choices_are_validated(self):
        project = self.project_with_cubes(1)
        bad_choices = [{"preset": ""}, {"colour": "grey"}, {"preset": 5}, {"preset": "x" * 200},
                       {"preset": "PLA", "colour": ["grey"]}, {"preset": "PLA", "colour": "g" * 100}, "PLA", ["PLA"]]
        for bad in bad_choices:
            status, error = self.save(project, lambda d: d.update(filaments={"1": bad}))
            self.assertEqual(status, 400, bad)
            self.assertIn("filament", error["error"].lower())
        for bad in ({"9": GREY}, {"x": GREY}, [GREY]):
            self.assertEqual(self.save(project, lambda d: d.update(filaments=bad))[0], 400, bad)
        self.assertFalse(self.fetch(project)["data"].get("filaments"))

    def test_a_project_saved_before_pin_and_filament_loads_and_saves_unchanged(self):
        project = self.project_with_cubes()
        before = self.fetch(project)["data"]
        self.assertNotIn("filaments", before)
        self.assertTrue(all("pinned" not in o for o in before["objects"]))
        status, resaved = self.save(project, lambda d: None)
        self.assertEqual(status, 200, resaved)
        self.assertNotIn("filaments", resaved["data"])
        self.assertTrue(all("pinned" not in o for o in resaved["data"]["objects"]))
        self.assertEqual(self.save(resaved, lambda d: None)[1]["data"], resaved["data"])   # and saving again changes nothing

    def test_manual_moves_merges_and_deletes_still_work_on_pinned_objects(self):
        project = self.project_with_cubes()
        status, project = self.save(project, lambda d: (d["objects"][0].update(pinned=True),
                                                       d.update(filaments={"1": GREY, "2": GREY})))
        self.assertEqual(status, 200, project)
        status, moved = self.save(project, lambda d: d["objects"][0].update(position=[60, 70], rotation=[0, 0, 90]))
        self.assertEqual(status, 200, moved)
        pinned = moved["data"]["objects"][0]
        self.assertEqual((pinned["pinned"], pinned["position"], pinned["rotation"]), (True, [60.0, 70.0], [0.0, 0.0, 90.0]))
        status, split = self.save(moved, lambda d: (d["plates"].append({"id": 2, "name": "Plate 2"}), d["objects"][0].update(plate=2)))
        self.assertEqual(status, 200, split)
        status, merged = self.call("POST", f"/api/projects/{project['id']}/plates/2/merge", {"into": 1})
        self.assertEqual(status, 200, merged)
        self.assertEqual([p["id"] for p in merged["data"]["plates"]], [1])
        self.assertTrue(merged["data"]["objects"][0]["pinned"])
        status, after = self.call("DELETE", f"/api/projects/{project['id']}/models/1")
        self.assertEqual(status, 200, after)
        self.assertEqual([o["model"] for o in after["data"]["objects"]], [2])
        self.assertEqual(list(after["data"]["filaments"]), ["2"])    # the deleted model's filament choice goes with it


if __name__ == "__main__":
    unittest.main()
