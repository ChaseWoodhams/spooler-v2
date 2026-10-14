"""Orca 3MF imports are inspected and reviewed in staging before they change a Project (ticket 09)."""

import asyncio
import io
import json
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
from tests.orca_archives import FAMILIES, archive
from tests.test_jobs import FakePrinter
from tests.test_slicer import cube_stl, fake_runner, make_fake_orca

GEOMETRY = not splits.missing_modules()
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PROJECT = {"printer_settings_id": "Elegoo Centauri Carbon 0.4 nozzle", "print_settings_id": "0.20mm Standard @Elegoo CC 0.4 nozzle",
           "filament_settings_id": ["Elegoo PLA @ECC"], "filament_type": ["PLA"], "curr_bed_type": "Textured PEI Plate"}


def staged() -> list:
    root = projects.projects_dir() / ".import-staging"
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


def tree(path: Path) -> list:
    return sorted(str(p.relative_to(path)) for p in path.rglob("*")) if path.exists() else []


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class ImportReviewCase(unittest.IsolatedAsyncioTestCase):
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

    async def project_with_cube(self):
        project = projects.create_project("Brackets")
        data = cube_stl(20)
        projects.import_model(project["id"], io.BytesIO(data), len(data), "cube.stl")
        await self.run_worker()
        return projects.get_project(project["id"])

    def prepare(self, data, name="set.3mf", target=None, **options):
        return projects.prepare_import(io.BytesIO(data), len(data), name, target=target, **options)

    def snapshot(self, project_id):
        """Everything a reviewer could see of a Project: its row and the files below it."""
        record = projects.get_project(project_id)
        record.pop("server_time", None)
        return json.dumps(record, sort_keys=True), tree(projects.project_dir(project_id))


class LossReportTests(ImportReviewCase):
    async def test_each_loss_family_is_named_by_scope_before_the_project_changes(self):
        project = await self.project_with_cube()
        for code, (fixture, scope, name) in FAMILIES.items():
            with self.subTest(code):
                before = self.snapshot(project["id"])
                review = self.prepare(archive(project=PROJECT, **fixture), target=project["id"])
                found = [loss for loss in review["report"]["losses"] if loss["code"] == code]
                self.assertEqual([(loss["scope"], loss["name"]) for loss in found], [(scope, name)])
                self.assertTrue(review["required"])
                self.assertFalse(review["blocked"])
                self.assertEqual(self.snapshot(project["id"]), before, "inspection must not touch the Project")
                projects.cancel_import(review["token"])
        self.assertEqual(staged(), [])

    async def test_a_clean_project_needs_no_acknowledgement(self):
        project = await self.project_with_cube()
        review = self.prepare(archive(project=PROJECT), target=project["id"])
        self.assertEqual(review["report"]["losses"], [])
        self.assertFalse(review["required"])
        projects.cancel_import(review["token"])

    async def test_false_positives_are_avoided_for_empty_or_default_content(self):
        project = await self.project_with_cube()
        data = archive(project=PROJECT, objects=[{"id": 1, "name": "Plain", "paint": {"paint_supports": ""}}],
                       extras={"Metadata/layer_heights_profile.txt": "", "Metadata/layer_config_ranges.xml": "<objects/>",
                               "Metadata/custom_gcode_per_layer.xml": "<custom_gcodes_per_layer><plate><plate_info id=\"1\"/></plate></custom_gcodes_per_layer>"})
        review = self.prepare(data, target=project["id"])
        self.assertEqual(review["report"]["losses"], [])
        projects.cancel_import(review["token"])

    async def test_combined_fixture_lists_every_family_for_its_own_object(self):
        project = await self.project_with_cube()
        objects = [{"id": 1, "name": "Alpha", "parts": [("normal_part", {}), ("modifier_part", {})], "paint": {"paint_seam": "4", "paint_supports": "8"}},
                   {"id": 2, "name": "Beta", "items": [{"transform": "-1 0 0 0 1 0 0 0 1 100 100 0", "plate": 2}, {"printable": False, "plate": 2}]}]
        extras = {"Metadata/layer_heights_profile.txt": "object_id=2|0 0.2 5 0.1\n"}
        review = self.prepare(archive(objects=objects, project=PROJECT, extras=extras, plates=[{"id": 1}, {"id": 2, "name": "Back"}]),
                              target=project["id"])
        pairs = sorted((loss["code"], loss["name"]) for loss in review["report"]["losses"])
        self.assertEqual(pairs, [("disabled-copy", "Beta"), ("mirrored-transform", "Beta"), ("modifier-volume", "Alpha"),
                                 ("paint-seam", "Alpha"), ("paint-supports", "Alpha"), ("variable-layer-height", "Beta")])
        projects.cancel_import(review["token"])

    async def test_cancel_removes_staging_and_a_second_cancel_is_harmless(self):
        project = await self.project_with_cube()
        review = self.prepare(archive(**FAMILIES["paint-seam"][0]), target=project["id"])
        self.assertEqual(staged(), [review["token"]])
        projects.cancel_import(review["token"])
        projects.cancel_import(review["token"])
        self.assertEqual(staged(), [])
        with self.assertRaises(jobs.JobError) as caught:
            projects.cancel_import("../" + review["token"])
        self.assertEqual(caught.exception.status, 400)


