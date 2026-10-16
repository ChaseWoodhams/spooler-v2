"""Planner fit checks stay cheap and safe (auto-plating ticket 22): adding to a plate reuses its layout, a set that
did not fit rules out its supersets, and a supported plate keeps the larger gap between its Objects."""
import math
import unittest

import nesting
import planner
import splits

GEOMETRY = not splits.missing_modules()
BED = (0.0, 0.0, 250.0, 250.0)


def rect(w, d):
    return ((-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2))


def cand(w=60, d=60, *, clearance=6.0, needs_support=False):
    return planner.Candidate((0, 0, 0), rect(w, d), clearance, 20.0, 10.0, w * d / 100, 1.0 if needs_support else 0.0,
                             0.0, needs_support, False)


def problem(items):
    return planner.Problem(tuple(items), {"g": planner.Group(0.7, 0.2, lambda *a: 600)}, {"A": 0.7, "B": 0.7}, BED)


def item(key, c):
    return planner.Item(key, key, "g", (c,), (0,))


def shapes_of(run, layout):
    out = []
    for placed in layout:
        c = run.items[placed["key"]].candidates[0]
        a = math.radians(placed["angle"])
        pts = [(x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)) for x, y in c.outline]
        cx, cy = (max(p[0] for p in pts) + min(p[0] for p in pts)) / 2, (max(p[1] for p in pts) + min(p[1] for p in pts)) / 2
        out.append({"key": placed["key"], "plate": 1, "clearance": c.clearance,
                    "polygon": [(x - cx + placed["center"][0], y - cy + placed["center"][1]) for x, y in pts]})
    return out


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class FitTests(unittest.TestCase):
    def test_adding_one_object_reuses_the_layout_and_stays_valid(self):
        run = planner._Run(problem([item(k, cand()) for k in range(1, 5)]))
        run.fit(None, ((1, 0), (2, 0), (3, 0)))
        before = run.nests
        layout = run.fit(None, ((1, 0), (2, 0), (3, 0), (4, 0)))
        self.assertIsNotNone(layout)
        self.assertEqual(run.nests, before)           # no full nest was made for the fourth
        self.assertEqual(run.inserts, 1)
        self.assertEqual(sorted(i["key"] for i in layout), [1, 2, 3, 4])
        self.assertEqual(nesting.placement_problems(shapes_of(run, layout), BED), [])

    def test_a_set_that_did_not_fit_rules_out_its_supersets_without_nesting(self):
        run = planner._Run(problem([item(k, cand(120, 120)) for k in range(1, 7)]))
        self.assertIsNone(run.fit(None, ((1, 0), (2, 0), (3, 0), (4, 0), (5, 0))))
        used = run.nests
        self.assertIsNone(run.fit(None, ((1, 0), (2, 0), (3, 0), (4, 0), (5, 0), (6, 0))))
        self.assertEqual(run.nests, used)

    def test_a_plate_with_supports_keeps_the_support_gap_between_all_its_objects(self):
        plain, supported = cand(60, 60, clearance=6.0), cand(60, 60, clearance=14.0, needs_support=True)
        run = planner._Run(problem([item(1, plain), item(2, supported)]))
        layout = run.fit(None, ((1, 0), (2, 0)))
        shapes = shapes_of(run, layout)
        self.assertEqual({s["clearance"] for s in shapes}, {6.0, 14.0})
        from shapely.geometry import Polygon
        gap = Polygon(shapes[0]["polygon"]).distance(Polygon(shapes[1]["polygon"]))
        self.assertGreaterEqual(gap, 14.0 - 0.01)


if __name__ == "__main__":
    unittest.main()
