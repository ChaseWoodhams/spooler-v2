"""The rod bill of materials and the rod page of the Assembly guide.

Seams: build_bom on a split result's pins and plan (counts, spares, cut length, sticks, fallback, reconcile), and the
generated PDF read back (rod page order, row text, totals, the 100 mm ruler)."""
import json
import re
import unittest
from unittest import mock

import assembly_bom
import assembly_pdf
import splits
from tests.test_assembly_guide import GuideCase, GEOMETRY, page_texts
from tests.test_assembly_plan import cut, grid_split, joint, part, pins, stack_split


def rods_split(count, small=0):
    """Two parts joined by `count` 2 x 12 mm rods and a third by `small` 1 x 7 mm rods."""
    parts = [part(1, (0, 0, 0, 40, 40, 40)), part(2, (40, 0, 0, 80, 40, 40)), part(3, (80, 0, 0, 120, 40, 40))]
    first = pins(1, 2, "x", (40, 10, 20), count)
    second = pins(2, 3, "x", (80, 10, 20), small, connector="1 mm rod")
    for pin in second:
        pin.update(diameter=1.0, hole_diameter=1.34, depth=4.0)
    joints = [joint(1, 2, 1, rods=count), joint(2, 3, 2, rods=small, connector="1 mm rod")]
    return {"parts": parts, "joints": joints, "pins": first + second}, [cut(1, "x", 40.0), cut(2, "x", 80.0)]


def fake_plan(connectors_per_step):
    return {"sections": [{"steps": [{"id": f"A{i + 1}", "rods": sum(c.values()), "connectors": c}
                                    for i, c in enumerate(connectors_per_step)]}]}


def fake_pins(label, number, diameter=2.0, depth=6.5):
    return [{"connector": label, "diameter": diameter, "depth": depth}] * number


class BomTests(unittest.TestCase):
    def test_seat_numbers(self):
        all_pins = fake_pins("2 mm rod", 72) + fake_pins("1 mm rod", 2, 1.0, 4.0)
        plan = fake_plan([{"2 mm rod": 40}, {"2 mm rod": 32, "1 mm rod": 2}])
        bom = assembly_bom.build_bom(all_pins, plan)
        main, fallback = bom.sizes
        self.assertEqual((main.label, main.fallback, main.length, main.count, main.spares, main.to_cut), ("2 mm rod", False, 12.0, 72, 8, 80))
        self.assertEqual((main.total_length, main.per_stick, main.sticks), (864.0, 74, 2))
        self.assertEqual((fallback.label, fallback.fallback, fallback.length, fallback.count, fallback.spares, fallback.sticks),
                         ("1 mm rod", True, 7.0, 2, 2, 1))
        self.assertEqual(fallback.steps, ("A2",))
        self.assertEqual(bom.total_rods, 74)

    def test_spares_are_the_larger_of_2_or_10_percent_rounded_up(self):
        self.assertEqual([assembly_bom.spares_for(n) for n in (1, 2, 20, 21, 72, 100)], [2, 2, 2, 3, 8, 10])

    def test_no_pins_means_no_bom(self):
        self.assertIsNone(assembly_bom.build_bom([], fake_plan([{}])))

    def test_step_counts_that_do_not_match_the_pins_are_refused(self):
        with self.assertRaises(assembly_bom.BomMismatch):
            assembly_bom.build_bom(fake_pins("2 mm rod", 10), fake_plan([{"2 mm rod": 9}]))
        with self.assertRaises(assembly_bom.BomMismatch):
            assembly_bom.build_bom(fake_pins("2 mm rod", 10), fake_plan([{"2 mm rod": 5}, {"2 mm rod": 4}]))

    def test_pins_are_named_pins(self):
        bom = assembly_bom.build_bom(fake_pins("1.75 mm pin", 3, 1.75), fake_plan([{"1.75 mm pin": 3}]))
        self.assertEqual(bom.kind, "pin")


def ruler_widths(pdf: bytes) -> list:
    """Widths in points of every filled rectangle in the PDF."""
    return [float(m.group(1)) for m in re.finditer(rb"[-\d.]+ [-\d.]+ ([-\d.]+) [-\d.]+ re\b", pdf)]


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class RodPageTests(GuideCase):
    def test_the_rod_page_follows_the_cover_and_has_the_rows(self):
        split_id = self.make_split(*rods_split(72, 2))
        self.generate(split_id)
        pages = page_texts(self.pdf(split_id))
        self.assertIn("Rods to cut", pages[1])
        self.assertNotIn("Rods to cut", pages[0])
        page = pages[1]
        for text in ("2 mm rod", "12 mm +/-0.5", "72", "864 mm", "2 x 1000 mm (about 39 in)", "(about 34 in)",
                     "Cut 80: 72 for the build plus 8 spares", "1 mm rod", "(fallback)", "7 mm +/-0.5", "1 stick",
                     "Fallback size, used in step A1", "Do not mix sizes", "must measure 100 mm", "Actual size",
                     "test-fit every rod", "deburr", "masking tape", "rubber bands", "Rod stock: steel, brass or carbon fibre"):
            self.assertIn(text.lower(), page.lower(), text)
        self.assertNotIn("Fallback size", pages[0])
        self.assertTrue(pages[2].startswith("Parts inventory"))   # the inventory follows the rod page
        self.assertIn("Section", pages[3])      # the steps start after the inventory

    def test_the_ruler_measures_100_mm_and_the_rods_are_true_size(self):
        split_id = self.make_split(*rods_split(4, 2))
        self.generate(split_id)
        widths = ruler_widths(self.pdf(split_id))
        mm = assembly_pdf.MM
        self.assertTrue(any(abs(w - 100 * mm) < 0.01 for w in widths))
        self.assertTrue(any(abs(w - 12 * mm) < 0.01 for w in widths))
        self.assertTrue(any(abs(w - 7 * mm) < 0.01 for w in widths))
        self.assertIn(b"/PrintScaling /None", self.pdf(split_id))

    def test_a_glue_only_split_has_no_rod_page(self):
        result, cuts = stack_split(3)
        result["pins"] = []
        for j in result["joints"]:
            j.update(rods=0, connectors={})
        split_id = self.make_split(result, cuts)
        self.generate(split_id)
        pages = page_texts(self.pdf(split_id))
        self.assertIn("no rods needed", pages[0])
        self.assertFalse(any("Rods to cut" in p for p in pages))
        self.assertIn("glue the face", pages[2])   # pages[1] is the inventory

    def test_a_split_with_rods_does_not_say_no_rods_needed(self):
        split_id = self.make_split(*grid_split())
        self.generate(split_id)
        self.assertNotIn("no rods needed", page_texts(self.pdf(split_id))[0])

    def test_generation_refuses_when_the_steps_disagree_with_the_rod_page(self):
        split_id = self.make_split(*grid_split())
        splits.start_guide(split_id, {})
        real = splits.assembly_plan(split_id)
        broken = json.loads(json.dumps(real))
        broken["sections"][0]["steps"][0]["connectors"] = {"2 mm rod": 1}
        with mock.patch.object(splits, "assembly_plan", return_value=broken):
            self.settle()
        status = splits.guide_status(split_id)
        self.assertEqual(status["state"], "failed")
        self.assertIn("do not match", status["error"])


if __name__ == "__main__":
    unittest.main()
