"""Cover overview, parts inventory and last page of the Assembly guide, read back from a generated PDF."""
import re
import unittest
from unittest import mock

import assembly_inventory
import assembly_render
import assembly_rules
import splits
from tests.test_assembly_guide import GEOMETRY, GuideCase, box_mesh, lattice_split, page_texts
from tests.test_assembly_plan import cut, grid_split, joint, part, pins

LABEL = {"text": "1", "face": "cut", "cut": 1, "neighbour": 2, "position": [40.0, 20.0, 5.0], "normal": [1.0, 0.0, 0.0],
         "up": [0.0, 0.0, 1.0], "height_mm": 7.0}


def thin_split(count, width=120.0, depth=10.0):
    """`count` long, thin bars side by side along y, each cut from the next by its own plane."""
    parts = [part(i + 1, (0, i * depth, 0, width, (i + 1) * depth, 6)) for i in range(count)]
    joints = [joint(i + 1, i + 2, i + 1) for i in range(count - 1)]
    all_pins = [p for i in range(count - 1) for p in pins(i + 1, i + 2, "y", (width / 2, (i + 1) * depth, 3))]
    return {"parts": parts, "joints": joints, "pins": all_pins}, [cut(i + 1, "y", (i + 1) * depth) for i in range(count - 1)]


def scene_of(result):
    boxes = [box_mesh(p["bbox"]) for p in sorted(result["parts"], key=lambda p: p["number"])]
    return assembly_render.Scene.from_split(result, boxes)


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class CardSizingTests(unittest.TestCase):
    def test_a_long_thin_part_gets_a_tall_cell_and_a_second_view(self):
        scene = scene_of(thin_split(2)[0])
        self.assertTrue(assembly_inventory.card_is_tall(scene, 1))
        self.assertEqual(assembly_inventory.card_views(scene, 1), 2)

    def test_a_broad_part_gets_one_view_in_an_ordinary_cell(self):
        scene = scene_of(grid_split()[0])
        self.assertFalse(assembly_inventory.card_is_tall(scene, 1))
        self.assertEqual(assembly_inventory.card_views(scene, 1), 1)

    def test_the_width_threshold_is_a_rule(self):
        scene = scene_of(thin_split(2)[0])
        with mock.patch.object(assembly_rules, "THIN_WIDTH_MM", 5.0), mock.patch.object(assembly_rules, "TALL_ASPECT", 100.0):
            self.assertEqual(assembly_inventory.card_views(scene, 1), 1)
            self.assertFalse(assembly_inventory.card_is_tall(scene, 1))


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
        shown = [int(n) for t in pages for n in re.findall(r"(?<![\w:])(\d+)(?= (?:Plate:|not on a plate yet|have it))", t)]
        self.assertEqual(shown, list(range(1, 17)))
        self.assertEqual(pages[0].count("have it"), 15)
        self.assertEqual(pages[1].count("have it"), 1)

    def test_a_part_shows_its_plate_or_says_it_is_not_on_a_plate_yet(self):
        pages = self.inventory(self.texts(self.make_split(*grid_split()), {2: "Plate 3"}))
        self.assertIn("2 Plate: Plate 3", pages[0])
        self.assertIn("1 not on a plate yet", pages[0])
        self.assertNotIn("Plate: -", pages[0])

    def test_the_plate_line_is_left_out_when_no_part_is_on_a_plate(self):
        text = " ".join(self.inventory(self.texts(self.make_split(*grid_split()))))
        self.assertNotIn("Plate", text)
        self.assertNotIn("not on a plate yet", text)

    def test_unnumbered_parts_are_listed_with_their_size_on_the_inventory_page_and_not_on_a_section_page(self):
        result, cuts = grid_split()
        result["unlabelled"] = [1]
        pages = self.texts(self.make_split(result, cuts))
        inventory = self.inventory(pages)
        self.assertRegex(inventory[0], r"Part 1 \(\d+ mm\)")
        self.assertIn("No number printed", inventory[0])
        others = " ".join(t for t in pages if not t.startswith("Parts inventory"))
        self.assertNotIn("No number printed", others)

    def test_there_is_no_unnumbered_note_when_every_part_has_a_number(self):
        text = " ".join(self.inventory(self.texts(self.make_split(*grid_split()))))
        self.assertNotIn("No number printed", text)

    def test_a_one_line_legend_explains_the_number_marker(self):
        text = self.inventory(self.texts(self.make_split(*grid_split())))[0]
        self.assertIn("Small circle on a picture: where the printed number is.", text)

    def test_a_part_with_a_label_says_where_its_number_is_and_one_without_says_to_use_the_shape(self):
        result, cuts = grid_split()
        result["parts"][0]["label"] = dict(LABEL)
        text = self.inventory(self.texts(self.make_split(result, cuts)))[0]
        self.assertIn("number is here (face against part 2)", text)
        self.assertEqual(text.count("no number: identify by shape"), len(result["parts"]) - 1)

    def test_each_card_shows_the_largest_dimension_in_millimetres(self):
        text = self.inventory(self.texts(self.make_split(*grid_split())))[0]
        self.assertEqual(re.findall(r"Largest: (\d+) mm", text), ["50"] * 4)

    def test_long_thin_parts_take_taller_cells_so_the_inventory_runs_to_more_pages_without_losing_a_card(self):
        pages = self.inventory(self.texts(self.make_split(*thin_split(12))))
        self.assertEqual(len(pages), 2)
        shown = [int(n) for t in pages for n in re.findall(r"(?<![\w:])(\d+)(?= (?:not on a plate yet|have it|Plate:))", t)]
        self.assertEqual(shown, list(range(1, 13)))
        self.assertEqual(sum(t.count("have it") for t in pages), 12)
        self.assertEqual(sum(len(re.findall(r"Largest: 120 mm", t)) for t in pages), 12)

    def test_a_single_strip_of_all_parts_at_one_scale_is_on_the_first_inventory_page_only(self):
        result, cuts = lattice_split(n=4)
        pages = self.inventory(self.texts(self.make_split(result, cuts)))
        self.assertEqual(pages[0].count("All parts at one scale"), 1)
        self.assertNotIn("All parts at one scale", pages[1])

    def test_page_order_is_cover_rod_inventory_steps_last(self):
        split_id = self.make_split(*lattice_split())
        pages = self.texts(split_id)
        kind = ["cover" if i == 0 else "tree" if t.startswith("Section tree") else "rod" if "100 mm" in t and "Parts inventory" not in t else
                "inventory" if t.startswith("Parts inventory") else "last" if "left over" in t else "step"
                for i, t in enumerate(pages)]
        collapsed = [k for i, k in enumerate(kind) if i == 0 or k != kind[i - 1]]
        self.assertEqual(collapsed, ["cover", "tree", "rod", "inventory", "step", "last"])

    def test_a_glue_only_split_has_no_rod_page_but_the_same_order(self):
        result, cuts = grid_split()
        result["pins"] = []
        pages = self.texts(self.make_split(result, cuts))
        self.assertTrue(pages[1].startswith("Section tree"))      # the Sections still nest in a glue-only split
        self.assertTrue(pages[2].startswith("Parts inventory"))
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
