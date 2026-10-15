"""The faithful per-Plate Orca package (ticket 12), tested at the Project service and HTTP boundary.

The template runner stands in for `orca-slicer --export-3mf`; real Orca is in tests/test_project_export_package_native.py.
Artifacts are opened and inspected as a user would, then each Plate file is imported through the normal import flow."""
import copy
import io
import json
import unittest
import zipfile
from unittest import mock

import jobs
import orca_3mf
import projects
import slicer
from tests import test_projects
from tests import test_project_export
from tests.test_projects import template_runner

PACKAGE = "plate-3mf-package"


class PackageCase(test_project_export.ExportCase):
    def asa_on_plate_two(self, project):
        slicer.save_user_preset("filament", "Workshop ASA", "Elegoo PLA @ECC", {"filament_type": ["ASA"], "nozzle_temperature": ["260"]})
        data = copy.deepcopy(project["data"])
        data["filaments"] = {"2": {"preset": "Workshop ASA", "colour": "black"}}
        return projects.update_project(project["id"], {"data": data, "revision": project["revision"]})

    def package(self, project, runner=None):
        plan = projects.export_plan(project["id"])
        with mock.patch.object(slicer, "runner", runner or template_runner()):
            return plan, projects.export_project(project["id"], plan["revision"], plan["fingerprint"], PACKAGE)

    @staticmethod
    def open(artifact):
        archive = zipfile.ZipFile(io.BytesIO(artifact["body"]))
        return archive, {name: archive.read(name) for name in archive.namelist()}


class PlanTests(PackageCase):
    async def test_a_project_one_file_cannot_keep_is_offered_as_a_package_that_is_not_a_3mf(self):
        project = self.set_plates(await self.two_plate_project(), [{}, {"wall_loops": "4"}])
        plan = projects.export_plan(project["id"])
        self.assertFalse(plan["faithful"])
        package = plan["package"]
        self.assertTrue(package["available"], package)
        self.assertEqual((package["representation"], package["content_type"], package["filename"]),
                         (PACKAGE, "application/zip", "Bench set - Plate files.zip"))
        self.assertTrue(package["default"], "the package is the default faithful alternative when one file cannot keep the Project")
        self.assertEqual([(f["plate_id"], f["name"], f["material"]["type"]) for f in package["files"]],
                         [(1, "Front", "PLA"), (2, "Plate 2", "PLA")])
        self.assertEqual([f["filename"] for f in package["files"]], ["Plate 1 - Front.3mf", "Plate 2 - Plate 2.3mf"])
        self.assertIsNone(plan["filename"], "the single file stays unavailable")
        self.assertIn("not a Spooler backup", package["note"])

    async def test_a_faithful_single_file_leaves_the_package_as_a_non_default_choice(self):
        plan = projects.export_plan((await self.two_plate_project())["id"])
        self.assertTrue(plan["faithful"])
        self.assertTrue(plan["package"]["available"])
        self.assertFalse(plan["package"]["default"])

    async def test_blockers_make_the_package_unavailable(self):
        project = await self.two_plate_project()
        data = copy.deepcopy(project["data"])
        data["objects"] = []
        project = projects.update_project(project["id"], {"data": data, "revision": project["revision"]})
        plan = projects.export_plan(project["id"])
        self.assertFalse(plan["package"]["available"])
        with self.assertRaises(jobs.JobError) as refused:
            projects.export_project(project["id"], plan["revision"], plan["fingerprint"], PACKAGE)
        self.assertEqual((refused.exception.status, refused.exception.issues[0]["code"]), (409, "nothing-to-export"))


