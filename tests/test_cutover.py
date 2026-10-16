"""The entry points on the planner (auto-plating ticket 23): "Lay out on plates", "Open as project" and adding another Split's Parts
give a layout at once (the planner's greedy first-fit seed) and start a planner run over the whole Project; Quick print, Copies
and auto place use `nest` alone, keep the user's orientation and start no run. Saved Projects open unchanged.

OrcaSlicer is a fake (`tests.test_slicer.fake_runner`); the splits are real cuts of box models.
"""
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import database
import jobs
import placement_read
import plan_job
import preflight
import projects
import slicer
import splits
import state
from tests.test_jobs import FakePrinter
from tests.test_project_workflow import GEOMETRY, box_stl
from tests.test_slicer import cube_stl, fake_runner, make_fake_orca

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class CutoverTests(unittest.IsolatedAsyncioTestCase):
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
        self._patches = [
            mock.patch.object(preflight, "HARDWARE_FILE", self.dir / "printer_hardware.json"),
            mock.patch.object(state, "printers", {"p1": FakePrinter("p1", "Printer 1"), "p2": FakePrinter("p2", "Printer 2")}),
            mock.patch.object(plan_job, "SEARCH_SECONDS", 3.0), mock.patch.object(plan_job, "MAX_STEPS", 20),
            mock.patch.object(plan_job, "CONFIRM_BUDGET_S", 20.0),
            mock.patch.object(slicer, "runner", fake_runner()),
        ]
        for patch in self._patches:
            patch.start()
        preflight.set_printer_hardware("p1", 0.4)
        preflight.set_printer_hardware("p2", 0.4)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SPHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        plan_job.wait_idle(90)
        for patch in reversed(self._patches):
            patch.stop()
        plan_job.reset()
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        slicer.configure()
        splits.configure()
        projects.configure()
        self._tmp.cleanup()

    # -- helpers ----------------------------------------------------------------------------------------------------------

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with _opener.open(request, timeout=90) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    async def settle(self):
        while await splits.process_next() or await projects.process_next():
            pass

    async def split_project(self, size=(400, 40, 20), name="beam.stl"):
        data = box_stl(self.dir / name, size)
        project = projects.create_project_from_upload(io.BytesIO(data), len(data), name)
        await self.settle()
        splits.request_plan(project["split_id"], {})
        await self.settle()
        splits.request_split(project["split_id"])
        await self.settle()
        return projects.get_project(project["id"])

    def finish(self, project_id, job, until=("done", "failed", "cancelled")):
        deadline = time.monotonic() + 120
        while job["state"] not in until and time.monotonic() < deadline:
            time.sleep(0.1)
            status, job = self.call("GET", f"/api/projects/{project_id}/plan/jobs/{job['id']}")
            self.assertEqual(status, 200, job)
        return job

    def problems(self, project_id):
        return placement_read.placement_problems(project_id, projects.get_project(project_id))

    # -- Lay out on plates --------------------------------------------------------------------------------------------------

    async def test_lay_out_gives_a_valid_layout_at_once_and_starts_a_run_whose_proposal_follows(self):
        project = await self.split_project()
        parts = splits.get_split(project["split_id"], detail=True)["result"]["parts"]
        status, laid = self.call("POST", f"/api/projects/{project['id']}/layout", {})
        self.assertEqual(status, 200, laid)
        self.assertEqual(len(laid["data"]["objects"]), len(parts))
        self.assertEqual([m["split"]["part"] for m in laid["models"]], [p["number"] for p in parts])
        self.assertTrue(all(o["plate"] in {p["id"] for p in laid["data"]["plates"]} for o in laid["data"]["objects"]))
        self.assertEqual(len({tuple(o["position"]) for o in laid["data"]["objects"]}), len(parts))   # no longer stacked on one spot
        job = laid["plan_job"]
        await self.settle()
        self.assertEqual(self.problems(project["id"]), [])                                           # a usable layout
        revision = projects.get_project(project["id"])["revision"]
        done = self.finish(project["id"], job)
        self.assertEqual(done["state"], "done", done)
        self.assertTrue(done["proposal"]["plans"])                                                     # a Proposal when the run finishes
        self.assertEqual(done["proposal"]["before"]["plates"], len(laid["data"]["plates"]))             # it was made over the laid-out Project
        self.assertEqual(projects.get_project(project["id"])["revision"], revision)                    # nothing moves until it is accepted

    async def test_laying_out_again_replaces_the_parts_and_still_starts_one_run(self):
        project = await self.split_project()
        parts = splits.get_split(project["split_id"], detail=True)["result"]["parts"]
        first = projects.lay_out_parts(project["id"])
        self.assertTrue(first["plan_job"]["id"])
        again = projects.lay_out_parts(project["id"])
        self.assertEqual((len(again["models"]), len(again["data"]["objects"])), (len(parts), len(parts)))
        self.assertNotEqual(again["plan_job"]["id"], first["plan_job"]["id"])
        await self.settle()                                  # the newer run goes on once the Parts are analysed; the older one is replaced
        self.assertEqual(self.finish(project["id"], again["plan_job"])["state"], "done")
        self.assertEqual(self.finish(project["id"], first["plan_job"], until=("cancelled", "done"))["state"], "cancelled")

    async def test_adding_another_splits_parts_leaves_the_existing_objects_where_they_are(self):
        project = await self.split_project()
        other = await self.split_project(size=(300, 50, 20), name="rail.stl")
        first = projects.lay_out_parts(project["id"])
        await self.settle()
        stands = lambda p: {o["id"]: (o["plate"], o["position"], o["rotation"]) for o in p["data"]["objects"]}
        before = stands(projects.get_project(project["id"]))
        parts = splits.get_split(other["split_id"], detail=True)["result"]["parts"]
        status, added = self.call("POST", f"/api/projects/{project['id']}/models/from-split", {"split_id": other["split_id"]})
        self.assertEqual(status, 200, added)
        now = stands(added)
        self.assertEqual({k: v for k, v in now.items() if k in before}, before)                         # nothing already laid out moved
        self.assertEqual(len(now), len(before) + len(parts))
        self.assertTrue(added["plan_job"]["id"])                                                         # the run covers the whole Project
        await self.settle()
        self.assertEqual(self.problems(project["id"]), [])
        done = self.finish(project["id"], added["plan_job"])
        planned = {o["id"] for p in done["proposal"]["plans"][0]["plate_rows"] for o in p["objects"]}
        self.assertEqual(planned, set(now))
        self.assertEqual(first["plan_job"]["project_id"], project["id"])

    async def test_whole_model_copies_nest_without_reorienting_and_start_no_run(self):
        data = box_stl(self.dir / "slab.stl", (30, 50, 10))
        project = projects.create_project_from_upload(io.BytesIO(data), len(data), "slab.stl")
        await self.settle()
        split_id = project["split_id"]
        projects._mutate(project["id"], "data", lambda d: d.update(orientations={str(split_id): [90.0, 0.0, 0.0]},
                                                                    split_copies={str(split_id): 4}))
        with mock.patch.object(plan_job, "start", side_effect=AssertionError("a run was started")):
            placed = projects.place_whole_model(project["id"], split_id)
            await self.settle()
        objects = placed["data"]["objects"]
        self.assertEqual(len(objects), 4)
        self.assertEqual({tuple(o["rotation"][:2]) for o in objects}, {(90.0, 0.0)})                      # stood on its side, as chosen
        self.assertEqual(len({o["model"] for o in objects}), 1)
        self.assertEqual(self.problems(project["id"]), [])

    async def test_auto_place_keeps_the_orientation_and_starts_no_run(self):
        data = cube_stl(30)
        project = projects.create_project_from_upload(io.BytesIO(data), len(data), "cube.stl", quick=True)
        await self.settle()
        with mock.patch.object(plan_job, "start", side_effect=AssertionError("a run was started")):
            result = projects.auto_place(project["id"])
        self.assertTrue(result["placed"])
        self.assertEqual([o["rotation"] for o in result["project"]["data"]["objects"]], [[0.0, 0.0, 0.0]])

    async def test_a_project_saved_before_the_planner_opens_unchanged(self):
        project = await self.split_project()
        projects.lay_out_parts(project["id"])
        await self.settle()
        plan_job.wait_idle(60)
        saved = projects.get_project(project["id"])
        projects.init()                                  # a restart: nothing may move, whichever path placed the objects
        status, opened = self.call("GET", f"/api/projects/{project['id']}")
        self.assertEqual(status, 200)
        self.assertEqual((opened["data"], opened["revision"]), (saved["data"], saved["revision"]))
