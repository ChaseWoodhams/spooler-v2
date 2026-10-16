"""Confirmation slices on a fixture Project through a fake OrcaSlicer: what is logged, and that the Project is left alone."""
import asyncio
import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import calibration
import confirmation
import database
import jobs
import planner
import projects
import slicer
import splits
from tests.test_slicer import SLICED_GCODE, cube_stl, fake_runner, make_fake_orca

GEOMETRY = not splits.missing_modules()
PRINTERS = {"dunt": 0.4, "og": 0.4}


def supported_runner(calls):
    """Writes G-code with 2 mm of support extrusion for every Object it is given, named the way Orca names them."""
    def run(args, timeout):
        calls.append(args)
        out = Path(args[args.index("--outputdir") + 1])
        plate = Path(args[-1])
        import zipfile
        names = re.findall(r'<object id="\d+" name="([^"]+)"', zipfile.ZipFile(plate).read("3D/3dmodel.model").decode())
        body = "".join(f"EXCLUDE_OBJECT_START NAME={n}_id_{i}_copy_0\n;TYPE:Support\nG1 X30 Y10 E2\nEXCLUDE_OBJECT_END NAME={n}_id_{i}_copy_0\n"
                       for i, n in enumerate(names, start=1))
        (out / "plate_1.gcode").write_text("M83\n" + body + SLICED_GCODE)
        return 0, ""
    return run


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class ConfirmationProjectTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        database.configure(self.dir / "spooler.db")
        jobs.init(self.dir)
        slicer.configure(self.dir, orca_path=make_fake_orca(self.dir / "tools"))
        slicer.init()
        splits.configure(self.dir)
        projects.configure(self.dir)
        projects.init()
        project = projects.create_project("Confirm")
        for n, size in enumerate((20, 30), start=1):
            data = cube_stl(size)
            projects.import_model(project["id"], io.BytesIO(data), len(data), f"part{n}.stl")

        async def work():
            with mock.patch.object(slicer, "runner", fake_runner()):
                while await projects.process_next():
                    pass
        asyncio.run(work())
        self.pid = project["id"]

        def change(data):
            template = {o["model"]: o for o in data["objects"]}
            data["objects"] = [{**template[model], "id": i, "plate": 1, "position": [30.0 + 20 * i, 50.0], "name": f"Object {i}"}
                               for i, model in enumerate((1, 2), start=1)]
            data["plates"] = [{"id": 1, "name": "Plate 1"}]
            data["profile"]["bundle_id"] = slicer.list_bundles()[0]["id"]
        projects._mutate(self.pid, "data", change)

    def tearDown(self):
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        slicer.configure()
        splits.configure()
        projects.configure()
        self._tmp.cleanup()

    def plans(self):
        record = projects.get_project(self.pid)
        problem = planner.problem_from_project(self.pid, record, plan_nozzle=0.4, printers=PRINTERS, models=calibration.fit_models())
        return record, problem, planner.plan_problem(problem, budget_s=10, max_steps=10)

    def log(self):
        import time_estimate
        return time_estimate.slice_log()

    def test_each_confirmation_slice_adds_a_full_slice_log_row_and_leaves_the_project_alone(self):
        record, problem, plans = self.plans()
        calls = []
        with mock.patch.object(slicer, "runner", supported_runner(calls)):
            result = confirmation.confirm_plans(self.pid, record, problem, plans, budget_s=20)
        self.assertFalse(result.plans[0].partly_estimated)
        self.assertEqual(result.plans[0].plates[0].seconds, 564)                # 9m 24s, the fake G-code's own figure
        rows = self.log()["slices"]
        self.assertEqual(len(rows), len(calls))
        self.assertEqual({r["kind"] for r in rows}, {"full"})
        self.assertTrue(all(r["settings_hash"] and r["bundle_id"] and r["nozzle"] == 0.4 for r in rows))
        after = projects.get_project(self.pid)
        self.assertEqual((after["revision"], after["data"]), (record["revision"], record["data"]))
        self.assertEqual(after["slices"], record["slices"])

    def test_a_supported_plate_logs_support_rows_with_the_proxy_volume(self):
        record, problem, plans = self.plans()
        import dataclasses
        # make the first candidate of every Object need supports so the planner's plates are supported
        items = tuple(dataclasses.replace(i, candidates=tuple(dataclasses.replace(c, needs_support=True, support_volume_cm3=3.0, support_g=1.0)
                                                              for c in i.candidates)) for i in problem.items)
        problem = dataclasses.replace(problem, items=items)
        plans = planner.plan_problem(problem, budget_s=10, max_steps=5)
        with mock.patch.object(slicer, "runner", supported_runner([])):
            result = confirmation.confirm_plans(self.pid, record, problem, plans, budget_s=20)
        self.assertGreater(result.plans[0].terms["support_g"], 0)
        log = self.log()
        self.assertTrue(log["support_log"])
        self.assertTrue(all(r["support_volume_cm3"] == 3.0 and r["support_g"] > 0 for r in log["support_log"]))
        self.assertTrue(all(r["support_volume_cm3"] is not None for r in log["slices"]))

    def test_a_reduced_slice_is_logged_as_reduced(self):
        record, problem, plans = self.plans()
        import dataclasses
        items = tuple(dataclasses.replace(i, candidates=tuple(dataclasses.replace(c, needs_support=True, support_volume_cm3=3.0, support_g=1.0)
                                                              for c in i.candidates)) for i in problem.items)
        problem = dataclasses.replace(problem, items=items)
        plans = planner.plan_problem(problem, budget_s=10, max_steps=5)
        with mock.patch.object(confirmation, "SLICE_S_PER_PRINT_S", 1.0), mock.patch.object(slicer, "runner", supported_runner([])):
            result = confirmation.confirm_plans(self.pid, record, problem, plans, budget_s=20)
        self.assertEqual({r["kind"] for r in self.log()["slices"]}, {"reduced"})
        self.assertTrue(all(p.partly_estimated for p in result.plans[0].plates))   # no time from a reduced slice
        self.assertTrue(all(p.support_measured for p in result.plans[0].plates))

    def test_a_slicer_that_fails_leaves_the_plan_on_proxy_figures(self):
        record, problem, plans = self.plans()
        with mock.patch.object(slicer, "runner", fake_runner(code=1)):
            result = confirmation.confirm_plans(self.pid, record, problem, plans, budget_s=20)
        self.assertTrue(result.plans[0].partly_estimated)
        self.assertEqual(self.log()["slices"], [])
        self.assertGreaterEqual(result.stats["failed"], 1)


if __name__ == "__main__":
    unittest.main()
