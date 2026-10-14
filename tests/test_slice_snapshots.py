"""Completed Slices stay tied to the inputs they were prepared with (reliable Project settings, ticket 08)."""
import copy
import json
import unittest
from unittest import mock

import database
import jobs
import projects
import slicer
from tests import test_settings_review
from tests.test_slicer import fake_runner


class SliceCase(test_settings_review.ReviewCase):
    def now(self, project_id):
        return projects.get_project(project_id)

    def change(self, project_id, change):
        return self.edit(self.now(project_id), change)

    async def slice(self, project_id, plates=(1,), during=None, **options):
        for plate in plates:
            projects.request_slice(project_id, plate, **options)
        runner = fake_runner()

        def run(args, timeout):
            result = runner(args, timeout)
            if during:
                during()
            return result
        with mock.patch.object(slicer, "runner", run):
            await self.run_worker()
        return self.now(project_id)

    def outdated(self, project_id):
        return {int(k): v.get("outdated") for k, v in self.now(project_id)["slices"].items()}


class SavedInputs(SliceCase):
    async def test_a_completed_slice_keeps_its_effective_inputs_and_provenance_apart_from_the_current_project(self):
        project = await self.petg_project()
        project = self.edit(project, lambda d: d["plates"][0].update(overrides={"wall_loops": "6"}))
        expected = projects.effective_settings(project["id"], 1)
        project = await self.slice(project["id"])
        self.change(project["id"], lambda d: d["plates"][0].update(overrides={"wall_loops": "9"}))

        review = projects.slice_inputs(project["id"], 1)

        self.assertEqual(review["saved"]["state"], "saved")
        saved = review["saved"]["inputs"]
        self.assertEqual(saved["fingerprint"], expected["fingerprint"])
        self.assertEqual(saved["fingerprint"], project["slices"]["1"]["fingerprint"])
        self.assertEqual(saved["process"]["wall_loops"], "6")
        self.assertEqual(saved["filament"]["filament_type"], ["PETG"])
        self.assertEqual([c["source"] for c in saved["provenance"]["process"]["wall_loops"]], ["preset", "plate"])
        self.assertEqual(saved["revision"], project["revision"])
        self.assertEqual(review["current"]["state"], "available")
        self.assertNotEqual(review["current"]["fingerprint"], saved["fingerprint"])
        self.assertTrue(review["slice"]["outdated"])
        diff = next(d for d in review["differences"] if d["key"] == "wall_loops")
        self.assertEqual((diff["saved"], diff["current"]), ("6", "9"))
        self.assertIn("Saved", review["label"])

    async def test_unchanged_slice_has_no_differences(self):
        project = await self.slice((await self.petg_project())["id"])
        review = projects.slice_inputs(project["id"], 1)
        self.assertFalse(review["slice"]["outdated"])
        self.assertEqual(review["differences"], [])
        self.assertEqual(review["current"]["fingerprint"], review["saved"]["inputs"]["fingerprint"])

    async def test_legacy_slice_reports_unavailable_evidence_and_keeps_old_outdated_behaviour(self):
        project = await self.petg_project()
        project = await self.slice(project["id"])
        slices = copy.deepcopy(project["slices"])
        slices["1"] = {k: v for k, v in slices["1"].items() if k not in ("inputs_version", "inputs_revision", "outdated")}
        slices["1"]["fingerprint"] = projects.fingerprint(project, 1)      # what Spooler stored before inputs were saved
        with database.transaction() as conn:
            conn.execute("UPDATE projects SET slices = ? WHERE id = ?", (json.dumps(slices), project["id"]))
        (projects.plate_dir(project["id"], 1) / "inputs.json").unlink(missing_ok=True)

        review = projects.slice_inputs(project["id"], 1)
        self.assertEqual(review["saved"]["state"], "unavailable")
        self.assertIsNone(review["saved"]["inputs"])
        self.assertIn("before", review["saved"]["reason"])
        self.assertEqual(review["differences"], [])
        self.assertFalse(self.outdated(project["id"])[1])                  # existing output is not discarded
        self.change(project["id"], lambda d: d["plates"][0].update(name="Front"))
        self.assertFalse(self.outdated(project["id"])[1])
        self.change(project["id"], lambda d: d["plates"][0].update(overrides={"wall_loops": "7"}))
        self.assertTrue(self.outdated(project["id"])[1])

    async def test_missing_saved_inputs_are_reported_unavailable_without_losing_outdated_detection(self):
        project = await self.slice((await self.petg_project())["id"])
        (projects.plate_dir(project["id"], 1) / "inputs.json").unlink()
        review = projects.slice_inputs(project["id"], 1)
        self.assertEqual(review["saved"]["state"], "unavailable")
        self.assertIn("missing", review["saved"]["reason"])
        self.change(project["id"], lambda d: d["plates"][0].update(overrides={"wall_loops": "7"}))
        self.assertTrue(self.outdated(project["id"])[1])

    async def test_an_unsliced_plate_has_no_slice_review(self):
        project = await self.petg_project()
        with self.assertRaises(jobs.JobError) as missing:
            projects.slice_inputs(project["id"], 1)
        self.assertEqual(missing.exception.status, 404)