class PackageContentTests(PackageCase):
    async def test_each_plate_file_carries_that_plates_own_process_and_object_settings(self):
        project = self.set_plates(await self.two_plate_project(), [{}, {"wall_loops": "4", "sparse_infill_density": "35%"}])
        plan, artifact = self.package(project)
        self.assertEqual((artifact["filename"], artifact["content_type"]), ("Bench set - Plate files.zip", "application/zip"))
        archive, files = self.open(artifact)
        self.assertEqual(sorted(files), ["MANIFEST.txt", "Plate 1 - Front.3mf", "Plate 2 - Plate 2.3mf"])
        one, two = (orca_3mf.parse(io.BytesIO(files[n])) for n in ("Plate 1 - Front.3mf", "Plate 2 - Plate 2.3mf"))
        self.assertEqual((one["project"]["wall_loops"], two["project"]["wall_loops"]), ("5", "4"))
        self.assertEqual(two["project"]["sparse_infill_density"], "35%")
        self.assertEqual([len(f["objects"]) for f in (one, two)], [1, 1])
        self.assertEqual([o["settings"] for o in one["objects"]], [{"wall_loops": "6"}], "the Object difference stays Object metadata")
        self.assertEqual([o["settings"] for o in two["objects"]], [{}], "a Plate setting is never copied onto Objects")
        self.assertEqual([p["name"] for p in one["plates"]], ["Front"])
        self.assertEqual(one["project"]["brim_width"], "5", "Orca's own defaults come from the template")
        for parsed in (one, two):
            self.assertEqual(parsed["project"]["curr_bed_type"], "High Temp Plate")
            self.assertEqual(len(parsed["plates"]), 1)

    async def test_placement_matches_what_a_slice_sends_with_no_plate_grid_offset(self):
        project = self.set_plates(await self.two_plate_project(), [{}, {"wall_loops": "4"}])
        plan, artifact = self.package(project)
        _, files = self.open(artifact)
        for name, saved in zip(("Plate 1 - Front.3mf", "Plate 2 - Plate 2.3mf"), project["data"]["objects"]):
            parsed = orca_3mf.parse(io.BytesIO(files[name]))
            instance = parsed["objects"][0]["instances"][0]
            verts, _ = projects.load_mesh(project["id"], saved["model"])
            expected = projects.place(verts, saved)
            self.assertLess(float(abs(parsed["objects"][0]["verts"] @ instance["matrix"] + instance["shift"] - expected).max()), 1e-4)

    async def test_two_materials_keep_their_own_filament_and_the_manifest_says_which_file_is_which(self):
        project = self.asa_on_plate_two(await self.two_plate_project())
        plan, artifact = self.package(project)
        _, files = self.open(artifact)
        one, two = (orca_3mf.parse(io.BytesIO(files[n]))["project"] for n in ("Plate 1 - Front.3mf", "Plate 2 - Plate 2.3mf"))
        self.assertEqual((one["filament_type"], two["filament_type"]), (["PLA"], ["ASA"]))
        self.assertEqual((one["filament_settings_id"], two["filament_settings_id"]), (["Elegoo PLA @ECC"], ["Workshop ASA"]))
        self.assertEqual(two["nozzle_temperature"], ["260"])
        manifest = files["MANIFEST.txt"].decode("utf-8")
        for plate in plan["plates"]:
            self.assertIn(plate["fingerprint"], manifest)
        for needle in ("Plate 2 - Plate 2.3mf", "ASA", "Workshop ASA", "Front", "Extract", "File > Open", "not a backup"):
            self.assertIn(needle, manifest)
        self.assertEqual([f["fingerprint"] for f in plan["package"]["files"]], [p["fingerprint"] for p in plan["plates"]])

    async def test_the_manifest_states_remaining_limitations(self):
        project = await self.two_plate_project()
        data = copy.deepcopy(project["data"])
        data["profile"]["bricklayers"] = {"enabled": True}
        data["plates"][1]["overrides"] = {"wall_loops": "4"}
        project = projects.update_project(project["id"], {"data": data, "revision": project["revision"]})
        plan, artifact = self.package(project)
        self.assertEqual([l["code"] for l in plan["limitations"]], ["bricklayers"])
        _, files = self.open(artifact)
        self.assertIn("BrickLayers", files["MANIFEST.txt"].decode())

    async def test_equal_plate_names_get_distinct_safe_filenames(self):
        project = await self.two_plate_project()
        data = copy.deepcopy(project["data"])
        data["plates"][0]["name"] = 'A/B: "x"'
        data["plates"][1]["name"] = 'A/B: "x"'
        data["plates"][1]["overrides"] = {"wall_loops": "4"}
        project = projects.update_project(project["id"], {"data": data, "revision": project["revision"]})
        plan, artifact = self.package(project)
        _, files = self.open(artifact)
        names = sorted(n for n in files if n.endswith(".3mf"))
        self.assertEqual(len(set(names)), 2)
        self.assertTrue(all("/" not in n and ":" not in n and '"' not in n for n in names), names)

    async def test_a_changed_project_or_unknown_representation_is_refused(self):
        project = self.set_plates(await self.two_plate_project(), [{}, {"wall_loops": "4"}])
        plan = projects.export_plan(project["id"])
        data = copy.deepcopy(project["data"])
        data["plates"][0]["name"] = "Renamed"
        projects.update_project(project["id"], {"data": data, "revision": project["revision"]})
        with mock.patch.object(slicer, "runner", template_runner()):
            with self.assertRaises(jobs.JobError) as stale:
                projects.export_project(project["id"], plan["revision"], plan["fingerprint"], PACKAGE)
            self.assertEqual((stale.exception.status, stale.exception.issues[0]["code"]), (409, "export-plan-stale"))
            fresh = projects.export_plan(project["id"])
            with self.assertRaises(jobs.JobError) as unknown:
                projects.export_project(project["id"], fresh["revision"], fresh["fingerprint"], "lossy")
            self.assertEqual(unknown.exception.status, 400)

    async def test_a_missing_orca_template_is_an_error_not_a_thinner_package(self):
        project = self.set_plates(await self.two_plate_project(), [{}, {"wall_loops": "4"}])
        plan = projects.export_plan(project["id"])
        with mock.patch.object(slicer, "runner", template_runner(code=1)):
            with self.assertRaises(jobs.JobError) as refused:
                projects.export_project(project["id"], plan["revision"], plan["fingerprint"], PACKAGE)
        self.assertEqual(refused.exception.issues[0]["code"], "export-template-unavailable")

    async def test_the_single_file_route_never_returns_a_package(self):
        project = self.set_plates(await self.two_plate_project(), [{}, {"wall_loops": "4"}])
        with self.assertRaises(jobs.JobError) as refused:
            projects.export_orca_project(project["id"])
        self.assertEqual(refused.exception.issues[0]["code"], "export-review-required")


