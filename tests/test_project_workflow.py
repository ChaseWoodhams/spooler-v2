"""The project workflow: a model upload becomes a project with a split, parts are packed onto plates, plates get their
own settings, every plate is sliced and sent to the queue as one batch, and the batch is spread over printers."""
import asyncio
import io
import json
import tempfile
import threading
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
from tests.test_jobs import GCODE, FakePrinter
from tests.test_slicer import cube_stl, fake_runner, make_fake_orca

GEOMETRY = not splits.missing_modules()
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def box_stl(path: Path, size) -> bytes:
    import trimesh
    trimesh.creation.box(size).export(path)
    return path.read_bytes()


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class WorkflowTests(unittest.IsolatedAsyncioTestCase):
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
        state.printers = {"p1": FakePrinter("p1", "Printer 1"), "p2": FakePrinter("p2", "Printer 2")}

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

    async def settle(self):
        """Run split stages and project imports/slices until nothing is left to do."""
        with mock.patch.object(slicer, "runner", fake_runner()):
            while await splits.process_next() or await projects.process_next():
                pass

    async def split_project(self, size=(400, 40, 20)):
        data = box_stl(self.dir / "beam.stl", size)
        project = projects.create_project_from_upload(io.BytesIO(data), len(data), "beam.stl")
        await self.settle()
        splits.request_split(project["split_id"])
        await self.settle()
        return projects.get_project(project["id"])

    def edit(self, project, **changes):
        data = json.loads(json.dumps(project["data"]))
        data.update(changes)
        return projects.update_project(project["id"], {"data": data, "revision": project["revision"]})

    async def test_an_upload_becomes_a_project_with_its_split(self):
        project = await self.split_project()
        split = splits.get_split(project["split_id"], detail=True)
        self.assertEqual((project["name"], split["state"]), ("beam", "split"))
        self.assertEqual(projects.list_projects()[0]["split_id"], split["id"])
        projects.delete_project(project["id"])
        with self.assertRaises(jobs.JobError):
            splits.get_split(split["id"])

    async def test_parts_are_packed_onto_as_few_plates_as_fit(self):
        project = await self.split_project()
        parts = splits.get_split(project["split_id"], detail=True)["result"]["parts"]
        self.assertGreater(len(parts), 1)
        laid = projects.lay_out_split(project["id"])
        self.assertEqual(len(laid["data"]["plates"]), 1)                     # small beam halves share the plate
        self.assertEqual(len(laid["data"]["objects"]), len(parts))
        self.assertEqual([m["split"]["part"] for m in laid["models"]], [p["number"] for p in parts])
        await self.settle()
        sliced = projects.request_slices(project["id"])
        await self.settle()
        sliced = projects.get_project(project["id"])
        self.assertEqual(sliced["slices"]["1"]["state"], "sliced", sliced["slices"]["1"].get("error"))  # the fit check passed

        again = projects.lay_out_split(project["id"])                        # laying out again replaces the parts
        self.assertEqual((len(again["models"]), len(again["data"]["objects"]), len(again["data"]["plates"])),
                         (len(parts), len(parts), 1))

    async def test_parts_needing_supports_get_their_own_plate_with_supports_on(self):
        project = await self.split_project()
        work = splits.work_dir(project["split_id"])
        result = json.loads((work / "result.json").read_text())
        result["parts"][0]["needs_supports"] = True
        (work / "result.json").write_text(json.dumps(result))
        laid = projects.lay_out_split(project["id"])
        plates = laid["data"]["plates"]
        self.assertEqual([p["overrides"] for p in plates], [{}, {"enable_support": "1", "support_type": "tree(auto)"}])
        supported = [o for o in laid["data"]["objects"] if o["plate"] == plates[1]["id"]]
        self.assertEqual([laid["models"][o["model"] - 1]["split"]["part"] for o in supported], [result["parts"][0]["number"]])

    async def test_a_model_that_fits_goes_on_a_plate_whole(self):
        data = cube_stl(30)
        project = projects.create_project_from_upload(io.BytesIO(data), len(data), "cube.stl")
        await self.settle()
        placed = projects.place_whole_model(project["id"])
        await self.settle()
        placed = projects.get_project(project["id"])
        self.assertEqual(([m["state"] for m in placed["models"]], [o["plate"] for o in placed["data"]["objects"]]), (["ready"], [1]))
        self.assertEqual(placed["models"][0]["split"], {"id": project["split_id"], "part": 0, "supports": False})

    async def test_plate_settings_sit_between_the_profile_and_the_object(self):
        project = await self.split_project()
        project = projects.lay_out_split(project["id"])
        await self.settle()
        project = projects.get_project(project["id"])
        profile = {**project["data"]["profile"], "process": {"wall_loops": "3", "sparse_infill_density": "10%"}}
        plates = [{**project["data"]["plates"][0], "overrides": {"wall_loops": 4, "brim_type": "no_brim"}}]
        objects = [{**o, "overrides": {"wall_loops": "6"}} if i == 0 else o for i, o in enumerate(project["data"]["objects"])]
        project = self.edit(project, profile=profile, plates=plates, objects=objects)
        self.assertEqual(project["data"]["plates"][0]["overrides"], {"brim_type": "no_brim", "wall_loops": "4"})
        with self.assertRaisesRegex(jobs.JobError, "isn't a process setting"):
            self.edit(project, plates=[{**plates[0], "overrides": {"not_a_setting": "1"}}])

        projects.request_slices(project["id"])
        await self.settle()
        project = projects.get_project(project["id"])
        process = json.loads((projects.plate_dir(project["id"], 1) / "profile" / "process.json").read_text())
        self.assertEqual((process["wall_loops"], process["sparse_infill_density"], process["brim_type"]), ("4", "10%", "no_brim"))
        changed = self.edit(project, plates=[{**project["data"]["plates"][0], "overrides": {"wall_loops": "5"}}])
        self.assertTrue(changed["slices"]["1"]["outdated"])

    async def test_merging_and_arranging_plates(self):
        project = projects.create_project("Cubes")
        for size in (20, 30, 40):
            data = cube_stl(size)
            projects.import_model(project["id"], io.BytesIO(data), len(data), f"cube{size}.stl")
        await self.settle()
        project = projects.get_project(project["id"])
        plates = [projects._new_plate(1), projects._new_plate(2), projects._new_plate(3)]
        objects = [{**o, "plate": i + 1} for i, o in enumerate(project["data"]["objects"])]
        project = self.edit(project, plates=plates, objects=objects)

        merged = projects.merge_plates(project["id"], 2, 1)
        self.assertEqual(([p["id"] for p in merged["data"]["plates"]], sorted(o["plate"] for o in merged["data"]["objects"])),
                         ([1, 3], [1, 1, 3]))
        packed = projects.arrange(merged["id"])
        self.assertEqual((len(packed["data"]["plates"]), {o["plate"] for o in packed["data"]["objects"]}), (1, {1}))
        projects.request_slices(packed["id"])
        await self.settle()
        self.assertEqual(projects.get_project(packed["id"])["slices"]["1"]["state"], "sliced")

        big = projects.create_project("Big")
        for _ in range(2):
            data = cube_stl(200)
            projects.import_model(big["id"], io.BytesIO(data), len(data), "big.stl")
        await self.settle()
        big = projects.get_project(big["id"])
        big = self.edit(big, plates=[projects._new_plate(1), projects._new_plate(2)],
                        objects=[{**o, "plate": i + 1} for i, o in enumerate(big["data"]["objects"])])
        with self.assertRaisesRegex(jobs.JobError, "don't fit on one plate") as refused:
            projects.merge_plates(big["id"], 2, 1)
        self.assertEqual(refused.exception.status, 409)
        self.assertEqual(len(projects.get_project(big["id"])["data"]["plates"]), 2)

    async def test_sending_a_batch_and_assigning_printers(self):
        project = projects.create_project("Batch")
        for size in (20, 30):
            data = cube_stl(size)
            projects.import_model(project["id"], io.BytesIO(data), len(data), f"cube{size}.stl")
        await self.settle()
        project = projects.get_project(project["id"])
        project = self.edit(project, plates=[projects._new_plate(1), projects._new_plate(2)],
                            objects=[{**o, "plate": i + 1} for i, o in enumerate(project["data"]["objects"])])
        with self.assertRaisesRegex(jobs.JobError, "Slice .*Plate 1.*Plate 2"):
            projects.queue_plates(project["id"], {})
        self.assertEqual({e["state"] for e in projects.request_slices(project["id"])["slices"].values()}, {"queued"})
        with self.assertRaisesRegex(jobs.JobError, "Nothing to slice"):
            projects.request_slices(project["id"])
        await self.settle()

        sent = projects.queue_plates(project["id"], {"leveling": True})
        self.assertEqual(len(sent["jobs"]), 2)
        self.assertEqual({j["batch_id"] for j in sent["jobs"]}, {sent["batch_id"]})
        self.assertEqual([(j["plate_id"], j["project_id"], j["print_opts"]["leveling"]) for j in sent["jobs"]],
                         [(1, project["id"], True), (2, project["id"], True)])
        with self.assertRaisesRegex(jobs.JobError, "already in the queue"):
            projects.queue_plates(project["id"], {"plates": [1]})
        self.assertEqual(len(projects.queue_plates(project["id"], {"plates": [1], "again": True})["jobs"]), 1)
        self.assertEqual(len(projects.project_jobs(project["id"])), 3)

        assigned = jobs.assign_printers([j["id"] for j in sent["jobs"]], ["p1", "p2"])
        self.assertEqual([j["printer_id"] for j in assigned], ["p1", "p2"])
        with self.assertRaisesRegex(jobs.JobError, "at least one printer"):
            jobs.assign_printers([sent["jobs"][0]["id"]], [])

    async def test_plates_slice_at_the_same_time_each_with_its_own_slicer_folder(self):
        project = projects.create_project("Parallel")
        for size in (20, 30):
            data = cube_stl(size)
            projects.import_model(project["id"], io.BytesIO(data), len(data), f"cube{size}.stl")
        await self.settle()
        project = projects.get_project(project["id"])
        project = self.edit(project, plates=[projects._new_plate(1), projects._new_plate(2)],
                            objects=[{**o, "plate": i + 1} for i, o in enumerate(project["data"]["objects"])])
        projects.request_slices(project["id"])

        both_running = threading.Barrier(2, timeout=10)   # only passes when the two slices overlap
        inner = fake_runner()
        folders = []

        def runner(args, timeout):
            folders.append(args[args.index("--datadir") + 1])
            both_running.wait()
            return inner(args, timeout)
        with mock.patch.object(slicer, "runner", runner):
            self.assertEqual(await asyncio.gather(projects.process_next(0), projects.process_next(1)), [True, True])

        self.assertEqual({e["state"] for e in projects.get_project(project["id"])["slices"].values()}, {"sliced"})
        self.assertEqual(len(set(folders)), 2)
        self.assertTrue(any(f.endswith("slicer-data-1") for f in folders))

    async def test_work_for_a_deleted_split_leaves_the_next_split_with_its_id_alone(self):
        data = cube_stl(20)
        first = splits.create_split(io.BytesIO(data), len(data), "first.stl", {})
        replacement = {}

        def deleted_meanwhile(split_id, stage):
            splits.delete_split(split_id)  # what deleting its project does while the stage runs
            replacement.update(splits.create_split(io.BytesIO(data), len(data), "second.stl", {}))
            raise jobs.JobError("The analyze step stopped unexpectedly (exit code 1)")
        with mock.patch.object(splits, "_run_stage", deleted_meanwhile):
            await splits.process_next()
        self.assertEqual(replacement["id"], first["id"])                     # SQLite reused the id
        self.assertEqual((splits.get_split(first["id"])["state"], splits.get_split(first["id"])["source_name"]),
                         ("analyzing", "second.stl"))

    async def test_an_import_for_a_deleted_project_leaves_the_next_project_alone(self):
        data = cube_stl(20)
        first = projects.create_project("First")
        projects.import_model(first["id"], io.BytesIO(data), len(data), "cube.stl")
        replacement = {}

        def deleted_meanwhile(project_id, model_id):
            projects.delete_project(project_id)
            second = projects.create_project("Second")
            projects.import_model(second["id"], io.BytesIO(data), len(data), "other.stl")
            replacement.update(projects.get_project(second["id"]))
            return {"state": "ready", "error": None, "size": [1.0, 1.0, 1.0]}
        with mock.patch.object(projects, "_run_import", deleted_meanwhile):
            await projects.process_next()
        self.assertEqual(replacement["id"], first["id"])
        model = projects.get_project(first["id"])["models"][0]
        self.assertEqual((model["name"], model["state"]), ("other", "importing"))


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class WorkflowApiTests(unittest.TestCase):
    def setUp(self):
        from http_handler import SPHandler
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
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SPHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        state.printers = self._saved_printers
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        slicer.configure()
        splits.configure()
        projects.configure()
        self._tmp.cleanup()

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/octet-stream" if raw is not None else "application/json"}
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with _opener.open(request, timeout=20) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_workflow_routes(self):
        status, project = self.call("POST", "/api/projects/upload?filename=cube.stl&scale=10", raw=cube_stl(20))
        self.assertEqual((status, project["name"]), (200, "cube"), project)
        self.assertEqual(splits.get_split(project["split_id"])["settings"]["scale"], 10.0)
        pid = project["id"]
        self.assertEqual(self.call("GET", f"/api/projects/{pid}/jobs"), (200, {"jobs": []}))
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/layout")[0], 409)       # not split yet
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/slice")[0], 409)        # nothing on the plates
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/queue")[0], 409)
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/arrange")[1]["id"], pid)
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/plates/1/merge", {"into": "x"})[0], 400)
        self.assertEqual(self.call("POST", "/api/jobs/assign", {"job_ids": [], "printer_ids": ["nope"]})[0], 404)
        status, other = self.call("POST", "/api/projects", {"name": "Again", "split_id": project["split_id"]})
        self.assertEqual((status, other["split_id"]), (200, project["split_id"]))
        self.assertEqual(self.call("POST", "/api/projects", {"split_id": 999})[0], 404)

    def import_gcode(self, bed="smooth"):
        return self.call("POST", f"/api/projects/upload?filename=benchy.gcode&bed={bed}", raw=GCODE)

    def test_gcode_import_makes_a_one_plate_project_with_an_imported_slice(self):
        status, project = self.import_gcode()
        self.assertEqual((status, project["name"]), (200, "benchy"), project)
        self.assertEqual(([p["id"] for p in project["data"]["plates"]], project["data"]["objects"], project["data"]["bed"]),
                         ([1], [], "smooth"))
        entry = project["slices"]["1"]
        self.assertEqual((entry["state"], entry["imported"], entry["estimate_s"], entry["estimate_g"]), ("sliced", True, 3723, 12.5))
        self.assertEqual(self.call("GET", f"/api/projects/{project['id']}/plates/1/analysis")[0], 200)  # the preview reads the file
        self.assertEqual(self.call("POST", "/api/projects/upload?filename=x.txt", raw=GCODE)[0], 400)
        self.assertEqual(self.call("POST", "/api/projects/upload?filename=x.gcode&bed=gold", raw=GCODE)[0], 400)

    def test_an_imported_slice_is_never_outdated(self):
        _, project = self.import_gcode()
        pid = project["id"]
        data = json.loads(json.dumps(project["data"]))
        data["bed"] = "textured"
        data["profile"]["process"] = {"layer_height": "0.3"}
        status, changed = self.call("PATCH", f"/api/projects/{pid}", {"data": data, "revision": project["revision"]})
        self.assertEqual(status, 200, changed)
        self.assertFalse(changed["slices"]["1"]["outdated"])
        self.assertFalse(self.call("GET", f"/api/projects/{pid}")[1]["slices"]["1"]["outdated"])
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/slice")[0], 409)         # nothing to slice again

    def test_an_imported_plate_is_sent_with_its_side_and_not_skipped(self):
        _, project = self.import_gcode("textured")
        pid = project["id"]
        status, sent = self.call("POST", f"/api/projects/{pid}/queue", {})
        self.assertEqual(status, 200, sent)
        self.assertEqual([(j["project_id"], j["plate_id"], j["print_opts"]["smooth_plate"]) for j in sent["jobs"]], [(pid, 1, False)])
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/queue", {"plates": [1]})[0], 409)  # already queued
        _, smooth = self.import_gcode("smooth")
        self.assertTrue(self.call("POST", f"/api/projects/{smooth['id']}/queue", {})[1]["jobs"][0]["print_opts"]["smooth_plate"])


if __name__ == "__main__":
    unittest.main()
