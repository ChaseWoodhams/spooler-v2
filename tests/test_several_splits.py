"""One project owning several splits: oversized models become splits (make_split), Create converts every 'split' row,
delete_project removes only the splits no other project uses, and a project with a single split behaves as before."""
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import database
import jobs
import projects
import slicer
import splits
import state
from tests.test_jobs import FakePrinter
from tests.test_project_workflow import GEOMETRY, box_stl
from tests.test_slicer import cube_stl, fake_runner, make_fake_orca


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class SeveralSplitsTests(unittest.IsolatedAsyncioTestCase):
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

    def tearDown(self):
        state.printers = self._saved_printers
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        slicer.configure()
        splits.configure()
        projects.configure()
        self._tmp.cleanup()

    async def settle(self):
        with mock.patch.object(slicer, "runner", fake_runner()):
            while await splits.process_next() or await projects.process_next():
                pass

    def add(self, project_id, name, size):
        data = box_stl(self.dir / name, size)
        projects.import_model(project_id, io.BytesIO(data), len(data), name)

    async def mixed_draft(self, small=3, big=2):
        draft = projects.create_project("Mixed", draft=True)
        for i in range(small):
            self.add(draft["id"], f"small{i}.stl", (20, 20, 10))
        for i in range(big):
            self.add(draft["id"], f"big{i}.stl", (400 + 10 * i, 40, 20))
        await self.settle()
        return projects.get_project(draft["id"])

    async def test_make_split_turns_an_oversized_model_into_a_split(self):
        draft = await self.mixed_draft(small=1, big=1)
        big = next(m for m in draft["models"] if m["file"] == "big0.stl")
        project = projects.make_split(draft["id"], big["id"])
        self.assertEqual(len(project["split_ids"]), 1)
        self.assertEqual(project["split_id"], project["split_ids"][0])
        self.assertEqual([m["file"] for m in project["models"]], ["small0.stl"])
        self.assertEqual(len(project["data"]["objects"]), 1)
        self.assertEqual(splits.get_split(project["split_id"])["source_name"], "big0.stl")
        self.assertFalse(projects.model_dir(draft["id"], big["id"]).exists())

    async def test_make_split_refuses_a_model_that_is_not_ready_or_missing(self):
        draft = projects.create_project("D", draft=True)
        self.add(draft["id"], "big.stl", (400, 40, 20))
        with self.assertRaises(jobs.JobError):
            projects.make_split(draft["id"], 1)   # still being analysed
        with self.assertRaises(jobs.JobError):
            projects.make_split(draft["id"], 99)

    async def test_create_converts_every_split_row_and_opens_on_split(self):
        draft = await self.mixed_draft(small=5, big=2)
        result = projects.create_draft(draft["id"], "Mixed")
        project = result["project"]
        self.assertEqual(result["step"], "model")
        self.assertEqual(len(project["split_ids"]), 2)
        self.assertEqual(sorted(m["file"] for m in project["models"]), [f"small{i}.stl" for i in range(5)])
        self.assertEqual(len(project["data"]["objects"]), 5)

    async def test_all_parts_and_the_small_models_end_up_in_the_plate_pool(self):
        project = projects.create_draft((await self.mixed_draft(small=2, big=2))["id"], "Mixed")["project"]
        await self.settle()
        for split_id in project["split_ids"]:
            splits.request_split(split_id)
        await self.settle()
        for split_id in project["split_ids"]:
            projects.lay_out_split(project["id"], split_id=split_id)
        await self.settle()
        laid = projects.get_project(project["id"])
        parts = sum(len(splits.get_split(s, detail=True)["result"]["parts"]) for s in laid["split_ids"])
        self.assertEqual(len(laid["data"]["objects"]), 2 + parts)
        self.assertEqual(sum(1 for m in laid["models"] if m.get("split")), parts)
        self.assertEqual({m["split"]["id"] for m in laid["models"] if m.get("split")}, set(laid["split_ids"]))

    async def test_a_second_split_can_be_placed_whole_by_its_id(self):
        draft = projects.create_project("D", draft=True)
        for name in ("a.stl", "b.stl"):
            data = cube_stl(30)
            projects.import_model(draft["id"], io.BytesIO(data), len(data), name)
        await self.settle()
        for model in projects.get_project(draft["id"])["models"]:
            projects.make_split(draft["id"], model["id"])
        await self.settle()
        project = projects.get_project(draft["id"])
        second = project["split_ids"][1]
        placed = projects.place_whole_model(project["id"], split_id=second)
        self.assertEqual([m["split"]["id"] for m in placed["models"]], [second])

    async def test_delete_project_removes_every_split_unless_another_project_uses_it(self):
        project = projects.create_draft((await self.mixed_draft(small=1, big=2))["id"], "M")["project"]
        first, second = project["split_ids"]
        other = projects.create_project("Other", split_id=second)
        projects.delete_project(project["id"])
        with self.assertRaises(jobs.JobError):
            splits.get_split(first)
        self.assertEqual(splits.get_split(second)["project_id"], other["id"])
        projects.delete_project(other["id"])
        with self.assertRaises(jobs.JobError):
            splits.get_split(second)

    async def test_a_split_reports_its_owner_when_it_is_not_the_projects_first(self):
        project = projects.create_draft((await self.mixed_draft(small=0, big=2))["id"], "M")["project"]
        self.assertEqual(splits.get_split(project["split_ids"][1])["project_id"], project["id"])

    async def test_a_single_split_project_is_unchanged(self):
        data = cube_stl(30)
        project = projects.create_project_from_upload(io.BytesIO(data), len(data), "cube.stl")
        self.assertEqual(project["split_ids"], [project["split_id"]])
        self.assertEqual(projects.create_project("none")["split_ids"], [])
        await self.settle()
        self.assertEqual(projects.intake(project["id"])["file"], "cube.stl")
        self.assertNotIn("split_ids", projects.intake(project["id"]))

    async def test_create_with_nothing_oversized_opens_on_plates(self):
        draft = await self.mixed_draft(small=2, big=0)
        result = projects.create_draft(draft["id"], "Small")
        self.assertEqual((result["step"], result["project"]["split_ids"]), ("plates", []))

    def test_the_migration_is_additive(self):
        path = self.dir / "old.db"
        conn = sqlite3.connect(path)
        for number, sql in enumerate(database.MIGRATIONS[:11], start=1):
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
        conn.execute("INSERT INTO projects (id, name, created_at, updated_at) VALUES (1, 'Old', 1.7e9, 1.7e9)")
        conn.commit()
        conn.close()
        database.configure(path)
        with mock.patch.object(projects.time, "time", return_value=1.7e9):
            projects.init()
        self.assertEqual(projects.get_project(1)["split_ids"], [])


if __name__ == "__main__":
    unittest.main()