TWO_PLATES = dict(objects=[{"id": 1, "name": "Alpha", "paint": {"paint_supports": "4"}, "meta": {"wall_loops": "6"},
                            "items": [{"plate": 1}, {"plate": 1, "transform": "1 0 0 0 1 0 0 0 1 150 150 0"}]},
                           {"id": 2, "name": "Beta", "parts": [("normal_part", {}), ("negative_part", {})],
                            "items": [{"plate": 2}]}],
                  plates=[{"id": 1, "name": "Front"}, {"id": 2, "name": "Back"}])


class CommitTests(ImportReviewCase):
    def confirm(self, review, acknowledged=True, **changes):
        return projects.confirm_import(review["token"], {**review["binding"], "acknowledged": acknowledged, **changes})

    async def test_continue_with_losses_commits_the_reviewed_delta_and_retains_the_records(self):
        project = await self.project_with_cube()
        data = archive(project=PROJECT, **TWO_PLATES)
        review = self.prepare(data, "Bench set.3mf", target=project["id"])
        before = self.snapshot(project["id"])

        with self.assertRaises(jobs.JobError) as caught:
            self.confirm(review, acknowledged=False)
        self.assertEqual((caught.exception.status, caught.exception.issues[0]["code"]), (409, "import-review-required"))
        self.assertEqual(self.snapshot(project["id"]), before)
        self.assertEqual(staged(), [review["token"]], "a refused acknowledgement keeps the review open")

        result = self.confirm(review)
        await self.run_worker()
        added = projects.get_project(project["id"])
        self.assertEqual([p["name"] for p in added["data"]["plates"]], ["Plate 1", "Front", "Back"])
        self.assertEqual([(o["name"], o["plate"]) for o in added["data"]["objects"]],
                         [("cube", 1), ("Alpha", 2), ("Alpha (2)", 2), ("Beta", 3)])
        self.assertEqual(added["data"]["objects"][1]["overrides"], {"wall_loops": "6"})
        self.assertEqual(added["revision"], project["revision"] + 1)
        self.assertEqual([m["state"] for m in added["models"]], ["ready"] * 3)
        self.assertEqual(staged(), [])

        record = projects.import_records(project["id"])[0]
        self.assertEqual(record["id"], review["token"])
        self.assertTrue(record["acknowledged"])
        self.assertEqual(sorted((loss["code"], loss["name"]) for loss in record["losses"]),
                         [("negative-volume", "Beta"), ("paint-supports", "Alpha")])
        self.assertEqual(result["import_record"]["id"], record["id"])
        self.assertEqual([m["origin"] for m in added["models"][1:]], [{"import": record["id"]}] * 2)
        path, name = projects.import_source(project["id"], record["id"])
        self.assertEqual((path.read_bytes(), name), (data, "Bench set.3mf"))

    async def test_slicing_a_converted_project_does_not_ask_for_the_same_acknowledgement_again(self):
        project = await self.project_with_cube()
        review = self.prepare(archive(project=PROJECT, **TWO_PLATES), target=project["id"])
        self.confirm(review)
        await self.run_worker()
        with mock.patch.object(slicer, "runner", fake_runner()):
            requested = projects.request_slice(project["id"], 2)
            await self.run_worker()
        self.assertEqual(projects.get_project(project["id"])["slices"]["2"]["state"], "sliced")
        self.assertEqual(len(projects.import_records(project["id"])[0]["losses"]), 2, "the accepted losses stay visible")
        self.assertTrue(requested)

    async def test_the_same_losses_are_reported_for_single_whole_project_and_append_imports(self):
        feature = {"id": 1, "name": "Painted", "paint": {"paint_seam": "4"}, "parts": [("normal_part", {}), ("support_blocker", {})]}
        single = archive(project=PROJECT, objects=[feature])
        multi = archive(project=PROJECT, objects=[feature, {"id": 2, "name": "Other"}])
        project = await self.project_with_cube()
        reports = []
        for data, target in ((single, None), (multi, None), (multi, project["id"])):
            review = self.prepare(data, target=target)
            reports.append(sorted((l["code"], l["scope"], l["name"]) for l in review["report"]["losses"]))
            projects.cancel_import(review["token"])
        self.assertEqual(reports[0], [("paint-seam", "model", "Painted"), ("support-blocker", "model", "Painted")])
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(reports[1], reports[2])

    async def test_single_model_import_starts_a_project_only_after_confirmation(self):
        data = archive(project=PROJECT, objects=[{"id": 1, "name": "Solo", "meta": {"wall_loops": "5"}, "paint": {"paint_supports": "4"}}])
        review = self.prepare(data, "solo.3mf")
        self.assertEqual((review["kind"], projects.list_projects(include_quick=True), splits.list_splits()), ("single", [], []))
        created = self.confirm(review)
        self.assertEqual(created["name"], "solo")
        self.assertEqual(len(created["split_ids"]), 1)
        self.assertEqual(created["data"]["profile"]["process"], {"wall_loops": "5"})
        self.assertEqual(created["import_record"]["losses"][0]["code"], "paint-supports")
        self.assertEqual(projects.import_source(created["id"], review["token"])[0].read_bytes(), data)

    async def test_whole_project_import_creates_the_project_with_its_plates_and_the_accepted_report(self):
        review = self.prepare(archive(project=PROJECT, **TWO_PLATES), "Bench set.3mf")
        self.assertEqual(projects.list_projects(include_quick=True), [])
        created = self.confirm(review)
        self.assertEqual([p["name"] for p in created["data"]["plates"]], ["Front", "Back"])
        self.assertEqual(sorted(l["code"] for l in created["import_record"]["losses"]), ["negative-volume", "paint-supports"])
        self.assertEqual(len(projects.list_projects()), 1)

    async def test_disabled_copies_are_left_out_of_every_import_path(self):
        spare = FAMILIES["disabled-copy"][0]["objects"][0]
        data = archive(project=PROJECT, objects=[spare, {"id": 2, "name": "Other"}])
        project = await self.project_with_cube()
        for target in (None, project["id"]):
            review = self.prepare(data, target=target)
            result = self.confirm(review)
            names = [o["name"] for o in result["data"]["objects"]]
            self.assertEqual(names.count("Spare"), 1, f"target={target}")
            self.assertNotIn("Spare (2)", names)


