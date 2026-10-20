"""What a later step of the Assembly guide says it joins to, read back from the PDF text.

The seam is the splits service: generate the guide for a split (automatic and edited plans) and read the page text."""
import re
import unittest

import splits
from tests.test_assembly_guide import GuideCase, page_texts
from tests.test_assembly_plan import GEOMETRY, stack_split


def body_of(case, split_id) -> str:
    return " ".join(page_texts(case.pdf(split_id))[1:])


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class BuiltSoFarTests(GuideCase):
    def test_a_small_group_is_named_by_its_parts(self):
        split_id = self.make_split(*stack_split(4))
        self.generate(split_id)
        self.assertIn("Add part 1 to parts 2, 3, 4", body_of(self, split_id))

    def test_a_large_group_is_named_by_the_step_that_finished_it(self):
        split_id = self.make_split(*stack_split(7))
        self.generate(split_id)
        body = body_of(self, split_id)
        self.assertIn("to the stack from A4", body)       # A5 joins the five parts that A4 finished
        self.assertIn("to the stack from A5", body)
        self.assertNotIn("parts built so far", body)

    def test_a_step_of_an_edited_plan_reads_the_same(self):
        split_id = self.make_split(*stack_split(7))
        splits.edit_plan(split_id, {"op": "move", "parts": [1], "to": [2]})
        self.generate(split_id)
        plan = splits.assembly_plan(split_id)
        big = [st for s in plan["sections"] for st in s["steps"] if len(st["receiver"]) > 4]
        self.assertTrue(big)
        body = body_of(self, split_id)
        self.assertRegex(body, r"to the stack from [A-Z]\d+")
        self.assertNotIn("parts built so far", body)

    def test_part_numbers_in_a_list_never_run_together(self):
        result, cuts = stack_split(25)
        split_id = self.make_split(result, cuts)
        splits.edit_plan(split_id, {"op": "move", "parts": [18], "to": [19]})
        self.generate(split_id)
        body = body_of(self, split_id)
        lists = re.findall(r"parts ((?:\d+, )+\d+)", body)
        self.assertTrue(any("18" in found for found in lists))
        for found in lists:
            self.assertTrue(all(1 <= int(n) <= 25 for n in found.split(", ")), found)
        self.assertNotRegex(body, r"parts? \d{3,}")


if __name__ == "__main__":
    unittest.main()
