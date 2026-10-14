"""Completed Slices are checked against what Orca reported before they are trusted (reliable Project settings, ticket 14).

Everything goes through the Project service, worker and queue with a fake Orca runner whose G-code ends in a configuration
footer built from the profile files it was handed (the way Orca writes one). No printer command is sent.
"""
import json
import unittest
from pathlib import Path
from unittest import mock

import jobs
import projects
import slicer
from tests.test_slice_snapshots import SliceCase
from tests.test_slicer import SLICED_GCODE, fake_runner


def render(value) -> str:
    """One footer value the way Orca prints it: vectors joined, newlines escaped."""
    if isinstance(value, list):
        value = ",".join(str(v) for v in value)
    return str(value).replace("\n", "\\n")


def footer_runner(tweak=None, footer=True):
    """A fake Orca whose G-code carries the loaded configuration as its footer; `tweak(config)` edits what it reports."""
    def run(args, timeout):
        fake_runner()(args, timeout)
        out = Path(args[args.index("--outputdir") + 1])
        machine, process = args[args.index("--load-settings") + 1].split(";")
        loaded = {}
        for path in (machine, process, args[args.index("--load-filaments") + 1]):
            loaded.update(json.loads(Path(path).read_text(encoding="utf-8")))
        reported = {k: render(v) for k, v in loaded.items() if v is not None}
        for identity in ("name", "from", "inherits", "instantiation", "type", "setting_id"):
            reported.pop(identity, None)       # Orca does not echo a preset's own identity fields
        if tweak:
            tweak(reported)
        if footer:
            lines = "".join(f"; {k} = {v}\n" for k, v in sorted(reported.items()))
            (out / "plate_1.gcode").write_text(SLICED_GCODE + "; CONFIG_BLOCK_START\n" + lines + "; CONFIG_BLOCK_END\n")
        return 0, ""
    return run


class ObservedCase(SliceCase):
    async def sliced(self, project, runner, **options):
        projects.request_slice(project["id"], 1, **options)
        with mock.patch.object(slicer, "runner", runner):
            await self.run_worker()
        return self.now(project["id"])

    def review(self, project):
        return projects.slice_inputs(project["id"], 1)["observed"]


