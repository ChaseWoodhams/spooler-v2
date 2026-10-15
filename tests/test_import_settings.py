"""Imported material and Plate settings: unmatched presets are resolved explicitly, appended files keep their settings (ticket 10)."""

import io
import json
import unittest
from unittest import mock

import jobs
import projects
import slicer
from tests.orca_archives import archive
from tests import test_import_review as base
from tests import test_projects
from tests.test_import_review import PROJECT, staged

CC = "Elegoo Centauri Carbon 0.4 nozzle"
STANDARD = "0.20mm Standard @Elegoo CC 0.4 nozzle"
PETG_FILE = {**PROJECT, "filament_settings_id": ["Brand PETG"], "filament_type": ["PETG"], "nozzle_diameter": ["0.4"],
             "nozzle_temperature": ["245"], "textured_plate_temp": ["80"], "filament_flow_ratio": ["0.95"],
             "filament_retraction_length": ["nil"], "fan_max_speed": ["60"], "filament_start_gcode": ["M117 petg"]}
ASA_FILE = {**PETG_FILE, "filament_settings_id": ["Brand ASA"], "filament_type": ["ASA"], "nozzle_temperature": ["255"]}
CUBES = dict(objects=[{"id": 1, "name": "Box"}, {"id": 2, "name": "Lid"}], plates=[{"id": 1, "name": "Front"}])


def add_presets(case):
    """PETG and ASA presets for the Centauri Carbon, alongside the fake tree's PLA."""
    folder = next((case.dir / "tools").rglob("filament")) / "ECC"
    for name, material, temp in (("Elegoo PETG @ECC", "PETG", "240"), ("Elegoo ASA @ECC", "ASA", "260")):
        (folder / f"{name}.json").write_text(json.dumps({
            "type": "filament", "from": "system", "instantiation": "true", "name": name, "inherits": "fdm_filament_common",
            "filament_type": [material], "nozzle_temperature": [temp], "compatible_printers": [CC]}))
    slicer._preset_cache.clear()


class SettingsCase(base.ImportReviewCase):
    def setUp(self):
        super().setUp()
        add_presets(self)

    def confirm(self, review, **changes):
        return projects.confirm_import(review["token"], {**review["binding"], "acknowledged": True, **changes})

    def resolve(self, review, resolution):
        return projects.resolve_import(review["token"], resolution)

    def filament_of(self, project_id):
        bundle = slicer.bundle_settings(projects.get_project(project_id)["data"]["profile"]["bundle_id"])
        return bundle["filament"]


