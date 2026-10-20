"""Cover, Section tree and finished page of the Assembly guide: read back from the PDF text and colours, and from the
overview art (strokes, badge places, camera choice) the module prepares."""
import io
import re
import unittest

import assembly_overview
import assembly_pdf
import assembly_render
import splits
from tests.test_assembly_guide import GEOMETRY, GuideCase, box_mesh, lattice_split, page_texts, stack_split, text_positions
from tests.test_assembly_plan import grid_split

if GEOMETRY:
    import numpy as np
    import trimesh


def page_streams(pdf: bytes) -> list:
    """The drawing commands of each page of an uncompressed PDF."""
    objects = pdf.split(b"endobj")
    pages = [o for o in objects if re.search(rb"/Type /Page\s*>>", o)]
    contents = {int(m.group(1)): m.group(2) for m in re.finditer(rb"(\d+) 0 obj\s*<<[^>]*?>>\s*stream\r?\n(.*?)endstream", pdf, re.S)}
    return [contents[int(re.search(rb"/Contents (\d+) 0 R", page).group(1))].decode("latin-1") for page in pages]


def colours_used(stream: str) -> set:
    """The distinct non-grey (r, g, b) colours a page sets, for lines or fills."""
    found = {tuple(float(v) for v in m.groups()[:3]) for m in re.finditer(r"([\d.]+) ([\d.]+) ([\d.]+) (?:RG|rg)\b", stream)}
    return {c for c in found if not (c[0] == c[1] == c[2])}


