"""Every rod of a split is counted in exactly one step of the plan, however the joints form cycles.

Seams: the plan from build_plan / fallback_plan (each pin's pair is covered by exactly one step's parts x receiver, and
the step rod counts add up to the pins) and build_bom on that plan (the reconcile check does not raise)."""
import collections
import random
import unittest

import assembly_bom
import assembly_plan
from tests.test_assembly_guide import lattice_split
from tests.test_assembly_plan import part, pins



def covered_pairs(plan) -> collections.Counter:
    """How many steps cover each pair of parts (a part of the step x a part of its receiver)."""
    seen = collections.Counter()
    for section in plan.sections:
        for step in section.steps:
            for a in step.parts:
                for b in step.receiver:
                    seen[tuple(sorted((a, b)))] += 1
    return seen


def plan_dict(plan) -> dict:
    return {"sections": [{"steps": [{"id": s.id, "rods": s.rods, "connectors": s.connectors} for s in sec.steps]} for sec in plan.sections]}


class RodCountCase(unittest.TestCase):
    def check(self, result, cuts, fallback=None):
        model = assembly_plan.SplitModel(result, cuts)
        planned = assembly_plan.fallback_plan(model) if fallback else assembly_plan.build_plan(model, fallback=False)
        self.assertIsNotNone(planned.plan, planned.failure)
        plan = planned.plan
        by_pair = collections.Counter(tuple(sorted(p["parts"])) for p in result["pins"])
        covered = covered_pairs(plan)
        for pair in by_pair:
            self.assertEqual(covered[pair], 1, f"pins of {pair} are counted in {covered[pair]} steps")
        self.assertEqual(sum(s.rods for sec in plan.sections for s in sec.steps), len(result["pins"]))
        bom = assembly_bom.build_bom(result["pins"], plan_dict(plan))   # raises BomMismatch when a rod is lost
        self.assertEqual(bom.total_rods, len(result["pins"]))
        return plan


class LatticeCycleTests(RodCountCase):
    def test_every_pin_of_a_lattice_is_counted_once_whatever_the_cut_order_and_weights(self):
        for seed in range(30):
            rng = random.Random(seed)
            n = (3, 4)[seed % 2]
            result, cuts = lattice_split(n)
            for p in result["parts"]:
                p["volume_cm3"] = rng.choice((5, 25, 60, 100))
            rng.shuffle(cuts)
            with self.subTest(seed=seed, n=n):
                self.check(result, cuts)
                self.check(result, cuts, fallback=True)


class SeatLikeCycleTests(RodCountCase):
    def test_a_ring_of_parts_cut_one_plane_at_a_time_counts_the_closing_joint(self):
        # four parts round a square, the last joint closes the ring: 1-2 and 3-4 by the first cut, 1-3 and 2-4 by the second
        result = {"parts": [part(1, (0, 50, 0, 50, 100, 10)), part(2, (50, 50, 0, 100, 100, 10)),
                            part(3, (0, 0, 0, 50, 50, 10)), part(4, (50, 0, 0, 100, 50, 10))],
                  "joints": [], "pins": []}
        from tests.test_assembly_plan import cut, joint
        cuts = [cut(1, "x", 50.0), cut(2, "y", 50.0)]
        for a, b, axis, at, cid, count in ((1, 2, "x", (50, 70, 5), 1, 3), (3, 4, "x", (50, 20, 5), 1, 2),
                                           (1, 3, "y", (20, 50, 5), 2, 2), (2, 4, "y", (70, 50, 5), 2, 3)):
            result["joints"].append(joint(a, b, cid, rods=count))
            result["pins"] += pins(a, b, axis, at, count)
        self.assertEqual(len(result["pins"]), 10)
        self.check(result, cuts)

    def test_sections_joined_late_to_parts_in_several_other_sections_count_each_joint_once(self):
        # a 2 x 6 strip cut across x five times then along y: the two rows are Sections that meet every column
        n = 6
        size = 40.0
        from tests.test_assembly_plan import cut, joint
        parts = [part(r * n + c + 1, (c * size, r * size, 0, (c + 1) * size, (r + 1) * size, 10)) for r in range(2) for c in range(n)]
        cuts = [cut(c + 1, "x", (c + 1) * size) for c in range(n - 1)] + [cut(n, "y", size)]
        joints, all_pins = [], []
        for r in range(2):
            for c in range(n - 1):
                a, b = r * n + c + 1, r * n + c + 2
                joints.append(joint(a, b, c + 1))
                all_pins += pins(a, b, "x", ((c + 1) * size, r * size + 10, 5))
        for c in range(n):
            joints.append(joint(c + 1, n + c + 1, n))
            all_pins += pins(c + 1, n + c + 1, "y", (c * size + 10, size, 5))
        self.check({"parts": parts, "joints": joints, "pins": all_pins}, cuts)
        self.check({"parts": parts, "joints": joints, "pins": all_pins}, cuts, fallback=True)


if __name__ == "__main__":
    unittest.main()