class UnmatchedMaterialTests(SettingsCase):
    async def test_the_blocker_offers_the_files_own_material_and_compatible_presets_with_differences(self):
        review = self.prepare(archive(project=PETG_FILE, **CUBES), "set.3mf")
        (blocker,) = review["report"]["blockers"]
        self.assertEqual(blocker["code"], "unmatched-material")
        imported = blocker["resolutions"]["imported"]
        self.assertEqual((imported["available"], imported["material"], imported["nozzle"]), (True, "PETG", "0.4"))
        presets = {p["preset"]: p for p in blocker["resolutions"]["presets"]}
        self.assertEqual(set(presets), {"Elegoo PLA @ECC", "Elegoo PETG @ECC", "Elegoo ASA @ECC"})
        self.assertEqual([p["same_material"] for p in blocker["resolutions"]["presets"] if p["preset"] == "Elegoo PETG @ECC"], [True])
        self.assertFalse(presets["Elegoo PLA @ECC"]["same_material"])
        change = {d["key"]: d for d in presets["Elegoo PETG @ECC"]["differences"]}["nozzle_temperature"]
        self.assertEqual((change["before"], change["after"]), ("245", "240"))
        self.assertEqual(projects.list_projects(include_quick=True), [])
        projects.cancel_import(review["token"])

    async def test_resolving_with_the_imported_configuration_keeps_the_material_in_an_immutable_snapshot(self):
        review = self.prepare(archive(project=PETG_FILE, **CUBES), "set.3mf")
        resolved = self.resolve(review, {"material": {"choice": "imported"}})
        self.assertFalse(resolved["blocked"])
        self.assertNotEqual(resolved["binding"]["conversion"], review["binding"]["conversion"])
        self.assertEqual(resolved["report"]["resolution"]["material"]["choice"], "imported")
        done = self.confirm(resolved)
        values = self.filament_of(done["id"])
        self.assertEqual((values["filament_type"], values["nozzle_temperature"], values["filament_flow_ratio"]), (["PETG"], ["245"], ["0.95"]))
        self.assertEqual(values["filament_retraction_length"], ["nil"], "nullable inheritance survives as Orca's canonical value")
        self.assertNotIn("filament_start_gcode", values, "scripts stay blocked")
        differences = done["import_record"]["differences"]
        self.assertIn(("filament_start_gcode", "blocked-setting"), [(d["key"], d["code"]) for d in differences])
        self.assertEqual(done["import_record"]["resolution"]["material"]["choice"], "imported")
        self.assertEqual(staged(), [])

    async def test_resolving_to_a_same_material_preset_applies_the_files_differences_on_it(self):
        review = self.prepare(archive(project=ASA_FILE, **CUBES), "set.3mf")
        done = self.confirm(self.resolve(review, {"material": {"choice": "preset", "preset": "Elegoo ASA @ECC"}}))
        project = projects.get_project(done["id"])
        self.assertEqual(self.filament_of(done["id"])["filament_type"], ["ASA"])
        self.assertEqual(project["data"]["profile"]["filament"]["nozzle_temperature"], ["255"])

    async def test_a_preset_of_another_material_needs_its_own_explicit_acceptance(self):
        review = self.prepare(archive(project=PETG_FILE, **CUBES), "set.3mf")
        with self.assertRaises(jobs.JobError) as caught:
            self.resolve(review, {"material": {"choice": "preset", "preset": "Elegoo PLA @ECC"}})
        self.assertEqual((caught.exception.status, caught.exception.issues[0]["code"]), (409, "material-change-unconfirmed"))
        self.assertEqual(self.prepare_state(review)["report"]["blockers"][0]["code"], "unmatched-material")
        accepted = self.resolve(review, {"material": {"choice": "preset", "preset": "Elegoo PLA @ECC", "accept_material_change": True}})
        self.assertFalse(accepted["blocked"])
        self.assertTrue(accepted["report"]["resolution"]["material"]["material_change"])
        done = self.confirm(accepted)
        self.assertEqual(self.filament_of(done["id"])["filament_type"], ["PLA"])

    def prepare_state(self, review):
        return projects.get_import_review(review["token"])

    async def test_unknown_presets_and_unselected_resolutions_do_not_unblock(self):
        review = self.prepare(archive(project=PETG_FILE, **CUBES), "set.3mf")
        for resolution in ({"material": {"choice": "preset", "preset": "No such preset"}}, {"material": {"choice": "default"}}, {}):
            with self.subTest(resolution), self.assertRaises(jobs.JobError):
                self.resolve(review, resolution)
        with self.assertRaises(jobs.JobError) as caught:
            self.confirm(review)
        self.assertEqual(caught.exception.issues[0]["code"], "import-blocked")
        self.assertEqual(projects.list_projects(include_quick=True), [])
        projects.cancel_import(review["token"])

    async def test_a_file_that_names_no_material_never_falls_back_to_the_default(self):
        nameless = {k: v for k, v in PETG_FILE.items() if k != "filament_settings_id"}
        review = self.prepare(archive(project=nameless, **CUBES), "set.3mf")
        (blocker,) = review["report"]["blockers"]
        self.assertEqual((blocker["code"], blocker["requested"]["material"], blocker["proposed"]["material"]), ("unmatched-material", "PETG", "PLA"))
        self.assertTrue(blocker["resolutions"]["imported"]["available"])
        projects.cancel_import(review["token"])
        typeless = {k: v for k, v in nameless.items() if k != "filament_type"}
        review = self.prepare(archive(project={**typeless, "filament_settings_id": ["Mystery"]}, **CUBES), "set.3mf")
        (blocker,) = review["report"]["blockers"]
        self.assertFalse(blocker["resolutions"]["imported"]["available"])
        with self.assertRaises(jobs.JobError):
            self.resolve(review, {"material": {"choice": "imported"}})
        projects.cancel_import(review["token"])

    async def test_a_wrong_nozzle_makes_the_imported_configuration_unavailable(self):
        review = self.prepare(archive(project={**PETG_FILE, "nozzle_diameter": ["0.6"]}, **CUBES), "set.3mf")
        imported = review["report"]["blockers"][0]["resolutions"]["imported"]
        self.assertFalse(imported["available"])
        self.assertIn("0.6", imported["reason"])
        projects.cancel_import(review["token"])

    async def test_a_resolution_is_bound_to_what_was_reviewed(self):
        review = self.prepare(archive(project=PETG_FILE, **CUBES), "set.3mf")
        resolved = self.resolve(review, {"material": {"choice": "imported"}})
        with self.assertRaises(jobs.JobError) as caught:
            projects.confirm_import(review["token"], {**review["binding"], "acknowledged": True})
        self.assertEqual(caught.exception.issues[0]["code"], "import-review-stale")
        self.assertEqual(staged(), [], "a stale confirmation discards the staging")
        self.assertEqual(projects.list_projects(include_quick=True), [])
        self.assertTrue(resolved["token"])

    async def test_a_single_model_file_resolves_the_same_way(self):
        review = self.prepare(archive(project=ASA_FILE, objects=[{"id": 1, "name": "Solo"}]), "solo.3mf")
        self.assertTrue(review["blocked"])
        done = self.confirm(self.resolve(review, {"material": {"choice": "imported"}}))
        self.assertEqual(self.filament_of(done["id"])["filament_type"], ["ASA"])


