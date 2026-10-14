"""Warnings on the assembly plan and guide: heavy joins, disconnected parts and unlabelled parts. None of them blocks."""
import re
import unittest

import assembly_pdf
import splits
from tests.test_assembly_guide import GuideCase, page_texts
from tests.test_assembly_plan import GEOMETRY, part, stack_split


def heavy_stack():
    """Two parts; the lighter part 1 is the one that moves, and weighs about 2.2 kg when printed (5000 cm3 x 1.24 x 0.35)."""
    result, cuts = stack_split(2)
    result["parts"][0]["volume_cm3"] = 5000.0
    result["parts"][1]["volume_cm3"] = 6000.0
    return result, cuts


def kinds(plan):
    return [(w["kind"], w.get("step")) for w in plan["warnings"]]


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class WarningTests(GuideCase):
    def test_a_light_small_plan_has_no_warnings(self):
        plan = splits.assembly_plan(self.make_split(*stack_split(3)))
        self.assertEqual(plan["warnings"], [])
        self.assertTrue(all(st["warnings"] == [] for st in sum((s["steps"] for s in plan["sections"]), [])))

    def test_a_step_moving_over_2_kg_is_a_heavy_join_but_the_plan_stays_ok(self):
        plan = splits.assembly_plan(self.make_split(*heavy_stack()))
        self.assertTrue(plan["ok"])
        heavy = [w for w in plan["warnings"] if w["kind"] == "heavy_join"]
        self.assertTrue(heavy)
        self.assertIn("get a helper or support it", heavy[0]["message"])
        self.assertGreater(heavy[0]["weight_kg"], 2.0)
        step = next(st for s in plan["sections"] for st in s["steps"] if st["id"] == heavy[0]["step"])
        self.assertEqual(step["warnings"][0]["kind"], "heavy_join")
        self.assertEqual(step["parts"], [1])

    def test_a_unit_over_400_mm_long_is_heavy_however_light(self):
        result, cuts = stack_split(2)
        result["parts"][0]["bbox"] = [-380, 0, 0, 40, 40, 40]
        result["parts"][1]["volume_cm3"] = 30.0
        plan = splits.assembly_plan(self.make_split(result, cuts))
        self.assertEqual([k for k, _ in kinds(plan)], ["heavy_join"])
        self.assertEqual(plan["warnings"][0]["longest_mm"], 420)

    def test_the_thresholds_are_split_settings(self):
        split_id = self.make_split(*heavy_stack())
        self.assertTrue(splits.assembly_plan(split_id)["warnings"])
        splits.update_settings(split_id, {"guide": {"heavy_kg": 3}})
        self.assertEqual(splits.assembly_plan(split_id)["warnings"], [])
        splits.update_settings(split_id, {"guide": {"heavy_kg": 0.1}})
        self.assertTrue(splits.assembly_plan(split_id)["warnings"])

    def test_a_disconnected_part_is_its_own_section_with_a_no_rods_warning_and_does_not_block(self):
        result, cuts = stack_split(2)
        result["parts"].append(part(3, (300, 0, 0, 340, 40, 40)))
        split_id = self.make_split(result, cuts)
        plan = splits.assembly_plan(split_id)
        self.assertTrue(plan["ok"])
        warning = next(w for w in plan["warnings"] if w["kind"] == "disconnected")
        self.assertEqual(warning["section"], "B")
        self.assertIn("No rods", warning["message"])
        self.assertEqual(self.generate(split_id)["state"], "ready")

    def test_unlabelled_parts_are_noted_in_the_plan(self):
        result, cuts = stack_split(3)
        result["unlabelled"] = [2, 3]
        plan = splits.assembly_plan(self.make_split(result, cuts))
        self.assertEqual(plan["unlabelled"], [2, 3])
        note = next(w for w in plan["warnings"] if w["kind"] == "unlabelled")
        self.assertIn("Parts 2, 3 have no number", note["message"])

    def test_warnings_do_not_change_the_plan_code(self):
        split_id = self.make_split(*heavy_stack())
        heavy_code = splits.assembly_plan(split_id)["code"]
        splits.update_settings(split_id, {"guide": {"heavy_kg": 50}})
        self.assertEqual(splits.assembly_plan(split_id)["code"], heavy_code)


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class GuideTextTests(GuideCase):
    def test_the_step_list_shows_a_heavy_join_under_its_step_and_names_unlabelled_parts(self):
        result, cuts = heavy_stack()
        result["unlabelled"] = [1]
        split_id = self.make_split(result, cuts)
        self.assertEqual(self.generate(split_id)["state"], "ready")
        body = " ".join(page_texts(self.pdf(split_id))[1:])
        plan = splits.assembly_plan(split_id)
        step = next(w["step"] for w in plan["warnings"] if w["kind"] == "heavy_join")
        self.assertIn("Part 1 has no number", body)
        self.assertLess(re.search(rf"\b{step} (Add|Join)", body).start(), body.index("Warning: Heavy join: get a helper or support it."))

    def test_the_helpers_are_empty_for_a_plain_plan(self):
        self.assertEqual(assembly_pdf.step_warning_lines({"warnings": []}), [])
        self.assertEqual(assembly_pdf.plan_note_lines({"warnings": [{"kind": "heavy_join", "message": "x"}]}), [])


if __name__ == "__main__":
    unittest.main()
