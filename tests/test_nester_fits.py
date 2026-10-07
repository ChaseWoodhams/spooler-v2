"""The Nester keeps every ready Object on the bed: free space first, then added Plates; one shared check says whether a layout is valid."""
import io
import unittest
from pathlib import Path

import nesting
import projects
from tests import test_arrange
from tests.test_slicer import cube_stl

_Base = test_arrange.ArrangeApiTests
FIXTURES = Path(__file__).parent / "fixtures" / "daz_shifter"
BED = (0.0, 0.0, 256.0, 256.0)
PURGE = (246.0, 0.0, 256.0, 20.0)


def square(x, y, side=20.0):
    return [(x, y), (x + side, y), (x + side, y + side), (x, y + side)]


def shape(key, polygon, clearance=6.0, plate=1):
    return {"key": key, "plate": plate, "polygon": polygon, "clearance": clearance}


class PlacementProblemsTests(unittest.TestCase):
    """nesting.placement_problems: the one definition of a valid layout (inside the bed, clear of the purge area,
    every pair as far apart as Arrange keeps them)."""

    def problems(self, *shapes):
        return [(p["key"], p["problem"], p["other"]) for p in nesting.placement_problems(list(shapes), BED, PURGE)]

    def test_objects_on_the_bed_and_apart_are_valid(self):
        self.assertEqual(self.problems(shape(1, square(10, 100)), shape(2, square(60, 100))), [])

    def test_an_object_past_any_bed_edge_is_off_the_bed(self):
        for corner in ((-5, 100), (100, -5), (240, 100), (100, 240)):
            self.assertEqual(self.problems(shape(1, square(*corner))), [(1, "off the bed", None)], corner)

    def test_an_object_touching_the_bed_edge_is_still_on_it(self):
        self.assertEqual(self.problems(shape(1, square(0, 100)), shape(2, square(236, 100))), [])

    def test_an_object_over_the_purge_area_is_flagged(self):
        self.assertEqual(self.problems(shape(1, square(236, 10))), [(1, "over the purge area", None)])

    def test_two_objects_closer_than_the_larger_clearance_overlap(self):
        close = self.problems(shape(1, square(10, 100), 6.0), shape(2, square(35, 100), 22.0))   # 5 mm apart
        self.assertEqual(close, [(1, "overlaps", 2), (2, "overlaps", 1)])
        self.assertEqual(self.problems(shape(1, square(10, 100), 6.0), shape(2, square(52, 100), 22.0)), [])   # 22 mm apart

    def test_objects_on_different_plates_never_overlap(self):
        self.assertEqual(self.problems(shape(1, square(10, 100), plate=1), shape(2, square(10, 100), plate=2)), [])


@unittest.skipUnless(test_arrange.GEOMETRY, "geometry libraries not installed")
class NesterFitsTests(unittest.TestCase):
    setUp, tearDown, call, project_with, save, lay, arrange = (_Base.setUp, _Base.tearDown, _Base.call, _Base.project_with,
                                                               _Base.save, _Base.lay, _Base.arrange)
    def problems(self, project):
        return projects.placement_problems(project["id"], projects.get_project(project["id"]))

    def test_a_plate_that_cannot_hold_everything_gets_a_plate_added_and_nothing_is_off_the_bed(self):
        project = self.project_with(cube_stl(100))
        project = self.lay(project, [(1, 1, (50, 50), False)] * 7)
        status, result = self.arrange(project)
        self.assertEqual(status, 200, result)
        self.assertEqual(len(result["data"]["plates"]), 2)   # four 100 mm cubes fit a plate, seven do not
        self.assertEqual(self.problems(result), [])
        self.assertEqual(sorted(o["plate"] for o in result["data"]["objects"]), [1, 1, 1, 1, 2, 2, 2])

    def test_free_space_around_a_pinned_object_is_used_before_a_plate_is_added(self):
        project = self.project_with(cube_stl(20), cube_stl(100))
        project = self.lay(project, [(2, 1, (60, 60), True), (1, 1, (50, 50), False), (1, 2, (50, 50), False), (1, 3, (50, 50), False)])
        status, result = self.arrange(project)
        self.assertEqual(status, 200, result)
        self.assertEqual({o["plate"] for o in result["data"]["objects"]}, {1})
        self.assertEqual(len(result["data"]["plates"]), 1)
        pinned = next(o for o in result["data"]["objects"] if o["id"] == 1)
        self.assertEqual((pinned["position"], pinned["rotation"]), ([60.0, 60.0], [0, 0, 0]))
        self.assertEqual(self.problems(result), [])

    def test_filament_choices_still_never_share_a_plate_and_both_plates_are_valid(self):
        project = self.project_with(cube_stl(20), cube_stl(20))
        project = self.save(project, lambda d: d.update(filaments={"2": test_arrange.GREY}))
        project = self.lay(project, [(1, 1, (50, 50), False), (2, 1, (50, 50), False), (1, 1, (50, 50), False), (2, 1, (50, 50), False)])
        status, result = self.arrange(project)
        self.assertEqual(status, 200, result)
        plate_of = {o["id"]: o["plate"] for o in result["data"]["objects"]}
        self.assertEqual(plate_of[1], plate_of[3])
        self.assertEqual(plate_of[2], plate_of[4])
        self.assertNotEqual(plate_of[1], plate_of[2])
        self.assertEqual(self.problems(result), [])

    def test_a_part_bigger_than_the_bed_is_reported_not_silently_left_off_it(self):
        project = self.project_with(cube_stl(20), cube_stl(300))
        project = self.lay(project, [(1, 1, (50, 50), False), (2, 1, (50, 50), False)])
        status, result = self.arrange(project)
        self.assertEqual(status, 200, result)
        self.assertIn("1 object", result["outcome"])
        self.assertIn("bigger than the bed", result["outcome"])
        self.assertEqual({p["key"] for p in self.problems(result)}, {2})   # the one that can't fit anywhere is the one named

    def test_the_whole_daz_shifter_set_is_on_the_bed_with_no_overlap_and_stays_so_when_arranged_again(self):
        project = projects.create_project("Daz shifter")
        for path in sorted(FIXTURES.glob("*.stl")):
            data = path.read_bytes()
            projects.import_model(project["id"], io.BytesIO(data), len(data), path.name)
        import asyncio

        async def work():
            while await projects.process_next():
                pass
        asyncio.run(work())
        for attempt in (1, 2, 3):
            status, result = self.call("POST", f"/api/projects/{project['id']}/arrange", {})
            self.assertEqual(status, 200, result)
            self.assertEqual(len(result["data"]["objects"]), 19)
            self.assertEqual(self.problems(result), [], f"arrange {attempt}")
            self.assertEqual(len(result["data"]["plates"]), 1, f"arrange {attempt} should keep the set on one plate: {result['outcome']}")


del _Base   # the borrowed methods are used above; keep the loader from running that class again here
