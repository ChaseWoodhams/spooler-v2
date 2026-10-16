"""Ranked steady orientation candidates from orientation.rank, on fixture meshes (auto-plating ticket 14)."""
import os
import time
import unittest
from pathlib import Path

import orientation
import splits

GEOMETRY = not splits.missing_modules()
BED = {"bed_x": 240, "bed_y": 240, "height": 250}

if GEOMETRY:
    import numpy as np
    import trimesh

    def mesh_of(m):
        return np.asarray(m.vertices), np.asarray(m.faces)

    def rank(m, **kw):
        verts, tris = mesh_of(m)
        kw.setdefault("limits", BED)
        return orientation.rank(verts, tris, (1, 1, 1), (0, 0, 0), **kw)

    def up_of(candidate, axis):
        """Where a model axis points after the candidate's rotation: its world Z component."""
        return float(orientation.rotation_matrix(candidate["rotation"])[2][axis])


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class RankTests(unittest.TestCase):
    def test_tall_thin_part_lies_down_first(self):
        post = trimesh.creation.box((10, 10, 200))
        result = rank(post)
        best = result["candidates"][0]
        self.assertAlmostEqual(up_of(best, 2), 0.0, places=3)    # the long axis is horizontal
        self.assertFalse(result["unavoidably_tippy"])
        self.assertIsNone(result["reason"])
        for c in result["candidates"]:                            # none of them stands the post on its end
            self.assertLess(abs(up_of(c, 2)), 0.99)

    def test_part_too_big_to_lie_flat_is_unavoidably_tippy(self):
        pole = trimesh.creation.box((10, 10, 340))               # too long for 240 x 240 even on the diagonal, and for 250 high
        result = rank(pole)
        self.assertTrue(result["unavoidably_tippy"])
        self.assertIn("bed", result["reason"])

    def test_ball_has_no_steady_orientation_and_says_why(self):
        ball = trimesh.creation.icosphere(subdivisions=3, radius=20)
        result = rank(ball)
        self.assertTrue(result["unavoidably_tippy"])
        self.assertIn("steady", result["reason"])
        self.assertTrue(result["candidates"])                    # still offers something that fits

    def test_a_part_that_needs_no_support_says_so_and_an_overhang_does(self):
        flat = rank(trimesh.creation.box((40, 40, 10)))["candidates"][0]
        self.assertFalse(flat["needs_support"])
        self.assertEqual(flat["support_source"], "raycast")
        # a T lying on its side always has a 30 mm ceiling over the stem somewhere: standing on the stem's end is unsteady
        top = trimesh.creation.box((60, 20, 4), transform=trimesh.transformations.translation_matrix((0, 0, 20)))
        stem = trimesh.creation.box((20, 20, 40), transform=trimesh.transformations.translation_matrix((0, 0, 0)))
        tee = trimesh.util.concatenate([top, stem])
        result = rank(tee)
        self.assertFalse(result["unavoidably_tippy"])
        self.assertTrue(all(c["support_volume"] >= 0 for c in result["candidates"]))

    def test_cut_face_wins_a_tie(self):
        # a 30 mm cube prints equally well on any face; the one marked as a cut face is first
        cube = trimesh.creation.box((30, 30, 30))
        plain = rank(cube)["candidates"]
        cut = rank(cube, cut_normals=[(1, 0, 0)])["candidates"]
        self.assertTrue(cut[0]["cut_face"])
        self.assertFalse(any(c["cut_face"] for c in plain))
        self.assertAlmostEqual(abs(up_of(cut[0], 0)), 1.0, places=3)    # its X face is the one that lies on the bed

    def test_wider_nozzle_wants_more_first_layer_contact(self):
        # a 4 x 4 mm foot is enough to stand on for a 0.4 nozzle, not for a 0.7
        peg = trimesh.creation.box((4, 4, 6))
        self.assertFalse(rank(peg, nozzle=0.4)["unavoidably_tippy"])
        base = trimesh.creation.box((30, 30, 2), transform=trimesh.transformations.translation_matrix((0, 0, 4)))
        small = trimesh.util.concatenate([peg, base])
        self.assertFalse(rank(small, nozzle=0.7)["unavoidably_tippy"])    # the wide face is available

    def test_ranking_a_mid_sized_part_is_quick(self):
        ball = trimesh.creation.icosphere(subdivisions=6, radius=30)       # about 80k triangles
        lump = trimesh.util.concatenate([ball, trimesh.creation.box((80, 80, 4))])
        start = time.perf_counter()
        rank(lump)
        self.assertLess(time.perf_counter() - start, 1.0)


RESEARCH_STL = Path(os.environ.get("SPOOLER_RESEARCH_STL", Path(os.environ.get("TEMP", ".")) / "bench" / "stl"))


@unittest.skipUnless(GEOMETRY and RESEARCH_STL.is_dir() and any(RESEARCH_STL.glob("*.stl")),
                     "the six research parts aren't on this machine (set SPOOLER_RESEARCH_STL)")
class RealPartTests(unittest.TestCase):
    def test_top_candidate_is_the_ray_cast_minimum(self):
        for path in sorted(RESEARCH_STL.glob("*.stl")):
            with self.subTest(part=path.name):
                m = trimesh.load(path, force="mesh", process=False)
                start = time.perf_counter()
                result = rank(m)
                elapsed = time.perf_counter() - start
                volumes = [c["support_volume"] for c in result["candidates"]]
                self.assertEqual(result["candidates"][0]["support_source"], "raycast")
                self.assertLessEqual(volumes[0], min(volumes) * 1.02 + 1.0)
                if len(m.faces) < 100000:     # a seat hip Part is about this big
                    self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