class WhatMakesASliceOutdated(SliceCase):
    async def two_plates(self):
        project = await self.petg_project(extra_model=True)
        return await self.slice(project["id"], plates=(1, 2))

    async def test_descriptive_and_unchanged_effective_edits_leave_every_slice_current(self):
        project = await self.two_plates()
        walls = projects.effective_settings(project["id"], 1)["process"]["wall_loops"]
        self.assertEqual(self.outdated(project["id"]), {1: False, 2: False})
        before = self.now(project["id"])["revision"]

        def describe(data):
            data["plates"][0]["name"] = "Front"
            data["objects"][0]["name"] = "Bracket"
            data["filaments"]["2"]["colour"] = "jet"
            data["plates"][0]["overrides"] = {"wall_loops": walls}         # same as what it already inherits
            data["objects"][0]["overrides"] = {"wall_loops": walls}
        self.change(project["id"], describe)
        self.change(project["id"], lambda d: None)                          # a revision-only save
        self.assertEqual(self.now(project["id"])["revision"], before + 2)
        self.assertEqual(self.outdated(project["id"]), {1: False, 2: False})

    async def test_project_default_filament_tuning_outdates_only_plates_on_the_default(self):
        project = await self.two_plates()
        self.change(project["id"], lambda d: d["profile"]["filament"].update(nozzle_temperature=["245"]))
        self.assertEqual(self.outdated(project["id"]), {1: True, 2: False})

    async def test_alternate_filament_content_outdates_its_plate(self):
        project = await self.two_plates()
        self.preset("Workshop ASA 2", "ASA", 270)
        self.change(project["id"], lambda d: d["filaments"].update({"2": {"preset": "Workshop ASA 2", "colour": "black"}}))
        self.assertEqual(self.outdated(project["id"]), {1: False, 2: True})

    async def test_print_affecting_edits_outdate_the_affected_plate(self):
        edits = {
            "plate setting": lambda d: d["plates"][0].update(overrides={"wall_loops": "7"}),
            "eligible object setting": lambda d: d["objects"][0].update(overrides={"sparse_infill_density": "25%"}),
            "placement": lambda d: d["objects"][0].update(position=[100.0, 90.0]),
            "scale": lambda d: d["objects"][0].update(scale=[1.1, 1.1, 1.1]),
            "bed": lambda d: d.update(bed="smooth"),
            "bricklayers": lambda d: d["profile"].update(bricklayers={"enabled": True}),
        }
        for name, edit in edits.items():
            with self.subTest(name):
                project = await self.petg_project()
                project = await self.slice(project["id"])
                self.assertFalse(self.outdated(project["id"])[1])
                self.change(project["id"], edit)
                self.assertTrue(self.outdated(project["id"])[1])

    async def test_unresolvable_current_inputs_mark_the_slice_unresolved_instead_of_failing_the_read(self):
        project = await self.two_plates()
        data = copy.deepcopy(self.now(project["id"])["data"])
        del data["filaments"]["2"]["snapshot"]                              # an alternate that can no longer be verified
        with database.transaction() as conn:
            conn.execute("UPDATE projects SET data = ? WHERE id = ?", (json.dumps(data), project["id"]))
        slices = self.now(project["id"])["slices"]
        self.assertTrue(slices["2"]["outdated"])
        self.assertEqual(slices["2"]["outdated_reason"], "inputs-unresolved")
        self.assertFalse(slices["1"]["outdated"])
        self.assertEqual(projects.slice_inputs(project["id"], 2)["current"]["state"], "unresolved")
        with self.assertRaisesRegex(jobs.JobError, "changed after"):
            projects.queue_plate(project["id"], 2, {})


