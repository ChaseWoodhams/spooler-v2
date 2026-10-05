"""Quick print intake of 3MF and G-code: objects and plates are counted before anything is created, a single object stays
quick, anything bigger becomes a normal Project, and G-code queues without slicing. Everything goes through the HTTP API."""
import asyncio
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import numpy as np

import database
import jobs
import orca_3mf
import projects
import slicer
import splits
import state
from tests.test_jobs import GCODE, FakePrinter
from tests.test_orca_3mf import CUBE, TRIS
from tests.test_slicer import fake_runner, make_fake_orca

GEOMETRY = not splits.missing_modules()
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class QuickIntakeApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from http_handler import SPHandler
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._saved_printers = state.printers
        database.configure(self.dir / "spooler.db")
        jobs.init(self.dir)
        slicer.configure(self.dir, orca_path=make_fake_orca(self.dir / "tools"))
        slicer.init()
        splits.configure(self.dir)
        projects.configure(self.dir)
        projects.init()
        state.printers = {"p1": FakePrinter("p1", "Printer 1")}
        patcher = mock.patch("http_handler._auth_ok", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SPHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    async def asyncTearDown(self):
        for task in list(jobs._start_tasks.values()):
            task.cancel()
        await asyncio.sleep(0)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        state.printers = self._saved_printers
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        slicer.configure()
        splits.configure()
        projects.configure()
        self._tmp.cleanup()

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with _opener.open(request, timeout=20) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    async def settle(self):
        with mock.patch.object(slicer, "runner", fake_runner()):
            while await splits.process_next() or await projects.process_next():
                pass

    async def quick_upload(self, data: bytes, filename: str, extra: str = ""):
        status, project = self.call("POST", f"/api/projects/upload?filename={filename}&quick=1{extra}", raw=data)
        self.assertEqual(status, 200, project)
        await self.settle()
        return project

    @staticmethod
    def plain_3mf(objects: int, disabled: int = 0) -> bytes:
        """A 3MF from a generic tool: a mesh and build items, no Orca metadata."""
        verts = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in (CUBE + 10))
        tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in TRIS)
        items = "".join(f'<item objectid="1" transform="1 0 0 0 1 0 0 0 1 {50 * n} 0 0"/>' for n in range(objects))
        items = "".join(f'<item objectid="1" printable="0" transform="1 0 0 0 1 0 0 0 1 {50 * n} 90 0"/>' for n in range(disabled)) + items
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(orca_3mf.MODEL_PATH, f'<model unit="millimeter"><resources><object id="1"><mesh><vertices>{verts}</vertices>'
                             f'<triangles>{tris}</triangles></mesh></object></resources><build>{items}</build></model>')
        return buffer.getvalue()

    @staticmethod
    def orca_3mf(names=("Bench",), extra_plates=0) -> bytes:
        objects = [{"name": n, "verts": CUBE, "tris": TRIS, "matrix": np.eye(3), "shift": np.array([60.0 + 40 * i, 80.0, 2.0]),
                    "plate": 1, "settings": {}} for i, n in enumerate(names)]
        plates = [{"id": n, "name": ""} for n in range(1, 2 + extra_plates)]
        config = {"printer_settings_id": "Elegoo Centauri Carbon 0.4 nozzle", "print_settings_id": "Some User Preset",
                  "filament_settings_id": ["Elegoo PLA @ECC"], "wall_loops": "5", "layer_height": "0.28", "curr_bed_type": "High Temp Plate"}
        return orca_3mf.build(objects, plates, config)

    def visible_ids(self):
        return [p["id"] for p in self.call("GET", "/api/projects")[1]["projects"]]

    async def test_a_single_object_plain_3mf_stays_quick(self):
        project = await self.quick_upload(self.plain_3mf(1), "part.3mf")
        self.assertIs(project["quick"], True)
        self.assertNotIn("routed", project)
        intake = self.call("GET", f"/api/projects/{project['id']}/intake")[1]
        self.assertEqual((intake["state"], intake["can_print"], intake["kind"]), ("ready", True, "model"))
        self.assertEqual(self.visible_ids(), [])

    async def test_a_disabled_object_does_not_make_a_single_object_3mf_a_multi_object_one(self):
        project = await self.quick_upload(self.plain_3mf(1, disabled=2), "part.3mf")
        self.assertIs(project["quick"], True)
        self.assertNotIn("routed", project)
        self.assertEqual(self.call("GET", f"/api/projects/{project['id']}/intake")[1]["can_print"], True)

    async def test_a_multi_object_plain_3mf_is_routed_to_a_visible_project_not_merged(self):
        project = await self.quick_upload(self.plain_3mf(2), "pair.3mf")
        self.assertIs(project["quick"], False)
        self.assertEqual(project["routed"]["to"], "project")
        self.assertIn("2 objects", project["routed"]["reason"])
        self.assertEqual(self.visible_ids(), [project["id"]])
        self.assertEqual(len(project["data"]["objects"]), 2)          # two objects, not one merged mesh
        # a normal (not quick) upload of the same file no longer merges them either
        status, normal = self.call("POST", "/api/projects/upload?filename=pair.3mf", raw=self.plain_3mf(2))
        self.assertEqual((status, len(normal["data"]["objects"])), (200, 2))

    async def test_a_single_object_orca_3mf_passes_intake_with_its_settings_as_starting_values(self):
        project = await self.quick_upload(self.orca_3mf(), "bench.3mf")
        self.assertIs(project["quick"], True)
        self.assertNotIn("routed", project)
        self.assertEqual(self.visible_ids(), [])
        profile = project["data"]["profile"]
        self.assertEqual((profile["process"]["wall_loops"], profile["process"]["layer_height"], project["data"]["bed"]),
                         ("5", "0.28", "smooth"))
        intake = self.call("GET", f"/api/projects/{project['id']}/intake")[1]
        self.assertEqual((intake["state"], intake["can_print"], intake["kind"], intake["file"]), ("ready", True, "model", "bench.stl"))
        # a change made in the dialog overrides the embedded value
        data = json.loads(json.dumps(project["data"]))
        data["profile"]["process"]["wall_loops"] = "3"
        status, changed = self.call("PATCH", f"/api/projects/{project['id']}", {"data": data, "revision": project["revision"]})
        self.assertEqual(status, 200, changed)
        self.assertEqual(changed["data"]["profile"]["process"], {"wall_loops": "3", "layer_height": "0.28"})
        # and it slices and queues like any quick model
        self.assertEqual(self.call("POST", f"/api/projects/{project['id']}/print", {"printer_id": "p1"})[0], 200)
        await self.settle()
        self.assertEqual([(j["plate_id"], j["state"]) for j in projects.project_jobs(project["id"])], [(1, "queued")])

    async def test_an_orca_3mf_with_two_objects_goes_to_a_visible_project(self):
        project = await self.quick_upload(self.orca_3mf(("Bench", "Lid")), "set.3mf")
        self.assertIs(project["quick"], False)
        self.assertEqual(project["routed"]["to"], "project")
        self.assertEqual(self.visible_ids(), [project["id"]])
        self.assertEqual(len(project["data"]["objects"]), 2)

    async def test_an_orca_3mf_with_two_plates_goes_to_a_visible_project(self):
        project = await self.quick_upload(self.orca_3mf(extra_plates=1), "plates.3mf")
        self.assertIs(project["quick"], False)
        self.assertIn("2 plates", project["routed"]["reason"])
        self.assertEqual(len(project["data"]["plates"]), 2)
        self.assertEqual(self.visible_ids(), [project["id"]])

    async def test_a_broken_3mf_is_refused_before_any_project_exists(self):
        status, error = self.call("POST", "/api/projects/upload?filename=bad.3mf&quick=1", raw=b"not a zip")
        self.assertGreaterEqual(status, 400, error)
        self.assertEqual(self.call("GET", "/api/projects?include_quick=1")[1]["projects"], [])

    async def test_gcode_makes_a_quick_project_that_queues_without_slicing(self):
        project = await self.quick_upload(GCODE, "benchy.gcode", "&bed=smooth")
        self.assertIs(project["quick"], True)
        entry = project["slices"]["1"]
        self.assertEqual((entry["state"], entry["imported"], project["data"]["bed"]), ("sliced", True, "smooth"))
        self.assertEqual(self.visible_ids(), [])
        intake = self.call("GET", f"/api/projects/{project['id']}/intake")[1]
        self.assertEqual((intake["state"], intake["kind"], intake["can_print"], intake["file"]), ("ready", "gcode", True, "benchy.gcode"))
        self.assertEqual(intake["estimate"]["seconds"], 3723)
        self.assertEqual(self.call("POST", f"/api/projects/{project['id']}/print", {})[0], 409)   # needs a printer
        with mock.patch.object(slicer, "runner", side_effect=AssertionError("G-code must not be sliced")):
            status, sent = self.call("POST", f"/api/projects/{project['id']}/print", {"printer_id": "p1", "leveling": True})
            self.assertEqual(status, 200, sent)
        self.assertEqual(sent["slices"]["1"]["state"], "sliced")
        (job,) = projects.project_jobs(project["id"])
        self.assertEqual((job["state"], job["printer_id"], job["plate_id"]), ("queued", "p1", 1))
        self.assertTrue(job["print_opts"]["leveling"])
        status, check = self.call("GET", f"/api/jobs/{job['id']}/preflight")
        self.assertEqual(status, 200, check)


if __name__ == "__main__":
    unittest.main()