class AppendSettingsTests(SettingsCase):
    PETG_PRESET_FILE = {**PROJECT, "filament_settings_id": ["Elegoo PETG @ECC"], "filament_type": ["PETG"], "nozzle_diameter": ["0.4"],
                        "wall_loops": "5", "layer_height": "0.16", "sparse_infill_density": "25%", "post_process": ["rm -rf x"]}

    async def test_a_matched_material_and_process_deltas_become_a_snapshot_choice_and_plate_overrides(self):
        project = await self.project_with_cube()
        before = projects.get_project(project["id"])
        review = self.prepare(archive(project=self.PETG_PRESET_FILE, **CUBES), "petg.3mf", target=project["id"])
        self.assertFalse(review["blocked"])
        done = self.confirm(review)
        data = done["data"]
        new_models = [m["id"] for m in done["models"] if m["id"] not in {x["id"] for x in before["models"]}]
        for model_id in new_models:
            choice = data["filaments"][str(model_id)]
            self.assertEqual(choice["preset"], "Elegoo PETG @ECC")
            self.assertEqual(slicer.filament_snapshot(choice["snapshot"], slicer.bundle_settings(data["profile"]["bundle_id"])["machine"])[0]["material"], ["PETG"])
        self.assertEqual(data["profile"], before["data"]["profile"], "the Project defaults are untouched")
        imported = [p for p in data["plates"] if p["id"] not in {x["id"] for x in before["data"]["plates"]}]
        self.assertEqual([p["overrides"] for p in imported],
                         [{"layer_height": "0.16", "wall_loops": "5"}])
        self.assertEqual([p["overrides"] for p in before["data"]["plates"]], [{}])
        found = done["import_record"]["differences"]
        self.assertIn(("post_process", "blocked-setting"), [(d.get("key"), d["code"]) for d in found])
        uncompared = next(d for d in found if d["code"] == "uncompared-settings")
        self.assertIn("sparse_infill_density", uncompared["keys"], "a value the preset doesn't state can't be compared, so it is reported, not silently dropped")

    async def test_each_independent_plate_file_keeps_its_own_material_and_settings(self):
        project = await self.project_with_cube()
        petg = {**self.PETG_PRESET_FILE, "wall_loops": "4"}
        asa = {**PROJECT, "filament_settings_id": ["Elegoo ASA @ECC"], "filament_type": ["ASA"], "nozzle_diameter": ["0.4"], "wall_loops": "6"}
        first = self.confirm(self.prepare(archive(project=petg, objects=[{"id": 1, "name": "Box"}]), "a.3mf", target=project["id"]))
        second = self.confirm(self.prepare(archive(project=asa, objects=[{"id": 1, "name": "Lid"}]), "b.3mf", target=project["id"]))
        data = projects.get_project(project["id"])["data"]
        materials = {data["objects"][i]["name"]: data["filaments"][str(data["objects"][i]["model"])]["preset"] for i in range(len(data["objects"]))
                     if str(data["objects"][i]["model"]) in data.get("filaments", {})}
        self.assertEqual(materials, {"Box": "Elegoo PETG @ECC", "Lid": "Elegoo ASA @ECC"})
        self.assertEqual(sorted(str(p["overrides"].get("wall_loops")) for p in data["plates"]), ["4", "6", "None"])
        self.assertTrue(first and second)

    async def test_an_unmatched_material_must_be_resolved_for_an_append_too(self):
        project = await self.project_with_cube()
        review = self.prepare(archive(project=PETG_FILE, **CUBES), "set.3mf", target=project["id"])
        self.assertEqual([b["code"] for b in review["report"]["blockers"]], ["unmatched-material"])
        done = self.confirm(self.resolve(review, {"material": {"choice": "imported"}}))
        choice = next(iter(done["data"]["filaments"].values()))
        self.assertEqual(choice["preset"], "Brand PETG (imported)")
        _, values = slicer.filament_snapshot(choice["snapshot"], slicer.bundle_settings(done["data"]["profile"]["bundle_id"])["machine"])
        self.assertEqual((values["filament_type"], values["filament_retraction_length"]), (["PETG"], ["nil"]))

    async def test_the_same_material_as_the_project_adds_no_choice_and_no_overrides(self):
        project = await self.project_with_cube()
        same = {**PROJECT, "nozzle_diameter": ["0.4"]}
        done = self.confirm(self.prepare(archive(project=same, **CUBES), "same.3mf", target=project["id"]))
        self.assertNotIn("filaments", done["data"])
        self.assertEqual([p["overrides"] for p in done["data"]["plates"]], [{}, {}])

    async def test_an_incompatible_printer_or_bed_needs_a_separate_project_or_a_reviewed_conversion(self):
        project = await self.project_with_cube()
        other = {**PROJECT, "printer_settings_id": "Prusa MK4 0.4 nozzle"}
        for file_settings, code in ((other, "incompatible-machine"), ({**PROJECT, "curr_bed_type": "High Temp Plate"}, "incompatible-bed"),
                                    ({**PROJECT, "nozzle_diameter": ["0.6"]}, "incompatible-machine")):
            with self.subTest(code):
                review = self.prepare(archive(project=file_settings, **CUBES), "x.3mf", target=project["id"])
                (blocker,) = review["report"]["blockers"]
                self.assertEqual(blocker["code"], code)
                self.assertEqual(blocker["options"], ["separate-project", "convert"])
                with self.assertRaises(jobs.JobError):
                    self.confirm(review)
                converted = self.resolve(review, {"conflicts": "convert"})
                self.assertFalse(converted["blocked"])
                done = self.confirm(converted)
                self.assertEqual(done["data"]["profile"], projects.get_project(project["id"])["data"]["profile"])
                self.assertEqual(done["import_record"]["resolution"]["conflicts"], "convert")
                self.assertTrue(any(d["code"] == code for d in done["import_record"]["differences"]))
                projects.delete_project(project["id"])
                project = await self.project_with_cube()

    async def test_an_unmatched_printer_is_mapped_to_a_stock_printer_for_a_new_project(self):
        other = {**PROJECT, "printer_settings_id": "Prusa MK4 0.4 nozzle"}
        fresh = self.prepare(archive(project=other, **CUBES), "x.3mf")
        self.assertEqual([b["code"] for b in fresh["report"]["blockers"]], ["unmatched-printer"])
        offered = fresh["report"]["blockers"][0]["resolutions"]["printers"]
        self.assertEqual([p["printer"] for p in offered][:1], [CC])
        done = self.confirm(self.resolve(fresh, {"printer": CC}))
        self.assertEqual(slicer.bundle_settings(done["data"]["profile"]["bundle_id"])["machine"]["name"], CC)

    async def test_a_stale_target_revision_voids_a_resolved_append(self):
        project = await self.project_with_cube()
        review = self.prepare(archive(project=PETG_FILE, **CUBES), "set.3mf", target=project["id"])
        resolved = self.resolve(review, {"material": {"choice": "imported"}})
        projects.update_project(project["id"], {"name": "Renamed", "expected_revision": projects.get_project(project["id"])["revision"]})
        with self.assertRaises(jobs.JobError) as caught:
            self.confirm(resolved)
        self.assertEqual(caught.exception.issues[0]["code"], "import-review-stale")
        with self.assertRaises(jobs.JobError):
            self.resolve(resolved, {"material": {"choice": "imported"}})


