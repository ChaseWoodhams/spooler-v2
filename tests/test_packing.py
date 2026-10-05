import unittest

from packing import GAP_MM, MARGIN_MM, pack

BED = (0.0, 0.0, 256.0, 256.0)
PURGE = (246.0, 0.0, 256.0, 20.0)


def footprints(plate, sizes):
    """(x0, y0, x1, y1) of every part on a plate, from the packer's centres and turns."""
    out = []
    for item in plate["items"]:
        w, d = sizes[item["key"]]
        if item["turned"]:
            w, d = d, w
        x, y = item["center"]
        out.append((x - w / 2, y - d / 2, x + w / 2, y + d / 2))
    return out


def apart(a, b, gap):
    return a[2] + gap <= b[0] + 1e-6 or b[2] + gap <= a[0] + 1e-6 or a[3] + gap <= b[1] + 1e-6 or b[3] + gap <= a[1] + 1e-6


class PackingTests(unittest.TestCase):
    def check_plates(self, plates, sizes):
        for plate in plates:
            rects = footprints(plate, sizes)
            for i, rect in enumerate(rects):
                if plate["items"][i]["oversize"]:
                    continue
                self.assertGreaterEqual(rect[0], BED[0] + MARGIN_MM - 1e-6)
                self.assertGreaterEqual(rect[1], BED[1] + MARGIN_MM - 1e-6)
                self.assertLessEqual(rect[2], BED[2] - MARGIN_MM + 1e-6)
                self.assertLessEqual(rect[3], BED[3] - MARGIN_MM + 1e-6)
                self.assertTrue(apart(rect, PURGE, 0), f"{rect} overlaps the purge area")
                for other in rects[i + 1:]:
                    self.assertTrue(apart(rect, other, GAP_MM), f"{rect} and {other} are closer than the gap")

    def test_small_parts_share_a_plate(self):
        sizes = {n: (60.0, 40.0) for n in range(1, 10)}
        plates = pack([{"key": n, "size": s, "group": "plain"} for n, s in sizes.items()], BED, PURGE)
        self.assertEqual(len(plates), 1)
        self.assertEqual(sorted(i["key"] for i in plates[0]["items"]), list(sizes))
        self.check_plates(plates, sizes)

    def test_big_parts_spread_over_plates(self):
        sizes = {1: (240.0, 110.0), 2: (240.0, 110.0), 3: (110.0, 240.0), 4: (200.0, 200.0), 5: (30.0, 30.0)}
        plates = pack([{"key": n, "size": s, "group": "plain"} for n, s in sizes.items()], BED, PURGE)
        self.check_plates(plates, sizes)
        self.assertLess(len(plates), len(sizes))
        self.assertEqual(sorted(i["key"] for p in plates for i in p["items"]), list(sizes))

    def test_a_part_turns_when_that_is_the_only_way_to_share_a_plate(self):
        sizes = {1: (240.0, 110.0), 2: (110.0, 240.0)}   # side by side they need 350 mm; turned, they stack in 232 mm
        plates = pack([{"key": n, "size": s, "group": "plain"} for n, s in sizes.items()], BED, PURGE)
        self.check_plates(plates, sizes)
        self.assertEqual(len(plates), 1)
        self.assertEqual(sum(i["turned"] for i in plates[0]["items"]), 1)

    def test_groups_never_share_a_plate_and_keep_their_order(self):
        items = [{"key": 1, "size": (50, 50), "group": "supports"}, {"key": 2, "size": (50, 50), "group": "plain"},
                 {"key": 3, "size": (50, 50), "group": "supports"}]
        plates = pack(items, BED, PURGE)
        self.assertEqual([(p["group"], sorted(i["key"] for i in p["items"])) for p in plates], [("supports", [1, 3]), ("plain", [2])])

    def test_a_part_bigger_than_the_bed_gets_its_own_plate(self):
        sizes = {1: (300.0, 50.0), 2: (50.0, 50.0)}
        plates = pack([{"key": n, "size": s, "group": "plain"} for n, s in sizes.items()], BED, PURGE)
        self.assertEqual(len(plates), 2)
        big = next(p for p in plates if p["items"][0]["key"] == 1)
        self.assertEqual((len(big["items"]), big["items"][0]["oversize"], big["items"][0]["center"]), (1, True, (128.0, 128.0)))


    def test_a_wider_gap_keeps_supported_parts_further_apart(self):
        sizes = {n: (60.0, 40.0) for n in range(1, 7)}
        plates = pack([{"key": n, "size": s, "group": "supports"} for n, s in sizes.items()], BED, PURGE, gap=20.0)
        self.assertEqual(sorted(i["key"] for p in plates for i in p["items"]), list(sizes))
        for plate in plates:
            rects = footprints(plate, sizes)
            for i, rect in enumerate(rects):
                for other in rects[i + 1:]:
                    self.assertTrue(apart(rect, other, 20.0), f"{rect} and {other} are closer than 20 mm")


if __name__ == "__main__":
    unittest.main()
