"""Import and Quick print: whether an imported model fits the bed (so Split is skipped), the scale guard, and
Slice & print as one action. Everything is exercised through the HTTP API."""
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
import state
from tests.test_jobs import FakePrinter
from tests.test_project_workflow import box_stl
from tests.test_slicer import cube_stl, fake_runner, make_fake_orca

GEOMETRY = not splits.missing_modules()
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class QuickPrintApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from http_handler import SPHandler
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
        patcher = mock.patch("http_handler._auth_ok", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SPHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    async def asyncTearDown(self):
        for task in list(jobs._start_tasks.values()):
            task.cancel()
        await asyncio.sleep(0)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        state.printers = self._saved_printers
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        slicer.configure()
        splits.configure()
        projects.configure()
        self._tmp.cleanup()

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with _opener.open(request, timeout=20) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    async def settle(self):
        with mock.patch.object(slicer, "runner", fake_runner()):
            while await splits.process_next() or await projects.process_next():
                pass

    async def import_model(self, data: bytes, filename="model.stl", scale=None):
        query = f"filename={filename}" + (f"&scale={scale}" if scale else "")
        status, project = self.call("POST", f"/api/projects/upload?{query}", raw=data)
        self.assertEqual(status, 200, project)
        await self.settle()
        return project["id"]

    async def quick_upload(self, data: bytes, filename="model.stl"):
        status, project = self.call("POST", f"/api/projects/upload?filename={filename}&quick=1", raw=data)
        self.assertEqual(status, 200, project)
        await self.settle()
        return project

    async def test_a_quick_upload_makes_a_quick_project_with_its_intake_result(self):
        project = await self.quick_upload(cube_stl(30), "cube.stl")
        self.assertIs(project["quick"], True)
        self.assertEqual(project["name"], "cube")
        self.assertIsNotNone(project["split_id"])
        intake = self.call("GET", f"/api/projects/{project['id']}/intake")[1]
        self.assertEqual((intake["state"], intake["can_print"], intake["file"]), ("ready", True, "cube.stl"))
        normal = await self.import_model(cube_stl(20), "normal.stl")
        self.assertIs(self.call("GET", f"/api/projects/{normal}")[1]["quick"], False)

    async def test_a_quick_project_stays_out_of_the_project_list_but_resolves_by_id(self):
        quick = await self.quick_upload(cube_stl(30), "cube.stl")
        normal = await self.import_model(cube_stl(20), "normal.stl")
        listed = [p["id"] for p in self.call("GET", "/api/projects")[1]["projects"]]
        self.assertEqual(listed, [normal])
        everything = [p["id"] for p in self.call("GET", "/api/projects?include_quick=1")[1]["projects"]]
        self.assertEqual(sorted(everything), sorted([quick["id"], normal]))
        status, fetched = self.call("GET", f"/api/projects/{quick['id']}")
        self.assertEqual((status, fetched["id"]), (200, quick["id"]))

    async def test_a_quick_project_never_counts_as_an_active_project_on_the_dashboard(self):
        quick = await self.quick_upload(cube_stl(30), "cube.stl")
        self.assertEqual(self.call("POST", f"/api/projects/{quick['id']}/auto-place", {})[0], 200)
        await self.settle()
        self.assertEqual(self.call("POST", f"/api/projects/{quick['id']}/slice", {})[0], 200)
        await self.settle()
        self.assertEqual(projects.get_project(quick["id"])["slices"]["1"]["state"], "sliced")
        status, dash = self.call("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        self.assertEqual(dash["projects"], [])
        normal = await self.import_model(cube_stl(20), "normal.stl")
        self.call("POST", f"/api/projects/{normal}/auto-place", {})
        self.call("POST", f"/api/projects/{normal}/slice", {})
        await self.settle()
        self.assertEqual([p["id"] for p in self.call("GET", "/api/dashboard")[1]["projects"]], [normal])

    async def test_a_quick_projects_split_is_owned_not_loose(self):
        quick = await self.quick_upload(cube_stl(30), "cube.stl")
        status, loose = self.call("POST", "/api/splits/upload?filename=loose.stl", raw=cube_stl(25))
        self.assertEqual(status, 200, loose)
        by_id = {s["id"]: s for s in self.call("GET", "/api/splits")[1]["splits"]}
        self.assertEqual(by_id[quick["split_id"]]["project_id"], quick["id"])
        self.assertIsNone(by_id[loose["id"]]["project_id"])

    async def test_open_as_project_promotes_a_quick_project_with_its_model_plate_and_slice(self):
        quick = await self.quick_upload(cube_stl(30), "cube.stl")
        self.call("POST", f"/api/projects/{quick['id']}/auto-place", {})
        self.call("POST", f"/api/projects/{quick['id']}/slice", {})
        await self.settle()
        before = self.call("GET", f"/api/projects/{quick['id']}")[1]
        self.assertNotIn(quick["id"], [p["id"] for p in self.call("GET", "/api/projects")[1]["projects"]])
        status, opened = self.call("POST", f"/api/projects/{quick['id']}/open-as-project", {})
        self.assertEqual(status, 200, opened)
        self.assertEqual((opened["id"], opened["quick"]), (quick["id"], False))
        listed = self.call("GET", "/api/projects")[1]["projects"]
        self.assertEqual(listed[0]["id"], quick["id"])
        after = self.call("GET", f"/api/projects/{quick['id']}")[1]
        self.assertIs(after["quick"], False)
        self.assertEqual(after["models"], before["models"])
        self.assertEqual(after["data"]["plates"], before["data"]["plates"])
        self.assertEqual(after["slices"]["1"]["state"], "sliced")

    async def test_open_as_project_hands_off_a_model_too_big_for_the_bed(self):
        quick = await self.quick_upload(box_stl(self.dir / "long.stl", (400, 20, 20)), "long.stl")
        await self.settle()
        intake = self.call("GET", f"/api/projects/{quick['id']}/intake")[1]
        self.assertFalse(intake["can_print"])
        status, opened = self.call("POST", f"/api/projects/{quick['id']}/open-as-project", {})
        self.assertEqual((status, opened["quick"]), (200, False))
        self.assertIn(quick["id"], [p["id"] for p in self.call("GET", "/api/projects")[1]["projects"]])

    async def test_open_as_project_on_an_unknown_project_is_a_404(self):
        self.assertEqual(self.call("POST", "/api/projects/9999/open-as-project", {})[0], 404)

    async def test_a_model_that_fits_the_bed_is_reported_with_its_size_and_volume(self):
        pid = await self.import_model(cube_stl(30), "cube.stl")
        status, intake = self.call("GET", f"/api/projects/{pid}/intake")
        self.assertEqual(status, 200)
        self.assertEqual((intake["state"], intake["fits"], intake["can_print"]), ("ready", True, True))
        self.assertEqual(intake["size"], [30.0, 30.0, 30.0])
        self.assertEqual(intake["volume_cm3"], 27.0)          # 30 mm cube: 27 000 mm3
        self.assertFalse(intake["repaired"])
        self.assertEqual(intake["file"], "cube.stl")

    async def test_a_model_wider_than_the_bed_needs_the_split_steps(self):
        pid = await self.import_model(box_stl(self.dir / "beam.stl", (400, 40, 20)), "beam.stl")
        intake = self.call("GET", f"/api/projects/{pid}/intake")[1]
        self.assertEqual((intake["fits"], intake["can_print"], intake["scale_ok"]), (False, False, True))
        self.assertIn("Split", intake["reason"])
        placed = self.call("POST", f"/api/projects/{pid}/auto-place", {})[1]
        self.assertFalse(placed["placed"])
        self.assertEqual(placed["project"]["data"]["objects"], [])
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/print", {"printer_id": "p1"})[0], 409)

    async def test_the_purge_area_corner_counts_against_the_bed(self):
        # 240 x 240 fits a 256 bed, but not in the middle (the purge corner is in the way): it moves to another corner
        pid = await self.import_model(box_stl(self.dir / "wide.stl", (240, 240, 10)), "wide.stl")
        self.assertTrue(self.call("GET", f"/api/projects/{pid}/intake")[1]["fits"])
        placed = self.call("POST", f"/api/projects/{pid}/auto-place", {})[1]
        obj = placed["project"]["data"]["objects"][0]
        self.assertEqual(obj["position"], [120.0, 120.0])      # pushed to the left edge, clear of the purge area at the right
        await self.settle()
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/slice", {})[0], 200)
        await self.settle()
        self.assertEqual(projects.get_project(pid)["slices"]["1"]["state"], "sliced")     # the slicer's fit check agrees
        # 250 x 250 can't avoid the purge corner anywhere
        big = await self.import_model(box_stl(self.dir / "full.stl", (250, 250, 10)), "full.stl")
        self.assertFalse(self.call("GET", f"/api/projects/{big}/intake")[1]["fits"])

    async def test_a_model_that_fits_lands_on_one_auto_plate_once(self):
        pid = await self.import_model(cube_stl(30), "cube.stl")
        first = self.call("POST", f"/api/projects/{pid}/auto-place", {})[1]
        self.assertTrue(first["placed"])
        self.assertEqual([o["plate"] for o in first["project"]["data"]["objects"]], [1])
        self.assertEqual(len(first["project"]["data"]["plates"]), 1)
        second = self.call("POST", f"/api/projects/{pid}/auto-place", {})[1]
        self.assertFalse(second["placed"])
        self.assertEqual(len(second["project"]["data"]["objects"]), 1)

    async def test_an_implausible_scale_blocks_printing_until_the_one_click_fix(self):
        pid = await self.import_model(cube_stl(30), "cube.stl", scale="0.001")     # 0.03 mm: a model made in metres
        intake = self.call("GET", f"/api/projects/{pid}/intake")[1]
        self.assertEqual((intake["scale_ok"], intake["can_print"]), (False, False))
        self.assertEqual(intake["scale_fix"]["factor"], 1000.0)
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/auto-place", {})[1]["placed"], False)
        status, error = self.call("POST", f"/api/projects/{pid}/print", {"printer_id": "p1"})
        self.assertEqual(status, 409)
        self.assertIn("scale", error["error"])

        status, fixed = self.call("POST", f"/api/projects/{pid}/fix-scale", {})
        self.assertEqual(status, 200)
        self.assertEqual(fixed["state"], "analysing")                  # analysed again at the new scale
        await self.settle()
        intake = self.call("GET", f"/api/projects/{pid}/intake")[1]
        self.assertEqual((intake["scale_ok"], intake["can_print"], intake["size"]), (True, True, [30.0, 30.0, 30.0]))
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/fix-scale", {})[0], 409)   # nothing left to fix

    async def test_slice_and_print_slices_and_queues_in_one_action(self):
        pid = await self.import_model(cube_stl(30), "cube.stl")
        status, project = self.call("POST", f"/api/projects/{pid}/print", {"printer_id": "p1"})
        self.assertEqual(status, 200, project)
        self.assertEqual(project["data"]["plates"][0]["printer_id"], "p1")
        await self.settle()
        project = projects.get_project(pid)
        self.assertEqual(project["slices"]["1"]["state"], "sliced", project["slices"]["1"].get("error"))
        queued = projects.project_jobs(pid)
        self.assertEqual([(j["plate_id"], j["printer_id"], j["state"]) for j in queued], [(1, "p1", "queued")])
        self.assertEqual(project["slices"]["1"]["sent_job"], queued[0]["id"])

    async def test_a_quick_gcode_upload_reviews_its_preflight_then_queues_without_slicing(self):
        from tests.test_jobs import GCODE
        status, project = self.call("POST", "/api/projects/upload?filename=cube.gcode&quick=1", raw=GCODE)
        self.assertEqual(status, 200, project)
        pid = project["id"]
        intake = self.call("GET", f"/api/projects/{pid}/intake")[1]
        self.assertEqual((intake["kind"], intake["can_print"], intake["file"]), ("gcode", True, "cube.gcode"))
        status, reviewed = self.call("POST", f"/api/projects/{pid}/print", {"printer_id": "p1", "review": True})
        self.assertEqual(status, 200, reviewed)
        self.assertIn("checks", reviewed["review"])
        self.assertEqual(projects.project_jobs(pid), [])      # a review queues nothing
        status, project = self.call("POST", f"/api/projects/{pid}/print", {"printer_id": "p1", "leveling": True})
        self.assertEqual(status, 200, project)
        queued = projects.project_jobs(pid)
        self.assertEqual([(j["printer_id"], j["state"]) for j in queued], [("p1", "queued")])
        self.assertEqual(self.call("GET", f"/api/jobs/{queued[0]['id']}/preflight")[0], 200)

    def standing_plate_stl(self):
        """A 240 x 150 x 5 plate stood on a corner: its diagonal makes it 276 mm tall, past the printer's 256, though it
        lies flat comfortably."""
        import numpy as np
        import trimesh
        mesh = trimesh.creation.box((240, 150, 5))
        mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 4, [0, 0, 1]))
        mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [0, 1, 0]))
        path = self.dir / "plate.stl"
        mesh.export(path)
        return path.read_bytes()

    async def test_lay_flat_puts_a_too_tall_model_on_its_largest_face_and_it_then_fits_and_prints(self):
        pid = await self.import_model(self.standing_plate_stl(), "plate.stl")
        before = self.call("GET", f"/api/projects/{pid}/intake")[1]
        self.assertEqual((before["fits"], before["can_print"]), (False, False))
        self.assertAlmostEqual(before["size"][2], 276.0, delta=1.0)
        self.assertEqual(before["rotation"], [0.0, 0.0, 0.0])
        status, after = self.call("POST", f"/api/projects/{pid}/lay-flat", {})
        self.assertEqual(status, 200, after)
        self.assertEqual((after["fits"], after["can_print"]), (True, True), after)
        for got, want in zip(sorted(after["size"]), [5.0, 150.0, 240.0]):
            self.assertAlmostEqual(got, want, delta=0.2)
        self.assertEqual(after["size"][2], 5.0)     # the thin side is the height
        self.assertEqual(self.call("GET", f"/api/projects/{pid}/intake")[1]["size"], after["size"])   # remembered
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/print", {"printer_id": "p1"})[0], 200)
        await self.settle()
        project = projects.get_project(pid)
        self.assertEqual(project["data"]["objects"][0]["rotation"], after["rotation"])
        self.assertEqual(project["slices"]["1"]["state"], "sliced", project["slices"]["1"].get("error"))

    async def test_laying_flat_a_model_already_flat_changes_nothing(self):
        pid = await self.import_model(box_stl(self.dir / "slab.stl", (80, 60, 10)), "slab.stl")
        after = self.call("POST", f"/api/projects/{pid}/lay-flat", {})[1]
        self.assertEqual(after["size"], [80.0, 60.0, 10.0])

    async def test_laying_flat_a_cylinder_standing_on_its_cap_leaves_it_upright(self):
        import trimesh
        trimesh.creation.cylinder(radius=15, height=40).export(self.dir / "cyl.stl")   # both caps tie as the largest face
        pid = await self.import_model((self.dir / "cyl.stl").read_bytes(), "cyl.stl")
        after = self.call("POST", f"/api/projects/{pid}/lay-flat", {})[1]
        self.assertEqual(after["rotation"][:2], [0.0, 0.0], after)    # not turned upside down
        self.assertAlmostEqual(after["size"][2], 40.0, delta=0.2)

    async def test_laying_flat_a_model_on_its_edge_gives_it_its_widest_footprint(self):
        pid = await self.import_model(box_stl(self.dir / "slab.stl", (10, 60, 80)), "slab.stl")
        after = self.call("POST", f"/api/projects/{pid}/lay-flat", {})[1]
        self.assertEqual(sorted(after["size"]), [10.0, 60.0, 80.0])
        self.assertEqual(after["size"][2], 10.0)

    async def test_rotate_turns_the_model_90_degrees_about_the_vertical_axis(self):
        pid = await self.import_model(box_stl(self.dir / "slab.stl", (200, 100, 20)), "slab.stl")
        turned = self.call("POST", f"/api/projects/{pid}/rotate", {})[1]
        self.assertEqual((turned["size"], turned["rotation"]), ([100.0, 200.0, 20.0], [0.0, 0.0, 90.0]))
        for expected in (180.0, 270.0, 0.0):
            again = self.call("POST", f"/api/projects/{pid}/rotate", {})[1]
            self.assertEqual(again["rotation"], [0.0, 0.0, expected])
        self.assertEqual(again["size"], [200.0, 100.0, 20.0])
        self.call("POST", f"/api/projects/{pid}/rotate", {})
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/auto-place", {})[0], 200)
        await self.settle()
        placed = projects.get_project(pid)["data"]["objects"][0]
        self.assertEqual(placed["rotation"], [0.0, 0.0, 90.0])

    async def test_orientation_cannot_change_once_the_model_is_on_a_plate(self):
        pid = await self.import_model(cube_stl(30), "cube.stl")
        self.call("POST", f"/api/projects/{pid}/auto-place", {})
        for action in ("rotate", "lay-flat"):
            status, error = self.call("POST", f"/api/projects/{pid}/{action}", {})
            self.assertEqual(status, 409)
            self.assertIn("plate", error["error"])

    async def test_orientation_survives_a_save_of_the_project(self):
        pid = await self.import_model(box_stl(self.dir / "slab.stl", (200, 100, 20)), "slab.stl")
        self.call("POST", f"/api/projects/{pid}/rotate", {})
        project = self.call("GET", f"/api/projects/{pid}")[1]
        self.assertEqual(self.call("PATCH", f"/api/projects/{pid}", {"data": project["data"], "revision": project["revision"]})[0], 200)
        self.assertEqual(self.call("GET", f"/api/projects/{pid}/intake")[1]["size"], [100.0, 200.0, 20.0])

    async def test_slice_and_print_needs_a_printer(self):
        pid = await self.import_model(cube_stl(30), "cube.stl")
        projects.update_project(pid, {})                      # no printer remembered, none chosen
        status, error = self.call("POST", f"/api/projects/{pid}/print", {})
        self.assertEqual(status, 409)
        self.assertIn("printer", error["error"])


if __name__ == "__main__":
    unittest.main()