class CheckingOrcaOutput(ObservedCase):
    async def test_output_that_reports_the_expected_settings_is_consistent_and_queueable(self):
        project = await self.sliced(await self.petg_project(), footer_runner())

        entry = project["slices"]["1"]
        self.assertEqual(entry["state"], "sliced")
        self.assertEqual(entry["observed"]["state"], "consistent")
        self.assertTrue(entry["observed"]["trusted"])
        review = self.review(project)
        self.assertEqual(review["differences"], [])
        self.assertGreaterEqual(review["compared"], 8)
        self.assertIn("expected", review["label"].lower())
        job = projects.queue_plate(project["id"], 1, {"printer_id": "p1"})
        self.assertEqual(job["printer_id"], "p1")

    async def test_cosmetic_identity_and_formatting_differences_are_not_mismatches(self):
        def tweak(footer):
            footer["filament_settings_id"] = "A renamed preset"
            footer["filament_colour"] = "#123456"
            footer["layer_height"] = "0.200"                   # the same number written another way
            footer["filament_type"] = '"PETG"'                 # Orca quotes some strings
        project = await self.petg_project()
        project = await self.sliced(project, footer_runner(tweak))
        self.assertEqual(self.review(project)["differences"], [])
        self.assertEqual(project["slices"]["1"]["observed"]["state"], "consistent")

    async def test_unexpected_print_affecting_differences_are_shown_by_setting_and_scope_and_block_queueing(self):
        def tweak(footer):
            footer["nozzle_temperature"] = "230"               # from the Filament preset
            footer["wall_loops"] = "2"                         # from a Plate override
        project = await self.petg_project()
        project = self.edit(project, lambda d: d["plates"][0].update(overrides={"wall_loops": "6"}))
        project = await self.sliced(project, footer_runner(tweak), send=True)

        entry = project["slices"]["1"]
        self.assertEqual(entry["state"], "sliced")                             # the output is retained for review
        self.assertEqual(entry["observed"]["state"], "mismatch")
        self.assertFalse(entry["observed"]["trusted"])
        found = {d["key"]: d for d in self.review(project)["differences"]}
        self.assertEqual(set(found), {"nozzle_temperature", "wall_loops"})
        self.assertEqual((found["wall_loops"]["scope"], found["wall_loops"]["expected"], found["wall_loops"]["observed"]),
                         ("plate", "6", "2"))
        self.assertEqual(found["nozzle_temperature"]["scope"], "preset")
        self.assertEqual(found["nozzle_temperature"]["kind"], "filament")
        self.assertTrue(projects.plate_file(project["id"], 1, "gcode").is_file())
        self.assertTrue((projects.plate_dir(project["id"], 1) / "observed.json").is_file())
        self.assertFalse(entry["outdated"])
        # Slice & send does not queue it, and says why
        self.assertNotIn("sent_job", entry)
        self.assertIn("wall_loops", entry["send_error"])
        self.assertEqual(projects.project_jobs(project["id"]), [])
        with self.assertRaisesRegex(jobs.JobError, "does not match") as refused:
            projects.queue_plate(project["id"], 1, {"printer_id": "p1"})
        self.assertEqual(refused.exception.status, 409)
        self.assertIn("nozzle_temperature", str(refused.exception))
        # a person who has read the differences can still send it deliberately
        job = projects.queue_plate(project["id"], 1, {"printer_id": "p1", "accept_unverified": self.accepted(project)})
        self.assertEqual(job["printer_id"], "p1")

    def accepted(self, project):
        """What the person's 'Send anyway' repeats: the Slice whose differences they read (from the Slice review)."""
        shown = projects.slice_inputs(project["id"], 1)["slice"]
        return {"1": {"fingerprint": shown["fingerprint"], "sliced_at": shown["sliced_at"]}}

    async def test_sending_an_unverified_slice_anyway_is_bound_to_the_slice_that_was_reviewed(self):
        def tweak(footer):
            footer["wall_loops"] = "2"
        project = await self.petg_project()
        project = self.edit(project, lambda d: d["plates"][0].update(overrides={"wall_loops": "6"}))
        project = await self.sliced(project, footer_runner(tweak))
        reviewed = self.accepted(project)
        for blanket in (True, {}, {"1": {"fingerprint": "nope", "sliced_at": 0}}):
            with self.subTest(str(blanket)):
                with self.assertRaises(jobs.JobError) as refused:
                    projects.queue_plate(project["id"], 1, {"printer_id": "p1", "accept_unverified": blanket})
                self.assertEqual(refused.exception.status, 409)
        self.assertEqual(projects.project_jobs(project["id"]), [])
        project = await self.sliced(project, footer_runner(tweak))          # sliced again: a new Slice the person has not read
        with self.assertRaises(jobs.JobError) as stale:
            projects.queue_plate(project["id"], 1, {"printer_id": "p1", "accept_unverified": reviewed})
        self.assertEqual((stale.exception.status, stale.exception.issues[0]["code"]), (409, "unverified-slice-changed"))
        self.assertEqual(projects.project_jobs(project["id"]), [])
        job = projects.queue_plate(project["id"], 1, {"printer_id": "p1", "accept_unverified": self.accepted(project)})
        self.assertEqual(job["printer_id"], "p1")

    async def test_a_known_orca_normalization_is_recorded_and_does_not_make_the_slice_unverified(self):
        def tweak(footer):
            footer["enable_prime_tower"] = "0"                 # Orca drops the prime tower of a one-filament Plate
        project = await self.petg_project()
        project = self.edit(project, lambda d: d["plates"][0].update(overrides={"enable_prime_tower": "1"}))
        project = await self.sliced(project, footer_runner(tweak))
        review = self.review(project)
        self.assertEqual(review["state"], "normalized")
        self.assertTrue(review["trusted"])
        self.assertEqual(review["differences"], [])
        note = next(n for n in review["normalized"] if n["key"] == "enable_prime_tower")
        self.assertEqual((note["expected"], note["observed"]), ("1", "0"))
        self.assertIn("one filament", note["reason"].lower())
        projects.queue_plate(project["id"], 1, {"printer_id": "p1"})

    async def test_a_value_orca_does_not_recognise_is_reported_as_normalized_to_its_default(self):
        def tweak(footer):
            footer["sparse_infill_pattern"] = "rectilinear"
        # the stock Elegoo preset spells this value "zig-zag", which Orca 2.4.2 does not know; model that inherited value
        self.stock_process_value("sparse_infill_pattern", "zig-zag")
        project = await self.sliced(await self.petg_project(), footer_runner(tweak))
        note = next(n for n in self.review(project)["normalized"] if n["key"] == "sparse_infill_pattern")
        self.assertEqual((note["expected"], note["observed"]), ("zig-zag", "rectilinear"))
        self.assertIn("not a valid", note["reason"])
        self.assertEqual(self.review(project)["differences"], [])
        self.assertTrue(project["slices"]["1"]["observed"]["trusted"])

    async def test_an_invalid_inherited_value_is_only_benign_when_orca_fell_back_to_its_default_choice(self):
        def tweak(footer):
            footer["sparse_infill_pattern"] = "grid"           # a valid choice, but not the fallback for an unknown value
        self.stock_process_value("sparse_infill_pattern", "zig-zag")
        project = await self.sliced(await self.petg_project(), footer_runner(tweak))
        review = self.review(project)
        self.assertEqual(review["state"], "mismatch")
        self.assertEqual([d["key"] for d in review["differences"]], ["sparse_infill_pattern"])
        self.assertFalse(project["slices"]["1"]["observed"]["trusted"])

    def stock_process_value(self, key, value):
        """Change a stock process preset in the fake Orca install, as a vendor file can carry a value Spooler would not accept."""
        path = next(self.dir.glob("tools/*/resources/profiles/Elegoo/process/ECC/0.20mm Standard @Elegoo CC 0.4 nozzle.json"))
        record = json.loads(path.read_text(encoding="utf-8"))
        record[key] = value
        path.write_text(json.dumps(record), encoding="utf-8")
        slicer._preset_cache.clear()


