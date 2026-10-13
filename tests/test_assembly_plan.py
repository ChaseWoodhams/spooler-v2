"""The assembly plan for a finished split: Sections, steps, order, and "cannot be assembled as cut" reports.

Seam 1 is the splits service (splits.assembly_plan on a split folder). Seam 2 is the step validator, which later
tickets (edits, fallback planner) call on its own. Splits here are written straight into the split folder, as the
split engine leaves them, so no engine run is needed."""
import io
import json
import tempfile
import unittest
from pathlib import Path

import assembly_plan
import database
import splits

GEOMETRY = not splits.missing_modules()
if GEOMETRY:
    import manifold3d as m3d
    import numpy as np

    import split_engine


def part(number, box, volume=25.0):
    return {"number": number, "bbox": list(box), "volume_cm3": volume}


def cut(cut_id, axis, offset, parent=None):
    normal = [0.0, 0.0, 0.0]
    normal["xyz".index(axis)] = 1.0
    return {"id": cut_id, "parent": parent, "normal": normal, "offset": offset, "scope": None}


def joint(a, b, cut_id, rods=2, connector="2 mm rod"):
    return {"parts": [a, b], "area_mm2": 1000, "rods": rods, "connectors": {connector: rods} if rods else {}, "cuts": [cut_id], "keyed": True}


def pins(a, b, axis, at, count=2, connector="2 mm rod"):
    """`count` rods across the joint of parts a and b, along `axis` through the point `at`."""
    direction = [0.0, 0.0, 0.0]
    direction["xyz".index(axis)] = 1.0
    return [{"p": [at[0], at[1] + 5 * i, at[2]], "axis": direction, "connector": connector, "diameter": 2.0,
             "hole_diameter": 2.34, "depth": 6.5, "parts": [a, b]} for i in range(count)]


def grid_split():
    """Parts 1 2 on top and 3 4 below, cut by one plane across x and one across y: the 2 x 2 grid."""
    parts = [part(1, (0, 50, 0, 50, 100, 10)), part(2, (50, 50, 0, 100, 100, 10)),
             part(3, (0, 0, 0, 50, 50, 10)), part(4, (50, 0, 0, 100, 50, 10))]
    joints = [joint(1, 2, 1), joint(3, 4, 1), joint(1, 3, 2), joint(2, 4, 2)]
    all_pins = (pins(1, 2, "x", (50, 70, 5)) + pins(3, 4, "x", (50, 20, 5))
                + pins(1, 3, "y", (20, 50, 5)) + pins(2, 4, "y", (70, 50, 5)))
    return {"parts": parts, "joints": joints, "pins": all_pins}, [cut(1, "x", 50.0), cut(2, "y", 50.0)]


def stack_split(count=3):
    """Parts in a row along x, each cut from the next by its own plane."""
    parts = [part(i + 1, (i * 40, 0, 0, i * 40 + 40, 40, 40)) for i in range(count)]
    joints = [joint(i + 1, i + 2, i + 1) for i in range(count - 1)]
    all_pins = [p for i in range(count - 1) for p in pins(i + 1, i + 2, "x", ((i + 1) * 40, 10, 20))]
    return {"parts": parts, "joints": joints, "pins": all_pins}, [cut(i + 1, "x", (i + 1) * 40.0) for i in range(count - 1)]


class PlanServiceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        database.configure(self.dir / "spooler.db")
        splits.configure(self.dir)
        splits.init()

    def tearDown(self):
        database.configure(database.DB_FILE)
        splits.configure()
        self._tmp.cleanup()

    def make_split(self, result, cuts, meshes=None):
        """A split record whose folder holds the given result and cuts, as after the Split stage."""
        data = b"solid x\nendsolid x\n"
        record = splits.create_split(io.BytesIO(data), len(data), "model.stl")
        work = splits.work_dir(record["id"])
        (work / "result.json").write_text(json.dumps(result), encoding="utf-8")
        (work / "cuts.json").write_text(json.dumps({"mode": "smart", "cuts": cuts}), encoding="utf-8")
        if meshes:
            split_engine.write_mesh_bin(work / "parts_preview.bin", meshes)
        splits._set_state(record["id"], splits.SPLIT)
        return record["id"]