class DifferencesAreNotDroppedQuietly(SettingsCase):
    async def test_a_file_material_without_a_preset_keeps_its_filament_values_when_appended(self):
        project = await self.project_with_cube()
        unnamed = {**PROJECT, "filament_settings_id": ["Brand House PLA"], "filament_type": ["PLA"], "nozzle_diameter": ["0.4"],
                   "nozzle_temperature": ["233"]}
        done = self.confirm(self.prepare(archive(project=unnamed, **CUBES), "house.3mf", target=project["id"]))
        choice = next(iter(done["data"]["filaments"].values()))
        machine = slicer.bundle_settings(done["data"]["profile"]["bundle_id"])["machine"]
        _, values = slicer.filament_snapshot(choice["snapshot"], machine)
        self.assertEqual(values["nozzle_temperature"], ["233"], "the file's own temperatures are carried, not dropped")
        self.assertEqual(done["data"]["profile"], projects.get_project(project["id"])["data"]["profile"])

    async def test_settings_the_import_cannot_carry_need_acknowledgement_and_a_direct_upload_refuses_them(self):
        project = await self.project_with_cube()
        for label, extra in (("blocked", {"post_process": ["rm -rf x"]}), ("invalid", {"wall_loops": "banana"}),
                             ("uncompared", {"sparse_infill_density": "25%"})):
            with self.subTest(label):
                data = archive(project={**PROJECT, "nozzle_diameter": ["0.4"], **extra}, **CUBES)
                review = self.prepare(data, "x.3mf", target=project["id"])
                self.assertTrue(review["required"], "differences that are left out are shown and acknowledged")
                with self.assertRaises(jobs.JobError) as caught:
                    projects.confirm_import(review["token"], review["binding"])
                self.assertEqual(caught.exception.issues[0]["code"], "import-review-required")
                projects.cancel_import(review["token"])
                before = self.snapshot(project["id"])
                with self.assertRaises(jobs.JobError) as direct:
                    projects.import_model(project["id"], io.BytesIO(data), len(data), "x.3mf")
                self.assertEqual((direct.exception.status, direct.exception.issues[0]["code"]), (409, "import-review-required"))
                self.assertTrue(direct.exception.issues[0]["differences"])
                self.assertEqual(self.snapshot(project["id"]), before)


