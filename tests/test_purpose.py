"""Purpose on Models and the project default, and Orient all following it, seen through the project HTTP API."""
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

import database
import jobs
import projects
import slicer
import splits
from tests.test_orient import l_bracket
from tests.test_slicer import cube_stl, make_fake_orca

GEOMETRY = not splits.missing_modules()
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@unittest.skipUnless(GEOMETRY, "geometry libraries not installed")
class PurposeApiTests(unittest.TestCase):
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
        project = projects.create_project("Purpose")
        for data in stls:
            projects.import_model(project["id"], io.BytesIO(data), len(data), "part.stl")

        async def work():
            while await projects.process_next():
                pass
        asyncio.run(work())
        return projects.get_project(project["id"])

    def save(self, project, change):
        data = copy.deepcopy(project["data"])
        change(data)
        return self.call("PATCH", f"/api/projects/{project['id']}", {"data": data, "revision": project["revision"]})

    def reload(self, project):
        return self.call("GET", f"/api/projects/{project['id']}")[1]

    # storage

    def test_a_model_purpose_and_note_save_and_reload(self):
        project = self.project_with(cube_stl(20))
        status, saved = self.save(project, lambda d: d.update(purposes={"1": {"value": "functional", "note": "carries motor thrust"}}))
        self.assertEqual(status, 200, saved)
        self.assertEqual(self.reload(project)["data"]["purposes"], {"1": {"value": "functional", "note": "carries motor thrust"}})

    def test_the_project_default_saves_and_reloads(self):
        project = self.project_with(cube_stl(20))
        status, saved = self.save(project, lambda d: d.update(purpose={"value": "display", "note": "shelf piece"}))
        self.assertEqual(status, 200, saved)
        self.assertEqual(self.reload(project)["data"]["purpose"], {"value": "display", "note": "shelf piece"})

    def test_saving_uses_the_revision_guard(self):
        project = self.project_with(cube_stl(20))
        self.assertEqual(self.save(project, lambda d: d.update(purpose={"value": "draft"}))[0], 200)
        status, _ = self.save(project, lambda d: d.update(purpose={"value": "display"}))   # the same, now old, revision
        self.assertEqual(status, 409)

    def test_a_note_is_limited_to_300_characters(self):
        project = self.project_with(cube_stl(20))
        ok, saved = self.save(project, lambda d: d.update(purposes={"1": {"value": "draft", "note": "x" * 300}}))
        self.assertEqual(ok, 200, saved)
        status, body = self.save(self.reload(project), lambda d: d.update(purposes={"1": {"value": "draft", "note": "x" * 301}}))
        self.assertEqual(status, 400, body)
        self.assertIn("300", body["error"])

    def test_bad_purposes_are_refused(self):
        project = self.project_with(cube_stl(20))
        for bad in ({"purposes": {"1": {"value": "decorative"}}}, {"purposes": {"9": {"value": "draft"}}},
                    {"purposes": {"1": {"value": "draft", "note": 5}}}, {"purpose": {"value": None}}, {"purpose": "draft"},
                    {"purposes": []}):
            status, body = self.save(project, lambda d, bad=bad: d.update(bad))
            self.assertEqual(status, 400, (bad, body))

    def test_a_plain_general_project_default_is_stored_as_nothing(self):
        project = self.project_with(cube_stl(20))
        self.save(project, lambda d: d.update(purpose={"value": "general", "note": ""}))
        self.assertNotIn("purpose", self.reload(project)["data"])

    def test_a_project_saved_before_purpose_loads_and_saves_unchanged(self):
        project = self.project_with(cube_stl(20))
        self.assertNotIn("purpose", project["data"])
        self.assertNotIn("purposes", project["data"])
        status, saved = self.save(project, lambda d: None)
        self.assertEqual(status, 200)
        self.assertNotIn("purpose", saved["data"])
        self.assertNotIn("purposes", saved["data"])

    def test_deleting_a_model_takes_its_purpose_with_it(self):
        project = self.project_with(cube_stl(20), cube_stl(10))
        self.save(project, lambda d: d.update(purposes={"1": {"value": "draft"}, "2": {"value": "display"}}))
        status, after = self.call("DELETE", f"/api/projects/{project['id']}/models/1")
        self.assertEqual(status, 200, after)
        self.assertEqual(list(after["data"]["purposes"]), ["2"])

    # inheritance

    def test_an_absent_purpose_reads_as_general_and_follows_the_project_default(self):
        project = self.project_with(cube_stl(20), cube_stl(10))
        self.assertEqual(projects.effective_purpose(project["data"], 1), {"value": "general", "note": "", "source": "default"})
        saved = self.save(project, lambda d: d.update(purpose={"value": "functional", "note": "all brackets"},
                                                      purposes={"2": {"value": "display", "note": ""}}))[1]
        self.assertEqual(projects.effective_purpose(saved["data"], 1), {"value": "functional", "note": "all brackets", "source": "project"})
        self.assertEqual(projects.effective_purpose(saved["data"], 2), {"value": "display", "note": "", "source": "model"})

    def test_a_model_can_choose_general_over_a_different_project_default(self):
        project = self.project_with(cube_stl(20))
        saved = self.save(project, lambda d: d.update(purpose={"value": "functional"}, purposes={"1": {"value": "general"}}))[1]
        self.assertEqual(projects.effective_purpose(saved["data"], 1)["value"], "general")

    def test_copies_share_their_models_purpose(self):
        project = self.project_with(cube_stl(20))
        saved = self.save(project, lambda d: d.update(purposes={"1": {"value": "draft"}}))[1]
        status, more = self.call("POST", f"/api/projects/{project['id']}/copies", {"object": 1, "copies": 3, "revision": saved["revision"]})
        self.assertEqual(status, 200, more)
        self.assertEqual(len(more["data"]["objects"]), 3)
        self.assertEqual(more["data"]["purposes"], {"1": {"value": "draft", "note": ""}})
        for obj in more["data"]["objects"]:
            self.assertEqual(projects.effective_purpose(more["data"], obj["model"])["value"], "draft")

    # Orient all

    def orient(self, project, **body):
        status, result = self.call("POST", f"/api/projects/{project['id']}/orient-all", body)
        self.assertEqual(status, 200, result)
        return result

    def test_orient_all_default_mode_follows_the_effective_purpose(self):
        project = self.project_with(l_bracket())
        saved = self.save(project, lambda d: d.update(purposes={"1": {"value": "functional"}}))[1]
        result = self.orient(saved)
        self.assertEqual(result["orient_purpose"]["modes"], {"1": "strength"})
        explicit = self.orient(self.reload(project), mode="strength")
        self.assertEqual([o["rotation"] for o in result["data"]["objects"]], [o["rotation"] for o in explicit["data"]["objects"]])

    def test_general_and_draft_give_least_support(self):
        project = self.project_with(l_bracket())
        self.assertEqual(self.orient(project)["orient_purpose"]["modes"], {"1": "least-support"})
        saved = self.save(project, lambda d: d.update(purposes={"1": {"value": "draft"}}))[1]
        self.assertEqual(self.orient(saved)["orient_purpose"]["modes"], {"1": "least-support"})

    def test_display_is_looks_but_without_a_favoured_face_runs_with_looks_weights_and_says_so(self):
        project = self.project_with(l_bracket())
        saved = self.save(project, lambda d: d.update(purpose={"value": "display"}))[1]
        result = self.orient(saved)
        self.assertEqual(result["orient_purpose"]["modes"], {"1": "least-support"})   # looks and least-support weigh alike
        self.assertEqual(result["orient_purpose"]["looks_without_face"], [1])

    def test_an_explicit_mode_overrides_purpose(self):
        project = self.project_with(l_bracket())
        saved = self.save(project, lambda d: d.update(purposes={"1": {"value": "functional"}}))[1]
        self.assertEqual(self.orient(saved, mode="least-support")["orient_purpose"]["modes"], {"1": "least-support"})

    def test_each_object_follows_its_own_models_purpose(self):
        project = self.project_with(l_bracket(), l_bracket())
        saved = self.save(project, lambda d: d.update(purposes={"1": {"value": "functional"}}))[1]
        self.assertEqual(self.orient(saved)["orient_purpose"]["modes"], {"1": "strength", "2": "least-support"})


if __name__ == "__main__":
    unittest.main()