class GridTests(PlanServiceCase):
    def test_a_2_by_2_grid_gets_sections_never_one_part_at_a_time(self):
        result, cuts = grid_split()
        plan = splits.assembly_plan(self.make_split(result, cuts))
        self.assertTrue(plan["ok"])
        self.assertEqual([(s["letter"], s["parts"]) for s in plan["sections"]], [("B", [2, 4]), ("A", [1, 2, 3, 4])])
        steps = {s["id"]: s for s in (st for sec in plan["sections"] for st in sec["steps"])}
        self.assertEqual(list(steps), ["B1", "A1", "A2"])
        self.assertEqual((steps["B1"]["parts"], steps["B1"]["receiver"], steps["B1"]["direction"]), ([4], [2], [0.0, 1.0, 0.0]))
        self.assertEqual((steps["A1"]["parts"], steps["A1"]["receiver"], steps["A1"]["direction"]), ([3], [1], [0.0, 1.0, 0.0]))
        # the finished Section goes on as one step of its own, along the other axis
        self.assertEqual((steps["A2"]["parts"], steps["A2"]["receiver"], steps["A2"]["direction"]), ([2, 4], [1, 3], [-1.0, 0.0, 0.0]))
        self.assertEqual((steps["A2"]["section"], steps["A2"]["rods"]), ("B", 4))
        self.assertEqual(plan["summary"], {"parts": 4, "sections": 2, "steps": 3, "rods": 8})

    def test_every_step_of_the_plan_passes_the_validator(self):
        result, cuts = grid_split()
        model = assembly_plan.SplitModel(result, cuts)
        plan = assembly_plan.build_plan(model).plan
        for section in plan.sections:
            for step in section.steps:
                self.assertIsNone(assembly_plan.validate_step(model, step.parts, step.receiver, step.direction))


class RecordedRodsTests(PlanServiceCase):
    def test_rods_between_parts_that_the_joint_list_misses_still_count_as_a_joint(self):
        result, cuts = grid_split()
        expected = splits.assembly_plan(self.make_split(*grid_split()))
        result["joints"] = [j for j in result["joints"] if j["parts"] != [1, 3]]   # the engine paired that cut face differently
        plan = splits.assembly_plan(self.make_split(result, cuts))
        self.assertEqual(plan["code"], expected["code"])

    def test_rods_that_lie_on_no_cut_plane_are_a_data_problem(self):
        result, cuts = grid_split()
        result["joints"] = [j for j in result["joints"] if j["parts"] != [1, 3]]
        for pin in result["pins"]:
            if pin["parts"] == [1, 3]:
                pin["p"][1] = 80.0
        plan = splits.assembly_plan(self.make_split(result, cuts))
        self.assertEqual((plan["ok"], plan["failure"]["kind"], plan["failure"]["parts"]), (False, "anomaly", [1, 3]))


class SectionRuleTests(PlanServiceCase):
    def test_a_row_of_parts_is_one_section_built_out_from_its_heaviest_end(self):
        result, cuts = stack_split(4)
        plan = splits.assembly_plan(self.make_split(result, cuts))
        self.assertEqual([s["letter"] for s in plan["sections"]], ["A"])
        self.assertEqual([(s["id"], s["parts"], s["receiver"], s["direction"]) for s in plan["sections"][0]["steps"]],
                         [("A1", [4], [3], [-1.0, 0.0, 0.0]), ("A2", [2], [3, 4], [1.0, 0.0, 0.0]),
                          ("A3", [1], [2, 3, 4], [1.0, 0.0, 0.0])])

    def test_up_to_three_parts_that_join_the_same_unit_from_the_same_side_share_a_step(self):
        # a base slab with four small parts cut off its top by four planes along x: the base is heaviest, the
        # others lie on the same side and do not touch each other
        parts = [part(1, (0, 0, 0, 40, 100, 10), 100.0)] + [part(n, (40, (n - 2) * 25, 0, 90, (n - 2) * 25 + 20, 10), 5.0) for n in (2, 3, 4, 5)]
        joints = [joint(1, n, 1) for n in (2, 3, 4, 5)]
        all_pins = [p for n in (2, 3, 4, 5) for p in pins(1, n, "x", (40, (n - 2) * 25 + 5, 5))]
        plan = splits.assembly_plan(self.make_split({"parts": parts, "joints": joints, "pins": all_pins}, [cut(1, "x", 40.0)]))
        steps = [s["parts"] for sec in plan["sections"] for s in sec["steps"]]
        self.assertEqual(steps, [[3, 4, 5], [2]])

    def test_a_joining_a_finished_section_is_always_its_own_step(self):
        result, cuts = grid_split()
        plan = splits.assembly_plan(self.make_split(result, cuts))
        joins = [s for sec in plan["sections"] for s in sec["steps"] if s["kind"] == "section"]
        self.assertEqual([(s["id"], s["section"]) for s in joins], [("A2", "B")])

    def test_a_part_that_touches_nothing_is_its_own_section_and_does_not_block(self):
        result, cuts = stack_split(2)
        result["parts"].append(part(3, (300, 0, 0, 340, 40, 40)))
        plan = splits.assembly_plan(self.make_split(result, cuts))
        self.assertTrue(plan["ok"])
        self.assertEqual([(s["letter"], s["parts"], s["disconnected"]) for s in plan["sections"]],
                         [("B", [3], True), ("A", [1, 2], False)])