def box_scene(boxes: dict):
    """A Scene of axis-aligned boxes {number: (x0, y0, z0, x1, y1, z1)}, every pair touching."""
    meshes = {n: trimesh.Trimesh(*box_mesh(b), process=True) for n, b in boxes.items()}
    numbers = sorted(boxes)
    touching = {assembly_render._pair(a, b) for a in numbers for b in numbers if a < b}
    return assembly_render.Scene(meshes, [], touching)


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class CoverTests(GuideCase):
    def cover(self, result, cuts):
        split_id = self.make_split(result, cuts)
        self.generate(split_id)
        pdf = self.pdf(split_id)
        return split_id, pdf, page_texts(pdf)

    def test_the_cover_colours_each_section_differently_and_names_them_by_letter(self):
        _, pdf, texts = self.cover(*lattice_split())          # three Sections: A (main), B, C
        for letter in "ABC":
            self.assertRegex(texts[0], rf"\b{letter}\b")       # a letter on the picture or legend: distinct without colour
        self.assertGreaterEqual(len(colours_used(page_streams(pdf)[0])), 3)

    def test_the_cover_explains_the_plan_code_in_one_sentence(self):
        split_id, _, texts = self.cover(*grid_split())
        sentence = re.search(r"[^.]*\bplan code\b[^.]*\bsame plan\b[^.]*\.", texts[0], re.I)
        self.assertTrue(sentence, texts[0])

    def test_the_cover_shows_the_page_count_and_a_rough_build_time(self):
        _, _, texts = self.cover(*grid_split())                # 3 steps and one side Section
        self.assertIn(f"{len(texts)} pages", texts[0])
        self.assertIn("Rough build time: about 45 min", texts[0])    # 3 steps x 5 min + 30 min to cure the side Section

    def test_a_longer_build_is_given_in_hours(self):
        _, _, texts = self.cover(*lattice_split())             # 8 steps x 5 min + 2 side Sections x 30 min = 100 min
        self.assertIn("Rough build time: about 1 h 40 min", texts[0])

    def test_the_cover_stays_inside_the_safe_area(self):
        for paper in ("letter", "a4"):
            split_id = self.make_split(*lattice_split())
            self.generate(split_id, paper)
            safe = assembly_pdf.safe_area(paper)
            for x, y, text in text_positions(self.pdf(split_id))[0]:
                self.assertGreaterEqual(x, safe.left - 0.5, text)
                self.assertLessEqual(x, safe.right, text)
                self.assertGreaterEqual(y, safe.bottom, text)
                self.assertLessEqual(y, safe.top, text)


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class BadgeTests(unittest.TestCase):
    def test_crowded_badges_move_apart_and_the_moved_ones_get_a_leader_line(self):
        rect = (0, 0, 300, 200)
        points = [(150.0, 100.0)] * 5 + [(20.0, 20.0)]
        placed = assembly_overview.place_badges(points, 9, rect)
        centres = [centre for _, centre in placed]
        for i, a in enumerate(centres):
            for b in centres[i + 1:]:
                self.assertGreaterEqual(np.hypot(a[0] - b[0], a[1] - b[1]), 18 - 1e-6)
            self.assertTrue(0 <= a[0] <= 300 and 0 <= a[1] <= 200)
        leaders = [assembly_overview.needs_leader(anchor, centre, 9) for anchor, centre in placed]
        self.assertEqual(sum(leaders), 4)                      # the first of the five stays on its part
        self.assertFalse(leaders[-1])                          # a lone badge needs none


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class TreeTests(GuideCase):
    def texts(self, result, cuts):
        split_id = self.make_split(result, cuts)
        self.generate(split_id)
        return split_id, page_texts(self.pdf(split_id))

    def test_the_tree_follows_the_plan_from_the_main_section_down(self):
        split_id, _ = self.texts(*lattice_split())
        tree = assembly_overview.section_tree(splits.assembly_plan(split_id))
        self.assertEqual([(n["letter"], n["depth"], n["parent"], n["step"]) for n in tree],
                         [("A", 0, None, None), ("B", 1, "A", "A3"), ("C", 1, "A", "A4")])
        self.assertEqual([n["parts"] for n in tree], [[2, 5, 8], [3, 6, 9], [1, 4, 7]])   # each Section's own parts

    def test_a_split_with_one_section_has_no_tree(self):
        split_id, texts = self.texts(*stack_split(3))
        self.assertEqual(assembly_overview.section_tree(splits.assembly_plan(split_id)), [])
        self.assertFalse([t for t in texts if t.startswith("Section tree")])

    def test_the_tree_page_comes_after_the_cover_with_letters_parts_and_join_steps(self):
        _, texts = self.texts(*lattice_split())
        page = texts[1]
        self.assertTrue(page.startswith("Section tree"), page[:60])
        body = page.rsplit("model  |  Split", 1)[0]
        self.assertRegex(body, r"A \(main\).*parts 2, 5, 8")
        self.assertRegex(body, r"B .*parts 3, 6, 9.*joins Section A at step A3")
        self.assertRegex(body, r"C .*parts 1, 4, 7.*joins Section A at step A4")

    def test_a_glue_only_split_still_has_its_tree(self):
        result, cuts = grid_split()
        result["pins"] = []
        _, texts = self.texts(result, cuts)
        self.assertTrue(texts[1].startswith("Section tree"))

    def test_a_long_tree_flows_over_pages_inside_the_safe_area(self):
        sections = [{"letter": chr(65 + i), "main": i == 0, "parts": [i + 1], "start_part": i + 1, "steps": []} for i in range(26)]
        sections[0]["parts"] = list(range(1, 27))
        sections[0]["steps"] = [{"id": f"A{i}", "kind": "section", "section": s["letter"], "parts": s["parts"], "rods": 2, "receiver": [1]}
                                for i, s in enumerate(sections[1:], start=1)]
        plan = {"sections": sections[1:] + sections[:1], "summary": {"sections": 26}}
        buffer = io.BytesIO()
        canvas = assembly_pdf._GuideCanvas(buffer, pagesize=assembly_pdf.PAPERS["a4"], pageCompression=False, invariant=1)
        flow = assembly_pdf.PageFlow(canvas, "a4")
        assembly_overview.draw_tree(flow, plan)
        canvas.showPage()
        canvas.save()
        pdf = buffer.getvalue()
        pages = page_texts(pdf)[1:]               # the flow starts on a blank page standing in for the cover
        self.assertGreater(len(pages), 1)
        for text in pages:
            self.assertIn("Section tree", text)
        safe = assembly_pdf.safe_area("a4")
        for strings in text_positions(pdf):
            for x, y, text in strings:
                self.assertGreaterEqual(y, safe.bottom, text)
                self.assertLessEqual(y, safe.top, text)
        self.assertIn("joins Section A at step A25", " ".join(pages))


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class FinishedViewTests(GuideCase):
    def test_the_second_view_looks_at_the_largest_face_the_first_cannot_see(self):
        wall = box_scene({1: (0, 0, 0, 100, 10, 60), 2: (0, 10, 0, 100, 20, 60)})   # broad in x and z: its biggest faces face +-y
        first, second = assembly_overview.finished_cameras(wall)
        self.assertGreater(float(first @ np.array([0, 1.0, 0])), 0)               # the first view sees +y ...
        self.assertGreater(float(second @ np.array([0, -1.0, 0])), 0.5)           # ... so the second looks at -y

    def test_a_flat_plate_is_seen_from_below_in_the_second_view(self):
        plate = box_scene({1: (0, 0, 0, 100, 100, 5), 2: (100, 0, 0, 200, 100, 5)})
        first, second = assembly_overview.finished_cameras(plate)
        self.assertGreater(first[2], 0)
        self.assertGreater(float(second @ np.array([0, 0, -1.0])), 0.5)

    def test_the_last_page_shows_two_views(self):
        split_id = self.make_split(*lattice_split())
        self.generate(split_id)
        last = page_texts(self.pdf(split_id))[-1]
        self.assertIn("View 1", last)
        self.assertIn("View 2", last)
        self.assertIn("No parts or rods left over.", last)

    def test_part_boundaries_are_drawn_heavier_than_surface_creases(self):
        scene = box_scene({1: (0, 0, 0, 50, 50, 20), 2: (50, 0, 0, 100, 50, 20), 3: (100, 0, 0, 150, 50, 20)})
        art = assembly_overview.overview_art(scene, assembly_render.camera_candidates()[0], {})
        creases = [s for s in art.strokes if s.kind == "crease" and s.runs]
        joins = [s for s in art.strokes if s.kind == "join" and s.runs]
        self.assertTrue(creases)
        self.assertEqual({s.part for s in joins}, {1, 2, 3})
        self.assertGreater(min(s.weight for s in joins), max(s.weight for s in creases))

    def test_the_cover_colours_the_lines_of_each_section(self):
        scene = box_scene({1: (0, 0, 0, 50, 50, 20), 2: (50, 0, 0, 100, 50, 20)})
        colours = {1: (0.8, 0.3, 0.0), 2: (0.0, 0.4, 0.7)}
        art = assembly_overview.overview_art(scene, assembly_render.camera_candidates()[0], colours)
        self.assertEqual({s.part: s.colour for s in art.strokes if s.kind == "crease"}, colours)


if __name__ == "__main__":
    unittest.main()
