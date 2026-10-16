"""The planner on a small fixture Project (auto-plating ticket 16): what it leaves alone, what it never chooses."""
import asyncio
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import calibration
import database
import jobs
import planner
import projects
import slicer
import splits
from tests.test_slicer import cube_stl, fake_runner, make_fake_orca

GEOMETRY = not splits.missing_modules()
if GEOMETRY:
    from tests.test_orient import box, stl_of

PRINTERS = {"dunt": 0.7, "og": 0.4}
FAST = dict(budget_s=20, max_steps=40)


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class PlannerProjectTests(unittest.TestCase):
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

    def tearDown(self):
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        slicer.configure()
        splits.configure()
        projects.configure()
        self._tmp.cleanup()

    def project_with(self, *stls, placements):
        """placements: [(model number from 1, plate, pinned)]; objects are numbered from 1."""
        project = projects.create_project("Plan")
        for n, data in enumerate(stls, start=1):
            projects.import_model(project["id"], io.BytesIO(data), len(data), f"part{n}.stl")

        async def work():
            with mock.patch.object(slicer, "runner", fake_runner()):
                while await projects.process_next():
                    pass
        asyncio.run(work())

        def change(data):
            template = {o["model"]: o for o in data["objects"]}
            data["objects"] = [{**template[model], "id": i, "plate": plate, "position": [30.0 + 20 * i, 50.0], "name": f"Object {i}",
                                **({"pinned": True} if pinned else {})} for i, (model, plate, pinned) in enumerate(placements, start=1)]
            data["plates"] = [{"id": p, "name": f"Plate {p}"} for p in range(1, max(p for _, p, _ in placements) + 1)]
        projects._mutate(project["id"], "data", change)
        return project["id"]

    def plan(self, pid, **kw):
        record = projects.get_project(pid)
        kw = {**FAST, **kw}
        return planner.plan_project(pid, record, plan_nozzle=0.7, printers=PRINTERS, models=calibration.fit_models(), **kw)

    @staticmethod
    def keys(plan):
        return sorted(i.key for p in plan.plates for i in p.items)

    def test_plans_every_unpinned_object_on_plates_of_the_printer_with_the_nozzle(self):
        pid = self.project_with(cube_stl(20), cube_stl(30), placements=[(1, 1, False), (2, 2, False), (1, 3, False), (2, 3, False)])
        plans = self.plan(pid)
        self.assertTrue(plans)
        self.assertEqual(self.keys(plans[0]), [1, 2, 3, 4])
        self.assertEqual({p.printer for p in plans[0].plates}, {"dunt"})       # only Dunt has the 0.7 nozzle
        self.assertEqual(plans, sorted(plans, key=lambda p: p.cost))

    def test_no_avoidable_tippy_orientation_is_chosen(self):
        pid = self.project_with(stl_of(box(10, 10, 150)), placements=[(1, 1, False)])
        plan = self.plan(pid)[0]
        self.assertEqual(plan.terms["unavoidable_tippy"], 0)
        rotation = plan.plates[0].items[0].rotation          # lying down: the 150 mm axis is no longer the vertical one
        import orientation
        self.assertLess(abs(orientation.rotation_matrix(rotation)[2][2]), 0.99)

    def test_pinned_and_kept_plates_are_untouched(self):
        pid = self.project_with(cube_stl(20), placements=[(1, 1, True), (1, 1, False), (1, 2, False), (1, 3, False)])
        now = time.time()
        with database.transaction() as conn:   # plate 3 is queued
            conn.execute("INSERT INTO jobs (name, state, source, file_name, created_at, updated_at, project_id, plate_id) "
                         "VALUES ('x', 'queued', 'upload', 'x.gcode', ?, ?, ?, 3)", (now, now, pid))
        plan = self.plan(pid)[0]
        self.assertEqual(self.keys(plan), [2, 3])             # the pinned cube (1) and the queued plate's (4) are not planned
        self.assertEqual([p.anchor for p in plan.plates if p.anchor is not None], [1])
        self.assertIn(2, [i.key for p in plan.plates if p.anchor == 1 for i in p.items])   # it joined the pinned cube's plate

    def test_filament_choices_never_share_a_plate(self):
        pid = self.project_with(cube_stl(20), cube_stl(25), placements=[(1, 1, False), (2, 2, False)])
        projects._mutate(pid, "data", lambda d: d.update(filaments={"2": {"preset": "Elegoo PETG @ECC", "colour": "grey"}}))
        plan = self.plan(pid)[0]
        self.assertEqual(len(plan.plates), 2)

    def test_same_project_same_plans(self):
        pid = self.project_with(cube_stl(20), cube_stl(35), cube_stl(50), placements=[(1, 1, False), (2, 1, False), (3, 2, False), (1, 2, False), (2, 3, False)])
        first = [p.as_dict() for p in self.plan(pid, budget_s=60, max_steps=30)]
        again = [p.as_dict() for p in self.plan(pid, budget_s=60, max_steps=30)]
        self.assertEqual(first, again)

    def test_greedy_seed_alone_plans_everything(self):
        pid = self.project_with(cube_stl(20), placements=[(1, 1, False), (1, 2, False)])
        seed = planner.greedy_seed_project(pid, projects.get_project(pid), plan_nozzle=0.7, printers=PRINTERS)
        self.assertEqual(self.keys(seed), [1, 2])

    def test_no_printer_with_the_plan_nozzle_leaves_objects_unplanned(self):
        pid = self.project_with(cube_stl(20), placements=[(1, 1, False)])
        plan = planner.plan_project(pid, projects.get_project(pid), plan_nozzle=0.7, printers={"dunt": None, "og": 0.4}, **FAST)[0]
        self.assertEqual(self.keys(plan), [])
        self.assertEqual([k for k, _ in plan.unplanned], [1])

    def test_only_the_named_objects_are_planned_and_the_others_stay_as_obstacles(self):
        pid = self.project_with(cube_stl(20), placements=[(1, 1, False), (1, 1, False), (1, 2, False)])
        record = projects.get_project(pid)
        problem = planner.problem_from_project(pid, record, plan_nozzle=0.7, printers=PRINTERS, only={3})
        self.assertEqual([i.key for i in problem.items], [3])
        self.assertEqual(sorted(p.key for p in problem.pinned), [1, 2])      # fixed where they stand
        self.assertEqual(set(problem.anchors), {1})                           # plate 2 holds only the new object: not an anchor

    def test_meshes_given_stand_in_for_a_model_still_importing(self):
        import trimesh
        import numpy as np
        pid = self.project_with(cube_stl(20), placements=[(1, 1, False)])
        data = cube_stl(25)
        projects.import_model(pid, io.BytesIO(data), len(data), "late.stl")      # not analysed: still importing
        record = projects.get_project(pid)
        late = record["models"][-1]
        self.assertNotEqual(late["state"], "ready")
        box_mesh = trimesh.creation.box((25, 25, 25))
        mesh = (np.asarray(box_mesh.vertices, float), np.asarray(box_mesh.faces))
        record = projects.get_project(pid)
        without = planner.problem_from_project(pid, record, plan_nozzle=0.7, printers=PRINTERS, only={2})
        self.assertEqual([i.key for i in without.items], [])
        problem = planner.problem_from_project(pid, record, plan_nozzle=0.7, printers=PRINTERS, only={2},
                                               meshes={late["id"]: {"full": mesh, "preview": mesh}})
        self.assertEqual([i.key for i in problem.items], [2])


if __name__ == "__main__":
    unittest.main()