class RoundTripTests(PackageCase):
    def import_file(self, data, name, target=None):
        review = projects.prepare_import(io.BytesIO(data), len(data), name, target=target)
        return review, review

    def effective(self, project_id, plate_id):
        return projects.effective_settings(project_id, plate_id)

    async def test_each_plate_file_opens_as_a_new_project_with_its_effective_settings(self):
        project = self.set_plates(await self.two_plate_project(), [{}, {"wall_loops": "4", "layer_height": "0.16"}])
        plan, artifact = self.package(project)
        _, files = self.open(artifact)
        for number, (name, plate) in enumerate((("Plate 1 - Front.3mf", 1), ("Plate 2 - Plate 2.3mf", 2)), start=1):
            original = projects.effective_settings(project["id"], plate)
            review = projects.prepare_import(io.BytesIO(files[name]), len(files[name]), name)
            self.assertFalse(review["blocked"], review["report"])
            done = projects.confirm_import(review["token"], {**review["binding"], "acknowledged": True})
            await self.run_worker()
            reopened = projects.get_project(done["id"])
            result = projects.effective_settings(done["id"], reopened["data"]["plates"][0]["id"])
            for key in ("wall_loops", "sparse_infill_density", "layer_height"):     # a one-Object file's Object settings become its Project's
                self.assertEqual(result["process"].get(key), original["objects"][0]["effective_process"].get(key), (name, key))
            self.assertEqual(result["filament"]["filament_type"], original["filament"]["filament_type"])
            self.assertEqual(reopened["data"]["bed"], project["data"]["bed"])

    async def test_a_multi_object_plate_file_keeps_its_objects_placement_settings_and_process(self):
        project = await self.two_plate_project()
        data = copy.deepcopy(project["data"])
        data["objects"][1]["plate"] = 1
        data["objects"][1]["position"] = [150.0, 120.0]
        data["plates"][0]["overrides"] = {"wall_loops": "4", "layer_height": "0.16"}
        project = projects.update_project(project["id"], {"data": data, "revision": project["revision"]})
        plan, artifact = self.package(project)
        _, files = self.open(artifact)
        (name,) = [n for n in files if n.endswith(".3mf")]
        review = projects.prepare_import(io.BytesIO(files[name]), len(files[name]), name)
        self.assertFalse(review["blocked"], review["report"])
        done = projects.confirm_import(review["token"], {**review["binding"], "acknowledged": True})
        await self.run_worker()
        reopened = projects.get_project(done["id"])
        original = projects.effective_settings(project["id"], 1)
        result = projects.effective_settings(done["id"], reopened["data"]["plates"][0]["id"])
        by_name = lambda r: {o["name"]: o["effective_process"] for o in r["objects"]}
        for obj, effective in by_name(original).items():
            for key in ("wall_loops", "layer_height"):
                self.assertEqual(by_name(result)[obj][key], effective[key], (obj, key))
        self.assertEqual({o["name"]: o["position"] for o in reopened["data"]["objects"]},
                         {o["name"]: o["position"] for o in project["data"]["objects"]})

    async def test_a_second_material_appends_to_a_compatible_project_as_a_snapshot_choice(self):
        project = self.asa_on_plate_two(await self.two_plate_project())
        plan, artifact = self.package(project)
        _, files = self.open(artifact)
        first = files["Plate 1 - Front.3mf"]
        second = files["Plate 2 - Plate 2.3mf"]
        review = projects.prepare_import(io.BytesIO(first), len(first), "one.3mf")
        base = projects.confirm_import(review["token"], {**review["binding"], "acknowledged": True})
        await self.run_worker()
        review = projects.prepare_import(io.BytesIO(second), len(second), "two.3mf", target=base["id"])
        self.assertFalse(review["blocked"], review["report"])
        done = projects.confirm_import(review["token"], {**review["binding"], "acknowledged": True})
        await self.run_worker()
        data = projects.get_project(done["id"])["data"]
        asa = [o for o in data["objects"] if str(o["model"]) in data.get("filaments", {})]
        self.assertEqual([data["filaments"][str(o["model"])]["preset"] for o in asa], ["Workshop ASA"])
        result = projects.effective_settings(done["id"], asa[0]["plate"])
        self.assertEqual(result["filament"]["filament_type"], ["ASA"])
        self.assertEqual(result["filament"]["nozzle_temperature"], ["260"])

    async def test_a_plate_file_with_a_different_bed_explains_the_append_conflict(self):
        project = self.set_plates(await self.two_plate_project(), [{}, {"wall_loops": "4"}])
        data = copy.deepcopy(project["data"])
        data["bed"] = "smooth" if data["bed"] == "textured" else "textured"
        project = projects.update_project(project["id"], {"data": data, "revision": project["revision"]})
        plan, artifact = self.package(project)
        _, files = self.open(artifact)
        other = await self.two_plate_project()
        raw = files["Plate 1 - Front.3mf"]
        review = projects.prepare_import(io.BytesIO(raw), len(raw), "textured.3mf", target=other["id"])
        self.assertIn("incompatible-bed", [b["code"] for b in review["report"]["blockers"]])
        self.assertTrue(review["blocked"])
        projects.cancel_import(review["token"])


