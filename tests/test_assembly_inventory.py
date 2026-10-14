"""Cover overview, parts inventory and last page of the Assembly guide, read back from a generated PDF."""
import re
import unittest
from unittest import mock

import splits
from tests.test_assembly_guide import GEOMETRY, GuideCase, lattice_split, page_texts
from tests.test_assembly_plan import grid_split

LABEL = {"text": "1", "face": "cut", "cut": 1, "neighbour": 2, "position": [40.0, 20.0, 5.0], "normal": [1.0, 0.0, 0.0],
         "up": [0.0, 0.0, 1.0], "height_mm": 7.0}


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class InventoryTests(GuideCase):
    def texts(self, split_id, plates=None):
        with mock.patch.object(splits, "_part_plates", return_value=plates or {}):
            self.generate(split_id)
        return page_texts(self.pdf(split_id))

    def inventory(self, pages):
        return [t for t in pages if t.startswith("Parts inventory")]

    def test_the_inventory_holds_15_parts_a_page_in_part_number_order(self):
        result, cuts = lattice_split(n=4)
        pages = self.inventory(self.texts(self.make_split(result, cuts)))
        self.assertEqual(len(pages), 2)
        shown = [int(n) for t in pages for n in re.findall(r"(?<![\w:])(\d+)(?= Plate:)", t)]
        self.assertEqual(shown, list(range(1, 17)))
        self.assertEqual(pages[0].count("Plate:"), 15)
        self.assertEqual(pages[1].count("Plate:"), 1)

    def test_a_part_shows_its_plate_or_a_dash(self):
        pages = self.inventory(self.texts(self.make_split(*grid_split()), {2: "Plate 3"}))
        self.assertIn("2 Plate: Plate 3", pages[0])
        self.assertIn("1 Plate: -", pages[0])

    def test_a_part_with_a_label_says_where_its_number_is_and_one_without_says_to_use_the_shape(self):
        result, cuts = grid_split()
        result["parts"][0]["label"] = dict(LABEL)
        text = self.inventory(self.texts(self.make_split(result, cuts)))[0]
        self.assertIn("number is here (face against part 2)", text)
        self.assertEqual(text.count("no number: identify by shape"), len(result["parts"]) - 1)

    def test_page_order_is_cover_rod_inventory_steps_last(self):
        split_id = self.make_split(*lattice_split())
        pages = self.texts(split_id)
        kind = ["cover" if i == 0 else "rod" if "100 mm" in t and "Parts inventory" not in t else
                "inventory" if t.startswith("Parts inventory") else "last" if "left over" in t else "step"
                for i, t in enumerate(pages)]
        collapsed = [k for i, k in enumerate(kind) if i == 0 or k != kind[i - 1]]
        self.assertEqual(collapsed, ["cover", "rod", "inventory", "step", "last"])

    def test_a_glue_only_split_has_no_rod_page_but_the_same_order(self):
        result, cuts = grid_split()
        result["pins"] = []
        pages = self.texts(self.make_split(result, cuts))
        self.assertTrue(pages[1].startswith("Parts inventory"))
        self.assertIn("left over", pages[-1])

    def test_the_last_page_has_the_nothing_left_over_tick(self):
        pages = self.texts(self.make_split(*grid_split()))
        self.assertIn("No parts or rods left over.", pages[-1])
        self.assertIn("page", pages[-1])

    def test_the_cover_shows_a_bubble_for_every_part(self):
        result, cuts = lattice_split()
        cover = self.texts(self.make_split(result, cuts))[0]
        for part in result["parts"]:
            self.assertIn(str(part["number"]), cover.split())


if __name__ == "__main__":
    unittest.main()
