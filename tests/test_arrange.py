"""One Arrange (the outline nester, or OrcaSlicer's own as a fallback), seen through the project HTTP API."""
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
from tests.test_projects import arrange_runner
from tests.test_slicer import cube_stl, make_fake_orca

GEOMETRY = not splits.missing_modules()
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
GREY = {"preset": "Elegoo PETG @ECC", "colour": "grey"}
SUPPORTS = {"enable_support": "1", "support_type": "tree(auto)"}
BED = (0.0, 0.0, 256.0, 256.0)
PURGE = (246.0, 0.0, 256.0, 20.0)


def box_facets(x0, y0, z0, x1, y1, z1):
    v = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    faces = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
             (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    return "".join("facet normal 0 0 0\n outer loop\n" + "".join(f"  vertex {v[i][0]} {v[i][1]} {v[i][2]}\n" for i in f)
                   + " endloop\nendfacet\n" for f in faces)


def mushroom_stl() -> bytes:
    """A 40 x 40 mm cap on a 10 mm stem: its flat underside overhangs, so it needs supports standing as it is."""
    return ("solid mushroom\n" + box_facets(15, 15, 0, 25, 25, 20) + box_facets(0, 0, 20, 40, 40, 30) + "endsolid mushroom\n").encode()


def footprint(obj, side):
    """A square part's outline on the bed: `side` mm across, turned by the object's rotation about Z."""
    from shapely import affinity
    from shapely.geometry import box
    cx, cy = obj["position"]
    return affinity.rotate(box(cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2), obj["rotation"][2], origin=(cx, cy))


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class ArrangeApiTests(unittest.TestCase):
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
            with _opener.open(request, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def project_with(self, *stls):
        """A project with one model per STL, one object each, all on plate 1."""
        project = projects.create_project("Brackets")
        for n, data in enumerate(stls, start=1):
            projects.import_model(project["id"], io.BytesIO(data), len(data), f"part{n}.stl")

        async def work():
            while await projects.process_next():
                pass
        asyncio.run(work())
        return projects.get_project(project["id"])

    def save(self, project, change):
        data = copy.deepcopy(project["data"])
        change(data)
        status, saved = self.call("PATCH", f"/api/projects/{project['id']}", {"data": data, "revision": project["revision"]})
        self.assertEqual(status, 200, saved)
        return saved

    def lay(self, project, placements):
        """Objects of the project's models, as [(model, plate, (x, y), pinned)] from id 1; plates are made as needed."""
        def change(data):
            templates = {o["model"]: o for o in data["objects"]}
            data["objects"] = [{**templates[model], "id": i, "plate": plate, "position": list(at), "name": f"Object {i}",
                                **({"pinned": True} if pinned else {})}
                               for i, (model, plate, at, pinned) in enumerate(placements, start=1)]
            for plate in range(len(data["plates"]) + 1, max(p for _, p, _, _ in placements) + 1):
                data["plates"].append({"id": plate, "name": f"Plate {plate}"})
        return self.save(project, change)

    def arrange(self, project, plate=None, **body):
        path = f"/api/projects/{project['id']}/" + (f"plates/{plate}/arrange" if plate else "arrange")
        return self.call("POST", path, body)

    def by_plate(self, record):
        out = {}
        for o in record["data"]["objects"]:
            out.setdefault(o["plate"], []).append(o["id"])
        return out

    def assert_clear(self, record, sides, needs):
        """Every pair on a plate is at least max of their clearances apart, and nothing is past the bed edge or in the purge area."""
        from shapely.geometry import box
        for plate, ids in self.by_plate(record).items():
            shapes = {i: footprint(next(o for o in record["data"]["objects"] if o["id"] == i), sides[i]) for i in ids}
            for i, poly in shapes.items():
                x0, y0, x1, y1 = poly.bounds
                self.assertGreaterEqual(min(x0 - BED[0], y0 - BED[1], BED[2] - x1, BED[3] - y1), 3 - 1e-3, f"object {i} is too near the bed edge")
                self.assertGreaterEqual(poly.distance(box(*PURGE)), needs[i] - 1e-3, f"object {i} is too near the purge area")
                for j in ids:
                    if j > i:
                        self.assertGreaterEqual(poly.distance(shapes[j]), max(needs[i], needs[j]) - 1e-3, f"objects {i} and {j} are too close")

    def test_arrange_all_packs_every_plate_onto_as_few_as_fit_with_clearance(self):
        project = self.project_with(cube_stl(20))
        project = self.lay(project, [(1, 1, (50, 50), False), (1, 2, (50, 50), False), (1, 3, (50, 50), False), (1, 3, (150, 150), False)])
        status, result = self.arrange(project)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.by_plate(result), {1: [1, 2, 3, 4]})
        self.assertEqual([p["id"] for p in result["data"]["plates"]], [1])
        self.assert_clear(result, {i: 20 for i in range(1, 5)}, {i: 6 for i in range(1, 5)})
        self.assertEqual(self.call("GET", f"/api/projects/{project['id']}")[1]["data"], result["data"])

    def test_arrange_one_plate_leaves_the_others_alone(self):
        project = self.project_with(cube_stl(20))
        project = self.lay(project, [(1, 1, (30, 30), False), (1, 1, (31, 31), False), (1, 2, (77, 78), False)])
        status, result = self.arrange(project, plate=1)
        self.assertEqual(status, 200, result)
        objects = {o["id"]: o for o in result["data"]["objects"]}
        self.assertEqual((objects[3]["plate"], objects[3]["position"]), (2, [77.0, 78.0]))
        self.assertEqual((objects[1]["plate"], objects[2]["plate"]), (1, 1))
        self.assert_clear({"data": {"objects": [objects[1], objects[2]]}}, {1: 20, 2: 20}, {1: 6, 2: 6})
        self.assertEqual(self.arrange(project, plate=9)[0], 409)

    def test_a_pinned_object_stays_put_and_its_plate_is_never_emptied(self):
        project = self.project_with(cube_stl(20))
        project = self.lay(project, [(1, 1, (60, 60), False), (1, 1, (100, 60), False), (1, 2, (140, 140), True),
                                     (1, 3, (30, 200), True)])
        status, result = self.arrange(project)
        self.assertEqual(status, 200, result)
        objects = {o["id"]: o for o in result["data"]["objects"]}
        self.assertEqual([(objects[i]["plate"], objects[i]["position"], objects[i]["rotation"]) for i in (3, 4)],
                         [(2, [140.0, 140.0], [0.0, 0.0, 0.0]), (3, [30.0, 200.0], [0.0, 0.0, 0.0])])
        self.assertEqual(sorted(p["id"] for p in result["data"]["plates"]), [2, 3])      # plate 1 held nothing pinned and went
        self.assertEqual({objects[1]["plate"], objects[2]["plate"]} <= {2, 3}, True)     # the others packed around the pins
        self.assert_clear(result, {i: 20 for i in range(1, 5)}, {i: 6 for i in range(1, 5)})

    def test_an_object_needing_supports_keeps_22_mm_and_a_plain_one_6_mm(self):
        project = self.project_with(mushroom_stl(), cube_stl(20))
        project = self.save(project, lambda d: d["profile"]["process"].update(SUPPORTS))
        project = self.lay(project, [(1, 1, (50, 50), False), (1, 1, (60, 60), False), (2, 1, (70, 70), False), (2, 1, (80, 80), False)])
        status, result = self.arrange(project)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.by_plate(result), {1: [1, 2, 3, 4]})
        sides, needs = {1: 40, 2: 40, 3: 20, 4: 20}, {1: 22, 2: 22, 3: 6, 4: 6}   # the cubes have no overhang: no supports
        self.assert_clear(result, sides, needs)
        shapes = {o["id"]: footprint(o, sides[o["id"]]) for o in result["data"]["objects"]}
        self.assertLess(shapes[3].distance(shapes[4]), 8)                          # plain cubes are not held at the supported gap

    def test_without_supports_enabled_a_part_with_an_overhang_keeps_the_plain_gap(self):
        project = self.project_with(mushroom_stl())
        project = self.lay(project, [(1, 1, (50, 50), False), (1, 1, (60, 60), False)])
        status, result = self.arrange(project)
        self.assertEqual(status, 200, result)
        a, b = (footprint(o, 40) for o in result["data"]["objects"])
        self.assertGreaterEqual(a.distance(b), 6 - 1e-3)
        self.assertLess(a.distance(b), 8)

    def test_plate_settings_turn_supports_on_for_that_plate_only(self):
        project = self.project_with(mushroom_stl())
        project = self.lay(project, [(1, 1, (50, 50), False), (1, 1, (60, 60), False)])
        project = self.save(project, lambda d: d["plates"][0].update(overrides=SUPPORTS))
        status, result = self.arrange(project)
        self.assertEqual(status, 200, result)
        a, b = (footprint(o, 40) for o in result["data"]["objects"])
        self.assertGreaterEqual(a.distance(b), 22 - 1e-3)
        self.assertEqual(result["data"]["plates"][0]["overrides"], {"enable_support": "1", "support_type": "tree(auto)"})

    def test_plates_never_mix_filament_choices(self):
        project = self.project_with(cube_stl(20), cube_stl(20))
        project = self.lay(project, [(1, 1, (50, 50), False), (2, 1, (100, 50), False), (1, 2, (50, 50), False), (2, 2, (100, 50), False)])
        project = self.save(project, lambda d: d.update(filaments={"2": GREY}))
        self.assertIsNotNone(projects.plate_mix(project["data"], 1))
        status, result = self.arrange(project)
        self.assertEqual(status, 200, result)
        self.assertEqual(len(result["data"]["plates"]), 2)
        for plate in result["data"]["plates"]:
            self.assertIsNone(projects.plate_mix(result["data"], plate["id"]))
        self.assertEqual(sorted(sorted(ids) for ids in self.by_plate(result).values()), [[1, 3], [2, 4]])

    def test_arranging_one_mixed_plate_moves_the_other_filament_to_a_plate_of_its_own(self):
        project = self.project_with(cube_stl(20), cube_stl(20))
        project = self.lay(project, [(1, 1, (50, 50), False), (2, 1, (100, 50), False)])
        project = self.save(project, lambda d: d.update(filaments={"2": GREY}))
        status, result = self.arrange(project, plate=1)
        self.assertEqual(status, 200, result)
        self.assertEqual(sorted(self.by_plate(result).values()), [[1], [2]])
        self.assertTrue(all(projects.plate_mix(result["data"], p["id"]) is None for p in result["data"]["plates"]))

    def test_a_pinned_object_keeps_its_plate_when_the_filaments_differ(self):
        project = self.project_with(cube_stl(20), cube_stl(20))
        project = self.lay(project, [(1, 1, (50, 50), True), (2, 1, (100, 50), False), (2, 2, (50, 50), False)])
        project = self.save(project, lambda d: d.update(filaments={"2": GREY}))
        status, result = self.arrange(project)
        self.assertEqual(status, 200, result)
        objects = {o["id"]: o for o in result["data"]["objects"]}
        self.assertEqual((objects[1]["plate"], objects[1]["position"]), (1, [50.0, 50.0]))
        self.assertNotEqual(objects[2]["plate"], 1)
        self.assertEqual(objects[2]["plate"], objects[3]["plate"])

    def test_arranging_makes_a_moved_plates_slice_outdated(self):
        project = self.project_with(cube_stl(20))
        project = self.lay(project, [(1, 1, (50, 50), False), (1, 1, (51, 51), False)])
        slice_state = {"1": {"state": "sliced", "fingerprint": projects.fingerprint(project, 1)}}
        projects._mutate(project["id"], "slices", lambda s: s.update(slice_state))
        self.assertFalse(self.call("GET", f"/api/projects/{project['id']}")[1]["slices"]["1"]["outdated"])
        status, result = self.arrange(project)
        self.assertEqual(status, 200, result)
        self.assertTrue(result["slices"]["1"]["outdated"])

    def test_the_layout_can_be_computed_without_changing_the_project(self):
        project = self.project_with(cube_stl(20))
        project = self.lay(project, [(1, 1, (50, 50), False), (1, 2, (50, 50), True), (1, 3, (50, 50), False)])
        layout = projects.arrange_layout(project["id"], project)
        self.assertEqual(self.call("GET", f"/api/projects/{project['id']}")[1]["revision"], project["revision"])
        self.assertEqual([(p["plate"], sorted(p["fixed"]), sorted(i["key"] for i in p["items"])) for p in layout], [(2, [2], [1, 3])])

    def test_orca_can_be_chosen_and_the_choice_is_kept_in_the_project(self):
        project = self.project_with(cube_stl(20), cube_stl(30))
        runner = arrange_runner([(60.0, 60.0, 0.0), (180.0, 150.0, 90.0)])
        with mock.patch.object(slicer, "runner", runner):
            status, result = self.arrange(project, plate=1, engine="orca")
            self.assertEqual(status, 200, result)
            self.assertEqual([(o["position"], o["rotation"][2]) for o in result["data"]["objects"]], [([60.0, 60.0], 0.0), ([180.0, 150.0], 90.0)])
            self.assertEqual(result["data"]["arrange_engine"], "orca")
            self.assertEqual(self.call("GET", f"/api/projects/{project['id']}")[1]["data"]["arrange_engine"], "orca")
            self.assertEqual(self.arrange(result, plate=1)[0], 200)                 # no engine named: the project's choice stands
            self.assertEqual(len(runner.calls), 2)
            status, result = self.arrange(result, plate=1, engine="nester")
            self.assertEqual(status, 200, result)
            self.assertNotIn("arrange_engine", result["data"])
            self.assertEqual(len(runner.calls), 2)                                  # the nester didn't need OrcaSlicer
        self.assertEqual(self.arrange(project, engine="magic")[0], 400)

    def test_orca_is_refused_on_a_plate_holding_pinned_objects(self):
        project = self.project_with(cube_stl(20))
        project = self.lay(project, [(1, 1, (50, 50), False), (1, 1, (120, 120), True), (1, 2, (50, 50), False)])
        runner = arrange_runner([(70.0, 70.0, 0.0)])
        with mock.patch.object(slicer, "runner", runner):
            status, refused = self.arrange(project, plate=1, engine="orca")
            self.assertEqual(status, 400, refused)
            self.assertIn("pinned", refused["error"])
            self.assertEqual(self.arrange(project, plate=2, engine="orca")[0], 200)   # a plate without pins is fine
        after = self.call("GET", f"/api/projects/{project['id']}")[1]
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual({o["id"]: o["position"] for o in after["data"]["objects"] if o["id"] in (1, 2)}, {1: [50.0, 50.0], 2: [120.0, 120.0]})

    def test_orca_arranges_each_plate_when_asked_for_all(self):
        project = self.project_with(cube_stl(20))
        project = self.lay(project, [(1, 1, (50, 50), False), (1, 2, (50, 50), False)])
        runner = arrange_runner([(70.0, 70.0, 0.0)])
        with mock.patch.object(slicer, "runner", runner):
            status, result = self.arrange(project, engine="orca")
        self.assertEqual(status, 200, result)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual([o["position"] for o in result["data"]["objects"]], [[70.0, 70.0], [70.0, 70.0]])

    def test_a_project_saved_without_the_engine_choice_stays_as_it_was(self):
        project = self.project_with(cube_stl(20))
        self.assertNotIn("arrange_engine", project["data"])
        self.assertNotIn("arrange_engine", self.save(project, lambda d: None)["data"])


if __name__ == "__main__":
    unittest.main()
