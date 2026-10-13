"""Generating the Assembly guide PDF for a finished split (text-only slice).

The seam is the splits service: start a guide on a split in the split or queued state, settle the background job, and
check what a user could see: the job's status and progress, the split's own state, the stored PDF's date and plan code,
the paper size, the out-of-date marking and the text and page footers of the PDF itself."""
import asyncio
import json
import re
import unittest
from unittest import mock

import assembly_pdf
import jobs
import splits
from tests.test_assembly_plan import PlanServiceCase, GEOMETRY, grid_split, stack_split, part, cut, joint, pins

if GEOMETRY:
    import manifold3d as m3d
    import numpy as np

    import assembly_render
    import split_engine


def page_texts(pdf: bytes) -> list:
    """The text drawn on each page of an uncompressed PDF, one string per page."""
    objects = pdf.split(b"endobj")
    pages = [o for o in objects if re.search(rb"/Type /Page\s*>>", o)]
    contents = {int(m.group(1)): m.group(2) for m in re.finditer(rb"(\d+) 0 obj\s*<<[^>]*?>>\s*stream\r?\n(.*?)endstream", pdf, re.S)}
    out = []
    for page in pages:
        ref = re.search(rb"/Contents (\d+) 0 R", page)
        body = contents[int(ref.group(1))].decode("latin-1")
        out.append(" ".join(m.replace("\\(", "(").replace("\\)", ")") for m in re.findall(r"\(((?:\\.|[^\\)])*)\) Tj", body)))
    return out


def media_box(pdf: bytes) -> tuple:
    box = re.search(rb"/MediaBox \[ ?0 0 ([\d.]+) ([\d.]+) ?\]", pdf)
    return float(box.group(1)), float(box.group(2))


def box_mesh(bbox):
    mesh = m3d.Manifold.cube([bbox[3] - bbox[0], bbox[4] - bbox[1], bbox[5] - bbox[2]]).translate(list(bbox[:3])).to_mesh()
    return np.asarray(mesh.vert_properties)[:, :3], np.asarray(mesh.tri_verts)


