"""Reviewed adoption of preset refreshes and Spoolman properties, through the Project service boundary."""
import copy
import io
import json
import os
import unittest
from unittest import mock

import farm
import jobs
import projects
import settings_review
import slicer
from tests import test_projects
from tests.test_slicer import cube_stl

MACHINE = slicer.DEFAULT_PRESETS["machine"]
PROCESS = slicer.DEFAULT_PRESETS["process"]
PETG_SPOOL = {"id": 7, "remaining_weight": 800.0, "initial_weight": 1000.0, "price": 25.0, "location": "", "archived": False,
              "filament": {"material": "PETG", "name": "Clear", "density": 1.27, "diameter": 1.75, "weight": 1000,
                           "settings_extruder_temp": 238, "settings_bed_temp": 75, "vendor": {"name": "Sunlu"}}}


def tree(path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*")) if path.exists() else []


class ReviewCase(unittest.IsolatedAsyncioTestCase):
    setUp = test_projects.ProjectTests.setUp
    tearDown = test_projects.ProjectTests.tearDown
    asyncTearDown = test_projects.ProjectTests.asyncTearDown
    run_worker = test_projects.ProjectTests.run_worker
    project_with_cube = test_projects.ProjectTests.project_with_cube

    def preset(self, name, material, temperature, **more):
        return slicer.save_user_preset("filament", name, "Elegoo PLA @ECC",
                                       {"filament_type": [material], "nozzle_temperature": [str(temperature)], **more})

    def raw_preset(self, name, **fields):
        """Change a saved user preset the way an outside editor could, past Spooler's own validation."""
        for path in slicer.user_presets_dir("filament").glob("*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["name"] == name:
                path.write_text(json.dumps({**record, **fields}), encoding="utf-8")
                return
        raise AssertionError(name)

    def review(self, project, target=None, **more):
        body = {"source": "preset", "target": target or {"kind": "default"}}
        preview = settings_review.preview(project["id"], body)
        return preview, {**body, "revision": preview["revision"], "token": preview["token"], **more}

    def edit(self, project, change):
        data = copy.deepcopy(project["data"])
        change(data)
        return projects.update_project(project["id"], {"data": data, "revision": project["revision"]})

    async def petg_project(self, extra_model=False):
        """A Project whose default Filament is the user's PETG; with `extra_model`, Model 2 on Plate 2 prints in ASA."""
        self.preset("Workshop PETG", "PETG", 240)
        project = await self.project_with_cube()
        if extra_model:
            self.preset("Workshop ASA", "ASA", 260)
            mesh = cube_stl(10)
            projects.import_model(project["id"], io.BytesIO(mesh), len(mesh), "second.stl")
            await self.run_worker()
            project = projects.get_project(project["id"])
        bundle = slicer.create_bundle(MACHINE, PROCESS, "Workshop PETG")

        def change(data):
            data["profile"]["bundle_id"] = bundle["id"]
            if extra_model:
                data["plates"].append({"id": 2, "name": "ASA Plate"})
                data["objects"][1]["plate"] = 2
                data["filaments"] = {"2": {"preset": "Workshop ASA", "colour": "black"}}
        return self.edit(project, change)


class PresetRefreshPreview(ReviewCase):
    async def test_preview_shows_before_after_material_and_nozzle_without_changing_anything(self):
        project = await self.petg_project()
        self.preset("Workshop PETG", "PETG", 250)          # the user edits the preset after the Project saved it
        before_files = tree(slicer.profiles_dir())

        preview = settings_review.preview(project["id"], {"source": "preset", "target": {"kind": "default"}})

        self.assertEqual(preview["revision"], project["revision"])
        self.assertEqual(preview["current"]["material"], ["PETG"])
        self.assertEqual(preview["proposed"]["material"], ["PETG"])
        self.assertEqual(preview["proposed"]["nozzle"], "0.4")
        self.assertEqual(preview["proposed"]["name"], "Workshop PETG")
        change = next(c for c in preview["changes"] if c["key"] == "nozzle_temperature")
        self.assertEqual((change["before"], change["after"]), (["240"], ["250"]))
        self.assertEqual(preview["issues"], [])
        self.assertEqual([p["id"] for p in preview["affected"]["plates"]], [1])
        # the saved effective settings still use the immutable 240 until the change is adopted
        self.assertEqual(projects.effective_settings(project["id"], 1)["filament"]["nozzle_temperature"], ["240"])
        self.assertEqual(projects.get_project(project["id"])["revision"], project["revision"])
        self.assertEqual(tree(slicer.profiles_dir()), before_files)       # cancelling leaves no stored profile behind


class PresetRefreshApply(ReviewCase):
    async def test_apply_adopts_only_the_default_and_keeps_alternate_snapshots_overrides_and_undo(self):
        project = await self.petg_project(extra_model=True)
        project = self.edit(project, lambda d: (d["profile"]["filament"].update(nozzle_temperature_initial_layer=["245"]),
                                                d["plates"][0].update(overrides={"wall_loops": "5"}),
                                                d["objects"][0].update(overrides={"sparse_infill_density": "25%"})))
        old_default = project["data"]["profile"]["bundle_id"]
        asa_snapshot = project["data"]["filaments"]["2"]["snapshot"]
        self.preset("Workshop PETG", "PETG", 250)
        self.preset("Workshop ASA", "ASA", 300)             # an alternate's catalogue entry changes too, and must not follow
        preview, request = self.review(project)

        adopted = settings_review.apply(project["id"], request)

        self.assertEqual(adopted["revision"], project["revision"] + 1)
        self.assertNotEqual(adopted["data"]["profile"]["bundle_id"], old_default)
        self.assertEqual(adopted["data"]["profile"]["filament"], {"nozzle_temperature_initial_layer": ["245"]})
        self.assertEqual(adopted["data"]["filaments"], project["data"]["filaments"])
        self.assertEqual(adopted["data"]["filaments"]["2"]["snapshot"], asa_snapshot)
        self.assertEqual(adopted["data"]["plates"][0]["overrides"], {"wall_loops": "5"})
        self.assertEqual(adopted["data"]["objects"][0]["overrides"], {"sparse_infill_density": "25%"})
        self.assertEqual(projects.effective_settings(project["id"], 1)["filament"]["nozzle_temperature"], ["250"])
        asa = projects.effective_settings(project["id"], 2)
        self.assertEqual((asa["filament"]["filament_type"], asa["filament"]["nozzle_temperature"]), (["ASA"], ["260"]))
        # the previous locked profile stays available, so an ordinary undo (a normal edit back) still works
        undone = self.edit(adopted, lambda d: d["profile"].update(bundle_id=old_default))
        self.assertEqual(projects.effective_settings(undone["id"], 1)["filament"]["nozzle_temperature"], ["240"])

    async def test_adoption_makes_a_sliced_plate_outdated(self):
        project = await self.petg_project()
        projects.request_slice(project["id"], 1)
        from tests.test_slicer import fake_runner
        with mock.patch.object(slicer, "runner", fake_runner()):
            await self.run_worker()
        self.assertFalse(projects.get_project(project["id"])["slices"]["1"]["outdated"])
        self.preset("Workshop PETG", "PETG", 250)
        preview, request = self.review(projects.get_project(project["id"]))
        self.assertEqual(preview["affected"]["outdated_slices"], [1])
        settings_review.apply(project["id"], request)
        self.assertTrue(projects.get_project(project["id"])["slices"]["1"]["outdated"])


class PresetRefreshRejections(ReviewCase):
    async def test_changed_project_or_catalogue_after_preview_requires_a_new_preview(self):
        project = await self.petg_project()
        self.preset("Workshop PETG", "PETG", 250)
        _, request = self.review(project)

        edited = self.edit(project, lambda d: d["plates"][0].update(name="Renamed"))      # a concurrent edit
        with self.assertRaisesRegex(jobs.JobError, "preview the change again") as stale:
            settings_review.apply(project["id"], request)
        self.assertEqual(stale.exception.status, 409)
        self.assertEqual(projects.get_project(project["id"])["data"], edited["data"])

        _, request = self.review(edited)
        self.preset("Workshop PETG", "PETG", 255)                                           # the catalogue moves on
        with self.assertRaisesRegex(jobs.JobError, "changed since this review") as stale:
            settings_review.apply(project["id"], request)
        self.assertEqual(stale.exception.status, 409)
        self.assertEqual(projects.get_project(project["id"])["data"], edited["data"])

        _, request = self.review(edited)
        settings_review.apply(project["id"], request)
        with self.assertRaises(jobs.JobError):                                              # the same review cannot apply twice
            settings_review.apply(project["id"], request)

    async def test_deleted_or_incompatible_preset_cannot_be_adopted_and_saved_settings_keep_working(self):
        project = await self.petg_project()
        slicer.delete_user_preset("filament", "Workshop PETG")
        preview, request = self.review(project)
        self.assertEqual(preview["issues"][0]["code"], "preset-unavailable")
        self.assertIsNone(preview["proposed"])
        with self.assertRaises(jobs.JobError) as rejected:
            settings_review.apply(project["id"], request)
        self.assertEqual(rejected.exception.issues[0]["code"], "preset-unavailable")
        self.assertEqual(projects.get_project(project["id"])["data"], project["data"])
        self.assertEqual(projects.effective_settings(project["id"], 1)["filament"]["nozzle_temperature"], ["240"])

        self.preset("Workshop PETG", "PETG", 250)
        self.raw_preset("Workshop PETG", compatible_printers=["Elegoo Centauri Carbon 2 0.4 nozzle"])
        preview, request = self.review(project)
        self.assertEqual(preview["issues"][0]["code"], "incompatible-filament")
        with self.assertRaises(jobs.JobError):
            settings_review.apply(project["id"], request)
        self.assertEqual(projects.get_project(project["id"])["data"], project["data"])

    async def test_material_change_needs_explicit_acknowledgement(self):
        project = await self.petg_project()
        self.preset("Workshop PETG", "ASA", 260)
        preview, request = self.review(project)
        self.assertTrue(preview["material_changed"])
        self.assertEqual((preview["current"]["material"], preview["proposed"]["material"]), (["PETG"], ["ASA"]))
        self.assertEqual(preview["issues"][0]["code"], "material-change")
        with self.assertRaisesRegex(jobs.JobError, "PETG to ASA"):
            settings_review.apply(project["id"], request)
        self.assertEqual(projects.get_project(project["id"])["data"], project["data"])
        adopted = settings_review.apply(project["id"], {**request, "accept_material_change": True})
        self.assertEqual(projects.effective_settings(adopted["id"], 1)["filament"]["filament_type"], ["ASA"])

    async def test_new_conflict_with_existing_overrides_rejects_everything_without_rebasing(self):
        project = await self.petg_project()
        project = self.edit(project, lambda d: (d["plates"][0].update(overrides={"wall_loops": "5"}),
                                                d["objects"][0].update(overrides={"sparse_infill_density": "25%"})))
        self.raw_preset("Workshop PETG", nozzle_temperature_range_low=["300"], nozzle_temperature_range_high=["200"])
        preview, request = self.review(project)
        self.assertEqual(preview["issues"][0]["code"], "coupled-setting")
        self.assertEqual(preview["issues"][0]["key"], "nozzle_temperature_range_low")
        with self.assertRaises(jobs.JobError) as rejected:
            settings_review.apply(project["id"], request)
        self.assertEqual(rejected.exception.issues[0]["code"], "coupled-setting")
        self.assertEqual(projects.get_project(project["id"])["data"], project["data"])


class AlternateRefresh(ReviewCase):
    async def test_alternate_refresh_replaces_only_that_models_snapshot_and_keeps_the_old_one(self):
        project = await self.petg_project(extra_model=True)
        old = project["data"]["filaments"]["2"]
        self.preset("Workshop ASA", "ASA", 300)
        self.preset("Workshop PETG", "PETG", 250)           # the default's catalogue entry changing is not part of this review
        target = {"kind": "model", "model_id": 2}
        preview, request = self.review(project, target)

        self.assertEqual(preview["applies_to"], "model-filament")
        self.assertEqual((preview["current"]["sha256"], preview["proposed"]["name"]), (old["snapshot"]["sha256"], "Workshop ASA"))
        self.assertEqual((preview["proposed"]["material"], preview["proposed"]["nozzle"]), (["ASA"], "0.4"))
        change = next(c for c in preview["changes"] if c["key"] == "nozzle_temperature")
        self.assertEqual((change["before"], change["after"]), (["260"], ["300"]))
        self.assertEqual([p["id"] for p in preview["affected"]["plates"]], [2])

        adopted = settings_review.apply(project["id"], request)

        choice = adopted["data"]["filaments"]["2"]
        self.assertEqual((choice["preset"], choice["colour"]), ("Workshop ASA", "black"))
        self.assertNotEqual(choice["snapshot"]["sha256"], old["snapshot"]["sha256"])
        self.assertEqual(adopted["data"]["profile"], project["data"]["profile"])    # the default is unchanged
        self.assertEqual(projects.effective_settings(project["id"], 2)["filament"]["nozzle_temperature"], ["300"])
        self.assertEqual(projects.effective_settings(project["id"], 1)["filament"]["nozzle_temperature"], ["240"])
        slicer.filament_snapshot(old["snapshot"], slicer.bundle_settings(adopted["data"]["profile"]["bundle_id"])["machine"])   # undo target intact

    async def test_alternate_without_verified_content_can_be_reselected_through_the_same_review(self):
        project = await self.petg_project(extra_model=True)
        import database
        data = copy.deepcopy(project["data"])
        data["filaments"]["2"] = {"preset": "Workshop ASA", "colour": "black"}          # a legacy name-only choice
        with database.transaction() as conn:
            conn.execute("UPDATE projects SET data = ? WHERE id = ?", (json.dumps(data), project["id"]))
        project = projects.get_project(project["id"])
        preview, request = self.review(project, {"kind": "model", "model_id": 2})
        self.assertIsNone(preview["current"])
        self.assertEqual(preview["changes"], [])
        adopted = settings_review.apply(project["id"], request)
        self.assertEqual(adopted["data"]["filaments"]["2"]["snapshot"]["material"], ["ASA"])

    async def test_model_without_an_alternate_has_nothing_to_refresh(self):
        project = await self.petg_project(extra_model=True)
        with self.assertRaisesRegex(jobs.JobError, "no alternate Filament"):
            settings_review.preview(project["id"], {"source": "preset", "target": {"kind": "model", "model_id": 1}})


class SpoolAdoption(ReviewCase):
    def spoolman(self, **spool):
        """A disposable Spoolman: `self.spools` is what its API reports. The live service is never contacted."""
        self.spools = {7: copy.deepcopy(PETG_SPOOL)}
        self.spools[7].update(spool)
        patcher = mock.patch.object(farm, "_get", lambda path: self.spools[int(path.rsplit("/", 1)[1])])
        patcher.start()
        self.addCleanup(patcher.stop)

    def review_spool(self, project, **more):
        body = {"source": "spool", "spool_id": 7}
        preview = settings_review.preview(project["id"], body)
        return preview, {**body, "revision": preview["revision"], "token": preview["token"], **more}

    async def test_preview_is_default_only_with_before_after_and_separate_stock(self):
        self.spoolman()
        project = await self.petg_project(extra_model=True)
        preview, _ = self.review_spool(project)

        self.assertEqual(preview["applies_to"], "project-default")
        by_key = {c["key"]: (c["before"], c["after"]) for c in preview["changes"]}
        self.assertEqual(by_key["nozzle_temperature"], (["240"], ["238"]))
        self.assertEqual(by_key["filament_density"][1], ["1.27"])
        self.assertEqual(by_key["textured_plate_temp"][1], ["75"])
        self.assertEqual(by_key["filament_cost"][1], ["25.0"])
        self.assertEqual(preview["proposed"]["material"], ["PETG"])          # validated against the chosen default, unchanged
        self.assertEqual(preview["spool"]["material"], "PETG")
        self.assertEqual(preview["stock"], {"remaining_weight": 800.0, "initial_weight": 1000.0})
        self.assertFalse(any("remaining" in c["key"] for c in preview["changes"]))
        self.assertEqual([p["id"] for p in preview["affected"]["plates"]], [1])
        self.assertEqual([p["id"] for p in preview["affected"]["alternate_plates"]], [2])
        self.assertEqual(preview["preserved"]["alternates"][0]["sha256"], project["data"]["filaments"]["2"]["snapshot"]["sha256"])
        self.assertEqual(projects.get_project(project["id"])["revision"], project["revision"])

    async def test_apply_changes_only_the_default_overrides_and_remaining_stock_never_matters(self):
        self.spoolman()
        project = await self.petg_project(extra_model=True)
        project = self.edit(project, lambda d: d["profile"]["filament"].update(
            filament_retraction_length=[None], filament_cost=["99"]))
        preview, request = self.review_spool(project)
        self.spools[7]["remaining_weight"] = 120.0                    # stock moves between review and apply: not a settings change

        adopted = settings_review.apply(project["id"], request)

        profile = adopted["data"]["profile"]
        self.assertEqual(profile["spool_id"], 7)
        self.assertEqual(profile["filament"]["nozzle_temperature"], ["238"])
        self.assertEqual(profile["filament"]["filament_retraction_length"], ["nil"])    # unrelated nullable override kept
        self.assertEqual(profile["filament"]["filament_cost"], ["25.0"])
        self.assertEqual(adopted["data"]["filaments"], project["data"]["filaments"])   # ASA alternate keeps its own snapshot
        asa = projects.effective_settings(project["id"], 2)["filament"]
        self.assertEqual((asa["filament_type"], asa["nozzle_temperature"]), (["ASA"], ["260"]))
        self.assertEqual(projects.effective_settings(project["id"], 1)["filament"]["nozzle_temperature"], ["238"])

        # later Spoolman changes (properties or stock) alone leave the saved effective settings alone
        self.spools[7]["filament"].update(settings_extruder_temp=225, density=1.4)
        self.spools[7]["remaining_weight"] = 5.0
        self.assertEqual(projects.get_project(project["id"])["data"], adopted["data"])
        self.assertEqual(projects.effective_settings(project["id"], 1)["filament"]["nozzle_temperature"], ["238"])
        later = settings_review.preview(project["id"], {"source": "spool", "spool_id": 7})
        self.assertEqual(dict((c["key"], c["after"]) for c in later["changes"])["nozzle_temperature"], ["225"])
        self.assertEqual(projects.get_project(project["id"])["revision"], adopted["revision"])

    async def test_changed_spool_properties_or_project_require_a_new_preview(self):
        self.spoolman()
        project = await self.petg_project()
        _, request = self.review_spool(project)
        self.spools[7]["filament"]["settings_extruder_temp"] = 245
        with self.assertRaisesRegex(jobs.JobError, "changed since this review") as stale:
            settings_review.apply(project["id"], request)
        self.assertEqual(stale.exception.status, 409)
        _, request = self.review_spool(project)
        edited = self.edit(project, lambda d: d["plates"][0].update(name="Renamed"))
        with self.assertRaisesRegex(jobs.JobError, "Project changed"):
            settings_review.apply(project["id"], request)
        self.assertEqual(projects.get_project(project["id"])["data"], edited["data"])

    async def test_material_mismatch_is_rejected_and_names_a_matching_preset(self):
        self.spoolman(**{"filament": {**PETG_SPOOL["filament"], "material": "ASA"}})
        project = await self.petg_project()
        preview, request = self.review_spool(project)
        self.assertEqual(preview["issues"][0]["code"], "spool-material-mismatch")
        self.assertIn("ASA", preview["issues"][0]["message"])
        with self.assertRaises(jobs.JobError) as rejected:
            settings_review.apply(project["id"], request)
        self.assertEqual(rejected.exception.issues[0]["code"], "spool-material-mismatch")
        self.assertEqual(projects.get_project(project["id"])["data"], project["data"])

    async def test_properties_the_preset_cannot_accept_are_listed_not_silently_dropped(self):
        self.spoolman(**{"filament": {**PETG_SPOOL["filament"], "settings_extruder_temp": 99999}})
        project = await self.petg_project()
        preview, request = self.review_spool(project)
        self.assertIn("nozzle_temperature", [s["key"] for s in preview["skipped"]])
        self.assertNotIn("nozzle_temperature", [c["key"] for c in preview["changes"]])
        adopted = settings_review.apply(project["id"], request)
        self.assertEqual(projects.effective_settings(adopted["id"], 1)["filament"]["nozzle_temperature"], ["240"])

    async def test_unreachable_spoolman_is_an_error_and_changes_nothing(self):
        project = await self.petg_project()
        with mock.patch("urllib.request.urlopen", side_effect=OSError("refused")):
            with self.assertRaisesRegex(jobs.JobError, "Spoolman isn't reachable"):
                settings_review.preview(project["id"], {"source": "spool", "spool_id": 7})
        self.assertEqual(projects.get_project(project["id"])["data"], project["data"])


@unittest.skipUnless(test_projects.GEOMETRY, "geometry libraries not installed")
class ReviewHttp(unittest.TestCase):
    setUp = test_projects.ProjectApiTests.setUp
    tearDown = test_projects.ProjectApiTests.tearDown
    call = test_projects.ProjectApiTests.call

    def petg_project(self):
        slicer.save_user_preset("filament", "Workshop PETG", "Elegoo PLA @ECC", {"filament_type": ["PETG"], "nozzle_temperature": ["240"]})
        project = self.call("POST", "/api/projects", {"name": "Review"})[1]
        bundle = slicer.create_bundle(MACHINE, PROCESS, "Workshop PETG")
        data = copy.deepcopy(project["data"])
        data["profile"]["bundle_id"] = bundle["id"]
        return self.call("PATCH", f"/api/projects/{project['id']}", {"data": data, "revision": project["revision"]})[1]

    def test_preset_refresh_review_and_adoption_over_http(self):
        project = self.petg_project()
        base = f"/api/projects/{project['id']}/settings"
        slicer.save_user_preset("filament", "Workshop PETG", "Elegoo PLA @ECC", {"filament_type": ["PETG"], "nozzle_temperature": ["250"]})
        code, preview = self.call("POST", f"{base}/review", {"source": "preset", "target": {"kind": "default"}})
        self.assertEqual(code, 200)
        self.assertEqual(self.call("GET", f"/api/projects/{project['id']}")[1]["revision"], project["revision"])   # a review writes nothing
        adopt = {"source": "preset", "target": {"kind": "default"}, "revision": preview["revision"], "token": preview["token"]}

        self.assertEqual(self.call("POST", f"{base}/adopt", {**adopt, "token": "stale"})[0], 409)
        self.assertEqual(self.call("POST", f"{base}/adopt", {**adopt, "revision": None})[0], 409)
        code, adopted = self.call("POST", f"{base}/adopt", adopt)
        self.assertEqual((code, adopted["revision"]), (200, project["revision"] + 1))
        self.assertNotEqual(adopted["data"]["profile"]["bundle_id"], project["data"]["profile"]["bundle_id"])
        self.assertEqual(self.call("POST", f"{base}/adopt", adopt)[0], 409)          # the same review cannot be replayed

    def test_blocked_adoption_returns_the_reasons(self):
        project = self.petg_project()
        slicer.delete_user_preset("filament", "Workshop PETG")
        base = f"/api/projects/{project['id']}/settings"
        code, preview = self.call("POST", f"{base}/review", {"source": "preset"})
        self.assertEqual((code, preview["issues"][0]["code"]), (200, "preset-unavailable"))
        code, rejected = self.call("POST", f"{base}/adopt", {"source": "preset", "revision": preview["revision"], "token": preview["token"]})
        self.assertEqual((code, rejected["issues"][0]["code"]), (409, "preset-unavailable"))
        self.assertEqual(self.call("POST", f"{base}/review", {"source": "other"})[0], 400)

    def test_spool_review_and_adoption_over_http(self):
        project = self.petg_project()
        spools = {7: copy.deepcopy(PETG_SPOOL)}
        with mock.patch.object(farm, "_get", lambda path: spools[int(path.rsplit("/", 1)[1])]):
            base = f"/api/projects/{project['id']}/settings"
            code, preview = self.call("POST", f"{base}/review", {"source": "spool", "spool_id": 7})
            self.assertEqual((code, preview["applies_to"]), (200, "project-default"))
            code, adopted = self.call("POST", f"{base}/adopt", {"source": "spool", "spool_id": 7, "revision": preview["revision"], "token": preview["token"]})
        self.assertEqual((code, adopted["data"]["profile"]["spool_id"]), (200, 7))
        self.assertEqual(adopted["data"]["profile"]["filament"]["nozzle_temperature"], ["238"])


if __name__ == "__main__":
    unittest.main()
