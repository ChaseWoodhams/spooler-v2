"""Faithful single-file Orca 3MF export, tested at the Project service and HTTP boundary.

The template runner stands in for `orca-slicer --export-3mf`: like Orca it writes the loaded presets back inside a larger
project_settings.config. Real Orca fidelity is in tests/test_project_export_native.py."""
import copy
import io
import json
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import numpy as np

import jobs
import orca_3mf
import projects
import slicer
from tests import test_projects
from tests.test_projects import template_runner

class ExportCase(unittest.IsolatedAsyncioTestCase):
    setUp = test_projects.ProjectTests.setUp
    tearDown = test_projects.ProjectTests.tearDown
    asyncTearDown = test_projects.ProjectTests.asyncTearDown
    run_worker = test_projects.ProjectTests.run_worker
    orca_project = test_projects.ProjectTests.orca_project

    async def two_plate_project(self):
        data = self.orca_project()
        created = projects.create_project_from_upload(io.BytesIO(data), len(data), "Bench set.3mf")
        await self.run_worker()
        return projects.get_project(created["id"])

    def set_plates(self, project, overrides):
        data = copy.deepcopy(project["data"])
        for plate, values in zip(data["plates"], overrides):
            plate["overrides"] = values
        return projects.update_project(project["id"], {"data": data, "revision": project["revision"]})

    def export(self, project, runner=None):
        plan = projects.export_plan(project["id"])
        with mock.patch.object(slicer, "runner", runner or template_runner()):
            return plan, projects.export_project(project["id"], plan["revision"], plan["fingerprint"])


