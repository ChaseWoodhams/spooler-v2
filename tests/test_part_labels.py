"""Part numbers debossed into each part during the split (engine seam: result.json and the part STLs).

Orientation is pinned to 'as modelled' where a test needs to know which way a face prints; the real orientation
chooser is covered by the first test."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import manifold3d as m3d
    import numpy as np
    import trimesh

    import split_engine as engine
except ImportError:  # the geometry libraries are optional
    engine = None

M = m3d.Manifold if engine else None
ROD = {"kind": "rod", "diameter": 2.0, "fallback_diameter": 1.0}
DENSE_RODS = {"kind": "rod", "diameter": 2.0, "fallback_diameter": 1.0, "spacing": 6.0, "max_per_joint": 12}   # crowds the faces
NO_RODS = {"kind": "none"}


def cut(cut_id, normal, offset):
    return {"id": cut_id, "parent": None, "normal": list(normal), "offset": offset, "scope": None}


def as_modelled(man, cut_normals, limits):
    """choose_orientation stand-in: print every part exactly as modelled."""
    bb = np.asarray(man.bounding_box(), float)
    size = bb[3:] - bb[:3]
    placed = {"R": np.eye(3), "contact": 0.0, "overhang": 0.0, "height": float(size[2]),
              "footprint": [float(size[0]), float(size[1])]}
    return placed, 1.0


def load(path):
    mesh = trimesh.load(path, force="mesh")
    return engine.manifold_from_arrays(mesh.vertices, mesh.faces)


@unittest.skipIf(engine is None, "manifold3d / trimesh not installed")
class PartLabelTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.n = 0

    def tearDown(self):
        self._tmp.cleanup()

    def run_split(self, model, cuts, connectors=NO_RODS, labels=None, pinned=True):
        """Split `model` with exactly `cuts`; returns (result dict, work dir)."""
        self.n += 1
        work = self.root / f"w{self.n}"
        (work / "source").mkdir(parents=True)
        mesh = model.to_mesh()
        trimesh.Trimesh(vertices=np.asarray(mesh.vert_properties)[:, :3], faces=np.asarray(mesh.tri_verts), process=False
                        ).export(work / "source" / "model.stl")
        job = {"source": "source/model.stl", "connectors": connectors}
        if labels is not None:
            job["labels"] = labels
        (work / "job.json").write_text(json.dumps(job))
        engine.analyze(work)
        (work / "cuts.json").write_text(json.dumps({"mode": "smart", "cuts": cuts}))
        if pinned:
            with mock.patch.object(engine, "choose_orientation", as_modelled):
                engine.split(work)
        else:
            engine.split(work)
        return json.loads((work / "result.json").read_text()), work

    def pocket(self, work_on, work_off, number):
        """The material the label removed from a part: the unlabelled part minus the labelled one (print frame)."""
        name = f"part_{number:02d}.stl"
        return load(work_off / "parts" / name) - load(work_on / "parts" / name)

    # ── Slice 1: every part is numbered, on a cut face when one prints up or vertical ──

    def test_every_part_gets_a_label_on_a_vertical_cut_face_and_the_result_says_where(self):
        result, work = self.run_split(M.cube([300, 100, 40]), [cut(1, (1, 0, 0), 150.0)])
        self.assertEqual(result["unlabelled"], [])
        for part in result["parts"]:
            label = part["label"]
            self.assertEqual(label["face"], "cut")
            self.assertEqual(label["cut"], 1)
            self.assertEqual(label["neighbour"], 3 - part["number"])
            self.assertEqual(label["height_mm"], 7.0)
            self.assertEqual(label["tier"], 1)
            on_cut_plane = abs(label["position"][0] - 150.0) < 0.01          # model frame: the plane x = 150
            self.assertTrue(on_cut_plane)
            self.assertEqual(label["text"], str(part["number"]))
            mesh = trimesh.load(work / "parts" / part["file"], force="mesh")
            self.assertTrue(mesh.is_watertight)

    # ── The number is cut in, reads correctly from outside, and is underlined ──

    def test_the_number_is_debossed_to_depth_and_reads_correctly_seen_from_outside(self):
        cuts = [cut(1, (1, 0, 0), 150.0)]
        on, work_on = self.run_split(M.cube([300, 100, 40]), cuts)
        off, work_off = self.run_split(M.cube([300, 100, 40]), cuts, labels={"enabled": False})
        for number in (1, 2):
            before, after = load(work_off / "parts" / f"part_{number:02d}.stl"), load(work_on / "parts" / f"part_{number:02d}.stl")
            self.assertAlmostEqual((after ^ before).volume(), after.volume(), places=2)          # only ever cut in, never raised
            self.assertEqual(np.round(after.bounding_box(), 3).tolist(), np.round(before.bounding_box(), 3).tolist())
            pocket = before - after
            self.assertGreater(pocket.volume(), 3.0)
            ext = np.asarray(pocket.bounding_box())
            self.assertAlmostEqual(float(ext[3] - ext[0]), 0.6, delta=0.12)   # 0.6 deep, into the cut face (which faces along x)
        # part 2 is "2": viewed from outside (its cut face looks toward -x), the top right and bottom left strokes are cut,
        # the top left and bottom right are not, and the underline runs along the bottom
        label = on["parts"][1]["label"]
        normal, up = np.asarray(label["normal"]), np.asarray(label["up"])
        right = np.cross(up, normal)                       # looking at the face from outside: right = up x toward-the-viewer
        pocket = load(work_off / "parts" / "part_02.stl") - load(work_on / "parts" / "part_02.stl")

        def is_cut(point):
            probe = M.cube([0.2, 0.2, 0.2], True).translate([float(x) for x in point])
            return (pocket ^ probe).volume() > 0.004
        centre = np.asarray(label["position_print"]) - 0.3 * normal      # a little inside the part, mid-pocket
        text_centre = centre + 0.9 * up                                  # the digits sit above the underline
        def cut_at(offset_right, offset_up):
            return is_cut(text_centre + offset_right * right + offset_up * up)
        self.assertTrue(cut_at(1.6, 1.7))      # top right
        self.assertFalse(cut_at(-1.6, 1.7))    # top left
        self.assertTrue(cut_at(-1.6, -1.7))    # bottom left
        self.assertFalse(cut_at(1.6, -1.7))    # bottom right
        self.assertTrue(is_cut(centre - 3.9 * up))     # underline
        self.assertFalse(is_cut(centre - 3.9 * up + 8 * right))   # and nothing far to the side

    # ── Face tiers ──

    def test_the_bed_face_comes_before_a_cut_face_that_prints_facing_down(self):
        # a tilted cut: the part above it has a cut face facing down at 53 degrees (needs supports) and a flat bed face
        result, _ = self.run_split(M.cube([200, 100, 100]), [cut(1, (0.6, 0, 0.8), 60.0)])
        by_tier = {part["label"]["tier"]: part["label"] for part in result["parts"]}
        self.assertEqual(sorted(by_tier), [1, 2])
        self.assertEqual(by_tier[2]["face"], "bed")
        self.assertEqual(by_tier[1]["face"], "cut")
        self.assertAlmostEqual(by_tier[2]["position_print"][2], 0.0, places=2)   # on the bed
        self.assertEqual(result["unlabelled"], [])

    def test_a_down_facing_cut_face_is_used_last_when_the_part_has_no_bed_face(self):
        result, _ = self.run_split(M.cube([200, 100, 100]), [cut(1, (0.6, 0, 0.8), 130.0)])
        by_tier = {part["label"]["tier"]: part["label"] for part in result["parts"]}
        self.assertEqual(sorted(by_tier), [1, 3])
        self.assertEqual(by_tier[3]["face"], "cut")
        self.assertLess(by_tier[3]["normal"][2], -0.7)         # faces down
        self.assertEqual(by_tier[1]["face"], "cut")

    def test_within_a_tier_the_face_with_most_clear_room_wins(self):
        result, _ = self.run_split(M.cube([300, 200, 40]), [cut(1, (1, 0, 0), 150.0), cut(2, (0, 1, 0), 100.0)])
        self.assertEqual(len(result["parts"]), 4)
        for part in result["parts"]:       # every part has a 100 mm cut face (cut 1) and a 150 mm one (cut 2)
            self.assertEqual(part["label"]["cut"], 2)

    def test_equal_room_goes_to_the_lowest_neighbour_part_number(self):
        result, _ = self.run_split(M.cube([200, 200, 40]), [cut(1, (1, 0, 0), 100.0), cut(2, (0, 1, 0), 100.0)])
        self.assertEqual(len(result["parts"]), 4)
        for part in result["parts"]:
            self.assertEqual(part["label"]["neighbour"], min(n["part"] for n in part["neighbours"]))

    # ── Solid kept around the number ──

    def test_at_least_0_8_mm_of_solid_stays_around_the_number_with_rods_in_both_directions(self):
        cuts = [cut(1, (1, 0, 0), 28.0), cut(2, (0, 1, 0), 28.0)]
        model = M.cube([56, 56, 24])
        on, work_on = self.run_split(model, cuts, connectors=DENSE_RODS)
        off, work_off = self.run_split(model, cuts, connectors=DENSE_RODS, labels={"enabled": False})
        self.assertEqual({int(np.argmax(np.abs(pin["axis"]))) for pin in on["pins"]}, {0, 1})     # holes enter from both cut faces
        self.assertEqual(on["unlabelled"], [])
        for part in on["parts"]:
            number = part["number"]
            pocket = self.pocket(work_on, work_off, number)
            self.assertGreater(pocket.volume(), 3.0)
            mesh = pocket.to_mesh()
            points = np.asarray(mesh.vert_properties)[:, :3]
            origin = np.asarray(part["bbox"][:3])
            size = np.asarray(part["bbox"][3:]) - origin
            depth_axis = int(np.argmin(points.max(axis=0) - points.min(axis=0)))
            for axis in range(3):      # outside edges of the face
                if axis != depth_axis:
                    self.assertGreaterEqual(points[:, axis].min(), 0.8 - 0.06)
                    self.assertLessEqual(points[:, axis].max(), size[axis] - 0.8 + 0.06)
            for pin in on["pins"]:     # every hole in this part, whichever face it enters from
                if number not in pin["parts"]:
                    continue
                axis = np.asarray(pin["axis"])
                centre = np.asarray(pin["p"]) - origin
                along = np.clip((points - centre) @ axis, -pin["depth"], pin["depth"])
                gap = np.linalg.norm(points - (centre + np.outer(along, axis)), axis=1) - pin["hole_diameter"] / 2
                self.assertGreaterEqual(float(gap.min()), 0.8 - 0.06, f"part {number} label too close to a hole")

    def test_a_position_over_a_hidden_cavity_is_skipped_and_the_number_moves_to_solid_material(self):
        # a slot opening on the side wall runs 1 mm behind part 2's cut face, under most of the face: a number there would leave 0.4 mm
        cavity_lo, cavity_hi = np.array([29.0, -1.0, 4.0]), np.array([40.0, 46.0, 20.0])
        model = M.cube([56, 56, 24]) - M.cube(list(cavity_hi - cavity_lo)).translate(cavity_lo.tolist())
        cuts = [cut(1, (1, 0, 0), 28.0)]
        on, work_on = self.run_split(model, cuts)
        off, work_off = self.run_split(model, cuts, labels={"enabled": False})
        self.assertEqual(on["unlabelled"], [])
        points = np.asarray(self.pocket(work_on, work_off, 2).to_mesh().vert_properties)[:, :3] + np.asarray(on["parts"][1]["bbox"][:3])
        beyond = np.maximum(np.maximum(cavity_lo - points, points - cavity_hi), 0.0)
        self.assertGreaterEqual(float(np.linalg.norm(beyond, axis=1).min()), 0.8 - 0.06)

    # ── No room ──

    def test_a_part_too_small_for_7_mm_gets_a_smaller_number(self):
        result, _ = self.run_split(M.cube([24, 10.5, 10.5]), [cut(1, (1, 0, 0), 12.0)])
        self.assertEqual(result["unlabelled"], [])
        for part in result["parts"]:
            self.assertLess(part["label"]["height_mm"], 7.0)
            self.assertGreaterEqual(part["label"]["height_mm"], 5.5)

    def test_a_part_with_no_room_is_left_unlabelled_and_the_split_still_works(self):
        result, work = self.run_split(M.cube([130, 4, 4]), [cut(1, (1, 0, 0), 65.0)])
        self.assertEqual(result["unlabelled"], [1, 2])
        self.assertEqual([part["label"] for part in result["parts"]], [None, None])
        self.assertEqual(result["summary"]["parts"], 2)
        self.assertTrue((work / "parts" / "part_01.stl").is_file())

    # ── The switch ──

    def test_labelling_can_be_switched_off_and_then_leaves_parts_unchanged(self):
        cuts = [cut(1, (1, 0, 0), 150.0)]
        result, work = self.run_split(M.cube([300, 100, 40]), cuts, labels={"enabled": False})
        self.assertEqual(result["unlabelled"], [1, 2])
        self.assertFalse(result["summary"]["labels"])
        for part in result["parts"]:
            self.assertIsNone(part["label"])
            self.assertAlmostEqual(load(work / "parts" / part["file"]).volume(), 300 * 100 * 40 / 2, places=1)
        on, _ = self.run_split(M.cube([300, 100, 40]), cuts)
        self.assertTrue(on["summary"]["labels"])
        self.assertEqual(on["parts"][0]["volume_cm3"], result["parts"][0]["volume_cm3"])      # labels do not change the figures
        self.assertEqual(on["parts"][0]["neighbours"], result["parts"][0]["neighbours"])      # nor which parts touch

    def test_labelling_is_on_when_the_job_does_not_say(self):
        result, _ = self.run_split(M.cube([300, 100, 40]), [cut(1, (1, 0, 0), 150.0)], labels=None)
        self.assertTrue(result["summary"]["labels"])

    def test_the_real_orientation_chooser_also_labels_every_part(self):
        result, _ = self.run_split(M.cube([300, 100, 40]), [cut(1, (1, 0, 0), 150.0)], pinned=False)
        self.assertEqual(result["unlabelled"], [])


class LabelSettingTests(unittest.TestCase):
    """The switch in the split's fit settings (next to the Rod settings)."""

    def test_part_numbers_are_on_by_default_and_the_switch_is_kept(self):
        import jobs
        import splits
        self.assertEqual(splits.status()["defaults"]["labels"], {"enabled": True})
        settings = splits.normalize_settings({"labels": {"enabled": False}})
        self.assertEqual(settings["labels"], {"enabled": False})
        self.assertEqual(splits.normalize_settings({"connectors": {"depth": 8}}, settings)["labels"], {"enabled": False})   # other changes keep it
        self.assertEqual(splits.normalize_settings({"labels": {"enabled": True}}, settings)["labels"], {"enabled": True})
        older = {k: v for k, v in splits.DEFAULT_SETTINGS.items() if k != "labels"}    # a split saved before the switch existed
        self.assertEqual(splits.normalize_settings({}, older)["labels"], {"enabled": True})
        with self.assertRaisesRegex(jobs.JobError, "on or off"):
            splits.normalize_settings({"labels": {"enabled": "maybe"}})


if __name__ == "__main__":
    unittest.main()
