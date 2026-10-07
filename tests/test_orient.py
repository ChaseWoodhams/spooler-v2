"""Orient all and per-object orientation suggestions, seen through the project HTTP API."""
import asyncio
import copy
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import database
import jobs
import projects
import slicer
import splits
from tests.test_slicer import cube_stl, make_fake_orca

GEOMETRY = not splits.missing_modules()
FIXTURES = Path(__file__).parent / "fixtures" / "daz_shifter"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

if GEOMETRY:
    import manifold3d as m3d
    import numpy as np
    import trimesh

    def stl_of(man) -> bytes:
        mesh = man.to_mesh()
        return trimesh.Trimesh(np.asarray(mesh.vert_properties)[:, :3], np.asarray(mesh.tri_verts), process=False).export(file_type="stl")

    def box(x, y, z, at=(0, 0, 0)):
        return m3d.Manifold.cube([x, y, z]).translate(list(at))

    def mushroom():
        """A 40 mm disc 3 mm thick with a 10 mm post 30 mm tall standing on it: flat on the disc needs no support."""
        disc = m3d.Manifold.cylinder(3, 20, 20, 48)
        post = m3d.Manifold.cylinder(30, 5, 5, 48).translate([0, 0, 3])
        return stl_of(disc + post)

    def wedge():
        """A ramp: a right triangle 40 long and 20 high (x, z) extruded 10 mm along y, so its sloped face is under 30 degrees."""
        profile = m3d.CrossSection([[(0, 0), (40, 0), (0, 20)]])
        return stl_of(m3d.Manifold.extrude(profile, 10).rotate([90, 0, 0]).translate([0, 10, 0]))

    def cylinder():
        return stl_of(m3d.Manifold.cylinder(60, 10, 10, 64))

    def l_bracket():
        """A 40 mm plate 4 mm thick with a 4 mm wall 30 mm tall along one edge, 50 mm wide."""
        return stl_of(box(40, 50, 4) + box(4, 50, 30))

    def z_bracket():
        """A wide plate at the bottom and a narrower one at the top reaching the other way, joined by a column: either
        way up leaves one plate hanging, and the top one (26 mm of it) hangs less than the bottom one (36 mm)."""
        return stl_of(box(40, 30, 4, at=(-10, 0, 0)) + box(4, 30, 30, at=(15, 0, 0)) + box(30, 30, 4, at=(15, 0, 26)))