class SafetyTests(ImportReviewCase):
    def code(self, caught):
        return caught.exception.status, caught.exception.issues[0]["code"]

    def confirm(self, review, **changes):
        return projects.confirm_import(review["token"], {**review["binding"], "acknowledged": True, **changes})

    def target_of(self, project):
        return {"project_id": project["id"], "created_at": project["created_at"], "revision": project["revision"]}

    async def test_an_edit_between_review_and_confirm_makes_the_confirmation_stale(self):
        project = await self.project_with_cube()
        review = self.prepare(archive(project=PROJECT, **TWO_PLATES), target=project["id"])
        projects.update_project(project["id"], {"name": "Edited elsewhere", "revision": project["revision"]})
        after_edit = self.snapshot(project["id"])
        with self.assertRaises(jobs.JobError) as caught:
            self.confirm(review)
        self.assertEqual(self.code(caught), (409, "import-review-stale"))
        self.assertEqual(self.snapshot(project["id"]), after_edit)
        self.assertEqual(staged(), [])

    async def test_any_changed_binding_field_conflicts(self):
        project = await self.project_with_cube()
        for field, value in (("content_hash", "0" * 64), ("conversion", "0" * 64), ("report_version", 99),
                             ("target", {**self.target_of(project), "revision": 999})):
            with self.subTest(field):
                review = self.prepare(archive(project=PROJECT, **TWO_PLATES), target=project["id"])
                before = self.snapshot(project["id"])
                with self.assertRaises(jobs.JobError) as caught:
                    self.confirm(review, **{field: value})
                self.assertEqual(self.code(caught), (409, "import-review-stale"))
                self.assertEqual(self.snapshot(project["id"]), before)
                self.assertEqual(staged(), [])

    async def test_the_second_of_two_concurrent_reviews_conflicts_without_touching_the_first_import(self):
        project = await self.project_with_cube()
        first = self.prepare(archive(project=PROJECT, objects=[{"id": 1, "name": "One"}], plates=[{"id": 1, "name": "One"}]), target=project["id"])
        second = self.prepare(archive(project=PROJECT, objects=[{"id": 1, "name": "Two"}], plates=[{"id": 1, "name": "Two"}]), target=project["id"])
        self.confirm(first)
        landed = self.snapshot(project["id"])
        with self.assertRaises(jobs.JobError) as caught:
            self.confirm(second)
        self.assertEqual(self.code(caught), (409, "import-review-stale"))
        self.assertEqual(self.snapshot(project["id"]), landed)
        self.assertEqual([o["name"] for o in projects.get_project(project["id"])["data"]["objects"]], ["cube", "One"])
        self.assertEqual(staged(), [])

    async def test_a_draft_that_was_replaced_does_not_accept_a_stale_confirmation(self):
        draft = projects.create_project("Draft", draft=True)
        review = self.prepare(archive(project=PROJECT, **TWO_PLATES), target=draft["id"])
        projects.delete_project(draft["id"])
        reused = projects.create_project("Someone else")           # SQLite reuses the id
        self.assertEqual(reused["id"], draft["id"])
        before = self.snapshot(reused["id"])
        with self.assertRaises(jobs.JobError) as caught:
            self.confirm(review)
        self.assertEqual(self.code(caught), (409, "import-review-stale"))
        self.assertEqual(self.snapshot(reused["id"]), before)

    async def test_an_expired_review_is_refused_and_removed(self):
        project = await self.project_with_cube()
        with mock.patch.object(projects, "IMPORT_REVIEW_TTL_S", -1):
            review = self.prepare(archive(project=PROJECT, **TWO_PLATES), target=project["id"])
        before = self.snapshot(project["id"])
        with self.assertRaises(jobs.JobError) as caught:
            self.confirm(review)
        self.assertEqual(self.code(caught), (409, "import-review-expired"))
        self.assertEqual((self.snapshot(project["id"]), staged()), (before, []))

    async def test_conversion_failure_leaves_no_staging_and_no_project_change(self):
        project = await self.project_with_cube()
        before = self.snapshot(project["id"])
        with mock.patch.object(projects, "write_stl", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.prepare(archive(project=PROJECT, **TWO_PLATES), target=project["id"])
        self.assertEqual((self.snapshot(project["id"]), staged()), (before, []))

    async def test_commit_failure_removes_everything_it_published_and_keeps_old_slices(self):
        project = await self.project_with_cube()
        with mock.patch.object(slicer, "runner", fake_runner()):
            projects.request_slice(project["id"], 1)
            await self.run_worker()
        project = projects.get_project(project["id"])
        self.assertEqual(project["slices"]["1"]["state"], "sliced")
        review = self.prepare(archive(project=PROJECT, **TWO_PLATES), target=project["id"])
        before = self.snapshot(project["id"])
        with mock.patch.object(projects, "_retain_import", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.confirm(review)
        self.assertEqual(self.snapshot(project["id"]), before)
        self.assertEqual(staged(), [])
        self.assertEqual(projects.import_records(project["id"]), [])

    async def test_a_move_failing_midway_removes_only_the_models_it_had_moved(self):
        project = await self.project_with_cube()
        review = self.prepare(archive(project=PROJECT, **TWO_PLATES), target=project["id"])
        before = self.snapshot(project["id"])
        real, calls = projects.os.replace, []

        def flaky(source, target):
            calls.append(target)
            if len(calls) == 2:
                raise OSError("locked")
            return real(source, target)
        with mock.patch.object(projects.os, "replace", flaky):
            with self.assertRaises(OSError):
                self.confirm(review)
        self.assertEqual((self.snapshot(project["id"]), staged()), (before, []))

    async def test_failure_creating_a_new_project_leaves_no_project_or_staging(self):
        review = self.prepare(archive(project=PROJECT, **TWO_PLATES), "set.3mf")
        with mock.patch.object(projects, "_retain_import", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.confirm(review)
        self.assertEqual((projects.list_projects(include_quick=True), staged()), ([], []))
        single = self.prepare(archive(project=PROJECT, objects=[{"id": 1, "name": "Solo"}]), "solo.3mf")
        with mock.patch.object(projects, "_retain_import", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.confirm(single)
        self.assertEqual((projects.list_projects(include_quick=True), splits.list_splits(), staged()), ([], [], []))

    async def test_interrupted_staging_is_swept_without_touching_other_folders(self):
        project = await self.project_with_cube()
        pending = self.prepare(archive(project=PROJECT, **TWO_PLATES), target=project["id"])
        root = projects.projects_dir() / ".import-staging"
        broken = root / ("a" * 32)
        (broken / "models" / "0").mkdir(parents=True)
        (broken / "source.3mf").write_bytes(b"half an upload")
        other = root / "notes"
        other.mkdir()
        keep = projects.model_dir(project["id"], 1) / "job.json"
        projects.init()
        self.assertEqual(sorted(staged()), sorted(["notes", pending["token"]]))
        self.assertTrue(keep.is_file() and other.is_dir())
        self.assertEqual(projects.get_import_review(pending["token"])["token"], pending["token"])

    async def test_malformed_archives_are_refused_before_anything_is_staged(self):
        project = await self.project_with_cube()
        before = self.snapshot(project["id"])
        empty_zip = io.BytesIO()
        zipfile.ZipFile(empty_zip, "w").close()
        cut = io.BytesIO()
        with zipfile.ZipFile(cut, "w") as z:
            z.writestr("3D/3dmodel.model", "<model><resources><object id=\"1\">")
        for label, data in (("not a zip", b"PK nope"), ("no model", empty_zip.getvalue()), ("unterminated model xml", cut.getvalue()),
                            ("truncated model", archive(project=PROJECT).replace(b"</model>", b""))):
            with self.subTest(label):
                with self.assertRaises(jobs.JobError):
                    self.prepare(data, target=project["id"])
                self.assertEqual((self.snapshot(project["id"]), staged()), (before, []))

    async def test_unreadable_settings_files_block_the_import_instead_of_being_ignored(self):
        project = await self.project_with_cube()
        cases = {"malformed-settings": archive(project=PROJECT, model_settings="<config><object"),
                 "malformed-project-settings": archive(project="{not json")}
        for code, data in cases.items():
            with self.subTest(code):
                review = self.prepare(data, target=project["id"])
                self.assertTrue(review["blocked"])
                self.assertEqual([b["code"] for b in review["report"]["blockers"]], [code])
                with self.assertRaises(jobs.JobError) as caught:
                    self.confirm(review)
                self.assertEqual(self.code(caught), (409, "import-blocked"))
                projects.cancel_import(review["token"])
        self.assertEqual(staged(), [])

    async def test_a_file_with_every_copy_disabled_has_nothing_to_import(self):
        review = self.prepare(archive(project=PROJECT, objects=[{"id": 1, "name": "Off", "items": [{"printable": False}]}]), "off.3mf")
        self.assertEqual([b["code"] for b in review["report"]["blockers"]], ["nothing-printable"])
        projects.cancel_import(review["token"])


class MaterialSubstitutionTests(ImportReviewCase):
    PETG = {**PROJECT, "filament_settings_id": ["Generic PETG"], "filament_type": ["PETG"]}
    ASA = {**PROJECT, "filament_settings_id": ["Generic ASA"], "filament_type": ["ASA"]}

    def confirm(self, review):
        return projects.confirm_import(review["token"], {**review["binding"], "acknowledged": True})

    async def test_an_unmatched_petg_or_asa_preset_blocks_instead_of_becoming_pla(self):
        for project_settings, material in ((self.PETG, "PETG"), (self.ASA, "ASA")):
            with self.subTest(material):
                review = self.prepare(archive(project=project_settings, **TWO_PLATES), "set.3mf")
                (blocker,) = review["report"]["blockers"]
                self.assertEqual((blocker["code"], blocker["requested"]["material"], blocker["proposed"]["material"]),
                                 ("unmatched-material", material, "PLA"))
                self.assertEqual(blocker["proposed"]["nozzle"], "0.4")
                self.assertEqual(blocker["proposed"]["filament"], "Elegoo PLA @ECC")
                with self.assertRaises(jobs.JobError) as caught:
                    self.confirm(review)
                self.assertEqual((caught.exception.status, caught.exception.issues[0]["code"]), (409, "import-blocked"))
                self.assertEqual(projects.list_projects(include_quick=True), [])
                projects.cancel_import(review["token"])

    async def test_generic_acknowledgement_cannot_approve_a_substitution_alongside_other_losses(self):
        review = self.prepare(archive(project=self.PETG, **TWO_PLATES), "set.3mf")
        self.assertTrue(review["required"] and review["blocked"])
        with self.assertRaises(jobs.JobError) as caught:
            self.confirm(review)
        self.assertEqual(caught.exception.issues[0]["code"], "import-blocked")
        projects.cancel_import(review["token"])

    async def test_an_unmatched_printer_is_a_blocker_too(self):
        review = self.prepare(archive(project={**PROJECT, "printer_settings_id": "Prusa MK4 0.4 nozzle"}, **TWO_PLATES), "set.3mf")
        self.assertEqual([b["code"] for b in review["report"]["blockers"]], ["unmatched-printer"])
        projects.cancel_import(review["token"])

    async def test_a_substituted_preset_of_the_same_material_stays_a_note(self):
        review = self.prepare(archive(project={**PROJECT, "filament_settings_id": ["My PLA"]}, **TWO_PLATES), "set.3mf")
        self.assertFalse(review["blocked"])
        self.assertTrue(any("My PLA" in note for note in review["notes"]))
        projects.cancel_import(review["token"])

    async def test_appending_does_not_adopt_the_files_material_so_it_does_not_block(self):
        project = await self.project_with_cube()
        review = self.prepare(archive(project=self.PETG, **TWO_PLATES), target=project["id"])
        self.assertFalse(review["blocked"])
        projects.cancel_import(review["token"])


class DirectImportTests(ImportReviewCase):
    async def test_direct_import_paths_refuse_a_file_that_needs_review(self):
        project = await self.project_with_cube()
        lossy = archive(project=PROJECT, **TWO_PLATES)
        before = self.snapshot(project["id"])
        with self.assertRaises(jobs.JobError) as caught:
            projects.import_model(project["id"], io.BytesIO(lossy), len(lossy), "set.3mf")
        self.assertEqual((caught.exception.status, caught.exception.issues[0]["code"]), (409, "import-review-required"))
        self.assertEqual(sorted(l["code"] for l in caught.exception.issues[0]["losses"]), ["negative-volume", "paint-supports"])
        self.assertEqual(self.snapshot(project["id"]), before)
        with self.assertRaises(jobs.JobError):
            projects.create_project_from_upload(io.BytesIO(lossy), len(lossy), "set.3mf")
        blocked = archive(project={**PROJECT, "filament_settings_id": ["Generic PETG"], "filament_type": ["PETG"]})
        with self.assertRaises(jobs.JobError):
            projects.create_project_from_upload(io.BytesIO(blocked), len(blocked), "petg.3mf")
        self.assertEqual((len(projects.list_projects(include_quick=True)), staged()), (1, []))

    async def test_a_clean_file_still_imports_in_one_step(self):
        project = await self.project_with_cube()
        clean = archive(project=PROJECT, objects=[{"id": 1, "name": "Neat"}, {"id": 2, "name": "Tidy"}])
        added = projects.import_model(project["id"], io.BytesIO(clean), len(clean), "neat.3mf")
        self.assertEqual([o["name"] for o in added["data"]["objects"]], ["cube", "Neat", "Tidy"])
        self.assertEqual(added["import_record"]["losses"], [])


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class ImportReviewApiTests(unittest.TestCase):
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

    def request(self, method, path, body=None, raw=False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=body, method=method,
                                         headers={"Content-Type": "application/json" if not raw else "application/octet-stream"})
        try:
            with _opener.open(request, timeout=20) as resp:
                content = resp.read()
                return resp.status, (json.loads(content) if resp.headers.get_content_type() == "application/json" else content), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), e.headers

    def stage(self, data, query):
        return self.request("POST", f"/api/projects/import-review?filename=set.3mf&{query}", data, raw=True)

    def new_project(self, **body):
        return self.request("POST", "/api/projects", {"name": "Target", **body})[1]

    def test_review_confirm_and_retained_source_over_http(self):
        project = self.new_project()
        before = self.request("GET", f"/api/projects/{project['id']}")[1]
        data = archive(project=PROJECT, **TWO_PLATES)
        code, review, _ = self.stage(data, f"target={project['id']}")
        self.assertEqual(code, 200)
        self.assertTrue(review["required"])
        self.assertEqual(sorted(l["code"] for l in review["report"]["losses"]), ["negative-volume", "paint-supports"])
        self.assertEqual({**self.request("GET", f"/api/projects/{project['id']}")[1], "server_time": 0}, {**before, "server_time": 0})
        self.assertEqual(self.request("GET", f"/api/projects/import-review/{review['token']}")[1]["token"], review["token"])

        path = f"/api/projects/import-review/{review['token']}/confirm"
        code, refused, _ = self.request("POST", path, review["binding"])
        self.assertEqual((code, refused["issues"][0]["code"]), (409, "import-review-required"))
        code, done, _ = self.request("POST", path, {**review["binding"], "acknowledged": True})
        self.assertEqual(code, 200)
        self.assertEqual([p["name"] for p in done["data"]["plates"]], ["Plate 1", "Front", "Back"])

        code, listing, _ = self.request("GET", f"/api/projects/{project['id']}/imports")
        self.assertEqual((code, [r["id"] for r in listing["imports"]]), (200, [review["token"]]))
        self.assertEqual(len(listing["imports"][0]["losses"]), 2)
        code, source, headers = self.request("GET", f"/api/projects/{project['id']}/imports/{review['token']}/source")
        self.assertEqual((code, source), (200, data))
        self.assertIn("set.3mf", headers["Content-Disposition"])
        self.assertEqual(self.request("GET", f"/api/projects/{project['id']}/imports/{'0' * 32}/source")[0], 404)
        self.assertEqual(self.request("GET", f"/api/projects/{project['id']}/imports/..%2F..%2Fx/source")[0], 404)

    def test_cancel_leaves_the_project_untouched_and_the_token_unusable(self):
        project = self.new_project()
        before = self.request("GET", f"/api/projects/{project['id']}")[1]
        review = self.stage(archive(project=PROJECT, **TWO_PLATES), f"target={project['id']}")[1]
        self.assertEqual(self.request("DELETE", f"/api/projects/import-review/{review['token']}")[0], 200)
        self.assertEqual(self.request("DELETE", f"/api/projects/import-review/{review['token']}")[0], 200)
        code, gone, _ = self.request("POST", f"/api/projects/import-review/{review['token']}/confirm", {**review["binding"], "acknowledged": True})
        self.assertEqual((code, gone["issues"][0]["code"]), (404, "import-review-gone"))
        after = self.request("GET", f"/api/projects/{project['id']}")[1]
        self.assertEqual({**after, "server_time": 0}, {**before, "server_time": 0})
        self.assertEqual(staged(), [])

    def test_a_project_edit_makes_the_confirmation_conflict(self):
        project = self.new_project()
        review = self.stage(archive(project=PROJECT, **TWO_PLATES), f"target={project['id']}")[1]
        self.assertEqual(self.request("PATCH", f"/api/projects/{project['id']}", {"name": "Renamed"})[0], 200)
        code, stale, _ = self.request("POST", f"/api/projects/import-review/{review['token']}/confirm", {**review["binding"], "acknowledged": True})
        self.assertEqual((code, stale["issues"][0]["code"]), (409, "import-review-stale"))
        self.assertEqual(self.request("GET", f"/api/projects/{project['id']}")[1]["data"]["objects"], [])

    def test_draft_ownership_is_checked_when_staging_and_confirming(self):
        draft = self.new_project(draft=True)
        token = draft["created_at"]
        data = archive(project=PROJECT, **TWO_PLATES)
        self.assertEqual(self.stage(data, f"target={draft['id']}&draft_token=1")[0], 409)
        self.assertEqual(staged(), [])
        code, review, _ = self.stage(data, f"target={draft['id']}&draft_token={token}")
        self.assertEqual(code, 200)
        path = f"/api/projects/import-review/{review['token']}/confirm"
        self.assertEqual(self.request("POST", path, {**review["binding"], "acknowledged": True, "draft_token": 1})[0], 409)
        self.assertEqual(self.request("GET", f"/api/projects/{draft['id']}")[1]["data"]["objects"], [])

    def test_malformed_and_blocked_files_are_reported_without_changing_anything(self):
        project = self.new_project()
        code, error, _ = self.stage(b"PK nope", f"target={project['id']}")
        self.assertEqual(code, 400)
        self.assertEqual(self.stage(b"x", "target=999")[0], 404)
        self.assertEqual(staged(), [])
        code, review, _ = self.stage(archive(project={**PROJECT, "filament_settings_id": ["Generic PETG"], "filament_type": ["PETG"]}, **TWO_PLATES), "")
        self.assertEqual((code, review["blocked"], review["report"]["blockers"][0]["requested"]["material"]), (200, True, "PETG"))
        code, blocked, _ = self.request("POST", f"/api/projects/import-review/{review['token']}/confirm", {**review["binding"], "acknowledged": True})
        self.assertEqual((code, blocked["issues"][0]["code"]), (409, "import-blocked"))
        self.assertEqual(self.request("GET", "/api/projects")[1]["projects"][-1]["name"], "Target")
        self.assertEqual(len(self.request("GET", "/api/projects")[1]["projects"]), 1)

    def test_the_older_upload_routes_cannot_skip_the_review(self):
        project = self.new_project()
        lossy = archive(project=PROJECT, **TWO_PLATES)
        for path in (f"/api/projects/{project['id']}/models/upload?filename=set.3mf", "/api/projects/upload?filename=set.3mf"):
            code, error, _ = self.request("POST", path, lossy, raw=True)
            self.assertEqual((code, error["issues"][0]["code"]), (409, "import-review-required"), path)
        self.assertEqual(len(self.request("GET", "/api/projects?include_quick=1")[1]["projects"]), 1)
        self.assertEqual(self.request("GET", f"/api/projects/{project['id']}")[1]["data"]["objects"], [])
        self.assertEqual(staged(), [])

    def test_a_new_project_is_created_only_by_confirming(self):
        code, review, _ = self.stage(archive(project=PROJECT, **TWO_PLATES), "")
        self.assertEqual(self.request("GET", "/api/projects")[1]["projects"], [])
        code, created, _ = self.request("POST", f"/api/projects/import-review/{review['token']}/confirm", {**review["binding"], "acknowledged": True})
        self.assertEqual((code, [p["name"] for p in created["data"]["plates"]]), (200, ["Front", "Back"]))
        self.assertEqual(len(self.request("GET", "/api/projects")[1]["projects"]), 1)


if __name__ == "__main__":
    unittest.main()
