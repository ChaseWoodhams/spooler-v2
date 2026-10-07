"""One definition of a problem Object (off the bed, over the purge area, overlapping, failed model) feeds the Plates step,
the Slice findings and the queue send."""
import asyncio
import io
import json
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
from tests.test_projects import GEOMETRY
from tests.test_slicer import cube_stl, fake_runner, make_fake_orca


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class ProblemObjectTests(unittest.IsolatedAsyncioTestCase):
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

    async def project_with_cube(self, at, second=None):
        """One 20 mm cube on plate 1 centred at `at` (and a second object at `second`)."""
        project = projects.create_project("Brackets")
        data = cube_stl(20)
        projects.import_model(project["id"], io.BytesIO(data), len(data), "cube.stl")
        await self.run_worker()
        project = projects.get_project(project["id"])
        change = json.loads(json.dumps(project["data"]))
        change["objects"][0]["position"] = list(at)
        if second:
            change["objects"].append({**change["objects"][0], "id": 2, "name": "cube 2", "position": list(second)})
        return projects.update_project(project["id"], {"data": change, "revision": project["revision"]})

    def plate(self, project):
        return projects.plate_status(project["id"])["plates"][0]

    async def sliced(self, project):
        projects.request_slice(project["id"], 1)
        with mock.patch.object(slicer, "runner", fake_runner()):
            await self.run_worker()
        return projects.get_project(project["id"])["slices"]["1"]

    async def test_a_plate_with_everything_on_the_bed_has_no_problem(self):
        project = await self.project_with_cube((100, 100))
        plate = self.plate(project)
        self.assertEqual((plate["problems"], plate["problem_reason"]), ([], None))

    async def test_an_off_bed_object_is_a_problem_of_its_plate_with_a_reason(self):
        project = await self.project_with_cube((-50, 100))
        plate = self.plate(project)
        self.assertEqual([(p["key"], p["problem"]) for p in plate["problems"]], [(1, "off the bed")])
        self.assertEqual(plate["problem_reason"], "cube is off the bed")

    async def test_overlapping_objects_are_problems_of_both(self):
        project = await self.project_with_cube((100, 100), second=(105, 100))
        plate = self.plate(project)
        self.assertEqual(sorted((p["key"], p["problem"]) for p in plate["problems"]), [(1, "overlaps"), (2, "overlaps")])
        self.assertEqual(plate["problem_reason"], "cube overlaps cube 2")

    async def test_an_object_of_a_failed_model_is_a_problem(self):
        project = await self.project_with_cube((100, 100))
        projects._mutate(project["id"], "models", lambda models: models[0].update(state="failed", error="bad mesh"),
                         project["created_at"])
        plate = self.plate(project)
        self.assertEqual([(p["key"], p["problem"]) for p in plate["problems"]], [(1, "failed model")])

    async def test_slicing_a_plate_with_a_problem_records_it_as_a_finding(self):
        project = await self.project_with_cube((-50, 100))
        entry = await self.sliced(project)   # the slicer stops on an object past the bed, and the plate still says why
        mine = [f for f in entry["findings"] if f["level"] == "fail" and f["reason"] == "cube is off the bed"]
        self.assertEqual(len(mine), 1, entry["findings"])

    async def test_a_slice_that_goes_ahead_over_a_problem_records_it_too(self):
        project = await self.project_with_cube((100, 100), second=(105, 100))
        entry = await self.sliced(project)
        self.assertEqual(entry["state"], "sliced")
        self.assertEqual([f["reason"] for f in entry["findings"]], ["cube overlaps cube 2", "cube 2 overlaps cube"])

    async def test_a_clean_slice_has_no_placement_finding(self):
        project = await self.project_with_cube((100, 100))
        self.assertEqual((await self.sliced(project))["findings"], [])

    async def test_a_plate_with_a_problem_is_not_sent_to_the_queue(self):
        project = await self.project_with_cube((100, 100), second=(105, 100))
        await self.sliced(project)
        with self.assertRaisesRegex(jobs.JobError, "cube overlaps cube 2") as caught:
            projects.queue_plates(project["id"], {"plates": [1]})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(projects.project_jobs(project["id"]), [])
