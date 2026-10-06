import unittest

from shapely import affinity
from shapely.geometry import Polygon, box

from nesting import CLEARANCE_MM, EDGE_MARGIN_MM as EDGE, SUPPORT_CLEARANCE_MM, nest

BED = (0.0, 0.0, 256.0, 256.0)
PURGE = (246.0, 0.0, 256.0, 20.0)


def rect(w, d):
    return [(0.0, 0.0), (w, 0.0), (w, d), (0.0, d)]


def l_shape(size, arm):
    return [(0.0, 0.0), (size, 0.0), (size, arm), (arm, arm), (arm, size), (0.0, size)]


def placed(item, outline):
    """The part's outline where the nester put it: turned `angle` degrees about Z, bounding box centred on `center`."""
    poly = affinity.rotate(Polygon(outline), item["angle"], origin="centroid")
    x0, y0, x1, y1 = poly.bounds
    return affinity.translate(poly, item["center"][0] - (x0 + x1) / 2, item["center"][1] - (y0 + y1) / 2)


class NestingTests(unittest.TestCase):
    def check_plates(self, plates, parts, fixed=()):
        """parts: {key: (outline, clearance)}. Every pair keeps the larger of its two clearances, nothing passes the
        bed edge by EDGE or enters the purge area by its own clearance."""
        for plate in plates:
            shapes = [(i["key"], placed(i, parts[i["key"]][0]), parts[i["key"]][1]) for i in plate["items"] if not i["oversize"]]
            shapes += [(f["key"], Polygon(f["polygon"]), f["clearance"]) for f in fixed if f["key"] in plate["fixed"]]
            for n, (key, poly, b) in enumerate(shapes):
                x0, y0, x1, y1 = poly.bounds
                self.assertGreaterEqual(min(x0 - BED[0], y0 - BED[1], BED[2] - x1, BED[3] - y1), EDGE - 1e-6, f"{key} is too near the bed edge")
                self.assertGreaterEqual(poly.distance(box(*PURGE)), b - 1e-6, f"{key} is too near the purge area")
                for other, opoly, ob in shapes[n + 1:]:
                    self.assertGreaterEqual(poly.distance(opoly), max(b, ob) - 1e-6, f"{key} and {other} are closer than their clearance")

    def run_nest(self, parts, group="plain", **kwargs):
        items = [{"key": k, "outline": o, "clearance": b, "group": group} for k, (o, b) in parts.items()]
        return nest(items, BED, PURGE, **kwargs)

    def test_small_parts_share_a_plate_with_their_clearance(self):
        parts = {n: (rect(60, 40), CLEARANCE_MM) for n in range(1, 10)}
        plates = self.run_nest(parts)
        self.assertEqual(len(plates), 1)
        self.assertEqual(sorted(i["key"] for i in plates[0]["items"]), list(parts))
        self.check_plates(plates, parts)

    def test_a_pair_keeps_the_larger_of_its_two_clearances_not_the_sum(self):
        parts = {1: (rect(40, 40), SUPPORT_CLEARANCE_MM), 2: (rect(40, 40), CLEARANCE_MM)}
        plates = self.run_nest(parts)
        self.assertEqual(len(plates), 1)
        self.check_plates(plates, parts)
        a, b = (placed(i, parts[i["key"]][0]) for i in plates[0]["items"])
        self.assertLess(a.distance(b), SUPPORT_CLEARANCE_MM + 1.0)   # the sum would be 28 mm

    def test_parts_turn_to_fit_where_rectangles_would_not(self):
        bar = [(0.0, 0.0), (15.0, 0.0), (175.0, 157.0), (160.0, 157.0 + 15.0)]   # 15 mm wide, slanted 44.4 degrees
        parts = {1: (bar, CLEARANCE_MM), 2: (bar, CLEARANCE_MM), 3: (bar, CLEARANCE_MM)}
        plates = self.run_nest(parts)
        self.check_plates(plates, parts)
        self.assertEqual(len(plates), 1)   # packing.pack would need a plate each: the bounding boxes are 175 mm square

    def test_concave_parts_tuck_into_each_other(self):
        parts = {n: (l_shape(150, 40), CLEARANCE_MM) for n in range(1, 4)}   # bounding boxes 150 mm square: three do not fit by box
        plates = self.run_nest(parts)
        self.check_plates(plates, parts)
        self.assertEqual(len(plates), 1)

    def test_fixed_parts_stay_put_and_others_pack_around_them(self):
        pinned = {"key": "pin", "group": "plain", "slot": 0, "clearance": CLEARANCE_MM, "polygon": [(100, 100), (200, 100), (200, 200), (100, 200)]}
        parts = {n: (rect(60, 40), CLEARANCE_MM) for n in range(1, 13)}
        plates = self.run_nest(parts, fixed=[pinned])
        self.check_plates(plates, parts, fixed=[pinned])
        self.assertEqual(plates[0]["fixed"], ["pin"])
        self.assertGreater(len(plates[0]["items"]), 0)

    def test_a_fixed_part_on_a_later_plate_makes_that_plate(self):
        pinned = {"key": "pin", "group": "plain", "slot": 1, "clearance": CLEARANCE_MM, "polygon": [(10, 10), (50, 10), (50, 50), (10, 50)]}
        plates = nest([{"key": 1, "outline": rect(30, 30), "group": "plain"}], BED, PURGE, fixed=[pinned])
        self.assertEqual([p["fixed"] for p in plates], [[], ["pin"]])
        self.assertEqual([len(p["items"]) for p in plates], [1, 0])

    def test_big_parts_spread_over_plates_and_groups_never_share(self):
        items = [{"key": n, "outline": rect(240, 110), "group": "plain"} for n in range(1, 4)]
        items += [{"key": 4, "outline": rect(50, 50), "group": "supports"}, {"key": 5, "outline": rect(50, 50), "group": "plain"}]
        plates = nest(items, BED, PURGE)
        self.assertEqual(sorted(i["key"] for p in plates for i in p["items"]), [1, 2, 3, 4, 5])
        self.assertEqual([p["group"] for p in plates], ["plain", "plain", "supports"])
        self.assertEqual([i["key"] for i in plates[2]["items"]], [4])

    def test_a_part_bigger_than_the_bed_gets_its_own_plate(self):
        items = [{"key": 1, "outline": rect(400, 50), "group": "plain"}, {"key": 2, "outline": rect(50, 50), "group": "plain"}]
        plates = nest(items, BED, PURGE)
        self.assertEqual(len(plates), 2)
        big = next(p for p in plates if p["items"][0]["key"] == 1)
        self.assertEqual((len(big["items"]), big["items"][0]["oversize"], big["items"][0]["center"]), (1, True, (128.0, 128.0)))


if __name__ == "__main__":
    unittest.main()
