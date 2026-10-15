import json
import tempfile
import unittest
from pathlib import Path

try:
    import manifold3d as m3d
    import numpy as np
    import trimesh

    import split_engine as engine
except ImportError:  # the geometry libraries are optional
    engine = None


def channel(length=520.0):
    """A U channel with a boss through its floor: needs cuts on a 240 mm bed, and has thin walls."""
    M = m3d.Manifold
    body = M.cube([length, 140, 70]) - M.cube([length - 20, 116, 70]).translate([10, 12, 12])
    return M.batch_boolean([body, M.cylinder(90, 25, 25, 48).translate([length / 2, 70, -10])], m3d.OpType.Add)


def export(man, path: Path, soup: bool = False) -> None:
    mesh = man.to_mesh()
    verts, tris = np.asarray(mesh.vert_properties)[:, :3], np.asarray(mesh.tri_verts)
    if soup:  # an STL as exported by most programs: every triangle has its own vertices
        verts, tris = verts[tris].reshape(-1, 3), np.arange(len(tris) * 3).reshape(-1, 3)
    trimesh.Trimesh(vertices=verts, faces=tris, process=False).export(path)


@unittest.skipIf(engine is None, "manifold3d / trimesh not installed")
class EngineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work = Path(self._tmp.name)
        (self.work / "source").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def job(self, model, **settings):
        export(model, self.work / "source" / "model.stl", soup=True)
        (self.work / "job.json").write_text(json.dumps({"source": "source/model.stl", **settings}))

    def read(self, name):
        return json.loads((self.work / name).read_text())

    def test_smart_split_makes_fitting_parts_whose_holes_line_up(self):
        self.job(channel(), planner={"mode": "smart"}, connectors={"kind": "rod", "diameter": 2.0, "fallback_diameter": 1.0})
        engine.analyze(self.work)
        analysis = self.read("analysis.json")
        self.assertEqual(analysis["size"], [520.0, 140.0, 90.0])
        self.assertFalse(analysis["fits_whole"])
        self.assertEqual(analysis["repairs"], [])  # unmerged STL vertices are joined without counting as a repair

        engine.plan(self.work)
        check = self.read("check.json")
        self.assertTrue(check["cuts"])
        self.assertTrue(all(p["fits"] for p in check["pieces"]))

        engine.split(self.work)
        result = self.read("result.json")
        summary = result["summary"]
        self.assertEqual(summary["parts"], len(check["pieces"]))
        self.assertEqual(summary["holes_not_shared_by_two"], 0)
        self.assertEqual(summary["joints_without_rods"], 0)
        self.assertGreater(summary["rods"]["2 mm rod"], 0)
        self.assertAlmostEqual(summary["volume_cm3"], analysis["volume_cm3"], delta=1.0)  # only the holes are removed
        self.assertEqual(sorted(a["part"] for a in result["assembly"]), [p["number"] for p in result["parts"]])
        for pin in result["pins"]:
            self.assertEqual(len(pin["parts"]), 2)
        for part in result["parts"]:
            self.assertTrue(part["neighbours"])
            mesh = trimesh.load(self.work / "parts" / part["file"], force="mesh")
            self.assertTrue(mesh.is_watertight)
            self.assertLessEqual(max(mesh.extents[:2]), 240.01 * 2 ** 0.5)
            self.assertLessEqual(mesh.extents[2], 250.01)
            self.assertLessEqual(max(part["print_size"][:2]), 240.01)

    def test_a_hole_that_is_not_shared_by_two_parts_is_removed(self):
        self.job(channel(), planner={"mode": "smart"}, connectors={"kind": "rod", "diameter": 2.0, "fallback_diameter": 1.0})
        engine.analyze(self.work)
        engine.plan(self.work)
        real = engine.place_pins

        def with_stray_hole(*args):
            pins, islands = real(*args)
            if pins:
                stray = dict(pins[0], uv=[pins[0]["uv"][0] + 5000.0, pins[0]["uv"][1]], p=[pins[0]["p"][0] + 5000.0, *pins[0]["p"][1:]])
                pins.append(stray)
                islands[stray["island"]]["pins"] += 1
            return pins, islands

        engine.place_pins = with_stray_hole
        try:
            engine.split(self.work)
        finally:
            engine.place_pins = real
        summary = self.read("result.json")["summary"]
        self.assertEqual(summary["holes_not_shared_by_two"], 0)
        self.assertGreater(summary["holes_removed"], 0)
        rods = sum(summary["rods"].values())
        self.assertEqual(rods, sum(j["rods"] for j in self.read("result.json")["joints"]))

    def test_every_joint_gets_two_keyed_holes_away_from_the_edge(self):
        self.job(channel(), planner={"mode": "smart"}, connectors={"kind": "rod", "diameter": 2.0, "fallback_diameter": 1.0})
        engine.analyze(self.work)
        engine.plan(self.work)
        engine.split(self.work)
        result = self.read("result.json")
        self.assertEqual((result["summary"]["joints_single_rod"], result["summary"]["joints_unkeyed"]), (0, 0))
        for joint in result["joints"]:
            self.assertGreaterEqual(joint["rods"], 2)
            self.assertTrue(joint["keyed"])

    def test_a_pattern_that_fits_itself_after_a_turn_is_moved(self):
        cand = np.array([[x, y] for x in range(0, 101, 2) for y in range(0, 41, 2)], float)
        centre = cand.mean(axis=0)
        symmetric = [int(np.argmin(np.linalg.norm(cand - [10, 20], axis=1))), int(np.argmin(np.linalg.norm(cand - [90, 20], axis=1)))]
        self.assertFalse(engine.is_keyed(cand, symmetric, centre))
        fixed = engine.key_pattern(cand, symmetric, centre, gap=6.0)
        self.assertTrue(engine.is_keyed(cand, fixed, centre))
        self.assertEqual(len(fixed), 2)

    def test_holes_stay_off_thin_edges_when_there_is_room(self):
        cand = np.array([[x, y] for x in range(0, 61) for y in range(0, 61)], float)
        edge = np.minimum.reduce([cand[:, 0], cand[:, 1], 60 - cand[:, 0], 60 - cand[:, 1]])
        picked, _ = engine.pick_connectors(cand, edge, want=2, gap=10.0)
        self.assertEqual(len(picked), 2)
        self.assertTrue(all(edge[k] >= engine.EDGE_MARGIN for k in picked))

    def test_smart_cuts_prefer_even_slabs(self):
        self.job(channel(), planner={"mode": "smart"})
        engine.analyze(self.work)
        engine.plan(self.work)
        widths = sorted(max(p["size"]) for p in self.read("check.json")["pieces"])
        self.assertLess(widths[-1] - widths[0], 120, widths)

    def test_grid_is_even_or_bed_sized(self):
        model = m3d.Manifold.cube([520, 100, 40])
        limits = dict(engine.DEFAULT_PRINTER)
        self.assertEqual([c["offset"] for c in engine.plan_grid(model, limits, balanced=True)], [173.333, 346.667])
        self.assertEqual([c["offset"] for c in engine.plan_grid(model, limits, balanced=False)], [240.0, 480.0])
        self.assertTrue(all(c["parent"] is None for c in engine.plan_grid(model, limits, True)))

    def test_a_model_that_fits_is_one_part(self):
        self.job(m3d.Manifold.cube([60, 40, 30]), planner={"mode": "smart"})
        engine.analyze(self.work)
        engine.plan(self.work)
        self.assertEqual(self.read("cuts.json")["cuts"], [])
        engine.split(self.work)
        result = self.read("result.json")
        self.assertEqual((result["summary"]["parts"], result["joints"], result["assembly"]), (1, [], [{"part": 1, "joins": [], "rods": 0}]))

    def test_moving_a_cut_so_a_part_is_too_big_is_refused(self):
        self.job(channel(), planner={"mode": "grid", "balanced": True})
        engine.analyze(self.work)
        engine.plan(self.work)
        cuts = self.read("cuts.json")["cuts"]
        cuts[0]["offset"] = 20.0  # leaves a 327 mm piece
        (self.work / "cuts.json").write_text(json.dumps({"mode": "grid", "cuts": cuts}))
        engine.check(self.work)
        self.assertFalse(all(p["fits"] for p in self.read("check.json")["pieces"]))
        with self.assertRaisesRegex(engine.EngineError, "too big"):
            engine.split(self.work)

    def test_a_part_the_plan_says_fits_always_splits(self):
        # A bar laid corner to corner on a low printer fits the bed only on the diagonal: its tightest footprint is longer
        # than the bed, its axis-aligned one is not. The plan's "fits" and the Split's orientation must judge that the same way.
        bar = m3d.Manifold.cube([290, 40, 40], True).rotate([0, 0, 45])
        self.job(bar, planner={"mode": "smart"}, printer={"height": 50})
        engine.analyze(self.work)
        engine.plan(self.work)
        engine.check(self.work)
        pieces = self.read("check.json")["pieces"]
        self.assertTrue(all(p["fits"] for p in pieces))
        engine.split(self.work)  # no "no orientation that fits the printer"
        self.assertEqual(self.read("result.json")["summary"]["parts"], 1)

    def test_the_plan_and_the_orientation_use_one_fits_test(self):
        limits = engine.printer_limits({"printer": {"height": 50}})
        for dims in ([290, 40, 40], [200, 200, 10], [330, 20, 10], [100, 100, 60]):
            man = m3d.Manifold.cube(dims, True).rotate([0, 0, 45])
            described = engine.describe_pieces(engine.whole(man), limits)[0]["fits"]
            placed, _ = engine.choose_orientation(man, [], limits)
            self.assertEqual(described, placed is not None, dims)

    def test_a_split_refused_for_a_part_names_the_cut_to_move(self):
        self.job(channel(), planner={"mode": "grid", "balanced": True})
        engine.analyze(self.work)
        engine.plan(self.work)
        cuts = self.read("cuts.json")["cuts"]
        cuts[0]["offset"] = 20.0
        (self.work / "cuts.json").write_text(json.dumps({"mode": "grid", "cuts": cuts}))
        with self.assertRaisesRegex(engine.EngineError, rf"move cuts? [0-9, ]*{cuts[0]['id']}[,\s]"):
            engine.split(self.work)

    def test_a_cut_that_leaves_a_sliver_is_flagged_and_refused(self):
        self.job(channel(), planner={"mode": "grid", "balanced": True})
        engine.analyze(self.work)
        engine.plan(self.work)
        self.assertFalse(any(p["tiny"] for p in self.read("check.json")["pieces"]))
        cuts = self.read("cuts.json")["cuts"]
        cuts[0]["offset"] = 1.0  # leaves a 1 mm slab
        (self.work / "cuts.json").write_text(json.dumps({"mode": "grid", "cuts": cuts}))
        engine.check(self.work)
        self.assertTrue(any(p["tiny"] for p in self.read("check.json")["pieces"]))
        with self.assertRaisesRegex(engine.EngineError, "too small"):
            engine.split(self.work)

    def test_meshes_are_repaired(self):
        box = trimesh.creation.box((40, 40, 40))
        open_box = trimesh.Trimesh(vertices=box.vertices, faces=box.faces[:-2], process=False)
        model, repairs, _ = engine.build_solid(open_box)
        self.assertAlmostEqual(model.volume(), 64000, delta=1)
        self.assertTrue(repairs)

        overlapping = trimesh.util.concatenate([box, box.copy().apply_translation((20, 0, 0))])
        model, repairs, bodies = engine.build_solid(overlapping)
        self.assertEqual(bodies, 2)
        self.assertAlmostEqual(model.volume(), 96000, delta=1)
        self.assertIn("Merged overlapping shells", repairs[0])

        loops = engine.fan_fill(open_box)
        self.assertEqual(loops[2], [4])

    def test_invalid_input_is_explained(self):
        (self.work / "source" / "flat.stl").write_text("solid x\nfacet normal 0 0 1\n outer loop\n vertex 0 0 0\n vertex 1 0 0\n"
                                                        " vertex 0 1 0\n endloop\nendfacet\nendsolid x\n")
        (self.work / "job.json").write_text(json.dumps({"source": "source/flat.stl"}))
        self.assertEqual(engine.main(["split_engine.py", "analyze", str(self.work)]), 2)
        self.assertIn("error", self.read("error.json"))

        with self.assertRaisesRegex(engine.EngineError, "comes after"):
            engine.validate_cuts([{"id": 2, "normal": [1, 0, 0], "offset": 5, "parent": [1, 1]}])
        with self.assertRaisesRegex(engine.EngineError, "direction"):
            engine.validate_cuts([{"id": 1, "normal": [0, 0, 0], "offset": 5}])
        with self.assertRaisesRegex(engine.EngineError, "diameters"):
            engine.connector_specs({"connectors": {"diameter": 40}})
        self.assertEqual(engine.connector_specs({"connectors": {"kind": "none"}})[0], [])
        specs, _, _ = engine.connector_specs({"connectors": {"diameter": 2, "fallback_diameter": 1, "depth": 6.5}})
        self.assertEqual([(s["label"], s["depth"]) for s in specs], [("2 mm rod", 6.5), ("1 mm rod", 4.0)])

    def test_preview_files_can_be_read_back(self):
        path = self.work / "preview.bin"
        verts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], np.float32)
        engine.write_mesh_bin(path, [(verts, np.array([[0, 1, 2]], np.uint32))] * 2)
        data = path.read_bytes()
        self.assertEqual((data[:4], int.from_bytes(data[4:8], "little"), len(data)), (b"SPV1", 2, 8 + 2 * (8 + 36 + 12)))


if __name__ == "__main__":
    unittest.main()