class UnstatedSettingsTests(SettingsCase):
    """A setting the stock presets do not state is compared with Orca's own resolved value for those presets (one Orca run per
    locked profile, cached), so a real difference is carried and a value equal to Orca's default is not reported at all."""
    DEFAULTS = {"brim_type": "auto_brim", "sparse_infill_density": "15%"}
    FILE = {**PROJECT, "nozzle_diameter": ["0.4"], "brim_type": "no_brim", "sparse_infill_density": "15%"}

    def with_orca(self, code=0):
        runner = test_projects.template_runner(code=code, extras=self.DEFAULTS)
        return mock.patch.object(slicer, "runner", runner), runner

    async def test_a_new_project_carries_an_unstated_setting_that_differs_from_orcas_default(self):
        patch, _ = self.with_orca()
        with patch:
            done = self.confirm(self.prepare(archive(project=self.FILE, **CUBES), "brim.3mf"))
        self.assertEqual(done["data"]["profile"]["process"].get("brim_type"), "no_brim")
        self.assertNotIn("sparse_infill_density", done["data"]["profile"]["process"], "equal to Orca's default: nothing to carry")
        self.assertNotIn("uncompared-settings", [d["code"] for d in done["import_record"]["differences"]])

    async def test_values_equal_to_orcas_defaults_need_no_acknowledgement(self):
        patch, _ = self.with_orca()
        with patch:
            review = self.prepare(archive(project={**self.FILE, "brim_type": "auto_brim"}, **CUBES), "plain.3mf")
        self.assertNotIn("uncompared-settings", [d["code"] for d in review["report"]["differences"]])
        self.assertFalse(review["required"], review["report"]["differences"])
        projects.cancel_import(review["token"])

    async def test_appending_carries_an_unstated_difference_as_a_plate_override(self):
        project = await self.project_with_cube()
        patch, _ = self.with_orca()
        with patch:
            done = self.confirm(self.prepare(archive(project=self.FILE, **CUBES), "brim.3mf", target=project["id"]))
        added = [p for p in done["data"]["plates"] if p["id"] not in {x["id"] for x in project["data"]["plates"]}]
        self.assertEqual([p["overrides"].get("brim_type") for p in added], ["no_brim"])
        self.assertEqual(done["data"]["profile"], project["data"]["profile"], "the Project defaults are untouched")

    async def test_orcas_resolved_values_are_worked_out_once_per_locked_profile(self):
        patch, runner = self.with_orca()
        with patch:
            for name in ("a.3mf", "b.3mf"):
                projects.cancel_import(self.prepare(archive(project=self.FILE, **CUBES), name)["token"])
        self.assertEqual(len([call for call in runner.calls if "--export-3mf" in call]), 1)

    async def test_a_different_orca_works_its_resolved_values_out_again(self):
        """Orca's defaults belong to the Orca that reported them: another executable must not reuse them."""
        patch, runner = self.with_orca()
        with patch:
            projects.cancel_import(self.prepare(archive(project=self.FILE, **CUBES), "a.3mf")["token"])
            with mock.patch.object(slicer, "orca_identity", return_value="OrcaSlicer-9.9.10"):
                projects.cancel_import(self.prepare(archive(project=self.FILE, **CUBES), "b.3mf")["token"])
        self.assertEqual(len([call for call in runner.calls if "--export-3mf" in call]), 2)

    async def test_an_orca_run_that_fails_is_not_kept_even_if_it_left_a_readable_file(self):
        written = test_projects.template_runner(extras=self.DEFAULTS)
        def failing(args, timeout):
            written(args, timeout)
            return 1, "crashed after writing"
        failing.calls = written.calls
        with mock.patch.object(slicer, "runner", failing):
            review = self.prepare(archive(project=self.FILE, **CUBES), "a.3mf")
            self.assertIn("uncompared-settings", [d["code"] for d in review["report"]["differences"]])
            projects.cancel_import(review["token"])
            projects.cancel_import(self.prepare(archive(project=self.FILE, **CUBES), "b.3mf")["token"])
        self.assertEqual(len([call for call in written.calls if "--export-3mf" in call]), 2, "nothing was cached")

    async def test_without_orcas_resolved_values_unstated_settings_are_still_listed_and_acknowledged(self):
        patch, _ = self.with_orca(code=1)
        with patch:
            review = self.prepare(archive(project=self.FILE, **CUBES), "brim.3mf")
        uncompared = next(d for d in review["report"]["differences"] if d["code"] == "uncompared-settings")
        self.assertIn("brim_type", uncompared["keys"])
        self.assertTrue(review["required"])
        projects.cancel_import(review["token"])


