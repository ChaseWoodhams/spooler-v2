"""Packing density: how far apart Arrange and Plan plates keep objects, chosen for the project or one plate, judged by the
same number everywhere ("too close"), previewed per preset, and applied when a New project is created."""
import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path

import database
import jobs
import plan_layout
import projects
import slicer
import splits
import state
from tests.test_jobs import FakePrinter
from tests.test_projects import GEOMETRY
from tests.test_slicer import cube_stl, make_fake_orca


class PackSettingTests(unittest.TestCase):
    def test_presets_and_custom_gaps_are_cleaned(self):
        self.assertEqual(projects._clean_pack({"preset": "tight"}), {"preset": "tight", "gap": 3.0})
        self.assertEqual(projects._clean_pack({"preset": "roomy", "gap": 99}), {"preset": "roomy", "gap": 12.0})   # a preset's gap is its own
        self.assertEqual(projects._clean_pack({"preset": "custom", "gap": "8.04"}), {"preset": "custom", "gap": 8.0})
        self.assertEqual(projects._clean_pack({"preset": "tight", "tight_supports": 1}), {"preset": "tight", "gap": 3.0, "tight_supports": True})
        self.assertIsNone(projects._clean_pack(None))
        self.assertNotIn("tight_supports", projects._clean_pack({"preset": "tight", "tight_supports": True}, project=False))

    def test_refused_choices_say_why(self):
        for bad in ({"preset": "huge"}, {"preset": "custom"}, {"preset": "custom", "gap": 1}, {"preset": "custom", "gap": 41}, "tight"):
            with self.assertRaises(jobs.JobError, msg=bad):
                projects._clean_pack(bad)

    def test_the_supports_floor_holds_unless_the_project_allows_tighter(self):
        plates = [{"id": 1}, {"id": 2, "pack": {"preset": "roomy", "gap": 12.0}}]
        base = {"plates": plates}
        self.assertEqual(projects.pack_for(base, 1), {"preset": "normal", "gap": 6.0, "support_gap": 22.0, "tight_supports": False})
        tight = {"plates": plates, "pack": {"preset": "tight", "gap": 3.0}}
        self.assertEqual((projects.pack_for(tight, 1)["gap"], projects.pack_for(tight, 1)["support_gap"]), (3.0, 22.0))
        self.assertEqual(projects.pack_for(tight, 2)["gap"], 12.0, "a plate's own choice wins")
        allowed = {"plates": plates, "pack": {"preset": "tight", "gap": 3.0, "tight_supports": True}}
        self.assertEqual(projects.pack_for(allowed, 1)["support_gap"], 3.0)
        roomy = {"plates": plates, "pack": {"preset": "custom", "gap": 30.0}}
        self.assertEqual(projects.pack_for(roomy, 1)["support_gap"], 30.0, "a gap wider than the supports' reach is kept")


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class PackDensityProjectTests(unittest.IsolatedAsyncioTestCase):
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
        state.printers = {"p1": FakePrinter()}

    async def asyncTearDown(self):
        for task in list(jobs._start_tasks.values()):
            task.cancel()
        await asyncio.sleep(0)

    def tearDown(self):
        state.printers = self._saved_printers
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        slicer.configure()
        splits.configure()
        projects.configure()
        self._tmp.cleanup()

    async def run_worker(self):
        while await projects.process_next():
            pass

    async def project_of_cubes(self, count, size, draft=False):
        project = projects.create_project("Cubes", draft=draft)
        data = cube_stl(size)
        for n in range(count):
            projects.import_model(project["id"], io.BytesIO(data), len(data), f"cube{n}.stl")
        await self.run_worker()
        return projects.get_project(project["id"])

    def two_cubes(self, project, gap_between):
        """Two objects on plate 1, their edges `gap_between` mm apart."""
        change = json.loads(json.dumps(project["data"]))
        first = change["objects"][0]
        first["position"] = [100.0, 100.0]
        change["objects"] = [first, {**first, "id": 2, "name": "cube 2", "position": [100.0 + first_size(project) + gap_between, 100.0]}]
        return projects.update_project(project["id"], {"data": change, "revision": project["revision"]})

    async def test_the_chosen_density_decides_what_is_too_close(self):
        project = await self.project_of_cubes(1, 20)
        project = self.two_cubes(project, 4)   # 4 mm apart: Normal keeps 6
        kinds = lambda: sorted({p["problem"] for p in projects.plate_status(project["id"])["plates"][0]["problems"]})
        self.assertEqual(kinds(), ["too close"])
        projects.set_pack(project["id"], {"pack": {"preset": "tight"}})
        self.assertEqual(kinds(), [], "Tight keeps 3 mm, so 4 mm is fine")
        projects.set_pack(project["id"], {"pack": {"preset": "roomy"}})
        self.assertEqual(kinds(), ["too close"], "Roomy keeps 12 mm")
        projects.set_pack(project["id"], {"pack": None})
        self.assertEqual(projects.get_project(project["id"])["data"].get("pack"), None, "null goes back to Normal")

    async def test_a_plate_can_have_its_own_density(self):
        project = self.two_cubes(await self.project_of_cubes(1, 20), 4)
        projects.set_pack(project["id"], {"plate_id": 1, "plate_pack": {"preset": "tight"}})
        self.assertEqual(projects.plate_status(project["id"])["plates"][0]["problems"], [])
        saved = projects.get_project(project["id"])["data"]
        self.assertEqual(saved["plates"][0]["pack"], {"preset": "tight", "gap": 3.0})
        self.assertNotIn("pack", saved, "the project default is untouched")
        projects.set_pack(project["id"], {"plate_id": 1, "plate_pack": None})
        self.assertNotIn("pack", projects.get_project(project["id"])["data"]["plates"][0])

    async def test_the_choice_survives_a_save_from_the_page(self):
        project = await self.project_of_cubes(1, 20)
        projects.set_pack(project["id"], {"pack": {"preset": "custom", "gap": 9}})
        record = projects.get_project(project["id"])
        saved = projects.update_project(project["id"], {"data": record["data"], "revision": record["revision"]})
        self.assertEqual(saved["data"]["pack"], {"preset": "custom", "gap": 9.0})

    async def test_planning_at_a_density_changes_nothing_and_roomier_never_needs_fewer_plates(self):
        project = await self.project_of_cubes(5, 110)
        before = projects.get_project(project["id"])["revision"]
        counts = projects.plan_densities(project["id"])
        self.assertEqual(projects.get_project(project["id"])["revision"], before, "the preview keeps nothing")
        plates = [counts[p]["plates"] for p in ("tight", "normal", "roomy")]
        self.assertTrue(all(n >= 1 for n in plates), counts)
        self.assertEqual(plates, sorted(plates), f"a roomier plate holds no more: {counts}")
        what_if = projects.plan_preview(project["id"], pack={"preset": "roomy"})
        self.assertEqual(len(what_if["plates"]), counts["roomy"]["plates"])

    async def test_a_plan_keeps_objects_as_far_apart_as_the_density_says(self):
        project = await self.project_of_cubes(4, 40)
        projects.set_pack(project["id"], {"pack": {"preset": "roomy"}})
        record = projects.get_project(project["id"])
        planned = plan_layout.seed_data(project["id"], record)      # what Create applies to a new project
        projects.save_data(project["id"], record, planned["data"], planned["emptied"])
        record = projects.get_project(project["id"])
        self.assertEqual(projects.plate_problems(project["id"], record), [], "a plan is valid at its own density")
        on_first = [o["position"] for o in record["data"]["objects"] if o["plate"] == record["data"]["objects"][0]["plate"]]
        gaps = [max(abs(a[0] - b[0]), abs(a[1] - b[1])) - 40 for i, a in enumerate(on_first) for b in on_first[i + 1:]]
        self.assertTrue(all(g >= 11.9 for g in gaps), f"edges at least 12 mm apart: {gaps}")

    async def test_create_arranges_the_models_at_the_chosen_density(self):
        draft = await self.project_of_cubes(4, 30, draft=True)
        projects.set_pack(draft["id"], {"pack": {"preset": "tight"}})
        self.assertEqual(len({tuple(o["position"]) for o in projects.get_project(draft["id"])["data"]["objects"]}), 1, "they start stacked")
        created = projects.create_draft(draft["id"], "Garden")
        self.assertEqual(created["step"], "plates")
        self.assertTrue(created["arranged"]["summary"], created["arranged"])
        objects = created["project"]["data"]["objects"]
        self.assertEqual(len({(o["plate"], tuple(o["position"])) for o in objects}), 4, "no two models share a spot")
        self.assertEqual(created["project"]["data"]["pack"]["preset"], "tight")
        self.assertEqual(projects.plate_problems(draft["id"], created["project"]), [])

    async def test_a_new_project_starts_with_the_density_last_chosen(self):
        import remembered_defaults
        self.assertNotIn("pack", projects.create_project("First")["data"])
        remembered_defaults.save({"pack": {"preset": "roomy"}})
        self.assertEqual(projects.create_project("Second")["data"]["pack"], {"preset": "roomy", "gap": 12.0})
        with self.assertRaises(jobs.JobError):
            remembered_defaults.save({"pack": {"preset": "custom", "gap": 500}})
        remembered_defaults.save({"pack": None})
        self.assertNotIn("pack", projects.create_project("Third")["data"])

    async def test_create_can_leave_the_models_alone(self):
        draft = await self.project_of_cubes(3, 30, draft=True)
        created = projects.create_draft(draft["id"], "Hand made", arrange=False)
        self.assertIsNone(created["arranged"])
        self.assertEqual(len({tuple(o["position"]) for o in created["project"]["data"]["objects"]}), 1)


def first_size(project):
    return project["models"][0]["size"][0]


if __name__ == "__main__":
    unittest.main()
