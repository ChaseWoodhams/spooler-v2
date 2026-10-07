"""Plan plates (orient, pack across plates, order longest first) and accepting a plan, through the project HTTP API."""
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
import projects
import slicer
import splits
import state
from tests.test_slicer import cube_stl, fake_runner, make_fake_orca

GEOMETRY = not splits.missing_modules()
if GEOMETRY:
    from tests.test_orient import box, mushroom, stl_of
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
GREY = {"preset": "Elegoo PETG @ECC", "colour": "grey"}


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class PlanPlatesApiTests(unittest.TestCase):
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
        patcher = mock.patch("http_handler._auth_ok", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SPHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        slicer.configure()
        splits.configure()
        projects.configure()
        self._tmp.cleanup()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with _opener.open(request, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def settle(self):
        async def work():
            with mock.patch.object(slicer, "runner", fake_runner()):
                while await projects.process_next():
                    pass
        asyncio.run(work())

    def project_with(self, *stls):
        project = projects.create_project("Plan")
        for n, data in enumerate(stls, start=1):
            projects.import_model(project["id"], io.BytesIO(data), len(data), f"part{n}.stl")
        self.settle()
        return projects.get_project(project["id"])

    def save(self, project, change):
        data = copy.deepcopy(project["data"])
        change(data)
        status, saved = self.call("PATCH", f"/api/projects/{project['id']}", {"data": data, "revision": project["revision"]})
        self.assertEqual(status, 200, saved)
        return saved

    def lay(self, project, placements, filaments=None):
        """Objects as [(model, plate, (x, y), pinned)] from id 1; plates are made as needed."""
        def change(data):
            templates = {o["model"]: o for o in data["objects"]}
            data["objects"] = [{**templates[model], "id": i, "plate": plate, "position": list(at), "name": f"Object {i}",
                                **({"pinned": True} if pinned else {})}
                               for i, (model, plate, at, pinned) in enumerate(placements, start=1)]
            for plate in range(len(data["plates"]) + 1, max(p for _, p, _, _ in placements) + 1):
                data["plates"].append({"id": plate, "name": f"Plate {plate}"})
            if filaments:
                data["filaments"] = filaments
        return self.save(project, change)

    def plan(self, project):
        return self.call("POST", f"/api/projects/{project['id']}/plan", {})

    def accept(self, project, **body):
        """Accept the plan as it is now; pass fingerprint= to send the one of an earlier plan."""
        if "fingerprint" not in body:
            body["fingerprint"] = self.plan(project)[1]["fingerprint"]
        return self.call("POST", f"/api/projects/{project['id']}/plan/accept", {"revision": project["revision"], **body})

    def print_on_record(self, project, plate_id, outcome="completed"):
        with database.transaction() as conn:
            conn.execute("INSERT INTO prints (printer_id, printer_name, file_name, started_at, ended_at, outcome, project_id, plate_id) "
                         "VALUES ('p1', 'P1', 'x.gcode', 1, 2, ?, ?, ?)", (outcome, project["id"], plate_id))

    def queued_job(self, project, plate_id):
        now = time.time()
        with database.transaction() as conn:
            conn.execute("INSERT INTO jobs (name, state, source, file_name, created_at, updated_at, project_id, plate_id) "
                         "VALUES ('x', 'queued', 'upload', 'x.gcode', ?, ?, ?, ?)", (now, now, project["id"], plate_id))

    def test_a_plan_proposes_plates_and_changes_nothing(self):
        project = self.project_with(cube_stl(20))
        project = self.lay(project, [(1, 1, (50, 50), False), (1, 2, (50, 50), False), (1, 3, (50, 50), False)])
        status, plan = self.plan(project)
        self.assertEqual(status, 200, plan)
        self.assertEqual(plan["revision"], project["revision"])
        self.assertEqual(len(plan["plates"]), 1)
        first = plan["plates"][0]
        self.assertEqual((first["name"], first["parts"], first["turned"]), ("Plate 1", 3, 0))
        self.assertEqual(sorted(o["id"] for o in first["objects"]), [1, 2, 3])
        self.assertTrue(first["time_estimate"]["approximate"])
        self.assertGreater(first["time_estimate"]["seconds"], 0)
        self.assertEqual(plan["kept"], [])
        self.assertIn("1 plate", plan["summary"])
        after = self.call("GET", f"/api/projects/{project['id']}")[1]
        self.assertEqual((after["revision"], after["data"]), (project["revision"], project["data"]))

    def test_plates_never_mix_filaments_and_the_longest_comes_first(self):
        project = self.project_with(cube_stl(10), cube_stl(40))
        project = self.lay(project, [(1, 1, (50, 50), False), (2, 2, (50, 50), False), (1, 1, (90, 50), False)],
                           filaments={"2": GREY})
        status, plan = self.plan(project)
        self.assertEqual(status, 200, plan)
        self.assertEqual([(p["name"], p["filament"], sorted(o["id"] for o in p["objects"])) for p in plan["plates"]],
                         [("Plate 1", "Elegoo PETG grey", [2]), ("Plate 2", "the project default", [1, 3])])
        status, result = self.accept(project)
        self.assertEqual(status, 200, result)
        objects = {o["id"]: o["plate"] for o in result["data"]["objects"]}
        by_name = {p["id"]: p["name"] for p in result["data"]["plates"]}
        self.assertEqual([by_name[objects[i]] for i in (2, 1, 3)], ["Plate 1", "Plate 2", "Plate 2"])
        self.assertEqual([p["name"] for p in result["data"]["plates"]], ["Plate 1", "Plate 2"])

    def test_accepting_applies_the_plan_as_one_change_and_a_stale_revision_is_refused(self):
        project = self.project_with(cube_stl(20))
        project = self.lay(project, [(1, 1, (50, 50), False), (1, 2, (50, 50), False), (1, 3, (50, 50), False)])
        stale = {**project, "revision": project["revision"] - 1}
        status, refused = self.accept(stale)
        self.assertEqual(status, 409)
        self.assertIn("changed elsewhere", refused["error"])
        self.assertEqual(self.call("GET", f"/api/projects/{project['id']}")[1]["revision"], project["revision"])
        status, result = self.accept(project)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["revision"], project["revision"] + 1)      # one change, so the client records one undo step
        self.assertEqual([p["id"] for p in result["data"]["plates"]], [1])
        self.assertEqual({o["plate"] for o in result["data"]["objects"]}, {1})
        self.assertEqual(result["plan"]["plates"][0]["parts"], 3)
        self.assertEqual(self.call("GET", f"/api/projects/{project['id']}")[1]["data"], result["data"])
        self.assertEqual(self.accept(project)[0], 409)                     # the same revision twice: now stale

    def test_a_plan_can_be_sent_back_instead_of_a_revision(self):
        project = self.project_with(cube_stl(20))
        project = self.lay(project, [(1, 1, (50, 50), False), (1, 2, (50, 50), False)])
        plan = self.plan(project)[1]
        status, result = self.call("POST", f"/api/projects/{project['id']}/plan/accept", {"plan": plan})
        self.assertEqual(status, 200, result)
        self.assertEqual([p["id"] for p in result["data"]["plates"]], [1])
        self.assertEqual(self.call("POST", f"/api/projects/{project['id']}/plan/accept", {})[0], 400)

    def test_turned_counts_only_parts_stood_on_another_side_not_turns_about_z(self):
        project = self.project_with(mushroom(), stl_of(box(150, 10, 5)))
        def change(data):
            data["objects"][0]["rotation"] = [180.0, 0.0, 0.0]    # standing on its post: orienting lays it back on its disc
            data["objects"][1]["rotation"] = [0.0, 0.0, 45.0]     # a diagonal bar: the nester squares it up, a turn about Z only
        project = self.save(project, change)
        status, plan = self.plan(project)
        self.assertEqual(status, 200, plan)
        self.assertEqual([p["turned"] for p in plan["plates"]], [1])
        self.assertIn("1 turned", plan["summary"])
        result = self.accept(project)[1]
        bar = next(o for o in result["data"]["objects"] if o["id"] == 2)
        self.assertNotAlmostEqual(bar["rotation"][2] % 360, 45.0, places=0)   # it was turned about Z, and not counted

    def test_a_replanned_plate_whose_objects_changed_loses_its_printer_and_the_others_keep_theirs(self):
        project = self.project_with(cube_stl(20), cube_stl(30))
        with mock.patch.dict(state.printers, {"p1": object()}):
            project = self.lay(project, [(1, 1, (50, 50), False), (2, 2, (50, 50), False), (1, 3, (50, 50), False), (1, 4, (50, 50), False)],
                               filaments={"2": GREY})
            project = self.save(project, lambda d: [p.update(printer_id="p1") for p in d["plates"]])
        self.assertEqual([p["printer_id"] for p in project["data"]["plates"]], ["p1"] * 4)
        self.print_on_record(project, 3)
        result = self.accept(project)[1]
        plates = {p["id"]: p for p in result["data"]["plates"]}
        self.assertEqual(sorted(plates), [1, 2, 3])                            # plate 4 was folded into plate 1
        self.assertEqual({o["plate"] for o in result["data"]["objects"] if o["id"] in (1, 4)}, {1})
        self.assertIsNone(plates[1].get("printer_id"))                          # it now holds another object
        self.assertEqual(plates[2]["printer_id"], "p1")                         # replanned onto itself: same objects
        self.assertEqual(plates[3]["printer_id"], "p1")                         # kept as it is

    def test_a_plan_that_changed_since_it_was_reviewed_is_refused(self):
        project = self.project_with(cube_stl(20))
        project = self.lay(project, [(1, n, (50 + n, 60), False) for n in range(1, 4)])
        reviewed = self.plan(project)[1]
        self.assertEqual(len(reviewed["plates"]), 1)
        self.print_on_record(project, 3)             # no revision bump, but the plan the server would make now is different
        status, refused = self.accept(project, fingerprint=reviewed["fingerprint"])
        self.assertEqual(status, 409, refused)
        self.assertEqual(refused["error"], "The plan changed, review it again")
        self.assertEqual(self.call("GET", f"/api/projects/{project['id']}")[1]["revision"], project["revision"])
        fresh = self.plan(project)[1]
        self.assertNotEqual(fresh["fingerprint"], reviewed["fingerprint"])
        self.assertEqual(self.accept(project, fingerprint=fresh["fingerprint"])[0], 200)

    def test_accepting_needs_the_fingerprint_of_the_plan_reviewed(self):
        project = self.project_with(cube_stl(20))
        project = self.lay(project, [(1, 1, (50, 50), False), (1, 2, (50, 50), False)])
        status, refused = self.call("POST", f"/api/projects/{project['id']}/plan/accept", {"revision": project["revision"]})
        self.assertEqual(status, 400, refused)
        self.assertEqual(self.call("GET", f"/api/projects/{project['id']}")[1]["revision"], project["revision"])

    def test_replanned_plates_slices_become_outdated(self):
        project = self.project_with(cube_stl(20))
        project = self.lay(project, [(1, 1, (50, 50), False), (1, 2, (50, 50), False)])
        projects.request_slices(project["id"], [1])
        self.settle()
        sliced = projects.get_project(project["id"])
        self.assertEqual((sliced["slices"]["1"]["state"], sliced["slices"]["1"]["outdated"]), ("sliced", False))
        status, result = self.accept(sliced)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.call("GET", f"/api/projects/{project['id']}")[1]["slices"]["1"]["outdated"], True)

    def test_plates_with_a_print_on_record_or_a_queued_job_are_kept_as_they_are(self):
        project = self.project_with(cube_stl(20))
        project = self.lay(project, [(1, n, (50 + n, 60), False) for n in range(1, 6)])   # like SEAT: one part per plate
        self.print_on_record(project, 3)
        self.queued_job(project, 5)
        status, plan = self.plan(project)
        self.assertEqual(status, 200, plan)
        self.assertEqual([(k["id"], k["name"]) for k in plan["kept"]], [(3, "Plate 3"), (5, "Plate 5")])
        self.assertTrue(all(k["reason"] for k in plan["kept"]))
        self.assertEqual(len(plan["plates"]), 1)
        self.assertEqual(sorted(o["id"] for o in plan["plates"][0]["objects"]), [1, 2, 4])
        status, result = self.accept(project)
        self.assertEqual(status, 200, result)
        objects = {o["id"]: o for o in result["data"]["objects"]}
        for kept in (3, 5):
            self.assertEqual((objects[kept]["plate"], objects[kept]["position"]), (kept, [50.0 + kept, 60.0]))
        self.assertEqual({objects[i]["plate"] for i in (1, 2, 4)}, {1})
        self.assertEqual([p["name"] for p in result["data"]["plates"]], ["Plate 1", "Plate 3", "Plate 5"])

    def test_a_plate_whose_print_failed_is_kept_too(self):
        project = self.project_with(cube_stl(20))
        project = self.lay(project, [(1, 1, (50, 50), False), (1, 2, (50, 50), False)])
        self.print_on_record(project, 2, "failed")
        plan = self.plan(project)[1]
        self.assertEqual([k["id"] for k in plan["kept"]], [2])

    def test_a_pinned_object_keeps_its_plate_and_place_and_others_pack_around_it(self):
        project = self.project_with(cube_stl(20))
        project = self.lay(project, [(1, 1, (60, 60), False), (1, 2, (140, 140), True), (1, 3, (30, 200), False)])
        status, result = self.accept(project)
        self.assertEqual(status, 200, result)
        objects = {o["id"]: o for o in result["data"]["objects"]}
        self.assertEqual((objects[2]["position"], objects[2]["rotation"]), ([140.0, 140.0], [0.0, 0.0, 0.0]))
        self.assertEqual(objects[1]["plate"], objects[2]["plate"])
        self.assertEqual(objects[3]["plate"], objects[2]["plate"])
        self.assertEqual(len(result["data"]["plates"]), 1)

    def test_add_to_queue_sends_plates_in_plan_order(self):
        project = self.project_with(cube_stl(10), cube_stl(40))
        project = self.lay(project, [(1, 1, (50, 50), False), (2, 2, (50, 50), False)])
        project = self.save(project, lambda d: d["plates"][1].update(overrides={"layer_height": "0.28"}))   # keeps them on separate plates
        self.assertEqual(self.accept(project)[0], 200)
        projects.request_slices(project["id"])
        self.settle()
        status, sent = self.call("POST", f"/api/projects/{project['id']}/queue", {})
        self.assertEqual(status, 200, sent)
        record = projects.get_project(project["id"])
        names = {p["id"]: p["name"] for p in record["data"]["plates"]}
        models = {o["plate"]: o["model"] for o in record["data"]["objects"]}
        self.assertEqual([(names[j["plate_id"]], models[j["plate_id"]]) for j in sent["jobs"]], [("Plate 1", 2), ("Plate 2", 1)])


if __name__ == "__main__":
    unittest.main()