class ResolveApiTests(base.ImportReviewApiTests):
    def setUp(self):
        super().setUp()
        folder = next((self.dir / "tools").rglob("filament")) / "ECC"
        (folder / "Elegoo PETG @ECC.json").write_text(json.dumps({
            "type": "filament", "from": "system", "instantiation": "true", "name": "Elegoo PETG @ECC", "inherits": "fdm_filament_common",
            "filament_type": ["PETG"], "compatible_printers": [CC]}))
        slicer._preset_cache.clear()

    def test_resolve_then_confirm_over_http(self):
        code, review, _ = self.stage(archive(project=PETG_FILE, **CUBES), "")
        self.assertEqual((code, review["blocked"]), (200, True))
        path = f"/api/projects/import-review/{review['token']}"
        code, error, _ = self.request("POST", path + "/resolve", {"material": {"choice": "preset", "preset": "No such"}})
        self.assertGreaterEqual(code, 400)
        code, resolved, _ = self.request("POST", path + "/resolve", {"material": {"choice": "imported"}})
        self.assertEqual((code, resolved["blocked"]), (200, False))
        self.assertEqual(self.request("GET", path)[1]["binding"], resolved["binding"])
        code, stale, _ = self.request("POST", path + "/confirm", {**review["binding"], "acknowledged": True})
        self.assertEqual((code, stale["issues"][0]["code"]), (409, "import-review-stale"))

    def test_resolve_then_confirm_succeeds_and_nothing_exists_before(self):
        code, review, _ = self.stage(archive(project=PETG_FILE, **CUBES), "")
        path = f"/api/projects/import-review/{review['token']}"
        self.assertEqual(self.request("GET", "/api/projects")[1]["projects"], [])
        code, resolved, _ = self.request("POST", path + "/resolve", {"material": {"choice": "imported"}})
        self.assertEqual(self.request("GET", "/api/projects")[1]["projects"], [])
        code, done, _ = self.request("POST", path + "/confirm", {**resolved["binding"], "acknowledged": True})
        self.assertEqual(code, 200)
        self.assertEqual(done["import_record"]["resolution"]["material"]["choice"], "imported")

    def test_resolve_checks_draft_ownership_for_a_target(self):
        draft = self.new_project(draft=True)
        code, review, _ = self.stage(archive(project={**PROJECT, "printer_settings_id": "Prusa MK4"}, **CUBES), f"target={draft['id']}&draft_token={draft['created_at']}")
        self.assertEqual(code, 200)
        path = f"/api/projects/import-review/{review['token']}/resolve"
        self.assertEqual(self.request("POST", path, {"conflicts": "convert", "draft_token": 1})[0], 409)
        self.assertEqual(self.request("POST", path, {"conflicts": "convert", "draft_token": draft["created_at"]})[0], 200)


if __name__ == "__main__":
    unittest.main()
