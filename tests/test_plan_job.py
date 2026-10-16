"""The plate planner as a background job: start, progress, a stored Proposal, Accept (auto-plating ticket 18), through the HTTP API.

OrcaSlicer is a fake (`tests.test_slicer.fake_runner`); the printers and their installed nozzles are made up per test.
"""
import asyncio
import copy
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
import plan_job
import preflight
import projects
import slicer
import splits
import state
from tests.test_slicer import cube_stl, fake_runner, make_fake_orca

GEOMETRY = not splits.missing_modules()
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
GREY = {"preset": "Elegoo PETG @ECC", "colour": "grey"}

if GEOMETRY:
    import trimesh

    def ball_stl() -> bytes:
        return trimesh.creation.icosphere(subdivisions=3, radius=20).export(file_type="stl")


class FakePrinter:
    def __init__(self, printer_id, name):
        self.id, self.name = printer_id, name


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class PlanJobApiTests(unittest.TestCase):
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
            mock.patch.object(state, "printers", {"dunt": FakePrinter("dunt", "Dunt"), "og": FakePrinter("og", "OG")}),
            mock.patch.object(plan_job, "SEARCH_SECONDS", 10.0), mock.patch.object(plan_job, "MAX_STEPS", 40),
            mock.patch.object(plan_job, "CONFIRM_BUDGET_S", 30.0),
            mock.patch.object(slicer, "runner", fake_runner()),
        ]
        for patch in self._patches:
            patch.start()
        preflight.set_printer_hardware("dunt", 0.4)
        preflight.set_printer_hardware("og", 0.4)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SPHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        plan_job.wait_idle(60)
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
            with _opener.open(request, timeout=60) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def project_with(self, *stls):
        project = projects.create_project("Plan job")
        for n, data in enumerate(stls, start=1):
            projects.import_model(project["id"], io.BytesIO(data), len(data), f"part{n}.stl")

        async def work():
            while await projects.process_next():
                pass
        asyncio.run(work())
        record = projects.get_project(project["id"])
        projects._mutate(project["id"], "data", lambda d: d["profile"].update(bundle_id=slicer.list_bundles()[0]["id"]))
        return projects.get_project(record["id"])

    def lay(self, project, placements, filaments=None):
        """Objects as [(model, plate, pinned)] from id 1, side by side; plates are made as needed."""
        def change(data):
            templates = {o["model"]: o for o in data["objects"]}
            data["objects"] = [{**copy.deepcopy(templates[model]), "id": i, "plate": plate, "position": [30.0 + 22 * i, 50.0],
                                "name": f"Object {i}", **({"pinned": True} if pinned else {})}
                               for i, (model, plate, pinned) in enumerate(placements, start=1)]
            data["plates"] = [{"id": p, "name": f"Plate {p}"} for p in range(1, max(p for _, p, _ in placements) + 1)]
            if filaments:
                data["filaments"] = filaments
        projects._mutate(project["id"], "data", change)
        return projects.get_project(project["id"])

    def plan(self, project, wait=True):
        """Start a plan; with `wait`, the finished job (state done)."""
        status, job = self.call("POST", f"/api/projects/{project['id']}/plan", {})
        self.assertEqual(status, 200, job)
        return self.finish(project, job) if wait else job

    def finish(self, project, job, until=("done", "failed", "cancelled")):
        deadline = time.monotonic() + 90
        while job["state"] not in until and time.monotonic() < deadline:
            time.sleep(0.05)
            status, job = self.call("GET", f"/api/projects/{project['id']}/plan/jobs/{job['id']}")
            self.assertEqual(status, 200, job)
        return job

    def accept(self, project, job, plan=0, **body):
        return self.call("POST", f"/api/projects/{project['id']}/plan/accept",
                         {"proposal": job["proposal"]["token"], "plan": plan, **body})

    def current(self, project):
        return self.call("GET", f"/api/projects/{project['id']}")[1]

    def scattered(self):
        """Four small cubes, one per plate: a plan folds them onto fewer plates."""
        project = self.project_with(cube_stl(20), cube_stl(30))
        return self.lay(project, [(1, 1, False), (2, 2, False), (1, 3, False), (2, 4, False)])

    # -- starting a plan --------------------------------------------------------------------------------------------------

    def test_starting_a_plan_returns_a_job_changes_nothing_and_reports_progress(self):
        project = self.scattered()
        started = self.plan(project, wait=False)
        self.assertIsInstance(started["id"], int)
        self.assertEqual((started["project_id"], started["revision"]), (project["id"], project["revision"]))
        job = self.finish(project, started)
        self.assertEqual(job["state"], "done", job)
        phases = [e["phase"] for e in job["events"]]
        self.assertEqual([p for i, p in enumerate(phases) if p not in phases[:i]], ["reading", "searching", "confirming", "done"])
        self.assertEqual(job["events"][-1]["percent"], 100)
        self.assertTrue(all(a["percent"] <= b["percent"] for a, b in zip(job["events"], job["events"][1:])))
        after = self.current(project)
        self.assertEqual((after["revision"], after["data"], after["slices"]), (project["revision"], project["data"], project["slices"]))
        self.assertTrue(job["proposal"]["token"])

    def test_the_proposal_holds_the_best_plan_and_up_to_two_also_considered(self):
        job = self.plan(self.scattered())
        proposal = job["proposal"]
        self.assertTrue(1 <= len(proposal["plans"]) <= 3)
        best = proposal["plans"][0]
        self.assertEqual(best["index"], 0)
        self.assertEqual(best["plates"], len(best["plate_rows"]))
        self.assertLess(best["plates"], 4)                                   # four cubes fit fewer plates
        row = best["plate_rows"][0]
        for key in ("name", "printer", "seconds", "support_g", "time_measured", "support_measured", "partly_estimated", "objects"):
            self.assertIn(key, row)
        self.assertEqual(sorted(o["id"] for r in best["plate_rows"] for o in r["objects"]), [1, 2, 3, 4])
        self.assertIn(row["printer"], ("dunt", "og"))
        self.assertEqual(proposal["before"]["plates"], 4)
        self.assertGreater(proposal["before"]["makespan_s"], 0)
        self.assertEqual([p["status"] for p in proposal["printers"]], ["ok", "ok"])


if __name__ == "__main__":
    unittest.main()