class SliceMetadataTests(PackageCase):
    async def test_a_slice_plate_file_serializes_object_metadata_the_way_orca_reads_it(self):
        import tempfile
        from pathlib import Path
        project = await self.two_plate_project()
        obj = copy.deepcopy(project["data"]["objects"][0])
        obj["overrides"] = {"flag": True, "off": False, "list": ["1", "2"], "empty": None, "pct": "15%"}
        with tempfile.TemporaryDirectory() as tmp:
            projects.write_plate_3mf(Path(tmp) / "plate.3mf", project["id"], [obj], projects.machine_limits(
                projects.effective_settings(project["id"], 1)["machine"]))
            with zipfile.ZipFile(Path(tmp) / "plate.3mf") as archive:
                text = archive.read(orca_3mf.SETTINGS_PATH).decode()
        for key, value in {"flag": "1", "off": "0", "list": "1;2", "empty": "nil", "pct": "15%"}.items():
            self.assertIn(f'<metadata key="{key}" value="{value}"/>', text)

    async def test_a_turned_object_keeps_its_exact_bed_coordinates_in_the_slice_file(self):
        """Rounding a 45 degree turn to 4 decimals moved Orca's time estimate 0.15% away from the exported file's (found by the
        parity workflow); the Slice file now keeps the coordinates to 9 significant figures, like the export."""
        import re
        import tempfile
        from pathlib import Path
        project = await self.two_plate_project()
        obj = {**copy.deepcopy(project["data"]["objects"][0]), "rotation": [0.0, 0.0, 45.0], "scale": [0.8, 0.8, 0.8]}
        verts, _ = projects.load_mesh(project["id"], obj["model"])
        expected = projects.place(verts, obj)
        with tempfile.TemporaryDirectory() as tmp:
            projects.write_plate_3mf(Path(tmp) / "plate.3mf", project["id"], [obj], projects.machine_limits(
                projects.effective_settings(project["id"], 1)["machine"]))
            with zipfile.ZipFile(Path(tmp) / "plate.3mf") as archive:
                model = next(archive.read(n).decode() for n in archive.namelist() if n.endswith(".model"))
        written = [tuple(map(float, v)) for v in re.findall(r'<vertex x="([^"]+)" y="([^"]+)" z="([^"]+)"', model)]
        self.assertEqual(len(written), len(expected))
        worst = max(abs(a - b) for row, out in zip(expected, written) for a, b in zip(row, out))
        self.assertLess(worst, 1e-6)


