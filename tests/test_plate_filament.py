"""A plate's filament: derived from its objects' models, part of the slice fingerprint, refused when mixed."""
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
GREY = {"preset": "My PETG", "colour": "grey"}
WHITE = {"preset": "Elegoo PLA @ECC", "colour": "white"}


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class PlateFilamentApiTests(unittest.TestCase):
    def setUp(self):
        from http_handler import SPHandler
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        database.configure(self.dir / "spooler.db")
        jobs.init(self.dir)
        slicer.configure(self.dir, orca_path=make_fake_orca(self.dir / "tools"))
        slicer.init()
        slicer.save_user_preset("filament", "My PETG", "Elegoo PLA @ECC", {"nozzle_temperature": ["245"]})
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

    def run_slices(self):
        async def work():
            while await projects.process_next():
                pass
        with mock.patch.object(slicer, "runner", fake_runner()):
            asyncio.run(work())

    def two_plates(self, filaments):
        """Two cubes on two plates, with the given filament choices by model id."""
        project = self.project_with_cubes()

        def change(d):
            d["plates"].append({"id": 2, "name": "Plate 2"})
            d["objects"][1]["plate"] = 2
            d["filaments"] = filaments
        status, saved = self.save(project, change)
        self.assertEqual(status, 200, saved)
        return saved

    def test_a_plate_mixing_filament_choices_says_so_and_refuses_to_slice(self):
        project = self.project_with_cubes()
        status, project = self.save(project, lambda d: d.update(filaments={"1": GREY, "2": WHITE}))
        self.assertEqual(status, 200, project)
        message = "Plate 1 mixes My PETG grey and Elegoo PLA white"
        plates = self.call("GET", f"/api/projects/{project['id']}/status")[1]["plates"]
        self.assertEqual(plates[0]["mixed"], message)
        status, error = self.call("POST", f"/api/projects/{project['id']}/plates/1/slice")
        self.assertEqual((status, error["error"]), (409, message))
        status, error = self.call("POST", f"/api/projects/{project['id']}/slice", {})
        self.assertEqual((status, error["error"]), (409, message))
        self.assertEqual(self.fetch(project)["slices"], {})

    def test_a_choice_and_the_project_default_also_mix(self):
        project = self.project_with_cubes()
        status, project = self.save(project, lambda d: d.update(filaments={"1": GREY}))
        self.assertEqual(status, 200, project)
        status, error = self.call("POST", f"/api/projects/{project['id']}/plates/1/slice")
        self.assertEqual((status, error["error"]), (409, "Plate 1 mixes My PETG grey and the project default"))

    def test_a_plate_of_one_choice_is_not_mixed_even_with_several_objects(self):
        project = self.project_with_cubes()
        status, project = self.save(project, lambda d: d.update(filaments={"1": GREY, "2": GREY}))
        self.assertEqual(status, 200, project)
        self.assertIsNone(self.call("GET", f"/api/projects/{project['id']}/status")[1]["plates"][0]["mixed"])
        self.assertEqual(self.call("POST", f"/api/projects/{project['id']}/plates/1/slice")[0], 200)

    def test_changing_a_filament_choice_makes_the_plates_slice_outdated(self):
        project = self.project_with_cubes(1)
        self.assertEqual(self.call("POST", f"/api/projects/{project['id']}/plates/1/slice")[0], 200)
        self.run_slices()
        project = self.fetch(project)
        self.assertEqual((project["slices"]["1"]["state"], project["slices"]["1"]["outdated"]), ("sliced", False))
        status, project = self.save(project, lambda d: None)     # saving normalises the plate records
        self.assertEqual(status, 200, project)
        plates_before = copy.deepcopy(project["data"]["plates"])
        status, changed = self.save(project, lambda d: d.update(filaments={"1": GREY}))
        self.assertEqual(status, 200, changed)
        self.assertTrue(changed["slices"]["1"]["outdated"])
        self.assertEqual(changed["data"]["plates"], plates_before)      # nothing is stored on the plate
        status, back = self.save(changed, lambda d: d.pop("filaments"))
        self.assertEqual(status, 200, back)
        self.assertFalse(back["slices"]["1"]["outdated"])               # back to what was sliced
        self.assertFalse(self.call("GET", f"/api/projects/{project['id']}/status")[1]["plates"][0]["outdated"])

    def test_a_sliced_plate_uses_the_filament_preset_of_its_objects(self):
        project = self.project_with_cubes()
        status, project = self.save(project, lambda d: d.update(filaments={"1": GREY, "2": GREY}))
        self.assertEqual(status, 200, project)
        self.assertEqual(self.call("POST", f"/api/projects/{project['id']}/plates/1/slice")[0], 200)
        self.run_slices()
        self.assertEqual(self.fetch(project)["slices"]["1"]["state"], "sliced")
        used = json.loads((projects.plate_dir(project["id"], 1) / "profile" / "filament.json").read_text())
        self.assertEqual((used["name"], used["nozzle_temperature"]), ("My PETG", ["245"]))

    def test_a_plate_on_the_project_default_still_uses_the_profiles_filament(self):
        project = self.project_with_cubes(1)
        self.assertEqual(self.call("POST", f"/api/projects/{project['id']}/plates/1/slice")[0], 200)
        self.run_slices()
        used = json.loads((projects.plate_dir(project["id"], 1) / "profile" / "filament.json").read_text())
        self.assertEqual(used["name"], "Elegoo PLA @ECC")

    def test_an_unknown_filament_preset_fails_the_slice_with_a_reason(self):
        project = self.project_with_cubes(1)
        status, project = self.save(project, lambda d: d.update(filaments={"1": {"preset": "No such PETG"}}))
        self.assertEqual(status, 200, project)
        self.assertEqual(self.call("POST", f"/api/projects/{project['id']}/plates/1/slice")[0], 200)
        self.run_slices()
        entry = self.fetch(project)["slices"]["1"]
        self.assertEqual(entry["state"], "failed")
        self.assertIn("No such PETG", entry["error"])

    def test_the_project_page_totals_filament_per_choice(self):
        project = self.two_plates({"1": GREY})
        self.assertEqual(self.call("POST", f"/api/projects/{project['id']}/slice", {})[0], 200)
        self.run_slices()
        progress = self.call("GET", f"/api/projects/{project['id']}/status")[1]["progress"]
        totals = progress["filament_g"]["by_filament"]
        self.assertEqual([(t["label"], t["default"], t["total"], t["plates"]) for t in totals],
                         [("the project default", True, 3.6, 1), ("My PETG grey", False, 3.6, 1)])
        self.assertEqual(progress["filament_g"]["total"], 7.2)

    def test_a_mixed_plate_is_left_out_of_the_per_choice_totals(self):
        project = self.two_plates({})
        self.assertEqual(self.call("POST", f"/api/projects/{project['id']}/slice", {})[0], 200)
        self.run_slices()
        status, project = self.save(self.fetch(project), lambda d: (d["objects"][1].update(plate=1), d.update(filaments={"2": GREY})))
        self.assertEqual(status, 200, project)
        totals = self.call("GET", f"/api/projects/{project['id']}/status")[1]["progress"]["filament_g"]["by_filament"]
        self.assertEqual(totals, [])


if __name__ == "__main__":
    unittest.main()
