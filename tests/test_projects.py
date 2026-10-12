import asyncio
import io
import json
import math
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import database
import jobs
import projects
import slicer
import splits
import state
from tests.test_jobs import FakePrinter
from tests.test_slicer import SLICED_GCODE, cube_stl, fake_runner, make_fake_orca

GEOMETRY = not splits.missing_modules()
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def arrange_runner(placements):
    """Stands in for `orca-slicer --arrange 1 --export-3mf`: writes the given (x, y, degrees) per input file."""
    def run(args, timeout):
        run.calls.append(args)
        out = Path(args[args.index("--export-3mf") + 1])
        inputs = args[args.index("--export-3mf") + 2:]
        objects, items = [], []
        for index, (source, (x, y, turn)) in enumerate(zip(inputs, placements), start=1):
            c, s = math.cos(math.radians(turn)), math.sin(math.radians(turn))
            objects.append(f'<object id="{index}" type="model"><components>'
                           f'<component p:path="/3D/Objects/{Path(source).name}_{index}.model" objectid="1"/></components></object>')
            items.append(f'<item objectid="{index}" transform="{c} {s} 0 {-s} {c} 0 0 0 1 {x} {y} 0" printable="1"/>')
        with zipfile.ZipFile(out, "w") as archive:
            archive.writestr("3D/3dmodel.model", f'<model><resources>{"".join(objects)}</resources><build>{"".join(items)}</build></model>')
        return 0, ""
    run.calls = []
    return run


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class ProjectTests(unittest.IsolatedAsyncioTestCase):
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

    async def project_with_cube(self, size=20):
        project = projects.create_project("Brackets")
        data = cube_stl(size)
        projects.import_model(project["id"], io.BytesIO(data), len(data), "cube.stl")
        await self.run_worker()
        return projects.get_project(project["id"])

    def edit(self, project, **changes):
        data = json.loads(json.dumps(project["data"]))
        for key, value in changes.items():
            data[key] = value
        return projects.update_project(project["id"], {"data": data, "revision": project["revision"]})

    async def test_import_places_the_model_on_the_first_plate(self):
        project = await self.project_with_cube()
        self.assertEqual(project["data"]["profile"]["bundle_id"], slicer.list_bundles()[0]["id"])
        model = project["models"][0]
        self.assertEqual((model["state"], model["size"], model["name"]), ("ready", [20.0, 20.0, 20.0], "cube"))
        self.assertEqual(project["data"]["objects"], [{"id": 1, "model": 1, "name": "cube", "plate": 1, "position": [128.0, 128.0],
                                                     "rotation": [0.0, 0.0, 0.0], "scale": [1.0, 1.0, 1.0], "overrides": {}}])
        self.assertTrue(projects.model_preview(project["id"], 1).is_file())
        summary = projects.list_projects()[0]
        self.assertEqual((summary["name"], summary["objects"], summary["busy"]), ("Brackets", 1, False))

        bad = b"solid x\nendsolid x\n"
        projects.import_model(project["id"], io.BytesIO(bad), len(bad), "empty.stl")
        await self.run_worker()
        failed = projects.get_project(project["id"])["models"][1]
        self.assertEqual(failed["state"], "failed")
        self.assertTrue(failed["error"])

    async def test_settings_changes_are_validated(self):
        project = await self.project_with_cube()
        profile = {**project["data"]["profile"], "process": {"wall_loops": 4, "enable_support": True},
                   "filament": {"nozzle_temperature": 215}}
        updated = self.edit(project, profile=profile)
        self.assertEqual(updated["data"]["profile"]["process"], {"enable_support": "1", "wall_loops": "4"})
        self.assertEqual(updated["data"]["profile"]["filament"], {"nozzle_temperature": ["215"]})
        self.assertEqual(updated["revision"], project["revision"] + 1)

        with self.assertRaises(jobs.JobError) as stale:
            self.edit(project, bed="smooth")
        self.assertEqual(stale.exception.status, 409)

        for process, message in (({"layer_height": "thick"}, "must be a number"), ({"post_process": "calc.exe"}, "can't be overridden"),
                                 ({"machine_start_gcode": "M112"}, "can't be overridden"), ({"support_type": "wobbly"}, "valid choice"),
                                 ({"sparse_infill_density": "150%"}, "between 0 and 100"), ({"not_a_setting": "1"}, "isn't a process setting")):
            with self.assertRaisesRegex(jobs.JobError, message):
                self.edit(updated, profile={**updated["data"]["profile"], "process": process})

        objects = updated["data"]["objects"]
        with self.assertRaisesRegex(jobs.JobError, "plate that doesn't exist"):
            self.edit(updated, objects=[{**objects[0], "plate": 7}])
        with self.assertRaisesRegex(jobs.JobError, "Scale"):
            self.edit(updated, objects=[{**objects[0], "scale": [0, 1, 1]}])
        moved = self.edit(updated, objects=[{**objects[0], "rotation": [0, 0, 405], "overrides": {"brim_type": "no_brim"}}])
        self.assertEqual((moved["data"]["objects"][0]["rotation"], moved["data"]["objects"][0]["overrides"]),
                         ([0.0, 0.0, 45.0], {"brim_type": "no_brim"}))

    async def test_slicing_builds_the_plate_and_queues_it(self):
        project = await self.project_with_cube()
        objects = [{**project["data"]["objects"][0], "position": [60.0, 70.0], "rotation": [0.0, 0.0, 45.0],
                    "overrides": {"wall_loops": "5"}}]
        project = self.edit(project, objects=objects, bed="smooth",
                            profile={**project["data"]["profile"], "process": {"wall_loops": "3"}})
        self.assertEqual(projects.request_slice(project["id"], 1)["slices"]["1"]["state"], "queued")
        with self.assertRaisesRegex(jobs.JobError, "already being sliced"):
            projects.request_slice(project["id"], 1)

        runner = fake_runner()
        with mock.patch.object(slicer, "runner", runner):
            await self.run_worker()
        project = projects.get_project(project["id"])
        entry = project["slices"]["1"]
        self.assertEqual((entry["state"], entry["outdated"], entry["estimate_g"]), ("sliced", False, 3.6))

        work = projects.plate_dir(project["id"], 1)
        process = json.loads((work / "profile" / "process.json").read_text())
        self.assertEqual((process["wall_loops"], process["curr_bed_type"]), ("3", "High Temp Plate"))
        with zipfile.ZipFile(work / "plate.3mf") as archive:
            model = archive.read("3D/3dmodel.model").decode()
            config = archive.read("Metadata/model_settings.config").decode()
        xs = [float(v) for v in re.findall(r'<vertex x="([^"]+)"', model)]
        zs = [float(v) for v in re.findall(r' z="([^"]+)"', model)]
        half_diagonal = 20 * math.sqrt(2) / 2
        self.assertAlmostEqual(min(xs), 60 - half_diagonal, places=3)  # rotated 45° about its centre
        self.assertAlmostEqual(min(zs), 0.0, places=3)
        self.assertIn('<metadata key="wall_loops" value="5"/>', config)
        self.assertNotIn("Application", model)
        self.assertEqual(runner.calls[0][runner.calls[0].index("--arrange") + 1], "0")
        self.assertIn("feature_stats", projects.plate_analysis(project["id"], 1))
        self.assertTrue(projects.plate_file(project["id"], 1, "toolpath").is_file())

        gcode = (work / "plate.gcode").read_text()
        self.assertIn("; thumbnail begin 144x144 ", gcode)
        self.assertLess(gcode.index("HEADER_BLOCK_END"), gcode.index("THUMBNAIL_BLOCK_START"))
        self.assertLess(gcode.index("THUMBNAIL_BLOCK_END"), gcode.index("G28"))
        self.assertTrue(entry.get("thumbnail") or projects.plate_analysis(project["id"], 1)["thumbnail"])

        moved = self.edit(project, objects=[{**project["data"]["objects"][0], "position": [80.0, 70.0]}])
        self.assertTrue(moved["slices"]["1"]["outdated"])
        with self.assertRaisesRegex(jobs.JobError, "changed after it was sliced"):
            projects.queue_plate(project["id"], 1, {})
        renamed = projects.update_project(project["id"], {"name": "Brackets v2"})
        self.assertTrue(renamed["slices"]["1"]["outdated"])  # still outdated because of the move, not the name

        projects.request_slice(project["id"], 1)
        with mock.patch.object(slicer, "runner", fake_runner()):
            await self.run_worker()
        job = projects.queue_plate(project["id"], 1, {"printer_id": "p1", "leveling": True})
        self.assertEqual((job["name"], job["printer_id"], job["print_opts"]["smooth_plate"], job["print_opts"]["leveling"]),
                         ("Brackets v2_Plate 1", "p1", True, True))
        self.assertEqual(job["notes"], "Brackets v2 — Plate 1")
        with database.transaction() as conn:
            self.assertEqual(conn.execute("SELECT project_id FROM jobs WHERE id = ?", (job["id"],)).fetchone()[0], project["id"])

    def orca_project(self):
        import numpy as np
        import orca_3mf
        from tests.test_orca_3mf import CUBE, TRIS
        turned = (np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]], float)).T   # a quarter turn about Z
        objects = [{"name": "Bench", "verts": CUBE, "tris": TRIS, "matrix": np.eye(3), "shift": np.array([60.0, 80.0, 2.0]),
                    "plate": 1, "settings": {"wall_loops": "6", "made_up": "1"}},
                   {"name": "Lid", "verts": CUBE * 2, "tris": TRIS, "matrix": turned, "shift": np.array([100.0, 120.0, 4.0]),
                    "plate": 2, "settings": {}}]
        config = {"printer_settings_id": "Elegoo Centauri Carbon 0.4 nozzle", "print_settings_id": "Some User Preset",
                  "filament_settings_id": ["Elegoo PLA @ECC"], "wall_loops": "5", "nozzle_temperature": ["215"],
                  "layer_height": "0.2", "curr_bed_type": "High Temp Plate"}
        return orca_3mf.build(objects, [{"id": 1, "name": "Front"}, {"id": 2, "name": ""}], config)

    async def test_an_orca_project_opens_with_its_plates_placements_and_settings(self):
        data = self.orca_project()
        created = projects.create_project_from_upload(io.BytesIO(data), len(data), "Bench set.3mf")
        await self.run_worker()
        project = projects.get_project(created["id"])

        self.assertEqual(project["name"], "Bench set")
        self.assertEqual([(p["id"], p["name"]) for p in project["data"]["plates"]], [(1, "Front"), (2, "Plate 2")])
        self.assertEqual([m["state"] for m in project["models"]], ["ready", "ready"])
        bench, lid = project["data"]["objects"]
        self.assertEqual((bench["name"], bench["plate"], bench["position"], bench["overrides"]), ("Bench", 1, [60.0, 80.0], {"wall_loops": "6"}))
        self.assertEqual((lid["plate"], lid["position"], lid["rotation"][2], lid["scale"]), (2, [100.0, 120.0], 90.0, [1.0, 1.0, 1.0]))
        profile = project["data"]["profile"]
        self.assertEqual(project["data"]["bed"], "smooth")
        self.assertEqual((profile["process"], profile["filament"]), ({"wall_loops": "5"}, {"nozzle_temperature": ["215"]}))
        self.assertTrue(any("Some User Preset" in n for n in created["import_notes"]))
        self.assertTrue(any("made_up" in n for n in created["import_notes"]))

    async def test_adding_an_orca_project_appends_its_plates(self):
        project = await self.project_with_cube()
        data = self.orca_project()
        added = projects.import_model(project["id"], io.BytesIO(data), len(data), "Bench set.3mf")
        await self.run_worker()

        self.assertEqual([p["id"] for p in added["data"]["plates"]], [1, 2, 3])
        self.assertEqual([(o["name"], o["plate"]) for o in added["data"]["objects"]], [("cube", 1), ("Bench", 2), ("Lid", 3)])
        self.assertEqual(added["data"]["profile"], project["data"]["profile"])  # the open project keeps its own settings

    async def test_a_plain_3mf_is_still_added_as_one_model(self):
        project = await self.project_with_cube()
        buffer = io.BytesIO()
        import zipfile as z
        with z.ZipFile(buffer, "w") as archive:
            archive.writestr("3D/3dmodel.model", "<model/>")
        data = buffer.getvalue()
        added = projects.import_model(project["id"], io.BytesIO(data), len(data), "plain.3mf")
        self.assertEqual([m["file"] for m in added["models"]], ["cube.stl", "plain.3mf"])

    async def test_exporting_gives_orca_the_same_plates_and_placements(self):
        import orca_3mf
        data = self.orca_project()
        created = projects.create_project_from_upload(io.BytesIO(data), len(data), "Bench set.3mf")
        await self.run_worker()

        with mock.patch.object(slicer, "runner", arrange_runner([(0, 0, 0)])):
            exported = projects.export_orca_project(created["id"])
        parsed = orca_3mf.parse(io.BytesIO(exported))

        self.assertEqual([(p["id"], p["name"]) for p in parsed["plates"]], [(1, "Front"), (2, "Plate 2")])
        self.assertEqual([o["name"] for o in parsed["objects"]], ["Bench", "Lid"])
        bench, lid = parsed["objects"]
        self.assertEqual(bench["settings"], {"wall_loops": "6"})
        placed = orca_3mf.pose(lid["verts"], lid["instances"][0]["matrix"], lid["instances"][0]["shift"])
        self.assertEqual((lid["instances"][0]["plate"], placed["position"], round(placed["rotation"][2])), (2, [100.0, 120.0], 90))
        self.assertEqual(float((lid["verts"] @ lid["instances"][0]["matrix"] + lid["instances"][0]["shift"])[:, 2].min()), 0.0)
        self.assertEqual(parsed["project"]["curr_bed_type"], "High Temp Plate")
        self.assertEqual(parsed["project"]["wall_loops"], "5")
        self.assertEqual(parsed["project"]["print_settings_id"], "0.20mm Standard @Elegoo CC 0.4 nozzle")

    async def test_slice_and_send_without_a_printer_makes_a_job_for_the_next_free_printer(self):
        project = await self.project_with_cube()
        self.assertEqual(projects.request_slice(project["id"], 1, send=True)["slices"]["1"]["auto_send"], True)
        with mock.patch.object(slicer, "runner", fake_runner()):
            await self.run_worker()
        entry = projects.get_project(project["id"])["slices"]["1"]
        self.assertEqual((entry["state"], entry["sent_to"]), ("sliced", None))
        job = jobs.get_job(entry["sent_job"])
        self.assertEqual((job["state"], job["printer_id"], job["plate_id"]), ("queued", None, 1))

    async def test_slice_and_send_queues_the_plate_for_its_own_printer(self):
        project = await self.project_with_cube()
        plates = [{**project["data"]["plates"][0], "printer_id": "p1"}]
        project = self.edit(project, plates=plates)
        self.assertEqual(project["data"]["plates"][0]["printer_id"], "p1")
        self.assertEqual(projects.request_slice(project["id"], 1, send=True)["slices"]["1"]["auto_send"], True)
        with mock.patch.object(slicer, "runner", fake_runner()):
            await self.run_worker()

        entry = projects.get_project(project["id"])["slices"]["1"]
        self.assertEqual((entry["state"], entry["sent_to"]), ("sliced", "p1"))
        job = jobs.get_job(entry["sent_job"])
        self.assertEqual((job["state"], job["printer_id"], job["plate_id"]), ("queued", "p1", 1))

        # sliced again without "send": nothing more goes to the queue
        projects.request_slice(project["id"], 1)
        with mock.patch.object(slicer, "runner", fake_runner()):
            await self.run_worker()
        self.assertEqual(len(projects.project_jobs(project["id"])), 1)

    async def test_a_plate_that_cannot_be_sent_says_why(self):
        project = await self.project_with_cube()
        project = self.edit(project, plates=[{**project["data"]["plates"][0], "printer_id": "p1"}])
        projects.request_slice(project["id"], 1, send=True)
        with mock.patch.object(slicer, "runner", fake_runner()),                 mock.patch.object(projects, "queue_plate", side_effect=jobs.JobError("The bed is full")):
            await self.run_worker()
        entry = projects.get_project(project["id"])["slices"]["1"]
        self.assertEqual((entry["state"], entry["send_error"]), ("sliced", "The bed is full"))

    async def test_a_chosen_spool_is_kept_but_does_not_outdate_a_slice(self):
        project = await self.project_with_cube()
        projects.request_slice(project["id"], 1)
        with mock.patch.object(slicer, "runner", fake_runner()):
            await self.run_worker()
        sliced = projects.get_project(project["id"])
        chosen = self.edit(sliced, profile={**sliced["data"]["profile"], "spool_id": "7"})

        self.assertEqual(chosen["data"]["profile"]["spool_id"], 7)
        self.assertFalse(chosen["slices"]["1"]["outdated"])
        with self.assertRaisesRegex(jobs.JobError, "spool number"):
            self.edit(chosen, profile={**chosen["data"]["profile"], "spool_id": "abc"})

    async def test_a_second_slice_remembers_how_long_the_first_took(self):
        project = await self.project_with_cube()
        projects.request_slice(project["id"], 1)
        self.assertNotIn("last_duration_ms", projects.get_project(project["id"])["slices"]["1"])
        with mock.patch.object(slicer, "runner", fake_runner()):
            await self.run_worker()
        took = projects.get_project(project["id"])["slices"]["1"]["duration_ms"]

        queued = projects.request_slice(project["id"], 1)["slices"]["1"]
        self.assertEqual((queued["state"], queued["last_duration_ms"]), ("queued", took))
        projects._claim()
        projects._stage(project["id"], 1, "slicing")
        running = projects.get_project(project["id"])["slices"]["1"]
        self.assertEqual((running["state"], running["stage"], running["last_duration_ms"]), ("slicing", "slicing", took))
        self.assertGreaterEqual(projects.get_project(project["id"])["server_time"], running["started_at"])

    async def test_bricklayers_post_processes_a_plate(self):
        project = await self.project_with_cube()
        projects.request_slice(project["id"], 1)
        with mock.patch.object(slicer, "runner", fake_runner()):
            await self.run_worker()
        project = projects.get_project(project["id"])
        plain = project["slices"]["1"]["fingerprint"]
        self.assertIsNone(project["slices"]["1"]["bricklayers"])

        # turning it off everywhere, or on and off again, doesn't change what the plate would slice to
        off = self.edit(project, profile={**project["data"]["profile"], "bricklayers": {"enabled": False, "start_at_layer": 4}})
        self.assertFalse(off["slices"]["1"]["outdated"])
        with self.assertRaisesRegex(jobs.JobError, "between 0.8 and 1.5"):
            self.edit(off, profile={**off["data"]["profile"], "bricklayers": {"enabled": True, "extrusion_multiplier": 3}})
        on = self.edit(off, profile={**off["data"]["profile"], "bricklayers": {"enabled": True, "extrusion_multiplier": 1.1}})
        self.assertTrue(on["slices"]["1"]["outdated"])
        plate_off = self.edit(on, plates=[{**on["data"]["plates"][0], "bricklayers": {"enabled": False}}])
        self.assertFalse(plate_off["slices"]["1"]["outdated"])
        project = self.edit(plate_off, plates=[{**plate_off["data"]["plates"][0], "bricklayers": None}])

        def bricks(args, timeout):
            bricks.calls.append(args)
            source, target = Path(args[2]), Path(args[args.index("-outputFile") + 1])
            target.write_text(";== BrickLayers Post-Processed\n" + source.read_text())
            return 0, "done"
        bricks.calls = []
        projects.request_slice(project["id"], 1)
        with mock.patch.object(slicer, "runner", fake_runner()), mock.patch("brick_post.runner", bricks):
            await self.run_worker()
        project = projects.get_project(project["id"])
        entry = project["slices"]["1"]
        self.assertEqual((entry["state"], entry["outdated"]), ("sliced", False))
        self.assertEqual(entry["bricklayers"], {"enabled": True, "extrusion_multiplier": 1.1, "start_at_layer": 3})
        self.assertNotEqual(entry["fingerprint"], plain)
        args = bricks.calls[0]
        self.assertEqual((args[args.index("-extrusionMultiplier") + 1], args[args.index("-startAtLayer") + 1]), ("1.1", "3"))

        work = projects.plate_dir(project["id"], 1)
        process = json.loads((work / "profile" / "process.json").read_text())
        self.assertEqual({k: process[k] for k in ("enable_arc_fitting", "wall_generator", "wall_sequence")},
                         {"enable_arc_fitting": "0", "wall_generator": "classic", "wall_sequence": "inner wall/outer wall"})
        self.assertEqual((work / "plate.orca.gcode").read_text(), SLICED_GCODE)
        self.assertTrue((work / "plate.gcode").read_text().startswith(";== BrickLayers Post-Processed"))
        self.assertIn("-outputFile", (work / "slicer.log").read_text())

        failing = mock.Mock(return_value=(1, "ValueError: broken"))
        self.edit(project, profile={**project["data"]["profile"], "bricklayers": {"enabled": True, "start_at_layer": 5}})
        projects.request_slice(project["id"], 1)
        with mock.patch.object(slicer, "runner", fake_runner()), mock.patch("brick_post.runner", failing):
            await self.run_worker()
        entry = projects.get_project(project["id"])["slices"]["1"]
        self.assertEqual((entry["state"], entry["error"]), ("failed", "BrickLayers failed: ValueError: broken"))

    async def test_objects_off_the_bed_or_over_the_purge_area_are_refused(self):
        project = await self.project_with_cube()
        base = project["data"]["objects"][0]
        for changes, message in (({"position": [250.0, 128.0]}, "past the edge of the bed"),
                                 ({"position": [251.0, 10.0], "scale": [0.4, 0.4, 0.4]}, "purge area"),
                                 ({"scale": [14.0, 1.0, 1.0]}, "past the edge")):
            project = self.edit(project, objects=[{**base, **changes}])
            projects.request_slice(project["id"], 1)
            with mock.patch.object(slicer, "runner", fake_runner()):
                await self.run_worker()
            project = projects.get_project(project["id"])
            self.assertEqual(project["slices"]["1"]["state"], "failed")
            self.assertIn(message, project["slices"]["1"]["error"])

    async def test_arrange_can_use_orcaslicer_placements(self):
        project = await self.project_with_cube()
        data = cube_stl(30)
        projects.import_model(project["id"], io.BytesIO(data), len(data), "big.stl")
        await self.run_worker()
        runner = arrange_runner([(60.0, 60.0, 0.0), (180.0, 150.0, 90.0)])
        with mock.patch.object(slicer, "runner", runner):
            arranged = projects.arrange_plate(project["id"], 1, engine="orca")
        self.assertEqual([(o["position"], o["rotation"][2]) for o in arranged["data"]["objects"]],
                         [([60.0, 60.0], 0.0), ([180.0, 150.0], 90.0)])
        self.assertEqual(runner.calls[0][runner.calls[0].index("--arrange") + 1], "1")
        with self.assertRaisesRegex(jobs.JobError, "nothing ready"):
            projects.arrange_plate(project["id"], 99)

    async def test_split_parts_are_packed_onto_plates(self):
        import trimesh
        project = projects.create_project("Beam")
        beam = self.dir / "beam.stl"
        trimesh.creation.box((400, 40, 20)).export(beam)
        data = beam.read_bytes()
        split = splits.create_split(io.BytesIO(data), len(data), "beam.stl", {})
        while await splits.process_next():
            pass
        splits.request_split(split["id"])
        while await splits.process_next():
            pass
        with_parts = projects.add_split_parts(project["id"], split["id"])
        await self.run_worker()
        with_parts = projects.get_project(project["id"])
        parts = splits.get_split(split["id"], detail=True)["result"]["parts"]
        self.assertEqual(len(with_parts["models"]), len(parts))
        self.assertEqual({o["plate"] for o in with_parts["data"]["objects"]}, {1})  # the beam halves share a plate
        self.assertEqual([m["state"] for m in with_parts["models"]], ["ready"] * len(parts))
        self.assertEqual(with_parts["models"][0]["name"], "beam_part01")

    async def test_deleting_models_and_projects(self):
        project = await self.project_with_cube()
        emptied = projects.delete_model(project["id"], 1)
        self.assertEqual((emptied["models"], emptied["data"]["objects"]), ([], []))
        with self.assertRaisesRegex(jobs.JobError, "empty"):
            projects.request_slice(project["id"], 1)
        projects.delete_project(project["id"])
        self.assertFalse(projects.project_dir(project["id"]).exists())
        with self.assertRaises(jobs.JobError) as gone:
            projects.get_project(project["id"])
        self.assertEqual(gone.exception.status, 404)

    def test_profiles_accept_process_and_filament_changes(self):
        plain = slicer.list_bundles()[0]
        custom = slicer.derive_bundle(plain["id"], {"process": {"wall_loops": "6"}, "filament": {"nozzle_temperature": ["230"]}})
        self.assertEqual(custom["overrides"], {"filament": {"nozzle_temperature": ["230"]}, "process": {"wall_loops": "6"}})
        self.assertTrue(custom["label"].endswith("customised"))
        settings = slicer.bundle_settings(custom["id"])
        self.assertEqual((settings["process"]["wall_loops"], settings["filament"]["nozzle_temperature"]), ("6", ["230"]))
        tree = slicer.derive_bundle(custom["id"], {"enable_support": "1", "support_type": "tree(auto)"})  # flat = process
        self.assertEqual(tree["overrides"]["process"], {"enable_support": "1", "support_type": "tree(auto)", "wall_loops": "6"})
        self.assertIn("tree supports", tree["label"])


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class ProjectApiTests(unittest.TestCase):
    def test_project_progress_exposes_latest_unknown_outcome_without_changing_status(self):
        pid = self.call('POST', '/api/projects', {'name': 'Outcome fixture'})[1]['id']
        with database.transaction() as conn:
            for started, outcome in [(1, 'completed'), (2, 'unknown')]:
                print_id = conn.execute('INSERT INTO prints (printer_id, printer_name, file_name, project_id, plate_id, started_at, ended_at, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                                        ('fixture', 'Fixture printer', 'fixture.gcode', pid, 1, started, started + 1, outcome)).lastrowid
        code, status = self.call('GET', f'/api/projects/{pid}/status')
        self.assertEqual(code, 200)
        self.assertEqual(status['progress']['outcome_unknown'], 1)
        self.assertTrue(status['plates'][0]['outcome_unknown'])
        self.assertEqual(status['plates'][0]['status'], 'printed_ok')
        self.assertEqual(status['progress']['counts']['printed_ok'], 1)
        code, _ = self.call('POST', f'/api/prints/{print_id}/outcome', {'outcome': 'completed'})
        self.assertEqual(code, 200)
        resolved = self.call('GET', f'/api/projects/{pid}/status')[1]
        self.assertEqual(resolved['progress']['outcome_unknown'], 0)
        self.assertFalse(resolved['plates'][0]['outcome_unknown'])

    def setUp(self):
        from http_handler import SPHandler
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        database.configure(self.dir / "spooler.db")
        jobs.init(self.dir)
        slicer.configure(self.dir, orca_path=make_fake_orca(self.dir / "tools"))
        slicer.init()
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
        projects.configure()
        self._tmp.cleanup()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with _opener.open(request, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_project_routes(self):
        status, project = self.call("POST", "/api/projects", {"name": "Plates"})
        self.assertEqual((status, project["name"]), (200, "Plates"))
        pid = project["id"]
        self.assertEqual(self.call("GET", "/api/projects")[1]["projects"][0]["id"], pid)
        status, renamed = self.call("PATCH", f"/api/projects/{pid}", {"name": "Plates v2"})
        self.assertEqual((status, renamed["name"]), (200, "Plates v2"))
        data = {**renamed["data"], "profile": {**renamed["data"]["profile"], "process": {"wall_loops": "-3"}}}
        self.assertEqual(self.call("PATCH", f"/api/projects/{pid}", {"data": data})[0], 400)
        status, settings = self.call("GET", f"/api/slicer/profiles/{renamed['data']['profile']['bundle_id']}/settings")
        self.assertEqual((status, settings["process"]["wall_loops"]), (200, "3"))
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/plates/1/slice")[0], 409)  # empty plate
        self.assertEqual(self.call("GET", f"/api/projects/{pid}/plates/1/toolpath")[0], 404)
        self.assertEqual(self.call("GET", f"/api/projects/{pid}/models/x/preview")[0], 404)
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/plates/1/explode")[0], 404)
        self.assertEqual(self.call("DELETE", f"/api/projects/{pid}")[0], 200)
        self.assertEqual(self.call("GET", f"/api/projects/{pid}")[0], 404)

    def test_a_sliced_plate_downloads_as_a_named_gcode_file(self):
        pid = self.call("POST", "/api/projects", {"name": "Bracket: v2"})[1]["id"]
        projects._mutate(pid, "slices", lambda s: s.__setitem__("1", {"state": "sliced"}))
        folder = projects.plate_dir(pid, 1)
        folder.mkdir(parents=True)
        (folder / "plate.gcode").write_text("G28")

        with _opener.open(urllib.request.Request(f"{self.base}/api/projects/{pid}/plates/1/gcode"), timeout=10) as resp:
            self.assertEqual(resp.read(), b"G28")
            self.assertIn("Bracket_ v2_Plate 1.gcode", resp.headers.get("Content-Disposition", ""))


if __name__ == "__main__":
    unittest.main()
