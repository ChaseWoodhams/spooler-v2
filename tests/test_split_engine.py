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