class EditsDuringWork(SliceCase):
    async def test_an_edit_while_slicing_leaves_the_result_identified_as_outdated_and_unqueueable(self):
        project = await self.petg_project()
        before = projects.effective_settings(project["id"], 1)
        edit = lambda: self.change(project["id"], lambda d: d["plates"][0].update(overrides={"wall_loops": "7"}))
        project = await self.slice(project["id"], during=edit, send=True)

        entry = project["slices"]["1"]
        self.assertEqual(entry["state"], "sliced")
        self.assertEqual(entry["fingerprint"], before["fingerprint"])        # the inputs it was prepared with, not the newer edit
        self.assertTrue(entry["outdated"])
        self.assertNotIn("sent_job", entry)
        self.assertIn("slice", entry["send_error"])
        saved = projects.slice_inputs(project["id"], 1)["saved"]["inputs"]
        self.assertEqual(saved["process"]["wall_loops"], before["process"]["wall_loops"])
        with self.assertRaisesRegex(jobs.JobError, "changed after"):
            projects.queue_plate(project["id"], 1, {})
        self.assertEqual(projects.project_jobs(project["id"]), [])

    async def test_a_descriptive_edit_while_slicing_still_publishes_a_current_result(self):
        project = await self.petg_project()
        edit = lambda: self.change(project["id"], lambda d: d["plates"][0].update(name="Front"))
        project = await self.slice(project["id"], during=edit)
        self.assertFalse(project["slices"]["1"]["outdated"])

    async def test_removing_the_plate_while_slicing_does_not_attach_a_result_or_files(self):
        project = await self.petg_project()

        def move_and_remove(data):
            data["objects"][0]["plate"] = 2
            data["plates"] = [{"id": 2, "name": "Plate 2", "overrides": {}}]
        project = await self.slice(project["id"], during=lambda: self.change(project["id"], move_and_remove))
        self.assertNotIn("1", project["slices"])
        plates = projects.plate_dir(project["id"], 1).parent
        self.assertEqual(list(plates.iterdir()) if plates.exists() else [], [])

    async def test_a_project_replaced_while_slicing_never_receives_the_result(self):
        project = await self.petg_project()
        replacement = {}

        def replace():
            projects.delete_project(project["id"])
            replacement.update(projects.create_project("Other"))
        await self.slice(project["id"], during=replace)
        self.assertEqual(replacement["id"], project["id"])                   # SQLite handed the id to the new Project
        other = self.now(replacement["id"])
        self.assertEqual(other["slices"], {})
        plates = projects.plate_dir(other["id"], 1).parent
        self.assertEqual(list(plates.iterdir()) if plates.exists() else [], [])

    async def test_a_failed_reslice_leaves_its_log_and_no_staging_folders(self):
        project = await self.slice((await self.petg_project())["id"])
        self.change(project["id"], lambda d: d["plates"][0].update(overrides={"wall_loops": "7"}))
        projects.request_slice(project["id"], 1)
        with mock.patch.object(slicer, "runner", fake_runner(code=1)):
            await self.run_worker()
        self.assertEqual(self.now(project["id"])["slices"]["1"]["state"], "failed")
        folder = projects.plate_dir(project["id"], 1)
        self.assertTrue((folder / "slicer.log").is_file())
        self.assertEqual([p.name for p in folder.parent.iterdir()], ["1"])


class RevisionConflicts(SliceCase):
    async def test_stale_edits_and_resolutions_cannot_overwrite_a_newer_save(self):
        project = await self.petg_project()
        self.edit(project, lambda d: d["plates"][0].update(overrides={"wall_loops": "5"}))
        with self.assertRaises(jobs.JobError) as stale:
            self.edit(project, lambda d: d["plates"][0].update(overrides={"wall_loops": "8"}))
        self.assertEqual(stale.exception.status, 409)
        self.assertEqual(self.now(project["id"])["data"]["plates"][0]["overrides"], {"wall_loops": "5"})
        with self.assertRaises(jobs.JobError) as resolve:
            projects.resolve_setting(project["id"], {"revision": project["revision"], "scope": "plate", "affected_id": 1,
                                                     "kind": "process", "key": "wall_loops", "action": "remove"})
        self.assertEqual(resolve.exception.status, 409)
        self.assertEqual(self.now(project["id"])["data"]["plates"][0]["overrides"], {"wall_loops": "5"})


if __name__ == "__main__":
    unittest.main()
