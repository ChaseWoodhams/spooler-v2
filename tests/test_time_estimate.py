"""A plate's rough print time before slicing, the slicer's figure after, and the log of every slice's features,
seen through the backend HTTP API."""
import io
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import database
import jobs
import projects
import slicer
import splits
import state
from http_handler import SPHandler
from tests.test_jobs import FakePrinter
from tests.test_slicer import SLICED_GCODE, cube_stl, fake_runner, make_fake_orca

GEOMETRY = not splits.missing_modules()
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SLICER_SECONDS = 9 * 60 + 24   # "estimated printing time = 9m 24s" in the fake slicer's G-code


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class TimeEstimateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._saved_printers = state.printers
        database.configure(self.dir / "spooler.db")
        jobs.init(self.dir)
        slicer.configure(self.dir, orca_path=make_fake_orca(self.dir / "tools"))
        slicer.init()
        splits.configure(self.dir)
        projects.configure(self.dir)
        projects.init()
        state.printers = {"p1": FakePrinter("p1", "Printer 1")}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SPHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    async def asyncTearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def tearDown(self):
        state.printers = self._saved_printers
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        slicer.configure()
        splits.configure()
        projects.configure()
        self._tmp.cleanup()

    def get(self, path):
        with _opener.open(self.base + path, timeout=10) as resp:
            return json.loads(resp.read())

    async def settle(self):
        with mock.patch.object(slicer, "runner", fake_runner()):
            while await splits.process_next() or await projects.process_next():
                pass

    async def cube_project(self, size=30) -> int:
        """A project with one cube of `size` mm on plate 1."""
        data = cube_stl(size)
        project = projects.create_project_from_upload(io.BytesIO(data), len(data), "cube.stl")
        await self.settle()
        projects.place_whole_model(project["id"])
        await self.settle()
        return project["id"]

    def plate(self, project_id, plate_id=1) -> dict:
        return next(p for p in self.get(f"/api/projects/{project_id}/status")["plates"] if p["id"] == plate_id)

    async def test_an_unsliced_plate_reports_an_approximate_estimate_from_the_seed_coefficients(self):
        project_id = await self.cube_project(30)
        estimate = self.plate(project_id)["time_estimate"]
        self.assertEqual((estimate["source"], estimate["approximate"]), ("model", True))
        self.assertEqual(estimate["layers"], 150)                      # 30 mm tall at 0.2 mm layers
        self.assertTrue(0 < estimate["volume_cm3"] < 27)               # a 27 cm3 cube printed at 15% infill is not solid
        self.assertAlmostEqual(estimate["seconds"], 25 * 150 + 80 * estimate["volume_cm3"], delta=1)

    async def test_a_sliced_plate_reports_the_slicers_figure_until_the_slice_is_outdated(self):
        project_id = await self.cube_project(30)
        projects.request_slice(project_id, 1)
        await self.settle()
        estimate = self.plate(project_id)["time_estimate"]
        self.assertEqual((estimate["seconds"], estimate["approximate"], estimate["source"]), (SLICER_SECONDS, False, "slicer"))

        record = projects.get_project(project_id)
        taller = json.loads(json.dumps(record["data"]))
        taller["objects"][0]["scale"] = [1, 1, 2]
        projects.update_project(project_id, {"data": taller, "revision": record["revision"]})
        plate = self.plate(project_id)
        self.assertTrue(plate["outdated"])
        self.assertEqual((plate["time_estimate"]["source"], plate["time_estimate"]["approximate"], plate["time_estimate"]["layers"]),
                         ("model", True, 300))

    async def test_a_taller_plate_ranks_longer_by_layers_then_volume(self):
        short, tall = await self.cube_project(20), await self.cube_project(40)
        a, b = self.plate(short)["time_estimate"], self.plate(tall)["time_estimate"]
        self.assertEqual((a["layers"], b["layers"]), (100, 200))
        self.assertLess(a["seconds"], b["seconds"])

    # the slice log

    def log(self, query=""):
        return self.get("/api/slice-log" + query)["slices"]

    async def test_every_completed_slice_logs_its_features_with_the_slicers_estimate(self):
        project_id = await self.cube_project(30)
        before = self.plate(project_id)["time_estimate"]
        self.assertEqual(self.log(), [])
        projects.request_slice(project_id, 1)
        await self.settle()
        (row,) = self.log()
        self.assertEqual((row["project_id"], row["plate_id"], row["layers"]), (project_id, 1, 150))
        self.assertEqual((row["volume_cm3"], row["first_layer_cm2"]), (before["volume_cm3"], 9.0))   # one 30 x 30 mm footprint
        self.assertEqual((row["support"], row["brim"]), (False, False))
        self.assertEqual((row["estimate_s"], row["estimate_g"]), (SLICER_SECONDS, 3.6))
        self.assertEqual(row["model_s"], before["seconds"])      # what the rough estimate said, to measure it against
        self.assertAlmostEqual(row["deposited_cm3"], 1196.14 * 3.14159 * 0.875 ** 2 / 1000, places=1)   # 1.75 mm filament

        projects.request_slice(project_id, 1)                    # slicing again adds a row and leaves the first alone
        await self.settle()
        self.assertEqual([r["id"] for r in self.log()], [row["id"], row["id"] + 1])
        self.assertEqual(self.log(f"?project_id={project_id + 1}"), [])

    async def test_support_and_brim_use_is_logged_from_the_sliced_g_code(self):
        project_id = await self.cube_project(30)
        projects.request_slice(project_id, 1)

        def runner(args, timeout):
            out = Path(args[args.index("--outputdir") + 1])
            (out / "plate_1.gcode").write_text(SLICED_GCODE + ";TYPE:Brim\nG1 X20 Y10 E0.5 F1200\n;TYPE:Support\nG1 X30 Y10 E1\n")   # absolute E
            return 0, ""

        with mock.patch.object(slicer, "runner", runner):
            await projects.process_next()
        (row,) = self.log()
        self.assertEqual((row["support"], row["brim"]), (True, True))

    async def test_a_failed_slice_logs_nothing(self):
        project_id = await self.cube_project(30)
        projects.request_slice(project_id, 1)
        with mock.patch.object(slicer, "runner", fake_runner(code=1)):
            await projects.process_next()
        self.assertEqual(self.plate(project_id)["status"], "unsliced")
        self.assertEqual(self.log(), [])

    # the calibration columns and the per-Object support log

    SUPPORT_GCODE = (SLICED_GCODE + "M83\n;TYPE:Support\nEXCLUDE_OBJECT_START NAME=cube_id_0_copy_0\nG1 X20 Y10 E40\n"
                     ";TYPE:Support interface\nG1 X30 Y10 E10\nEXCLUDE_OBJECT_END NAME=cube_id_0_copy_0\n"
                     "; filament_diameter = 1.75\n; filament_density = 1.25\n")

    async def slice_with(self, project_id, gcode=SLICED_GCODE):
        projects.request_slice(project_id, 1)

        def runner(args, timeout):
            out = Path(args[args.index("--outputdir") + 1])
            (out / "plate_1.gcode").write_text(gcode)
            return 0, ""

        with mock.patch.object(slicer, "runner", runner):
            await projects.process_next()

    async def test_a_full_slice_logs_its_bundle_settings_hash_nozzle_kind_and_orca_version(self):
        project_id = await self.cube_project(30)
        bundle_id = projects.get_project(project_id)["data"]["profile"]["bundle_id"]
        await self.slice_with(project_id)
        (row,) = self.log()
        self.assertEqual((row["bundle_id"], row["kind"]), (bundle_id, "full"))
        self.assertRegex(row["settings_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(row["nozzle"], 0.4)
        self.assertIn("2.4.2", row["orca_version"])
        self.assertIsNone(row["support_volume_cm3"])        # filled in by the support proxy (issue 14)

    async def test_editing_a_bundles_settings_in_place_changes_the_settings_hash(self):
        project_id = await self.cube_project(30)
        bundle_id = projects.get_project(project_id)["data"]["profile"]["bundle_id"]
        await self.slice_with(project_id)
        (row,) = self.log()
        self.assertEqual(row["settings_hash"], slicer.settings_hash(bundle_id))
        process = slicer.profiles_dir() / bundle_id / "process.json"       # a slice would now refuse the bundle, so check the hash itself
        process.write_text(json.dumps({**json.loads(process.read_text()), "layer_height": "0.33"}))
        self.assertNotEqual(slicer.settings_hash(bundle_id), row["settings_hash"])

    async def test_support_grams_are_logged_for_the_plate_and_for_each_object(self):
        project_id = await self.cube_project(30)
        await self.slice_with(project_id, self.SUPPORT_GCODE)
        per_mm = 3.14159265 * 0.875 ** 2 * 1.25 / 1000
        data = self.get("/api/slice-log")
        (row,) = data["slices"]
        self.assertAlmostEqual(row["support_g"], 50 * per_mm, places=3)
        (obj,) = data["support_log"]
        self.assertEqual((obj["slice_log_id"], obj["object_name"], obj["bundle_hash"]), (row["id"], "cube_id_0_copy_0", row["settings_hash"]))
        self.assertAlmostEqual(obj["support_g"], 50 * per_mm, places=3)
        self.assertIsNone(obj["support_volume_cm3"])

    async def test_a_failed_slice_logs_no_support_rows_either(self):
        project_id = await self.cube_project(30)
        projects.request_slice(project_id, 1)
        with mock.patch.object(slicer, "runner", fake_runner(code=1)):
            await projects.process_next()
        self.assertEqual(self.get("/api/slice-log")["support_log"], [])


if __name__ == "__main__":
    unittest.main()
