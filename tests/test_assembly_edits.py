"""Editing the assembly plan: Move and Reorder edits saved with the split, applied at generation.

The seam is the splits service (edit_plan, edit_receivers, assembly_edits, assembly_plan, start_guide) on a split
folder; the replay function is also called directly for the Try to keep behaviour."""
import json
import unittest

import assembly_edits
import assembly_plan
import jobs
import splits
from tests.test_assembly_guide import GuideCase, page_texts
from tests.test_assembly_plan import GEOMETRY, grid_split, stack_split


def move(parts, to):
    return {"op": "move", "parts": parts, "to": to}


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class MoveTests(GuideCase):
    def test_a_valid_move_makes_a_new_section_and_is_saved_with_the_split(self):
        split_id = self.make_split(*stack_split(4))
        before = splits.assembly_plan(split_id)
        self.assertEqual(before["edits"]["state"], "none")
        done = splits.edit_plan(split_id, move([1], [2]))
        self.assertEqual((done["state"], done["count"]), ("applied", 1))
        plan = splits.assembly_plan(split_id)
        self.assertEqual([(s["letter"], s["parts"]) for s in plan["sections"]], [("B", [1, 2]), ("A", [1, 2, 3, 4])])
        self.assertNotEqual(plan["code"], before["code"])
        self.assertEqual(plan["edits"]["count"], 1)
        saved = json.loads((splits.work_dir(split_id) / "guide_edits.json").read_text())
        self.assertEqual(saved["edits"], [move([1], [2])])   # part sets only, never Section letters
        self.assertTrue(saved["fingerprint"])

    def test_every_step_of_an_edited_plan_passes_the_validator(self):
        split_id = self.make_split(*stack_split(4))
        splits.edit_plan(split_id, move([1], [2]))
        plan = splits.assembly_plan(split_id)
        model = splits._planning(split_id)["model"]
        for section in plan["sections"]:
            for step in section["steps"]:
                self.assertIsNone(assembly_plan.validate_step(model, step["parts"], step["receiver"], step["direction"]))

    def test_an_edited_plan_still_carries_its_warnings(self):
        result, cuts = stack_split(4)
        for p in result["parts"]:
            p["volume_cm3"] = 6000.0
        split_id = self.make_split(result, cuts)
        self.assertTrue(splits.assembly_plan(split_id)["warnings"])
        splits.edit_plan(split_id, move([1], [2]))
        plan = splits.assembly_plan(split_id)
        self.assertEqual(plan["edits"]["state"], "applied")
        self.assertTrue([w for w in plan["warnings"] if w["kind"] == "heavy_join"])
        steps = [st for s in plan["sections"] for st in s["steps"]]
        self.assertTrue(any(st["warnings"] for st in steps))

    def test_moving_a_part_back_makes_the_section_vanish(self):
        split_id = self.make_split(*stack_split(4))
        splits.edit_plan(split_id, move([1], [2]))
        splits.edit_plan(split_id, move([1], [1, 2, 3, 4]))
        plan = splits.assembly_plan(split_id)
        self.assertEqual([s["letter"] for s in plan["sections"]], ["A"])

    def test_a_move_the_validator_refuses_says_why_and_changes_nothing(self):
        split_id = self.make_split(*grid_split())
        code = splits.assembly_plan(split_id)["code"]
        with self.assertRaises(assembly_edits.EditRefused) as caught:
            splits.edit_plan(split_id, move([4], [3]))
        self.assertEqual((caught.exception.status, caught.exception.kind), (409, "rod_direction"))
        self.assertIn("degrees off", str(caught.exception))
        self.assertEqual(splits.assembly_plan(split_id)["code"], code)
        self.assertFalse((splits.work_dir(split_id) / "guide_edits.json").exists())

    def test_the_part_a_section_starts_from_cannot_be_moved_alone(self):
        split_id = self.make_split(*stack_split(4))
        start = splits.assembly_plan(split_id)["sections"][0]["start_part"]
        with self.assertRaises(assembly_edits.EditRefused) as caught:
            splits.edit_plan(split_id, move([start], [1]))
        self.assertEqual(caught.exception.kind, "base")

    def test_unknown_and_pointless_moves_are_refused(self):
        split_id = self.make_split(*stack_split(4))
        for edit, kind in ((move([9], [1]), "unknown_unit"), (move([1], [1, 2, 3, 4]), "same_receiver"), (move([1], [1]), "inside"),
                           ({"op": "rename"}, "bad_order")):
            with self.assertRaises(assembly_edits.EditRefused) as caught:
                splits.edit_plan(split_id, edit)
            self.assertEqual(caught.exception.kind, kind, edit)

    def test_only_receivers_the_validator_accepts_are_offered(self):
        split_id = self.make_split(*stack_split(4))
        offered = [r["parts"] for r in splits.edit_receivers(split_id, [1])["receivers"]]
        self.assertIn([2], offered)
        self.assertNotIn([4], offered)
        for target in offered:   # every offered receiver really is accepted
            splits.edit_plan(split_id, {"op": "reset"})
            splits.edit_plan(split_id, move([1], target))

    def test_a_finished_section_can_be_moved_as_one(self):
        split_id = self.make_split(*grid_split())
        offered = splits.edit_receivers(split_id, [2, 4])["receivers"]
        self.assertEqual(offered, [])   # it already sits on the only unit it could join


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class ReorderTests(GuideCase):
    def order(self, split_id):
        return [st["parts"] for st in splits.assembly_plan(split_id)["sections"][0]["steps"]]

    def test_steps_move_up_and_down_and_the_plan_regroups(self):
        split_id = self.make_split(*stack_split(4))
        self.assertEqual(self.order(split_id), [[4], [2], [1]])
        splits.edit_plan(split_id, {"op": "reorder", "parts": [2], "direction": "up"})
        self.assertEqual(self.order(split_id), [[2], [4], [1]])
        splits.edit_plan(split_id, {"op": "reorder", "parts": [2], "direction": "down"})
        self.assertEqual(self.order(split_id), [[4], [2], [1]])
        self.assertEqual(splits.assembly_edits(split_id)["count"], 2)

    def test_a_reorder_that_leaves_a_part_with_nothing_to_join_is_refused(self):
        split_id = self.make_split(*stack_split(4))
        with self.assertRaises(assembly_edits.EditRefused) as caught:
            splits.edit_plan(split_id, {"op": "reorder", "parts": [1], "direction": "up"})
        self.assertEqual(caught.exception.kind, "disconnected")
        with self.assertRaises(assembly_edits.EditRefused):
            splits.edit_plan(split_id, {"op": "reorder", "parts": [1], "direction": "down"})   # already last

    def test_the_base_can_be_swapped_with_the_first_added_part(self):
        split_id = self.make_split(*stack_split(4))
        start = splits.assembly_plan(split_id)["sections"][0]["start_part"]
        splits.edit_plan(split_id, {"op": "reorder", "parts": [4], "direction": "up"})
        plan = splits.assembly_plan(split_id)
        self.assertEqual(plan["sections"][0]["start_part"], 4)
        self.assertEqual(plan["sections"][0]["steps"][0]["parts"], [start])

    def test_an_order_that_is_not_a_permutation_is_refused(self):
        split_id = self.make_split(*stack_split(4))
        with self.assertRaises(assembly_edits.EditRefused) as caught:
            splits.edit_plan(split_id, {"op": "reorder", "section": [1, 2, 3, 4], "order": [[3], [4], [2]]})
        self.assertEqual(caught.exception.kind, "bad_order")


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class GenerationTests(GuideCase):
    def test_the_guide_applies_the_saved_edits_and_the_cover_says_so(self):
        split_id = self.make_split(*stack_split(4))
        splits.edit_plan(split_id, move([1], [2]))
        splits.edit_plan(split_id, {"op": "reorder", "parts": [4], "direction": "up"})
        self.generate(split_id)
        cover = page_texts(self.pdf(split_id))[0]
        self.assertIn("Edited plan (2 edits)", cover)
        self.assertIn(splits.assembly_plan(split_id)["code"], cover)
        self.assertEqual(splits.guide_status(split_id)["plan_code"], splits.assembly_plan(split_id)["code"])

    def test_the_same_split_and_the_same_edits_give_the_same_guide(self):
        codes, texts = [], []
        for _ in range(2):
            split_id = self.make_split(*stack_split(4))
            splits.edit_plan(split_id, move([1], [2]))
            self.generate(split_id)
            codes.append(splits.assembly_plan(split_id)["code"])
            texts.append([p.replace(f"Split {split_id}", "Split N") for p in page_texts(self.pdf(split_id))[1:]])
        self.assertEqual(codes[0], codes[1])
        self.assertEqual(texts[0], texts[1])

    def test_reset_deletes_the_edits_and_the_cover_goes_back_to_automatic(self):
        split_id = self.make_split(*stack_split(4))
        auto = splits.assembly_plan(split_id)["code"]
        splits.edit_plan(split_id, move([1], [2]))
        splits.edit_plan(split_id, {"op": "reset"})
        self.assertFalse((splits.work_dir(split_id) / "guide_edits.json").exists())
        self.assertEqual(splits.assembly_plan(split_id)["code"], auto)
        self.generate(split_id)
        self.assertIn("Automatic plan", page_texts(self.pdf(split_id))[0])

    def test_a_guide_made_before_an_edit_is_out_of_date_after_it(self):
        split_id = self.make_split(*stack_split(4))
        self.generate(split_id)
        splits.edit_plan(split_id, move([1], [2]))
        self.assertTrue(splits.guide_status(split_id)["out_of_date"])


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class OutdatedTests(GuideCase):
    def test_edits_survive_a_resplit_with_the_same_cuts_and_parts(self):
        split_id = self.make_split(*stack_split(4))
        splits.edit_plan(split_id, move([1], [2]))
        work = splits.work_dir(split_id)
        (work / "result.json").write_text((work / "result.json").read_text())   # rewritten, same content
        self.assertEqual(splits.assembly_plan(split_id)["edits"]["state"], "applied")

    def test_edits_become_outdated_when_a_cut_moves_and_are_not_applied(self):
        split_id = self.make_split(*stack_split(4))
        splits.edit_plan(split_id, move([1], [2]))
        work = splits.work_dir(split_id)
        cuts = json.loads((work / "cuts.json").read_text())
        cuts["cuts"][0]["offset"] += 1.0
        (work / "cuts.json").write_text(json.dumps(cuts))
        plan = splits.assembly_plan(split_id)
        self.assertEqual((plan["edits"]["state"], plan["edits"]["count"]), ("outdated", 1))
        self.assertEqual([s["letter"] for s in plan["sections"]], ["A"])   # the automatic plan
        self.assertEqual(len(splits.assembly_edits(split_id)["edits"]), 1)   # kept, not deleted
        with self.assertRaises(jobs.JobError):
            splits.edit_plan(split_id, move([1], [2]))
        self.generate(split_id)
        self.assertIn("Automatic plan", page_texts(self.pdf(split_id))[0])

    def test_an_edit_that_fails_at_generation_marks_the_whole_override_outdated(self):
        split_id = self.make_split(*stack_split(4))
        splits.edit_plan(split_id, move([1], [2]))
        work = splits.work_dir(split_id)
        saved = json.loads((work / "guide_edits.json").read_text())
        saved["edits"].append(move([3], [1]))   # part 3 is where Section A starts: not allowed
        (work / "guide_edits.json").write_text(json.dumps(saved))
        info = splits.assembly_plan(split_id)["edits"]
        self.assertEqual((info["state"], info["failed"]), ("outdated", 1))
        self.assertIn("Edit 2", info["reason"])

    def test_try_to_keep_keeps_the_edits_that_still_pass_and_lists_the_rest(self):
        split_id = self.make_split(*stack_split(4))
        record = splits._planning(split_id)
        edits = [move([1], [2]), move([3], [1]), {"op": "reorder", "section": [1, 2], "order": [[2], [1]]}]
        strict = assembly_edits.replay(record["model"], record["auto"].plan, edits)
        self.assertFalse(strict.ok)
        self.assertEqual(strict.dropped[0][0], 1)
        kept = assembly_edits.replay(record["model"], record["auto"].plan, edits, keep_going=True)
        self.assertEqual([i for i, _e, _r in kept.dropped], [1])
        self.assertEqual(len(kept.kept), 2)
        self.assertIsNotNone(kept.plan)

    def _outdate(self, split_id, extra=None):
        """Save edits (plus `extra` ones that cannot hold) the way an older split would have left them."""
        work = splits.work_dir(split_id)
        saved = json.loads((work / "guide_edits.json").read_text())
        saved["edits"] += extra or []
        (work / "guide_edits.json").write_text(json.dumps(saved))

    def test_the_fingerprint_is_stored_with_the_edits(self):
        split_id = self.make_split(*stack_split(4))
        splits.edit_plan(split_id, move([1], [2]))
        saved = assembly_edits.load(splits.work_dir(split_id))
        self.assertEqual(saved["fingerprint"], splits._planning(split_id)["model"].fingerprint)

    def test_discard_deletes_the_override_and_the_cover_says_automatic(self):
        split_id = self.make_split(*stack_split(4))
        splits.edit_plan(split_id, move([1], [2]))
        self._outdate(split_id, [move([3], [1])])
        done = splits.edit_plan(split_id, {"op": "discard"})
        self.assertEqual((done["state"], done["count"]), ("none", 0))
        self.assertFalse((splits.work_dir(split_id) / "guide_edits.json").exists())
        self.generate(split_id)
        self.assertIn("Automatic plan", page_texts(self.pdf(split_id))[0])

    def test_try_to_keep_through_the_service_keeps_what_passes_and_lists_the_dropped(self):
        split_id = self.make_split(*stack_split(4))
        splits.edit_plan(split_id, move([1], [2]))
        self._outdate(split_id, [move([3], [1])])
        self.assertEqual(splits.assembly_plan(split_id)["edits"]["state"], "outdated")
        done = splits.edit_plan(split_id, {"op": "keep"})
        self.assertEqual((done["state"], done["count"]), ("applied", 1))
        self.assertEqual([(d["index"], d["edit"]["parts"]) for d in done["dropped"]], [(1, [3])])
        self.assertTrue(done["dropped"][0]["reason"])
        self.assertEqual(assembly_edits.load(splits.work_dir(split_id))["edits"], [move([1], [2])])
        self.generate(split_id)
        self.assertIn("Edited plan (1 edit)", page_texts(self.pdf(split_id))[0])

    def test_try_to_keep_after_the_cuts_changed_re_stamps_the_split(self):
        split_id = self.make_split(*stack_split(4))
        splits.edit_plan(split_id, move([1], [2]))
        work = splits.work_dir(split_id)
        cuts = json.loads((work / "cuts.json").read_text())
        cuts["cuts"][0]["offset"] += 1.0
        (work / "cuts.json").write_text(json.dumps(cuts))
        self.assertEqual(splits.assembly_edits(split_id)["state"], "outdated")
        done = splits.edit_plan(split_id, {"op": "keep"})
        self.assertEqual(done["state"], "applied")
        self.assertEqual(splits.assembly_edits(split_id)["state"], "applied")

    def test_keep_and_discard_are_refused_when_nothing_is_outdated(self):
        split_id = self.make_split(*stack_split(4))
        splits.edit_plan(split_id, move([1], [2]))
        for op in ("keep", "discard"):
            with self.assertRaises(jobs.JobError):
                splits.edit_plan(split_id, {"op": op})
        self.assertEqual(splits.assembly_edits(split_id)["count"], 1)

    def test_a_failing_edit_at_generation_falls_back_to_automatic_and_names_the_edit(self):
        split_id = self.make_split(*stack_split(4))
        splits.edit_plan(split_id, move([1], [2]))
        self._outdate(split_id, [move([3], [1])])
        self.generate(split_id)
        self.assertIn("Automatic plan", page_texts(self.pdf(split_id))[0])
        self.assertIn("Edit 2", splits.assembly_edits(split_id)["reason"])


if __name__ == "__main__":
    unittest.main()
