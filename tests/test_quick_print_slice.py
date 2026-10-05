"""Quick print slicing and queueing: the options reach the slicer config, a preset change keeps them, leveling and
timelapse survive the fresh-slice path, bad models are blocked, and preflight warnings come before queueing.
Everything is exercised through the HTTP API (the harness is the Simple print one)."""
import hashlib
import json
import unittest
from pathlib import Path
from unittest import mock

import projects
import slicer
import splits
from tests import test_simple_print
from tests.test_jobs import GCODE, isolate_preflight
from tests.test_simple_print import GEOMETRY
from tests.test_slicer import cube_stl, fake_runner


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class QuickPrintSliceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        test_simple_print.SimplePrintApiTests.setUp(self)
        isolate_preflight(self, self.dir)

    asyncTearDown = test_simple_print.SimplePrintApiTests.asyncTearDown
    tearDown = test_simple_print.SimplePrintApiTests.tearDown
    call = test_simple_print.SimplePrintApiTests.call
    quick_upload = test_simple_print.SimplePrintApiTests.quick_upload

    async def settle(self):
        """Run the workers with a recording fake Orca."""
        runner = fake_runner()
        plain = runner

        def runner(args, timeout):          # a print that passes every preflight check
            result = plain(args, timeout)
            (Path(args[args.index("--outputdir") + 1]) / "plate_1.gcode").write_bytes(GCODE)
            return result
        runner.calls = plain.calls
        with mock.patch.object(slicer, "runner", runner):
            while await splits.process_next() or await projects.process_next():
                pass
        self.runner = runner
        return runner

    def slice_config(self):
        """The process settings the last slice handed to Orca."""
        args = self.runner.calls[-1]
        process_file = args[args.index("--load-settings") + 1].split(";")[1]
        return json.loads(Path(process_file).read_text(encoding="utf-8"))

    def bundle_hashes(self):
        return {str(p.relative_to(self.dir)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted((self.dir / "profiles").rglob("*.json"))}

    async def print_with(self, options=None, **extra):
        project = await self.quick_upload(cube_stl(30), "cube.stl")
        body = {"printer_id": "p1", **extra, **({"options": options} if options is not None else {})}
        status, result = self.call("POST", f"/api/projects/{project['id']}/print", body)
        self.assertEqual(status, 200, result)
        await self.settle()
        return project["id"]

    async def test_layer_height_infill_walls_reach_the_slicer_config(self):
        await self.print_with({"layer_height": 0.12, "infill": 25, "walls": 4})
        config = self.slice_config()
        self.assertEqual((config["layer_height"], config["sparse_infill_density"], config["wall_loops"]), ("0.12", "25%", "4"))

    async def test_supports_off_normal_and_tree(self):
        for choice, expected in (("off", ("0", None)), ("normal", ("1", "normal(auto)")), ("tree", ("1", "tree(auto)"))):
            with self.subTest(choice):
                await self.print_with({"supports": choice})
                config = self.slice_config()
                self.assertEqual(config["enable_support"], expected[0])
                if expected[1]:
                    self.assertEqual(config["support_type"], expected[1])

    async def test_brim_off_is_written_explicitly_auto_and_custom_width(self):
        await self.print_with({"brim": "off"})
        self.assertEqual(self.slice_config()["brim_type"], "no_brim")
        await self.print_with({"brim": "auto"})
        self.assertEqual(self.slice_config()["brim_type"], "auto_brim")
        await self.print_with({"brim": "custom", "brim_width": 7})
        config = self.slice_config()
        self.assertEqual((config["brim_type"], config["brim_width"]), ("outer_only", "7"))

    async def test_an_option_outside_the_choices_is_refused(self):
        project = await self.quick_upload(cube_stl(30), "cube.stl")
        for bad in ({"layer_height": 0.17}, {"supports": "everywhere"}, {"brim": "custom"}, {"infill": 150}, {"walls": 0},
                    {"colour": "red"}):
            with self.subTest(bad):
                status, error = self.call("POST", f"/api/projects/{project['id']}/print", {"printer_id": "p1", "options": bad})
                self.assertEqual(status, 400, error)
        self.assertEqual(projects.project_jobs(project["id"]), [])

    async def test_changing_the_preset_keeps_the_options_and_leaves_the_bundles_alone(self):
        project = await self.quick_upload(cube_stl(30), "cube.stl")
        first = project["data"]["profile"]["bundle_id"]
        other = slicer.derive_bundle(first, {"process": {"wall_loops": "5"}}, "Other preset")["id"]
        before = self.bundle_hashes()
        url = f"/api/projects/{project['id']}/print"
        status, result = self.call("POST", url, {"printer_id": "p1", "review": True,
                                                 "options": {"layer_height": 0.28, "supports": "tree"}})
        self.assertEqual(status, 200, result)
        await self.settle()
        status, result = self.call("POST", url, {"printer_id": "p1", "review": True, "preset": other})
        self.assertEqual(status, 200, result)
        await self.settle()
        self.assertEqual(self.call("GET", f"/api/projects/{project['id']}")[1]["data"]["profile"]["bundle_id"], other)
        config = self.slice_config()
        self.assertEqual((config["layer_height"], config["support_type"]), ("0.28", "tree(auto)"))   # kept
        self.assertEqual(config["wall_loops"], "5")                      # the new preset's own value applies
        self.assertEqual({k: v for k, v in self.bundle_hashes().items() if k in before}, before)

    async def test_leveling_and_timelapse_survive_the_fresh_slice_path(self):
        pid = await self.print_with(leveling=True, timelapse=True)
        job = projects.project_jobs(pid)[0]
        self.assertEqual((job["print_opts"]["leveling"], job["print_opts"]["timelapse"]), (True, True))
        pid = await self.print_with(leveling=False, timelapse=True)
        job = projects.project_jobs(pid)[0]
        self.assertEqual((job["print_opts"]["leveling"], job["print_opts"]["timelapse"]), (False, True))

    async def test_a_wrong_scale_model_is_blocked_with_the_scale_fix_then_prints(self):
        project = await self.quick_upload(cube_stl(0.03), "tiny.stl")
        status, error = self.call("POST", f"/api/projects/{project['id']}/print", {"printer_id": "p1"})
        self.assertEqual(status, 409)
        self.assertIn("scale", error["error"])
        self.assertEqual(projects.project_jobs(project["id"]), [])
        self.assertIsNotNone(self.call("GET", f"/api/projects/{project['id']}/intake")[1]["scale_fix"])
        self.call("POST", f"/api/projects/{project['id']}/fix-scale", {})
        await self.settle()
        self.assertEqual(self.call("POST", f"/api/projects/{project['id']}/print", {"printer_id": "p1"})[0], 200)

    async def test_a_model_too_big_for_the_bed_is_blocked_with_a_reason(self):
        project = await self.quick_upload(cube_stl(400), "huge.stl")
        status, error = self.call("POST", f"/api/projects/{project['id']}/print", {"printer_id": "p1"})
        self.assertEqual(status, 409)
        self.assertIn("doesn't fit", error["error"])
        self.assertEqual(projects.project_jobs(project["id"]), [])

    async def test_review_slices_and_returns_warnings_without_queueing_then_print_queues(self):
        project = await self.quick_upload(cube_stl(30), "cube.stl")
        url = f"/api/projects/{project['id']}/print"
        status, result = self.call("POST", url, {"printer_id": "p1", "review": True, "leveling": True})
        self.assertEqual(status, 200, result)
        await self.settle()
        self.assertEqual(projects.project_jobs(project["id"]), [])           # sliced, not queued
        status, result = self.call("POST", url, {"printer_id": "p1", "review": True, "leveling": True})
        self.assertEqual(status, 200, result)
        self.assertIsInstance(result["review"]["warnings"], list)                       # preflight warnings, still not queued
        self.assertEqual(projects.project_jobs(project["id"]), [])
        status, result = self.call("POST", url, {"printer_id": "p1", "leveling": True})
        self.assertEqual(status, 200, result)
        queued = projects.project_jobs(project["id"])
        self.assertEqual([(j["printer_id"], j["state"], j["print_opts"]["leveling"]) for j in queued], [("p1", "queued", True)])

    async def test_options_read_back_default_to_the_presets_values_and_remembered_choices(self):
        self.call("PATCH", "/api/defaults", {"printer": "p1", "plate_side": "smooth"})
        project = await self.quick_upload(cube_stl(30), "cube.stl")
        status, info = self.call("GET", f"/api/projects/{project['id']}/quick-options")
        self.assertEqual(status, 200, info)
        self.assertEqual(info["options"]["walls"], 3)                         # the preset's wall_loops
        self.assertEqual(info["options"]["layer_height"], 0.2)
        self.assertEqual(info["options"]["supports"], "off")
        self.assertEqual(info["options"]["brim"], "auto")                     # no brim_type in the preset
        self.assertIn(0.12, info["layer_heights"])
        self.assertEqual((info["printer_id"], info["bed"]), ("p1", "smooth"))
        self.call("POST", f"/api/projects/{project['id']}/print", {"printer_id": "p1", "review": True, "options": {"walls": 5}})
        self.assertEqual(self.call("GET", f"/api/projects/{project['id']}/quick-options")[1]["options"]["walls"], 5)


    async def test_reprint_with_changes_reopens_the_saved_choices_and_queues_a_new_job(self):
        """History's Reprint with changes: the modal reads the project's saved choices back, then Slice & print queues a
        NEW Job. Unchanged, the existing Slice is reused (same G-code); a changed option makes it Outdated and slices again."""
        import database
        project_id = await self.print_with({"walls": 5}, spool_id=None, bed="smooth")
        first = projects.project_jobs(project_id)
        self.assertEqual(len(first), 1)
        with database.transaction() as conn:         # the first Job finished: its Print stays as it was
            conn.execute("UPDATE jobs SET state = 'completed' WHERE id = ?", (first[0]["id"],))
        status, info = self.call("GET", f"/api/projects/{project_id}/quick-options")
        self.assertEqual((info["options"]["walls"], info["bed"], info["printer_id"]), (5, "smooth", "p1"))
        slices = len(self.runner.calls)
        url = f"/api/projects/{project_id}/print"
        status, result = self.call("POST", url, {"printer_id": "p1", "bed": "smooth", "review": True})   # unchanged reopen
        self.assertEqual((status, result["review"]["state"]), (200, "ready"), result)    # reused: nothing to wait for
        status, result = self.call("POST", url, {"printer_id": "p1", "bed": "smooth"})
        self.assertEqual(status, 200, result)
        self.assertEqual(len(self.runner.calls), slices)
        self.assertEqual(len(projects.project_jobs(project_id)), 2)
        with database.transaction() as conn:
            conn.execute("UPDATE jobs SET state = 'completed'")
        status, result = self.call("POST", url, {"printer_id": "p1", "bed": "smooth", "review": True, "options": {"walls": 7}})
        self.assertEqual((status, result["review"]["state"]), (200, "slicing"), result)   # changed: sliced again
        await self.settle()
        self.assertEqual(self.slice_config()["wall_loops"], "7")
        status, result = self.call("POST", url, {"printer_id": "p1", "bed": "smooth", "options": {"walls": 7}})
        self.assertEqual(status, 200, result)
        self.assertEqual(len(projects.project_jobs(project_id)), 3)


if __name__ == "__main__":
    unittest.main()
