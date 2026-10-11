"""The per-file intake table of a project: one row per imported model with its size, fit and unit verdict, and the
per-model unit fix (rescale). Seen through the project functions and the HTTP API."""
import asyncio
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


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class IntakeFilesTests(unittest.TestCase):
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
            with _opener.open(request, timeout=20) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def add(self, project, name, size, **settings):
        data = cube_stl(size)
        projects.import_model(project["id"], io.BytesIO(data), len(data), name, **settings)

    def settle(self):
        async def work():
            while await projects.process_next():
                pass
        asyncio.run(work())

    def files(self, project):
        status, body = self.call("GET", f"/api/projects/{project['id']}/intake-files")
        self.assertEqual(status, 200, body)
        return body

    def test_small_and_oversized_models_get_a_fits_or_split_row(self):
        project = projects.create_project("Mixed")
        self.add(project, "small.stl", 20)
        self.add(project, "huge.stl", 400)
        self.settle()
        body = self.files(project)
        small, huge = body["rows"]
        self.assertEqual((small["file"], small["state"], small["status"], small["fits"], small["size"]), ("small.stl", "ready", "fits", True, [20.0, 20.0, 20.0]))
        self.assertEqual((huge["file"], huge["status"], huge["fits"], huge["scale_ok"]), ("huge.stl", "split", False, True))
        self.assertEqual((small["model"], small["object"], small["copies"], small["scale"], small["up"]), (1, 1, 1, 1.0, "z"))
        self.assertEqual(body["summary"], {"models": 2, "objects": 2, "split": 1, "fits": 1, "check_units": 0, "analysing": 0, "failed": 0,
                                           "can_create": True, "steps": ["split", "plates", "slice", "queue"]})

    def test_a_model_that_fits_everywhere_skips_the_split_step(self):
        project = projects.create_project("Small")
        self.add(project, "small.stl", 20)
        self.settle()
        self.assertEqual(self.files(project)["summary"]["steps"], ["plates", "slice", "queue"])

    def test_a_doubtful_size_offers_its_scale_fix_and_blocks_create(self):
        project = projects.create_project("Units")
        self.add(project, "ok.stl", 20)
        self.add(project, "tiny.stl", 1, scale=0.2)   # 0.2 mm long: a model drawn in metres
        self.settle()
        body = self.files(project)
        tiny = body["rows"][1]
        self.assertEqual((tiny["status"], tiny["scale_ok"], tiny["scale"]), ("check_units", False, 0.2))
        self.assertEqual((tiny["scale_fix"]["factor"], tiny["scale_fix"]["scale"]), (1000.0, 200.0))
        self.assertEqual(tiny["size"], [0.2, 0.2, 0.2])
        self.assertEqual((body["summary"]["check_units"], body["summary"]["can_create"]), (1, False))

    def test_every_believable_unit_guess_is_offered_metres_first(self):
        limits = projects.machine_limits({})
        def fixes(raw):
            verdict = projects._verdict({"size": [0.0, 0.0, 0.0], "raw_size": raw}, 1.0, limits, (0.0, 0.0, 0.0))
            return [(f["unit"], f["factor"], f["size"]) for f in verdict["scale_fixes"]], verdict
        seat, verdict = fixes([0.974, 0.65, 0.617])   # a SEAT in metres: the single guess says inches, the list leads with metres
        self.assertEqual([u for u, _, _ in seat], ["metres", "inches", "decimetres"])
        self.assertEqual(seat[0][2], [974.0, 650.0, 617.0])
        self.assertEqual(seat[1][2], [24.7, 16.5, 15.7])
        self.assertEqual(verdict["fix"]["factor"], 25.4)   # scale_fix is unchanged
        self.assertEqual(fixes([0.2, 0.2, 0.2])[0][0][0], "metres")
        self.assertEqual([u for u, _, _ in fixes([0.45, 0.3, 0.1])[0]], ["metres", "inches", "decimetres"])
        self.assertEqual(fixes([20.0, 20.0, 20.0])[1]["scale_fixes"], [])   # a plausible model has nothing to fix
        self.assertEqual(verdict["scale_fixes"][0]["scale"], 1000.0)

    def test_rows_carry_the_unit_guesses(self):
        project = projects.create_project("Guesses")
        self.add(project, "tiny.stl", 1, scale=0.2)
        self.settle()
        fixes = self.files(project)["rows"][0]["scale_fixes"]
        self.assertEqual((fixes[0]["unit"], fixes[0]["factor"], fixes[0]["scale"], fixes[0]["size"]), ("metres", 1000.0, 200.0, [200.0, 200.0, 200.0]))

    def test_a_model_still_being_analysed_has_no_verdict(self):
        project = projects.create_project("Busy")
        self.add(project, "small.stl", 20)
        body = self.files(project)
        row = body["rows"][0]
        self.assertEqual((row["state"], row["status"], row["size"], row["scale_fix"]), ("importing", None, None, None))
        self.assertEqual((body["summary"]["analysing"], body["summary"]["can_create"]), (1, False))

    def test_a_failed_model_shows_its_error_and_blocks_create(self):
        project = projects.create_project("Broken")
        self.add(project, "bad.stl", 20)
        with mock.patch.object(projects, "_run_import", side_effect=jobs.JobError("The mesh has no solid volume")):
            self.settle()
        body = self.files(project)
        row = body["rows"][0]
        self.assertEqual((row["state"], row["status"], row["error"]), ("failed", None, "The mesh has no solid volume"))
        self.assertEqual((body["summary"]["failed"], body["summary"]["can_create"]), (1, False))

    def test_an_empty_project_cannot_be_created_from(self):
        project = projects.create_project("Nothing")
        body = self.files(project)
        self.assertEqual((body["rows"], body["summary"]["can_create"]), ([], False))

    def test_the_raw_size_is_kept_on_the_model(self):
        project = projects.create_project("Raw")
        self.add(project, "tiny.stl", 1, scale=0.2)
        self.settle()
        self.assertEqual(projects.get_project(project["id"])["models"][0]["raw_size"], [1.0, 1.0, 1.0])

    def test_rescaling_a_model_applies_the_fix_and_analyses_again(self):
        project = projects.create_project("Fix")
        self.add(project, "tiny.stl", 1, scale=0.2)
        self.settle()
        fix = self.files(project)["rows"][0]["scale_fix"]
        status, body = self.call("POST", f"/api/projects/{project['id']}/models/1/settings", {"scale": fix["scale"]})
        self.assertEqual(status, 200, body)
        model = body["models"][0]
        self.assertEqual((model["state"], model["import"], model.get("size")), ("importing", [200.0, "z"], None))
        folder = projects.model_dir(project["id"], 1)
        self.assertFalse((folder / "analysis.json").exists() or (folder / "model.npz").exists())
        self.assertEqual(json.loads((folder / "job.json").read_text())["scale"], 200.0)
        self.settle()
        row = self.files(project)["rows"][0]
        self.assertEqual((row["state"], row["scale_ok"], row["size"]), ("ready", True, [200.0, 200.0, 200.0]))

    def test_rescaling_changes_only_what_it_is_given(self):
        project = projects.create_project("Up")
        self.add(project, "cube.stl", 20, scale=2.0)
        self.settle()
        projects.rescale_model(project["id"], 1, None, "x")
        self.assertEqual(projects.get_project(project["id"])["models"][0]["import"], [2.0, "x"])

    def test_rescaling_is_refused_for_bad_values_unknown_models_and_laid_out_parts(self):
        project = projects.create_project("Refuse")
        self.add(project, "cube.stl", 20)
        self.settle()
        self.assertEqual(self.call("POST", f"/api/projects/{project['id']}/models/9/settings", {"scale": 2})[0], 404)
        self.assertEqual(self.call("POST", f"/api/projects/{project['id']}/models/1/settings", {"scale": -1})[0], 400)
        self.assertEqual(self.call("POST", f"/api/projects/{project['id']}/models/1/settings", {"up": "w"})[0], 400)
        projects._mutate(project["id"], "models", lambda models: models[0].update(split={"id": 1, "part": 1, "supports": False}))
        status, body = self.call("POST", f"/api/projects/{project['id']}/models/1/settings", {"scale": 2})
        self.assertEqual(status, 409, body)
        self.assertEqual(projects.get_project(project["id"])["models"][0]["state"], "ready")


if __name__ == "__main__":
    unittest.main()