class DeterminismTests(PlanServiceCase):
    def test_the_same_split_always_gives_the_same_plan_and_code(self):
        result, cuts = grid_split()
        first = splits.assembly_plan(self.make_split(result, cuts))
        again = splits.assembly_plan(self.make_split(json.loads(json.dumps(result)), cuts))
        self.assertEqual(first, again)
        self.assertRegex(first["code"], r"^[0-9a-f]{6}$")
        shuffled = dict(result, joints=list(reversed(result["joints"])), pins=list(reversed(result["pins"])))
        self.assertEqual(splits.assembly_plan(self.make_split(shuffled, list(cuts)))["code"], first["code"])

    def test_a_different_plan_has_a_different_code(self):
        grid = splits.assembly_plan(self.make_split(*grid_split()))
        stack = splits.assembly_plan(self.make_split(*stack_split(4)))
        self.assertNotEqual(grid["code"], stack["code"])


class ImpossibleSplitTests(PlanServiceCase):
    def test_a_pin_that_is_not_along_its_cut_is_reported_as_a_data_problem(self):
        result, cuts = grid_split()
        result["pins"][0]["axis"] = [0.0, 1.0, 0.0]   # a rod of the x cut lying along y
        plan = splits.assembly_plan(self.make_split(result, cuts))
        self.assertFalse(plan["ok"])
        self.assertTrue(plan["cannot_be_assembled"])
        failure = plan["failure"]
        self.assertEqual((failure["kind"], failure["parts"]), ("anomaly", [1, 2]))
        self.assertIn("part 1 and part 2", failure["message"].lower())
        self.assertTrue(failure["fixes"])

    def test_a_split_with_no_cut_history_is_reported_until_the_fallback_exists(self):
        result, _ = grid_split()
        plan = splits.assembly_plan(self.make_split(result, []))
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["failure"]["kind"], "anomaly")

    @unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
    def test_two_bodies_that_trap_each_other_name_the_joint_and_rank_fixes(self):
        """A ring (part 2) sits between the two thick ends of a bar (part 3); both lie on the same side of the cut."""
        M = m3d.Manifold
        base = M.cube([50, 80, 80])
        ring = M.cube([10, 40, 40]).translate([60, 20, 20]) - M.cube([10, 20, 20]).translate([60, 30, 30])
        bar = M.batch_boolean([M.cube([50, 4, 4]).translate([50, 38, 38]), M.cube([6, 30, 30]).translate([50, 25, 25]),
                               M.cube([10, 30, 30]).translate([80, 25, 25])], m3d.OpType.Add)
        bodies = [base, ring, bar]

        def arrays(man):
            mesh = man.to_mesh()
            return np.asarray(mesh.vert_properties)[:, :3], np.asarray(mesh.tri_verts)

        parts = [part(n, tuple(b.bounding_box()), v) for n, b, v in zip((1, 2, 3), bodies, (320.0, 3.0, 1.0))]
        result = {"parts": parts, "joints": [joint(1, 3, 1, rods=0), joint(1, 2, 1, rods=0)], "pins": []}
        plan = splits.assembly_plan(self.make_split(result, [cut(1, "x", 50.0)], meshes=[arrays(b) for b in bodies]))
        self.assertFalse(plan["ok"])
        self.assertTrue(plan["cannot_be_assembled"])
        failure = plan["failure"]
        self.assertEqual((failure["kind"], sorted(failure["parts"])), ("interlock", [2, 3]))
        self.assertIn("part 2 and part 3", failure["message"].lower())
        self.assertGreaterEqual(len(failure["fixes"]), 2)
        self.assertEqual(len(failure["point"]), 3)


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.result, self.cuts = grid_split()
        self.model = assembly_plan.SplitModel(self.result, self.cuts)

    def check(self, unit, placed, direction):
        return assembly_plan.validate_step(self.model, unit, placed, direction)

    def test_a_part_with_rods_on_two_axes_is_refused_with_the_joint_named(self):
        # one-part-at-a-time: part 4 meets part 3 along x and part 2 along y
        failure = self.check([4], [1, 2, 3], [-1.0, 0.0, 0.0])
        self.assertEqual(failure.kind, "rod_direction")
        self.assertEqual(failure.parts, (2, 4))
        self.assertIn("part 2 and part 4", failure.message.lower())
        self.assertAlmostEqual(failure.numbers["angle_deg"], 90.0)
        self.assertGreaterEqual(len(failure.fixes), 2)

    def test_the_rod_direction_allows_one_degree(self):
        within = [-1.0, 0.017, 0.0]    # about 0.97 degrees off
        beyond = [-1.0, 0.018, 0.0]    # about 1.03 degrees off
        self.assertIsNone(self.check([2, 4], [1, 3], within))
        self.assertEqual(self.check([2, 4], [1, 3], beyond).kind, "rod_direction")

    def test_sliding_away_from_the_cut_instead_of_towards_it_is_refused(self):
        failure = self.check([2, 4], [1, 3], [1.0, 0.0, 0.0])
        self.assertEqual((failure.kind, failure.parts), ("rod_side", (3, 4)))

    def test_a_unit_that_touches_nothing_placed_is_disconnected(self):
        failure = self.check([4], [1], [0.0, 1.0, 0.0])
        self.assertEqual(failure.kind, "disconnected")
        self.assertEqual(failure.parts, ())

    def test_a_placed_part_in_the_way_blocks_the_unit(self):
        row, cuts = stack_split(3)
        self.assertIsNone(assembly_plan.validate_step(assembly_plan.SplitModel(row, cuts), [1], [2, 3], [1.0, 0.0, 0.0]))
        row["parts"][2]["bbox"] = [-60, 0, 0, -10, 40, 40]   # part 3 sits where part 1 has to back away to
        blocked = assembly_plan.validate_step(assembly_plan.SplitModel(row, cuts), [1], [2, 3], [1.0, 0.0, 0.0])
        self.assertEqual((blocked.kind, blocked.parts), ("blocked", (1, 3)))

    def test_failures_are_typed_reports_that_serialise(self):
        failure = self.check([4], [1, 2, 3], [-1.0, 0.0, 0.0])
        report = failure.to_dict()
        self.assertEqual(set(report), {"kind", "parts", "point", "message", "numbers", "fixes"})
        json.dumps(report)

    def test_the_plan_fingerprint_changes_with_the_directions(self):
        a = assembly_plan.build_plan(self.model).plan
        b = assembly_plan.build_plan(assembly_plan.SplitModel(*stack_split(4))).plan
        self.assertNotEqual(a.fingerprint, b.fingerprint)
        self.assertEqual(a.code, a.fingerprint[:6])
        self.assertEqual(assembly_plan.build_plan(assembly_plan.SplitModel(*grid_split())).plan.fingerprint, a.fingerprint)

    def test_the_split_fingerprint_follows_cuts_and_part_sizes(self):
        same = assembly_plan.SplitModel(*grid_split()).fingerprint
        self.assertEqual(same, self.model.fingerprint)
        moved = grid_split()
        moved[1][0]["offset"] = 51.0
        self.assertNotEqual(assembly_plan.SplitModel(*moved).fingerprint, same)
        bigger = grid_split()
        bigger[0]["parts"][0]["volume_cm3"] = 26.0
        self.assertNotEqual(assembly_plan.SplitModel(*bigger).fingerprint, same)


if __name__ == "__main__":
    unittest.main()