class PackageHttpTests(unittest.TestCase):
    setUp = test_projects.ProjectApiTests.setUp
    tearDown = test_projects.ProjectApiTests.tearDown
    call = test_projects.ProjectApiTests.call

    def test_the_package_route_is_revision_bound_and_never_disguised_as_a_3mf(self):
        pid = self.call("POST", "/api/projects", {"name": "Plan: me"})[1]["id"]
        code, plan = self.call("GET", f"/api/projects/{pid}/export-plan")
        self.assertEqual((code, plan["package"]["available"]), (200, False))
        code, refused = self.call("GET", f"/api/projects/{pid}/export-package.zip?revision={plan['revision']}&fingerprint={plan['fingerprint']}")
        self.assertEqual((code, refused["issues"][0]["code"]), (409, "nothing-to-export"))
        code, stale = self.call("GET", f"/api/projects/{pid}/export-package.zip?revision=999&fingerprint=abc")
        self.assertEqual((code, stale["issues"][0]["code"]), (409, "export-plan-stale"))
        code, unbound = self.call("GET", f"/api/projects/{pid}/export-package.zip")
        self.assertEqual(code, 409, "a package is only generated for a plan the caller reviewed")
        self.assertEqual(unbound["issues"][0]["code"], "export-plan-stale")