class FaithfulExportTests(ExportCase):
    async def test_equal_plate_overrides_and_object_differences_export_as_one_faithful_file(self):
        project = await self.two_plate_project()
        data = copy.deepcopy(project["data"])
        data["plates"][1]["overrides"] = {"wall_loops": "5"}           # same effective value as the Project: a no-op
        data["objects"][1]["overrides"] = {"wall_loops": "5"}          # Lid already resolves to 5: nothing to carry
        project = projects.update_project(project["id"], {"data": data, "revision": project["revision"]})

        plan, artifact = self.export(project)

        self.assertTrue(plan["faithful"], plan)
        self.assertEqual((plan["representation"], plan["filename"], plan["content_type"]), ("single-3mf", "Bench set.3mf", "model/3mf"))
        self.assertEqual(plan["revision"], project["revision"])
        self.assertEqual([(p["id"], p["name"], p["material"]["type"]) for p in plan["plates"]], [(1, "Front", "PLA"), (2, "Plate 2", "PLA")])
        self.assertEqual([(o["name"], o["key"], o["value"]) for o in plan["object_overrides"]], [("Bench", "wall_loops", "6")])
        self.assertEqual((plan["differences"], plan["blockers"]), ([], []))
        self.assertEqual((artifact["filename"], artifact["content_type"]), ("Bench set.3mf", "model/3mf"))
        parsed = orca_3mf.parse(io.BytesIO(artifact["body"]))
        self.assertEqual([o["settings"] for o in parsed["objects"]], [{"wall_loops": "6"}, {}])
        self.assertEqual(parsed["project"]["wall_loops"], "5")
        self.assertEqual(parsed["project"]["brim_width"], "5")          # Orca's own defaults come from the template
        self.assertEqual(parsed["project"]["curr_bed_type"], "High Temp Plate")
        self.assertEqual(parsed["project"]["filament_settings_id"], ["Elegoo PLA @ECC"])

    async def test_exported_geometry_matches_the_placement_a_slice_sends(self):
        project = await self.two_plate_project()
        plan, artifact = self.export(project)
        parsed = orca_3mf.parse(io.BytesIO(artifact["body"]))
        for obj, saved in zip(parsed["objects"], project["data"]["objects"]):
            instance = obj["instances"][0]
            verts, _ = projects.load_mesh(project["id"], saved["model"])
            expected = projects.place(verts, saved)
            actual = obj["verts"] @ instance["matrix"] + instance["shift"]
            self.assertLess(float(np.abs(actual - expected).max()), 1e-4, saved["name"])

    async def test_a_plate_with_a_different_process_value_needs_review_and_never_flattens_into_an_object(self):
        project = await self.two_plate_project()
        project = self.set_plates(project, [{}, {"wall_loops": "4"}])

        plan = projects.export_plan(project["id"])

        self.assertFalse(plan["faithful"])
        (difference,) = [d for d in plan["differences"] if d["key"] == "wall_loops"]
        self.assertEqual((difference["kind"], [(p["id"], p["value"]) for p in difference["plates"]]), ("process", [(1, "5"), (2, "4")]))
        self.assertIsNone(plan["filename"])
        for operation in (lambda: projects.export_orca_project(project["id"]),
                          lambda: projects.export_project(project["id"], plan["revision"], plan["fingerprint"])):
            with self.assertRaises(jobs.JobError) as refused:
                operation()
            self.assertEqual(refused.exception.status, 409)
            self.assertEqual(refused.exception.issues[0]["code"], "export-review-required")
            self.assertTrue(any(i.get("key") == "wall_loops" for i in refused.exception.issues[0]["differences"]))
        self.assertEqual(projects.get_project(project["id"])["revision"], project["revision"])

    async def test_a_different_material_on_one_plate_is_a_difference_not_a_substitution(self):
        slicer.save_user_preset("filament", "Workshop ASA", "Elegoo PLA @ECC", {"filament_type": ["ASA"], "nozzle_temperature": ["260"]})
        project = await self.two_plate_project()
        data = copy.deepcopy(project["data"])
        data["filaments"] = {"2": {"preset": "Workshop ASA", "colour": "black"}}
        project = projects.update_project(project["id"], {"data": data, "revision": project["revision"]})

        plan = projects.export_plan(project["id"])

        self.assertFalse(plan["faithful"])
        self.assertEqual([(p["id"], p["material"]["type"], p["material"]["preset"]) for p in plan["plates"]],
                         [(1, "PLA", "Elegoo PLA @ECC"), (2, "ASA", "Workshop ASA")])
        self.assertIn(("filament", "filament_type"), [(d["kind"], d["key"]) for d in plan["differences"]])
        with self.assertRaises(jobs.JobError) as refused:
            projects.export_orca_project(project["id"])
        self.assertEqual(refused.exception.issues[0]["code"], "export-review-required")

    async def test_a_changed_project_must_be_planned_again(self):
        project = await self.two_plate_project()
        plan = projects.export_plan(project["id"])
        again = projects.export_plan(project["id"])
        self.assertEqual(plan["fingerprint"], again["fingerprint"])        # planning is repeatable and changes nothing
        data = copy.deepcopy(project["data"])
        data["plates"][0]["name"] = "Renamed"
        changed = projects.update_project(project["id"], {"data": data, "revision": project["revision"]})
        with mock.patch.object(slicer, "runner", template_runner()):
            with self.assertRaises(jobs.JobError) as stale:
                projects.export_project(project["id"], plan["revision"], plan["fingerprint"])
            self.assertEqual((stale.exception.status, stale.exception.issues[0]["code"]), (409, "export-plan-stale"))
            with self.assertRaises(jobs.JobError):                         # the old fingerprint on the new revision
                projects.export_project(project["id"], changed["revision"], plan["fingerprint"])
            fresh = projects.export_plan(project["id"])
            self.assertNotEqual(fresh["fingerprint"], plan["fingerprint"])
            self.assertTrue(projects.export_project(project["id"], fresh["revision"], fresh["fingerprint"])["body"])

    async def test_missing_orca_template_is_an_actionable_error_not_a_thinner_file(self):
        project = await self.two_plate_project()
        plan = projects.export_plan(project["id"])
        with mock.patch.object(slicer, "runner", template_runner(code=1)):
            with self.assertRaises(jobs.JobError) as refused:
                projects.export_project(project["id"], plan["revision"], plan["fingerprint"])
        self.assertEqual((refused.exception.status, refused.exception.issues[0]["code"]), (409, "export-template-unavailable"))

    def stock_machine_value(self, key, value):
        path = next(self.dir.glob("tools/*/resources/profiles/Elegoo/machine/ECC/Elegoo Centauri Carbon 0.4 nozzle.json"))
        record = json.loads(path.read_text(encoding="utf-8"))
        record[key] = value
        path.write_text(json.dumps(record), encoding="utf-8")
        slicer._preset_cache.clear()

    def scalar_template(self, key):
        """A template runner whose Orca writes `key` as a single value, as an Orca that has no vector for it would."""
        inner = template_runner()

        def run(args, timeout):
            code, text = inner(args, timeout)
            out = Path(args[args.index("--export-3mf") + 1])
            with zipfile.ZipFile(out) as archive:
                config = json.loads(archive.read("Metadata/project_settings.config"))
            config[key] = "1"
            with zipfile.ZipFile(out, "w") as archive:
                archive.writestr("Metadata/project_settings.config", json.dumps(config))
            return code, text
        return run

    async def test_a_vector_with_several_entries_is_never_truncated_to_fit_a_single_value_slot(self):
        self.stock_machine_value("retract_length_toolchange", ["1", "2"])
        project = await self.two_plate_project()
        plan = projects.export_plan(project["id"])
        with mock.patch.object(slicer, "runner", self.scalar_template("retract_length_toolchange")):
            with self.assertRaises(jobs.JobError) as refused:
                projects.export_project(project["id"], plan["revision"], plan["fingerprint"])
        issue = refused.exception.issues[0]
        self.assertEqual((refused.exception.status, issue["code"]), (409, "export-template-incomplete"))
        self.assertIn("retract_length_toolchange", issue["keys"])

    async def test_a_uniform_vector_still_fits_a_single_value_slot(self):
        self.stock_machine_value("retract_length_toolchange", ["1", "1"])
        project = await self.two_plate_project()
        plan = projects.export_plan(project["id"])
        with mock.patch.object(slicer, "runner", self.scalar_template("retract_length_toolchange")):
            artifact = projects.export_project(project["id"], plan["revision"], plan["fingerprint"])
        self.assertEqual(orca_3mf.parse(io.BytesIO(artifact["body"]))["project"]["retract_length_toolchange"], "1")

    async def test_object_metadata_serializes_typed_values_the_way_orca_reads_them(self):
        settings = {"wall_loops": 4, "flag": True, "off": False, "list": ["1", "2"], "empty": None, "pct": "15%"}
        data = orca_3mf.build([{"name": "A", "verts": np.zeros((3, 3)), "tris": np.array([[0, 1, 2]]), "matrix": np.eye(3),
                                "shift": np.zeros(3), "plate": 1, "settings": settings}], [{"id": 1, "name": "P"}], {})
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            text = archive.read(orca_3mf.SETTINGS_PATH).decode()
        for key, value in {"wall_loops": "4", "flag": "1", "off": "0", "list": "1;2", "empty": "nil", "pct": "15%"}.items():
            self.assertIn(f'<metadata key="{key}" value="{value}"/>', text)


class ExportHttpTests(unittest.TestCase):
    setUp = test_projects.ProjectApiTests.setUp
    tearDown = test_projects.ProjectApiTests.tearDown
    call = test_projects.ProjectApiTests.call

    def test_export_plan_and_download_are_bound_to_the_planned_revision(self):
        pid = self.call("POST", "/api/projects", {"name": "Plan: me"})[1]["id"]
        code, plan = self.call("GET", f"/api/projects/{pid}/export-plan")
        self.assertEqual(code, 200)
        self.assertEqual((plan["version"], plan["project_id"]), (1, pid))
        self.assertFalse(plan["faithful"])                                 # an empty Project has nothing to export
        self.assertEqual(plan["blockers"][0]["code"], "nothing-to-export")
        code, refused = self.call("GET", f"/api/projects/{pid}/export.3mf")
        self.assertEqual((code, refused["issues"][0]["code"]), (409, "nothing-to-export"))
        code, stale = self.call("GET", f"/api/projects/{pid}/export.3mf?revision=999&fingerprint=abc")
        self.assertEqual((code, stale["issues"][0]["code"]), (409, "export-plan-stale"))