class MissingEvidence(ObservedCase):
    async def test_output_without_a_configuration_footer_is_labelled_unavailable_not_verified(self):
        project = await self.sliced(await self.petg_project(), footer_runner(footer=False))
        entry = project["slices"]["1"]
        self.assertEqual(entry["observed"]["state"], "unavailable")
        self.assertFalse(entry["observed"]["trusted"])
        review = self.review(project)
        self.assertEqual(review["compared"], 0)
        self.assertIn("no configuration", review["reason"].lower())
        self.assertNotIn("verified", review["label"].lower().replace("not verified", ""))
        projects.queue_plate(project["id"], 1, {"printer_id": "p1"})            # nothing contradicts it, so it still queues

    async def test_settings_missing_from_the_footer_are_listed_as_unavailable_but_others_are_still_compared(self):
        def tweak(footer):
            del footer["wall_loops"]
            footer["nozzle_temperature"] = "231"
        project = await self.petg_project()
        project = await self.sliced(project, footer_runner(tweak))
        review = self.review(project)
        self.assertIn("wall_loops", [u["key"] for u in review["unavailable"]])
        self.assertEqual([d["key"] for d in review["differences"]], ["nozzle_temperature"])
        self.assertEqual(review["state"], "mismatch")

    async def test_a_slice_made_before_output_was_checked_reports_unavailable_and_still_queues(self):
        project = await self.sliced(await self.petg_project(), footer_runner())
        projects._mutate(project["id"], "slices", lambda s: s["1"].pop("observed"), project["created_at"])
        (projects.plate_dir(project["id"], 1) / "observed.json").unlink()
        review = projects.slice_inputs(project["id"], 1)["observed"]
        self.assertEqual(review["state"], "unavailable")
        self.assertIn("before", review["reason"])
        projects.queue_plate(project["id"], 1, {"printer_id": "p1"})


class NullableFilamentOverrides(ObservedCase):
    async def test_nil_overrides_expect_the_inherited_value_and_explicit_ones_expect_the_override(self):
        project = await self.petg_project()
        project = self.edit(project, lambda d: d["profile"].update(filament={"filament_z_hop": ["0.4"], "filament_wipe": ["nil"]}))

        def report(z_hop, wipe="1"):
            def tweak(footer):
                footer.pop("filament_wipe", None)               # Orca omits nil overrides from its footer
                footer["z_hop"], footer["wipe"] = z_hop, wipe
                footer["filament_z_hop"] = z_hop
            return tweak
        # the machine preset's own wipe is what a nil override leaves in force
        expected_wipe = projects.effective_settings(project["id"], 1)["machine"].get("wipe")
        good = await self.sliced(project, footer_runner(report("0.4", wipe=str(expected_wipe if expected_wipe is not None else "1"))))
        self.assertEqual(self.review(good)["differences"], [], self.review(good))
        wrong = await self.sliced(project, footer_runner(report("0.2")))
        self.assertEqual([d["key"] for d in self.review(wrong)["differences"]], ["filament_z_hop", "z_hop"])


class PostProcessing(ObservedCase):
    async def test_a_bricklayers_slice_names_the_post_processor_and_does_not_claim_its_transform_is_verified(self):
        project = await self.petg_project()
        project = self.edit(project, lambda d: d["profile"].update(bricklayers={"enabled": True}))

        def bricks(args, timeout):
            source, target = Path(args[2]), Path(args[args.index("-outputFile") + 1])
            target.write_text(";== BrickLayers Post-Processed\n" + source.read_text())
            return 0, "done"
        with mock.patch("brick_post.runner", bricks):
            project = await self.sliced(project, footer_runner())
        review = self.review(project)
        self.assertEqual(review["state"], "consistent")
        post = review["post_processing"]
        self.assertEqual(post["kind"], "bricklayers")
        self.assertFalse(post["transform_verified"])
        self.assertEqual(post["identity"], projects.brick_post.identity())
        self.assertIn("baseline", post["note"].lower())
        self.assertIn("not verified", " ".join(review["limits"]).lower())
        # the footer examined is Orca's own output, not the transformed file
        self.assertEqual(review["source"], "plate.orca.gcode")

    async def test_a_plain_slice_names_no_post_processor(self):
        project = await self.sliced(await self.petg_project(), footer_runner())
        self.assertIsNone(self.review(project)["post_processing"])
        self.assertEqual(self.review(project)["source"], "plate.gcode")


if __name__ == "__main__":
    unittest.main()