def lattice_split(n=3, size=40.0):
    """n x n parts in a lattice, cut by planes across x and then across y: several Sections, several steps in each."""
    number = lambda i, j: i * n + j + 1
    parts = [part(number(i, j), (j * size, i * size, 0, (j + 1) * size, (i + 1) * size, 10)) for i in range(n) for j in range(n)]
    cuts = [cut(k + 1, "x", (k + 1) * size) for k in range(n - 1)] + [cut(n + k, "y", (k + 1) * size) for k in range(n - 1)]
    joints, all_pins = [], []
    for k in range(n - 1):
        for i in range(n):
            a, b = number(i, k), number(i, k + 1)
            joints.append(joint(a, b, k + 1))
            all_pins += pins(a, b, "x", ((k + 1) * size, i * size + 10, 5))
        for j in range(n):
            a, b = number(k, j), number(k + 1, j)
            joints.append(joint(a, b, n + k))
            all_pins += pins(a, b, "y", (j * size + 10, (k + 1) * size, 5))
    return {"parts": parts, "joints": joints, "pins": all_pins}, cuts


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class GuideCase(PlanServiceCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(assembly_pdf, "COMPRESS", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_split(self, result, cuts, meshes=None):
        """Each part's preview mesh is a box of its bounding box, as the split engine would leave a real preview;
        `meshes=[]` leaves the preview out."""
        if meshes is None:
            meshes = [box_mesh(p["bbox"]) for p in sorted(result["parts"], key=lambda p: p["number"])]
        return super().make_split(result, cuts, meshes)

    def settle(self):
        async def run():
            while await splits.process_next():
                pass
        asyncio.run(run())

    def generate(self, split_id, paper=None):
        splits.start_guide(split_id, {"paper": paper} if paper else {})
        self.settle()
        return splits.guide_status(split_id)

    def pdf(self, split_id) -> bytes:
        path, _name = splits.guide_path(split_id)
        return path.read_bytes()


class JobTests(GuideCase):
    def test_generating_runs_as_a_job_and_leaves_the_split_state_alone(self):
        split_id = self.make_split(*grid_split())
        started = splits.start_guide(split_id, {})
        self.assertEqual(started["state"], "generating")
        self.assertEqual(splits.get_split(split_id)["state"], splits.SPLIT)
        self.settle()
        status = splits.guide_status(split_id)
        self.assertEqual(status["state"], "ready")
        self.assertEqual(splits.get_split(split_id)["state"], splits.SPLIT)
        self.assertTrue(status["generated_at"])
        self.assertEqual(status["plan_code"], splits.assembly_plan(split_id)["code"])
        self.assertFalse(status["out_of_date"])

    def test_it_is_allowed_for_a_queued_split_and_refused_in_other_states(self):
        split_id = self.make_split(*grid_split())
        splits._set_state(split_id, splits.QUEUED)
        self.assertEqual(self.generate(split_id)["state"], "ready")
        self.assertEqual(splits.get_split(split_id)["state"], splits.QUEUED)
        splits._set_state(split_id, splits.READY)
        with self.assertRaises(jobs.JobError):
            splits.start_guide(split_id, {})

    def test_status_shows_the_stage_while_generating_and_a_second_start_is_refused(self):
        split_id = self.make_split(*grid_split())
        splits.start_guide(split_id, {})
        status = splits.guide_status(split_id)
        self.assertEqual(status["state"], "generating")
        self.assertTrue(status["stage"])
        with self.assertRaises(jobs.JobError):
            splits.start_guide(split_id, {})

    def test_a_failure_in_the_renderer_is_reported_and_the_split_is_untouched(self):
        split_id = self.make_split(*grid_split())
        splits.start_guide(split_id, {})
        with mock.patch.object(assembly_pdf, "render_guide", side_effect=RuntimeError("boom")):
            self.settle()
        status = splits.guide_status(split_id)
        self.assertEqual((status["state"], status["error"]), ("failed", "boom"))
        self.assertEqual(splits.get_split(split_id)["state"], splits.SPLIT)
        self.assertEqual(self.generate(split_id)["state"], "ready")   # trying again works

    def test_a_failed_try_keeps_the_pdf_from_before(self):
        split_id = self.make_split(*grid_split())
        self.generate(split_id)
        splits.start_guide(split_id, {})
        with mock.patch.object(assembly_pdf, "render_guide", side_effect=RuntimeError("boom")):
            self.settle()
        self.assertTrue(self.pdf(split_id).startswith(b"%PDF"))

    def test_a_missing_pdf_library_disables_only_generating(self):
        split_id = self.make_split(*grid_split())
        with mock.patch.object(splits, "missing_guide_modules", return_value=["reportlab"]):
            self.assertFalse(splits.status()["guide"]["available"])
            self.assertTrue(splits.status()["available"])
            self.assertIn("pip install", splits.status()["guide"]["hint"])
            self.assertIn("reportlab", splits.guide_status(split_id)["blocked"])
            with self.assertRaises(jobs.JobError):
                splits.start_guide(split_id, {})

    def test_a_split_that_cannot_be_assembled_is_blocked_with_the_reason(self):
        result, cuts = grid_split()
        for pin in result["pins"]:
            if pin["parts"] == [1, 3]:
                pin["p"][1] = 80.0     # lies on no cut plane: a data problem
        result["joints"] = [j for j in result["joints"] if j["parts"] != [1, 3]]
        split_id = self.make_split(result, cuts)
        status = splits.guide_status(split_id)
        self.assertTrue(status["blocked"])
        with self.assertRaises(jobs.JobError):
            splits.start_guide(split_id, {})
        with self.assertRaises(jobs.JobError):
            splits.guide_path(split_id)


class PaperTests(GuideCase):
    def test_paper_is_letter_for_a_us_locale_and_a4_otherwise(self):
        for name, expected in (("en_US", "letter"), ("English_United States", "letter"), ("de_DE", "a4"), (None, "a4"), ("en_GB", "a4")):
            with self.subTest(name), mock.patch.object(splits, "system_locale", return_value=name):
                self.assertEqual(splits.default_paper(), expected)
                self.assertEqual(splits.status()["guide"]["default_paper"], expected)

    def test_the_default_and_the_override_set_the_page_size(self):
        split_id = self.make_split(*grid_split())
        with mock.patch.object(splits, "system_locale", return_value="en_US"):
            self.assertEqual(self.generate(split_id)["paper"], "letter")
        self.assertEqual(media_box(self.pdf(split_id)), (612.0, 792.0))
        self.assertEqual(self.generate(split_id, "a4")["paper"], "a4")
        width, height = media_box(self.pdf(split_id))
        self.assertAlmostEqual(width, 595.28, places=1)
        self.assertAlmostEqual(height, 841.89, places=1)

    def test_an_unknown_paper_is_refused(self):
        with self.assertRaises(jobs.JobError):
            splits.start_guide(self.make_split(*grid_split()), {"paper": "tabloid"})


class OutOfDateTests(GuideCase):
    def test_a_pdf_is_kept_but_marked_out_of_date_when_the_plan_changes(self):
        split_id = self.make_split(*grid_split())
        first = self.generate(split_id)
        result, cuts = stack_split(3)    # the split is re-run and comes out differently
        work = splits.work_dir(split_id)
        (work / "result.json").write_text(json.dumps(result), encoding="utf-8")
        (work / "cuts.json").write_text(json.dumps({"mode": "smart", "cuts": cuts}), encoding="utf-8")
        split_engine.write_mesh_bin(work / "parts_preview.bin", [box_mesh(p["bbox"]) for p in result["parts"]])
        status = splits.guide_status(split_id)
        self.assertEqual(status["state"], "ready")
        self.assertTrue(status["out_of_date"])
        self.assertEqual(status["plan_code"], first["plan_code"])
        self.assertTrue(self.pdf(split_id).startswith(b"%PDF"))
        again = self.generate(split_id)
        self.assertFalse(again["out_of_date"])
        self.assertNotEqual(again["plan_code"], first["plan_code"])


class ContentTests(GuideCase):
    def test_the_cover_has_the_name_counts_plan_line_and_code(self):
        split_id = self.make_split(*grid_split())
        self.generate(split_id)
        cover = page_texts(self.pdf(split_id))[0]
        code = splits.assembly_plan(split_id)["code"]
        for text in ("model", "4 parts", "2 Sections", "3 steps", "8 rods", "Automatic plan", code):
            self.assertIn(text, cover)

    def test_every_page_after_the_cover_has_a_footer_with_the_code_and_page_numbers(self):
        split_id = self.make_split(*stack_split(3))
        self.generate(split_id)
        pages = page_texts(self.pdf(split_id))
        code = splits.assembly_plan(split_id)["code"]
        self.assertGreaterEqual(len(pages), 2)
        self.assertNotIn("page 1 of", pages[0])
        for number, text in enumerate(pages[1:], start=2):
            self.assertIn(f"page {number} of {len(pages)}", text)
            self.assertIn(code, text)
            self.assertIn("model", text)
            self.assertIn(f"Split {split_id}", text)
            if "Rods to cut" not in text:   # the rod page belongs to no Section
                self.assertRegex(text, r"Sections? [A-Z]")

    def test_the_guide_lists_every_section_and_step_in_plan_order(self):
        split_id = self.make_split(*grid_split())
        self.generate(split_id)
        body = " ".join(page_texts(self.pdf(split_id))[1:])
        positions = [body.index(label) for label in ("Section B", "B1", "Section A", "A1", "A2")]
        self.assertEqual(positions, sorted(positions))

    def test_the_same_split_gives_the_same_plan_code_and_text(self):
        split_id = self.make_split(*grid_split())
        self.generate(split_id)
        second_id = self.make_split(*grid_split())
        self.generate(second_id)
        self.assertEqual(splits.guide_status(second_id)["plan_code"], splits.guide_status(split_id)["plan_code"])
        strip = lambda split, text: text.replace(f"Split {split}", "Split N")
        self.assertEqual([strip(second_id, t) for t in page_texts(self.pdf(second_id))],
                         [strip(split_id, t) for t in page_texts(self.pdf(split_id))])

    def test_the_download_is_named_after_the_model(self):
        split_id = self.make_split(*grid_split())
        self.generate(split_id)
        self.assertEqual(splits.guide_path(split_id)[1], "model - assembly guide.pdf")

    def test_the_safe_area_is_190_by_265_mm_and_fits_a4_and_letter(self):
        self.assertEqual((assembly_pdf.SAFE_WIDTH_MM, assembly_pdf.SAFE_HEIGHT_MM), (190, 265))
        for paper in ("a4", "letter"):
            width, height = assembly_pdf.PAPERS[paper]
            safe = assembly_pdf.safe_area(paper)
            self.assertAlmostEqual((safe.right - safe.left) / assembly_pdf.MM, 190)
            self.assertAlmostEqual((safe.top - safe.bottom) / assembly_pdf.MM, 265)
            self.assertGreaterEqual(safe.left, 0)
            self.assertGreaterEqual(safe.bottom, 0)
            self.assertLessEqual(safe.right, width)
            self.assertLessEqual(safe.top, height)


class LongGuideTests(GuideCase):
    def test_a_long_plan_flows_over_several_pages_each_with_its_footer(self):
        split_id = self.make_split(*stack_split(60))
        self.generate(split_id)
        pages = page_texts(self.pdf(split_id))
        self.assertGreater(len(pages), 2)
        for number, text in enumerate(pages[1:], start=2):
            self.assertIn(f"page {number} of {len(pages)}", text)
        body = " ".join(pages[1:])
        self.assertIn("A59", body)


def body_text(text: str) -> str:
    """A page's text without its footer."""
    return text.rsplit("model  |  Split", 1)[0]


def step_pages(pages: list) -> list:
    """The pages of the step section: the ones with a Section header or a step (not the cover or the rod page)."""
    return [t for t in pages[1:] if "Made of" in body_text(t) or step_ids(t)]


def step_ids(text: str) -> list:
    return re.findall(r"\b([A-Z]\d{1,2}) (?=Add|Join)", body_text(text))


class StepPageTests(GuideCase):
    def pages(self, split_id):
        self.generate(split_id)
        return page_texts(self.pdf(split_id))

    def test_steps_are_numbered_per_section_and_follow_the_plan(self):
        split_id = self.make_split(*lattice_split())
        pages = step_pages(self.pages(split_id))
        plan = splits.assembly_plan(split_id)
        expected = [step["id"] for section in plan["sections"] for step in section["steps"]]
        self.assertGreater(len(plan["sections"]), 2)
        self.assertEqual([i for text in pages for i in step_ids(text)], expected)
        self.assertEqual(len(set(expected)), len(expected))

    def test_two_steps_flow_to_a_page_and_a_step_is_never_split(self):
        split_id = self.make_split(*stack_split(7))   # one Section of six steps
        pages = step_pages(self.pages(split_id))
        per_page = [step_ids(text) for text in pages]
        self.assertEqual(per_page, [["A1", "A2"], ["A3", "A4"], ["A5", "A6"]])
        for text in pages:     # every step's caption sits on the page with its number
            self.assertEqual(body_text(text).count("slide"), len(step_ids(text)))

    def test_section_headers_are_inline_and_side_sections_end_with_set_aside(self):
        split_id = self.make_split(*lattice_split())
        pages = step_pages(self.pages(split_id))
        plan = splits.assembly_plan(split_id)
        body = " ".join(body_text(t) for t in pages)
        for section in plan["sections"]:
            header = body.index(f"Section {section['letter']}")
            if section["steps"]:
                self.assertLess(header, body.index(section["steps"][0]["id"] + " "))
            aside = f"Set aside as {section['letter']} (write {section['letter']} on a piece of tape)"
            self.assertEqual(body.count(aside), 0 if section["main"] else 1)
            if not section["main"] and section["steps"]:
                self.assertGreater(body.index(aside), body.index(section["steps"][-1]["id"] + " "))

    def test_a_section_header_is_never_alone_at_the_bottom_of_a_page(self):
        for size in (3, 4):
            split_id = self.make_split(*lattice_split(size))
            for text in step_pages(self.pages(split_id)):
                body = body_text(text)
                headers = list(re.finditer(r"Section [A-Z]( \(main\))? Made of", body))
                if headers:
                    self.assertTrue(step_ids(body[headers[-1].start():]), f"header without a step on its page: {body[:200]}")

    def test_the_same_split_draws_the_same_pages(self):
        first_id = self.make_split(*lattice_split())
        self.generate(first_id)
        second_id = self.make_split(*lattice_split())
        self.generate(second_id)
        strip = lambda split, pdf: pdf.replace(f"Split {split}".encode(), b"Split N")
        self.assertEqual(strip(second_id, self.pdf(second_id)), strip(first_id, self.pdf(first_id)))

    def test_a_split_without_part_previews_cannot_get_a_guide(self):
        split_id = self.make_split(*grid_split(), meshes=[])
        status = splits.guide_status(split_id)
        self.assertIn("preview", status["blocked"])
        with self.assertRaises(jobs.JobError):
            splits.start_guide(split_id, {})


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class CameraTests(unittest.TestCase):
    def test_the_first_fit_camera_sees_the_receiving_face_from_above(self):
        for u in ([1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, -1]):
            u = np.array(u, float)
            camera = assembly_render.choose_camera(None, {"direction": tuple(u)}, None)
            self.assertAlmostEqual(float(np.linalg.norm(camera)), 1.0)
            self.assertGreater(float(camera @ -u), 0.05)
            self.assertGreater(camera[2], 0, "from above")
        up = assembly_render.choose_camera(None, {"direction": (0, 0, 1)}, None)   # the face looks down: the camera is below
        self.assertLess(up[2], 0)

    def test_the_previous_camera_is_reused_while_it_still_sees_the_face(self):
        previous = assembly_render.choose_camera(None, {"direction": (1, 0, 0)}, None)
        same = assembly_render.choose_camera(None, {"direction": (1, 0, 0)}, previous)
        np.testing.assert_allclose(same, previous)
        kept = assembly_render.choose_camera(None, {"direction": (0.95, 0.3, 0)}, previous)
        np.testing.assert_allclose(kept, previous)       # the dot with the face normal is still above 0.3
        moved = assembly_render.choose_camera(None, {"direction": (-1, 0, 0)}, previous)
        self.assertGreater(float(moved @ np.array([1.0, 0, 0])), 0.05)
        self.assertFalse(np.allclose(moved, previous))

    def test_the_arrow_is_long_and_clear_of_the_moving_part(self):
        result, cuts = grid_split()
        meshes = [box_mesh(p["bbox"]) for p in sorted(result["parts"], key=lambda p: p["number"])]
        scene = assembly_render.Scene.from_split(result, meshes)
        step = {"id": "A1", "kind": "part", "parts": (2,), "receiver": (1,), "direction": (1.0, 0.0, 0.0), "rods": 2,
                "connectors": {}, "glue_only": False, "section": None}
        art = assembly_render.prepare_step(scene, step, assembly_render.choose_camera(scene, step, None))
        frame = assembly_render.layout_frames(art, 190 * assembly_render.MM)[1]
        arrow, mover = assembly_render.frame_two_marks(art, frame)
        a, b = np.array(arrow[0]), np.array(arrow[1])
        self.assertGreaterEqual(np.linalg.norm(b - a), 46.0)
        (lo, hi) = mover
        clear = lambda p: not (lo[0] <= p[0] <= hi[0] and lo[1] <= p[1] <= hi[1])
        self.assertTrue(all(clear(a + (b - a) * t) for t in np.linspace(0, 1, 25)))
