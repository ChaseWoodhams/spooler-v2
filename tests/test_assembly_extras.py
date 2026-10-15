"""Step extras of the Assembly guide: zoom circle, hole map, "you need" list and the "where this is" thumbnail.

Checked at the same seam as the step pages (a generated guide PDF: its text and structure, never pixels), plus the
zoom circle's placement, which is a geometric promise ("never covers a rod") a reader can verify on the page."""
import re
import unittest

import splits
from tests.test_assembly_guide import (GEOMETRY, GuideCase, body_text, box_mesh, lattice_split, page_texts, step_ids,
                                       step_pages)
from tests.test_assembly_plan import cut, joint, part, pins, stack_split

if GEOMETRY:
    import numpy as np

    import assembly_extras
    import assembly_render


def wide_split(rods=8):
    """Two 40 x 40 x 40 blocks joined by `rods` rods (a hole map join) and a third by two (no hole map)."""
    parts = [part(1, (0, 0, 0, 40, 40, 40)), part(2, (40, 0, 0, 80, 40, 40)), part(3, (80, 0, 0, 120, 40, 40))]
    joints = [joint(1, 2, 1, rods=rods), joint(2, 3, 2, rods=2)]
    spread = [p for i in range(rods) for p in pins(1, 2, "x", (40, 4 + 4 * (i % 4), 8 + 12 * (i // 4)), count=1)]
    all_pins = spread + pins(2, 3, "x", (80, 10, 20))
    return {"parts": parts, "joints": joints, "pins": all_pins}, [cut(1, "x", 40.0), cut(2, "x", 80.0)]


class ExtrasPageTests(GuideCase):
    def pages(self, result_and_cuts):
        split_id = self.make_split(*result_and_cuts)
        self.generate(split_id)
        return page_texts(self.pdf(split_id)), splits.assembly_plan(split_id)

    def test_the_hole_map_appears_only_for_a_join_of_five_or_more_rods(self):
        pages, plan = self.pages(wide_split(8))
        body = " ".join(body_text(t) for t in step_pages(pages))
        self.assertEqual(len(re.findall(r"Hole map", body)), 1)
        self.assertIn("Hole map: 8 rods", body)
        pages, _ = self.pages(stack_split(3))
        self.assertNotIn("Hole map", " ".join(pages))

    def test_the_you_need_list_gives_each_rod_size_with_its_count_and_the_part_or_section(self):
        pages, plan = self.pages(wide_split(8))
        texts = {step_ids(t)[0] if step_ids(t) else None: body_text(t) for t in step_pages(pages)}
        body = " ".join(texts.values())
        self.assertEqual(body.count("You need"), sum(len(s["steps"]) for s in plan["sections"]))
        self.assertIn("8 x 2 mm rod", body)
        self.assertIn("12 mm", body)            # cut length of a 6.5 mm deep hole: 2 x 6.5 - 1
        self.assertIn("Glue the join face", body)

    def test_every_step_has_a_zoom_with_its_magnification_and_the_thumbnail(self):
        pages, plan = self.pages(lattice_split())
        body = " ".join(body_text(t) for t in step_pages(pages))
        steps = sum(len(s["steps"]) for s in plan["sections"])
        self.assertEqual(len(re.findall(r"Zoom \d+(?:\.5)?x", body)), steps)
        self.assertEqual(body.count("Where this is"), steps)

    def test_no_text_is_smaller_than_nine_points(self):
        split_id = self.make_split(*wide_split(8))
        self.generate(split_id)
        sizes = [float(s) for s in re.findall(rb"/F\d+ ([\d.]+) Tf", self.pdf(split_id))]
        self.assertTrue(sizes)
        self.assertGreaterEqual(min(sizes), 9.0)

    def test_two_steps_still_share_a_page(self):
        pages, _ = self.pages(stack_split(7))
        self.assertEqual([step_ids(t) for t in step_pages(pages)], [["A1", "A2"], ["A3", "A4"], ["A5", "A6"]])


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class ZoomPlacementTests(unittest.TestCase):
    def art(self, result_and_cuts, direction=(1.0, 0.0, 0.0), mover=(2,), receiver=(1,)):
        result, _ = result_and_cuts
        meshes = [box_mesh(p["bbox"]) for p in sorted(result["parts"], key=lambda p: p["number"])]
        scene = assembly_render.Scene.from_split(result, meshes)
        step = {"id": "A1", "kind": "part", "parts": mover, "receiver": receiver, "direction": direction, "rods": 2,
                "connectors": {}, "glue_only": False, "section": None}
        return assembly_render.prepare_step(scene, step, assembly_render.choose_camera(scene, step, None))

    def check(self, art):
        frame = assembly_render.layout_frames(art, 190 * assembly_render.MM)[0]
        zoom = assembly_extras.zoom_circle(art, frame)
        self.assertIsNotNone(zoom)
        (cx, cy), radius, magnification = zoom
        self.assertAlmostEqual(radius * 2 / assembly_render.MM, 26.0, places=3)
        self.assertGreaterEqual(magnification, 2)
        self.assertTrue(frame[0] <= cx - radius and cx + radius <= frame[0] + frame[2])
        self.assertTrue(frame[1] <= cy - radius and cy + radius <= frame[1] + frame[3])
        for start, end, width in assembly_extras.rod_segments(art, frame):
            self.assertGreater(assembly_extras.segment_distance((cx, cy), start, end), radius + width / 2)

    def test_the_zoom_circle_is_inside_the_frame_and_never_covers_a_rod(self):
        self.check(self.art(wide_split(8)))
        self.check(self.art(wide_split(8), direction=(-1.0, 0.0, 0.0), mover=(1,), receiver=(2,)))
        self.check(self.art(stack_split(3), mover=(3,), receiver=(1, 2)))

    def test_a_step_without_rods_has_no_zoom_circle(self):
        art = self.art(wide_split(8))
        art.pins = []
        frame = assembly_render.layout_frames(art, 190 * assembly_render.MM)[0]
        self.assertIsNone(assembly_extras.zoom_circle(art, frame))
