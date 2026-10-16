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

import confirmation
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

    def test_the_proposal_carries_the_calibration_lines_for_the_partly_estimated_tooltip(self):
        proposal = self.plan(self.scattered())["proposal"]
        self.assertIsInstance(proposal["calibration"], list)
        self.assertTrue(all(isinstance(line, str) for line in proposal["calibration"]))

    def test_preview_is_the_instant_arrangement_and_changes_nothing(self):
        project = self.scattered()
        status, preview = self.call("POST", f"/api/projects/{project['id']}/plan/preview", {})
        self.assertEqual(status, 200, preview)
        self.assertIn("plates", preview)
        self.assertNotIn("proposal_token", preview)
        self.assertEqual(self.current(project)["revision"], project["revision"])

    def print_on_record(self, project, plate_id):
        with database.transaction() as conn:
            conn.execute("INSERT INTO prints (printer_id, printer_name, file_name, started_at, ended_at, outcome, project_id, plate_id) "
                         "VALUES ('dunt', 'Dunt', 'x.gcode', 1, 2, 'completed', ?, ?)", (project["id"], plate_id))

    def queued_job(self, project, plate_id):
        now = time.time()
        with database.transaction() as conn:
            conn.execute("INSERT INTO jobs (name, state, source, file_name, created_at, updated_at, project_id, plate_id) "
                         "VALUES ('x', 'queued', 'upload', 'x.gcode', ?, ?, ?, ?)", (now, now, project["id"], plate_id))

    # -- accept -----------------------------------------------------------------------------------------------------------

    def test_accept_applies_the_whole_plan_in_one_change_that_one_undo_reverses(self):
        project = self.scattered()
        job = self.plan(project)
        status, result = self.accept(project, job)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["revision"], project["revision"] + 1)
        best = job["proposal"]["plans"][0]
        self.assertEqual(len(result["data"]["plates"]), best["plates"])
        self.assertEqual({o["plate"] for o in result["data"]["objects"]}, {r["plate"] for r in best["plate_rows"]})
        self.assertEqual(self.current(project)["data"], result["data"])
        # the page's Undo saves the earlier data back over the new revision
        status, undone = self.call("PATCH", f"/api/projects/{project['id']}", {"data": project["data"], "revision": result["revision"]})
        self.assertEqual(status, 200, undone)
        self.assertEqual([(o["id"], o["plate"], o["position"]) for o in undone["data"]["objects"]],
                         [(o["id"], o["plate"], o["position"]) for o in project["data"]["objects"]])

    def slicing_never_object(self, bad):
        """Confirmation with a slicer that fails on any plate holding Object `bad` (OrcaSlicer's "Toolpaths conflict")."""
        real = confirmation.confirm_plans

        def slice_plate(job, timeout_s):
            if bad in job.keys:
                raise RuntimeError("Toolpaths conflict with each other")
            return confirmation.PlateMeasure(100 * len(job.keys), 0.0)
        return mock.patch.object(confirmation, "confirm_plans", lambda *a, **k: real(*a, **{**k, "slice_plate": slice_plate}))

    def test_a_plate_that_does_not_slice_is_flagged_and_accept_refuses_it_unless_forced(self):
        # four different cubes: Copies of one Model would share a cached slice, hiding which Object the fake slicer rejects
        project = self.lay(self.project_with(*(cube_stl(n) for n in (20, 25, 30, 35))), [(m, m, False) for m in (1, 2, 3, 4)])
        with self.slicing_never_object(3):
            job = self.plan(project)
        plan = job["proposal"]["plans"][0]
        self.assertEqual(job["state"], "done", job)
        self.assertFalse(plan["can_accept"])
        self.assertFalse(plan["recommended"])
        flagged = [r for r in plan["plate_rows"] if r["did_not_slice"]]
        self.assertEqual([[o["id"] for o in r["objects"]] for r in flagged], [[3]])      # replanned: Object 3 on a plate of its own
        self.assertEqual(plan["did_not_slice_plates"], [flagged[0]["name"]])
        self.assertIn("did not slice", job["proposal"]["summary"])
        status, refused = self.accept(project, job)
        self.assertEqual(status, 409, refused)
        self.assertEqual(self.current(project)["revision"], project["revision"])        # nothing changed
        status, forced = self.accept(project, job, allow_unsliced=True)
        self.assertEqual(status, 200, forced)

    def test_plates_that_slice_leave_the_plan_acceptable(self):
        plan = self.plan(self.scattered())["proposal"]["plans"][0]
        self.assertTrue(plan["can_accept"])
        self.assertTrue(plan["recommended"])
        self.assertEqual(plan["did_not_slice_plates"], [])

    def test_an_also_considered_plan_can_be_accepted_instead(self):
        project = self.scattered()
        job = self.plan(project)
        plans = job["proposal"]["plans"]
        self.assertGreater(len(plans), 1, "the fixture should give an alternative to choose")
        status, result = self.accept(project, job, plan=1)
        self.assertEqual(status, 200, result)
        self.assertEqual(len(result["data"]["plates"]), plans[1]["plates"])
        self.assertEqual(result["plan"]["index"], 1)
        self.assertEqual(self.accept(project, job, plan=0)[0], 404)    # the Proposal is used up

    def test_a_proposal_made_before_the_project_changed_is_superseded_and_refused(self):
        project = self.scattered()
        job = self.plan(project)
        self.assertFalse(job["proposal"]["superseded"])
        changed = self.call("PATCH", f"/api/projects/{project['id']}", {"data": {**project["data"], "bed": "smooth"},
                                                                       "revision": project["revision"]})[1]
        status, refused = self.accept(project, job)
        self.assertEqual(status, 409, refused)
        self.assertIn("changed elsewhere", refused["error"])
        self.assertEqual(self.current(project)["data"], changed["data"])
        self.assertTrue(self.call("GET", f"/api/projects/{project['id']}/plan/proposals/{job['proposal']['token']}")[1]["superseded"])

    def test_dismiss_changes_nothing_and_the_proposal_is_gone(self):
        project = self.scattered()
        job = self.plan(project)
        status, _ = self.call("POST", f"/api/projects/{project['id']}/plan/dismiss", {"proposal": job["proposal"]["token"]})
        self.assertEqual(status, 200)
        self.assertEqual(self.current(project)["data"], project["data"])
        self.assertEqual(self.accept(project, job)[0], 404)

    def test_pinned_objects_and_queued_or_printed_plates_are_untouched(self):
        project = self.project_with(cube_stl(20))
        project = self.lay(project, [(1, 1, True), (1, 1, False), (1, 2, False), (1, 3, False), (1, 4, False)])
        self.queued_job(project, 3)
        self.print_on_record(project, 4)
        job = self.plan(project)
        self.assertEqual(sorted(k["id"] for k in job["proposal"]["kept"]), [3, 4])
        result = self.accept(project, job)[1]
        before = {o["id"]: o for o in project["data"]["objects"]}
        for o in result["data"]["objects"]:
            if o["id"] in (1, 4, 5):    # the pinned cube, the queued plate's and the printed plate's
                self.assertEqual((o["plate"], o["position"], o["rotation"]),
                                 (before[o["id"]]["plate"], before[o["id"]]["position"], before[o["id"]]["rotation"]))
        self.assertEqual(sorted(p["id"] for p in result["data"]["plates"] if p["id"] in (3, 4)), [3, 4])

    def test_a_plate_queued_after_the_proposal_was_made_refuses_it(self):
        project = self.scattered()
        job = self.plan(project)
        self.queued_job(project, 2)
        status, refused = self.accept(project, job)
        self.assertEqual(status, 409, refused)
        self.assertEqual(self.current(project)["data"], project["data"])

    def test_replanned_plates_slices_become_outdated(self):
        project = self.scattered()
        projects.request_slices(project["id"], [2])

        async def work():
            while await projects.process_next():
                pass
        asyncio.run(work())
        project = projects.get_project(project["id"])
        self.assertFalse(project["slices"]["2"]["outdated"])
        job = self.plan(project)
        self.assertEqual(self.accept(project, job)[0], 200)
        slice_after = self.current(project)["slices"].get("2")
        self.assertTrue(slice_after is None or slice_after["outdated"])

    # -- printers ---------------------------------------------------------------------------------------------------------

    def test_a_printer_with_no_installed_nozzle_is_named_and_gets_no_plates(self):
        hardware = self.dir / "printer_hardware.json"
        data = json.loads(hardware.read_text())
        data.pop("og")
        hardware.write_text(json.dumps(data))
        project = self.scattered()
        job = self.plan(project)
        proposal = job["proposal"]
        og = next(p for p in proposal["printers"] if p["id"] == "og")
        self.assertEqual((og["status"], og["receives_plates"]), ("unset", False))
        self.assertIn("OG", og["message"])
        for plan in proposal["plans"]:
            self.assertEqual({r["printer"] for r in plan["plate_rows"]}, {"dunt"})
            self.assertEqual(plan["printers"]["og"]["plates"], [])
        result = self.accept(project, job)[1]
        self.assertEqual({p.get("printer_id") for p in result["data"]["plates"]}, {"dunt"})

    def test_a_printer_whose_nozzle_matches_nothing_is_named_too(self):
        preflight.set_printer_hardware("og", 0.6)
        job = self.plan(self.scattered())
        og = next(p for p in job["proposal"]["printers"] if p["id"] == "og")
        self.assertEqual((og["status"], og["receives_plates"]), ("mismatch", False))

    def test_no_printer_with_the_plan_nozzle_leaves_everything_unplanned(self):
        preflight.set_printer_hardware("dunt", 0.6)
        preflight.set_printer_hardware("og", 0.6)
        job = self.plan(self.scattered())
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(job["proposal"]["plans"], [])
        self.assertEqual(len(job["proposal"]["unplanned"]), 4)

    # -- tippy ------------------------------------------------------------------------------------------------------------

    def test_an_unavoidably_tippy_object_is_listed_with_its_reason_and_given_a_brim(self):
        project = self.project_with(ball_stl(), cube_stl(20))
        project = self.lay(project, [(1, 1, False), (2, 2, False)])
        job = self.plan(project)
        best = job["proposal"]["plans"][0]
        self.assertEqual([t["object"] for t in best["tippy_objects"]], [1])
        self.assertIn("steady", best["tippy_objects"][0]["reason"])
        self.assertEqual(best["tippy"], 1)
        result = self.accept(project, job)[1]
        objects = {o["id"]: o for o in result["data"]["objects"]}
        self.assertEqual(objects[1]["overrides"].get("brim_type"), "outer_only")
        self.assertNotIn("brim_type", objects[2].get("overrides") or {})
        self.assertEqual(self.call("PATCH", f"/api/projects/{project['id']}", {"data": result["data"], "revision": result["revision"]})[0], 200)


if __name__ == "__main__":
    unittest.main()
