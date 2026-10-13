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
from tests.test_assembly_plan import PlanServiceCase, GEOMETRY, grid_split, stack_split


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


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class GuideCase(PlanServiceCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(assembly_pdf, "COMPRESS", False)
        patcher.start()
        self.addCleanup(patcher.stop)

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