def up_of(obj, axis):
    """Where a model axis (0 x, 1 y, 2 z) points on the bed after the object's rotation: its world Z component."""
    return float(projects.rotation_matrix(obj["rotation"])[2][axis])


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class OrientApiTests(unittest.TestCase):
    def setUp(self):
        from http_handler import SPHandler
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        database.configure(self.dir / "spooler.db")
        jobs.init(self.dir)
        slicer.configure(self.dir, orca_path=make_fake_orca(self.dir / "tools"))
        slicer.init()
        splits.configure(self.dir)
        projects.configure(self.dir)
        projects.init()
        patcher = mock.patch("http_handler._auth_ok", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SPHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        slicer.configure()
        splits.configure()
        projects.configure()
        self._tmp.cleanup()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with _opener.open(request, timeout=60) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def project_with(self, *stls):
        """A project with one object per STL, all on plate 1, at the rotation the model was imported with (none)."""
        project = projects.create_project("Orient")
        for data in stls:
            projects.import_model(project["id"], io.BytesIO(data), len(data), "part.stl")

        async def work():
            while await projects.process_next():
                pass
        asyncio.run(work())
        return projects.get_project(project["id"])

    def edit(self, project, change):
        data = copy.deepcopy(project["data"])
        change(data)
        status, saved = self.call("PATCH", f"/api/projects/{project['id']}", {"data": data, "revision": project["revision"]})
        self.assertEqual(status, 200, saved)
        return saved

    def rotate(self, project, rotations):
        """Set each object's rotation (by position in the object list)."""
        def change(data):
            for obj, rotation in zip(data["objects"], rotations):
                obj["rotation"] = rotation
        return self.edit(project, change)

    def orient_all(self, project, **body):
        return self.call("POST", f"/api/projects/{project['id']}/orient-all", body)

    def test_a_mushroom_standing_on_its_post_is_laid_back_on_its_disc(self):
        project = self.rotate(self.project_with(mushroom()), [[180, 0, 0]])
        status, oriented = self.orient_all(project)
        self.assertEqual(status, 200, oriented)
        self.assertAlmostEqual(up_of(oriented["data"]["objects"][0], 2), 1.0, places=3)   # the post is vertical and points up

    def test_a_cylinder_lying_on_its_side_is_stood_up(self):
        project = self.rotate(self.project_with(cylinder()), [[90, 0, 0]])
        _, oriented = self.orient_all(project)
        self.assertAlmostEqual(abs(up_of(oriented["data"]["objects"][0], 2)), 1.0, places=3)

    def test_a_ramp_is_laid_so_its_sloped_face_is_not_underneath(self):
        project = self.rotate(self.project_with(wedge()), [[0, 160, 0]])
        _, oriented = self.orient_all(project)
        # Laid on its triangular end (the model's y axis vertical): no overhang and the lowest build height
        self.assertAlmostEqual(abs(up_of(oriented["data"]["objects"][0], 1)), 1.0, places=3)

    def test_an_l_bracket_goes_plate_down_with_the_wall_up(self):
        project = self.rotate(self.project_with(l_bracket()), [[0, 180, 90]])   # wall hanging down, then turned about Z
        _, oriented = self.orient_all(project)
        obj = oriented["data"]["objects"][0]
        self.assertAlmostEqual(up_of(obj, 2), 1.0, places=3)
        self.assertAlmostEqual(obj["rotation"][2], 90.0, places=2)   # the turn about Z is kept

    def test_a_part_already_in_its_best_orientation_is_left_alone(self):
        project = self.project_with(mushroom(), l_bracket())
        status, oriented = self.orient_all(project)
        self.assertEqual((status, oriented["orient"]), (200, {"oriented": [], "unchanged": [1, 2], "pinned": []}))
        self.assertEqual(oriented["revision"], project["revision"])

    def test_pinned_objects_are_skipped(self):
        project = self.rotate(self.project_with(mushroom(), mushroom()), [[180, 0, 0], [180, 0, 0]])
        project = self.edit(project, lambda d: d["objects"][0].update(pinned=True))
        _, oriented = self.orient_all(project)
        pinned, free = oriented["data"]["objects"]
        self.assertEqual(pinned["rotation"], [180, 0, 0])
        self.assertAlmostEqual(up_of(free, 2), 1.0, places=3)
        self.assertEqual(oriented["orient"], {"oriented": [2], "unchanged": [], "pinned": [1]})

    def test_a_plate_can_be_oriented_on_its_own(self):
        project = self.rotate(self.project_with(mushroom(), mushroom()), [[180, 0, 0], [180, 0, 0]])
        project = self.edit(project, lambda d: (d["plates"].append({"id": 2, "name": "Plate 2", "overrides": {}, "bricklayers": None}),
                                                d["objects"][1].update(plate=2)))
        _, oriented = self.orient_all(project, plate=2)
        self.assertEqual(oriented["data"]["objects"][0]["rotation"], [180, 0, 0])
        self.assertAlmostEqual(up_of(oriented["data"]["objects"][1], 2), 1.0, places=3)

    def test_importing_a_model_never_orients_it(self):
        project = self.project_with(mushroom())
        self.assertEqual(project["data"]["objects"][0]["rotation"], [0, 0, 0])
        data = mushroom()
        projects.import_model(project["id"], io.BytesIO(data), len(data), "again.stl", up="x")
        asyncio.run(self._drain())
        added = projects.get_project(project["id"])
        self.assertEqual([o["rotation"] for o in added["data"]["objects"]], [[0, 0, 0], [0, 0, 0]])

    async def _drain(self):
        while await projects.process_next():
            pass

    def test_a_suggestion_changes_nothing_until_it_is_accepted(self):
        project = self.rotate(self.project_with(mushroom()), [[180, 0, 0]])
        status, suggestion = self.call("GET", f"/api/projects/{project['id']}/objects/1/orientation")
        self.assertEqual(status, 200, suggestion)
        self.assertTrue(suggestion["changed"])
        self.assertGreater(suggestion["current"]["support"], suggestion["suggested"]["support"])
        self.assertEqual(projects.get_project(project["id"])["data"]["objects"][0]["rotation"], [180, 0, 0])
        status, accepted = self.call("POST", f"/api/projects/{project['id']}/objects/1/orient", {})
        self.assertEqual(status, 200, accepted)
        self.assertEqual(accepted["data"]["objects"][0]["rotation"], suggestion["rotation"])

    def test_accepting_works_on_a_pinned_object_but_orient_all_does_not(self):
        project = self.rotate(self.project_with(mushroom()), [[180, 0, 0]])
        project = self.edit(project, lambda d: d["objects"][0].update(pinned=True))
        _, accepted = self.call("POST", f"/api/projects/{project['id']}/objects/1/orient", {})
        self.assertAlmostEqual(up_of(accepted["data"]["objects"][0], 2), 1.0, places=3)

    def test_the_choice_of_mode_changes_the_suggestion(self):
        project = self.project_with(cylinder())   # standing, 60 mm tall
        base = f"/api/projects/{project['id']}/objects/1/orientation"
        _, least = self.call("GET", base)
        _, strength = self.call("GET", base + "?mode=strength")
        self.assertFalse(least["changed"])   # standing needs no support at all
        self.assertTrue(strength["changed"])  # lying down keeps the layer lines along the length, at the cost of some support
        self.assertAlmostEqual(abs(up_of({"rotation": strength["rotation"]}, 2)), 0.0, places=3)

    def test_looks_keeps_the_favoured_side_from_hanging_in_support(self):
        project = self.project_with(z_bracket())
        base = f"/api/projects/{project['id']}/objects/1/orientation"
        _, least = self.call("GET", base)
        self.assertFalse(least["changed"])   # the narrower top plate hangs less
        _, looks = self.call("GET", base + "?mode=looks&face=0,0,-1")   # the undersides should look good
        self.assertAlmostEqual(up_of({"rotation": looks["rotation"]}, 2), -1.0, places=3)
        status, _ = self.call("GET", base + "?mode=looks")
        self.assertEqual(status, 400)   # looks needs a side to favour
        status, _ = self.call("POST", f"/api/projects/{project['id']}/orient-all", {"mode": "looks"})
        self.assertEqual(status, 400)

    def test_the_overhang_angle_follows_the_profile(self):
        project = self.rotate(self.project_with(wedge()), [[180, 0, 0]])   # its 27 degree slope faces down
        base = f"/api/projects/{project['id']}/objects/1/orientation"
        _, usual = self.call("GET", base)
        self.assertEqual(usual["overhang_angle"], 30)   # the profile's support_threshold_angle
        self.assertGreater(usual["current"]["support"], 0)
        profile = project["data"]["profile"]
        project = self.edit(project, lambda d: d["profile"].update(process={**profile["process"], "support_threshold_angle": "20"}))
        _, relaxed = self.call("GET", base)
        self.assertEqual((relaxed["overhang_angle"], relaxed["current"]["support"]), (20, 0))
        _, asked = self.call("GET", base + "?overhang_angle=45")
        self.assertEqual(asked["overhang_angle"], 45)

    def test_orient_all_says_how_many_objects_it_turned(self):
        project = self.rotate(self.project_with(mushroom(), mushroom(), l_bracket()), [[180, 0, 0], [180, 0, 0], [0, 0, 0]])
        project = self.edit(project, lambda d: d["objects"][1].update(pinned=True))
        _, oriented = self.orient_all(project)
        self.assertEqual(oriented["outcome"], "Turned 1 object, 1 already on a good face, 1 pinned left alone")

    def test_orient_all_says_so_when_there_was_nothing_to_turn(self):
        project = self.project_with(mushroom(), l_bracket())
        _, oriented = self.orient_all(project)
        self.assertEqual(oriented["outcome"], "Turned 0 objects, 2 already on a good face")

    def test_orient_all_says_so_when_there_is_nothing_to_orient(self):
        project = projects.get_project(projects.create_project("Empty")["id"])
        _, oriented = self.orient_all(project)
        self.assertEqual(oriented["outcome"], "Nothing to orient")

    def test_orient_all_turns_the_daz_shifter_parts_that_are_not_on_a_good_face(self):
        stls = sorted(FIXTURES.glob("*.stl"))
        self.assertEqual(len(stls), 19)
        project = self.project_with(*(p.read_bytes() for p in stls))
        self.assertEqual(len(project["data"]["objects"]), 19)
        _, oriented = self.orient_all(project)
        before = {o["id"]: o["rotation"] for o in project["data"]["objects"]}
        turned = [o["id"] for o in oriented["data"]["objects"] if o["rotation"] != before[o["id"]]]
        self.assertEqual(sorted(turned), sorted(oriented["orient"]["oriented"]))
        self.assertGreaterEqual(len(turned), 10)   # most of these parts print on a face other than the one they are modelled on
        self.assertTrue(oriented["outcome"].startswith(f"Turned {len(turned)} objects"), oriented["outcome"])

    def test_a_project_changed_meanwhile_is_not_overwritten(self):
        project = self.rotate(self.project_with(mushroom()), [[180, 0, 0]])
        record = projects.get_project(project["id"])
        with mock.patch("projects._orientation_for", side_effect=lambda *a: (self.edit(record, lambda d: None), {"rotation": [0, 0, 0], "changed": True})[1]):
            status, result = self.orient_all(project)
        self.assertEqual(status, 409, result)
