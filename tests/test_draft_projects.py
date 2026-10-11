"""Draft projects: the hidden home of files uploaded before Create. Created at the first drop, promoted at Create,
swept when abandoned. Everything runs in a temp dir with a fake clock; the real data directory is never touched."""
import json
import sqlite3
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
import state
from tests.test_jobs import FakePrinter
from tests.test_slicer import make_fake_orca

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
HOUR = 3600.0
T0 = 1_000_000.0


class DraftProjectTests(unittest.TestCase):
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
        self.clock = T0
        clock = mock.patch.object(projects.time, "time", side_effect=lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SPHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

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

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with _opener.open(request, timeout=20) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def draft(self, name="Untitled project"):
        status, project = self.call("POST", "/api/projects", {"name": name, "draft": True})
        self.assertEqual(status, 200, project)
        return project

    def visible_ids(self):
        return [p["id"] for p in self.call("GET", "/api/projects")[1]["projects"]]

    def put_file(self, project_id):
        """A source file inside the project's folder."""
        path = projects.project_dir(project_id) / "models" / "source.stl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"solid")
        return path

    def ready_model(self, project_id):
        projects._mutate(project_id, "models", lambda m: m.append({"id": 1, "name": "a", "file": "a.stl", "state": "ready", "import": [1.0, "z"]}))

    def attach_split(self, project_id):
        with database.transaction() as conn:
            conn.execute("INSERT INTO splits (id, source_name, source_file, state, settings, created_at, updated_at) "
                         "VALUES (1, 'big.stl', 'big.stl', 'ready', '{}', 1, 1)")
            conn.execute("UPDATE projects SET split_id = 1 WHERE id = ?", (project_id,))

    # ── hidden ────────────────────────────────────────────────────────────────

    def test_a_draft_is_created_hidden_and_flagged(self):
        draft = self.draft()
        self.assertIs(draft["draft"], True)
        self.assertEqual(self.visible_ids(), [])
        self.assertEqual(projects.list_projects(include_quick=True), [])
        self.assertIs(self.call("GET", f"/api/projects/{draft['id']}")[1]["draft"], True)

    def test_a_normal_project_is_not_a_draft(self):
        status, project = self.call("POST", "/api/projects", {"name": "Real"})
        self.assertEqual((status, project["draft"]), (200, False))
        self.assertEqual(self.visible_ids(), [project["id"]])

    def test_a_drafts_split_is_marked_so_it_is_not_offered_as_parts_to_add(self):
        draft = self.draft()
        self.attach_split(draft["id"])
        listed = splits.list_splits()
        self.assertEqual((listed[0]["project_id"], listed[0]["project_draft"]), (draft["id"], True))

    # ── promote ───────────────────────────────────────────────────────────────

    def test_create_names_and_reveals_the_draft_keeping_its_models(self):
        draft = self.draft()
        projects._mutate(draft["id"], "models", lambda m: m.append({"id": 1, "name": "bracket.stl", "file": "bracket.stl", "state": "ready", "import": [1.0, "z"]}))
        status, result = self.call("POST", f"/api/projects/{draft['id']}/create", {"name": "  Brackets  "})
        self.assertEqual(status, 200, result)
        self.assertEqual((result["project"]["name"], result["project"]["draft"]), ("Brackets", False))
        self.assertEqual([m["name"] for m in result["project"]["models"]], ["bracket.stl"])
        self.assertEqual(result["step"], "plates")
        self.assertEqual(self.visible_ids(), [draft["id"]])

    def test_create_on_a_project_that_is_not_a_draft_is_refused(self):
        project = self.call("POST", "/api/projects", {"name": "Real"})[1]
        self.assertEqual(self.call("POST", f"/api/projects/{project['id']}/create", {"name": "X"})[0], 409)

    def test_create_with_an_unplaced_split_starts_at_the_model_step(self):
        draft = self.draft()
        self.attach_split(draft["id"])
        self.assertEqual(self.call("POST", f"/api/projects/{draft['id']}/create", {"name": "Big"})[1]["step"], "model")

    # ── sweep ─────────────────────────────────────────────────────────────────

    def test_the_sweep_deletes_an_idle_draft_with_its_files(self):
        draft = self.draft()
        path = self.put_file(draft["id"])
        self.clock = T0 + 5 * HOUR
        self.assertEqual(projects.sweep_drafts(idle_hours=4), [draft["id"]])
        self.assertFalse(path.exists())
        self.assertFalse(projects.project_dir(draft["id"]).exists())
        self.assertEqual(self.call("GET", f"/api/projects/{draft['id']}")[0], 404)

    def test_the_sweep_leaves_a_recently_used_draft_alone(self):
        draft = self.draft()
        self.clock = T0 + 3 * HOUR
        self.assertEqual(projects.sweep_drafts(idle_hours=4), [])
        self.clock = T0 + 5 * HOUR
        projects._mutate(draft["id"], "models", lambda m: m.append({"id": 1}))   # activity bumps updated_at
        self.assertEqual(projects.sweep_drafts(idle_hours=4), [])
        self.assertEqual(self.call("GET", f"/api/projects/{draft['id']}")[0], 200)

    def test_the_sweep_never_deletes_real_quick_or_promoted_projects(self):
        real_empty = projects.create_project("Empty but visible")
        quick = projects.create_project("Quick", quick=True)
        promoted = self.draft()
        self.ready_model(promoted["id"])
        self.call("POST", f"/api/projects/{promoted['id']}/create", {"name": "Promoted"})
        stale = self.draft()
        self.clock = T0 + 1000 * HOUR
        self.assertEqual(projects.sweep_drafts(idle_hours=4), [stale["id"]])
        for pid in (real_empty["id"], quick["id"], promoted["id"]):
            self.assertEqual(self.call("GET", f"/api/projects/{pid}")[0], 200)
            self.assertTrue(projects.project_dir(pid).exists())

    def test_the_idle_limit_comes_from_the_environment(self):
        draft = self.draft()
        self.clock = T0 + 30 * HOUR
        with mock.patch.dict("os.environ", {"DRAFT_IDLE_HOURS": "48"}):
            self.assertEqual(projects.sweep_drafts(), [])
        with mock.patch.dict("os.environ", {"DRAFT_IDLE_HOURS": "12"}):
            self.assertEqual(projects.sweep_drafts(), [draft["id"]])

    # ── beacon ────────────────────────────────────────────────────────────────

    def test_the_beacon_route_deletes_a_draft(self):
        draft = self.draft()
        self.put_file(draft["id"])
        self.assertEqual(self.call("POST", f"/api/projects/{draft['id']}/discard-draft")[0], 200)
        self.assertEqual(self.call("GET", f"/api/projects/{draft['id']}")[0], 404)
        self.assertFalse(projects.project_dir(draft["id"]).exists())

    def test_the_beacon_route_refuses_anything_that_is_not_a_draft(self):
        real = self.call("POST", "/api/projects", {"name": "Real"})[1]
        quick = projects.create_project("Q", quick=True)
        for pid in (real["id"], quick["id"]):
            self.assertEqual(self.call("POST", f"/api/projects/{pid}/discard-draft")[0], 409)
            self.assertEqual(self.call("GET", f"/api/projects/{pid}")[0], 200)

    # ── stale draft ids ───────────────────────────────────────────────────────

    def test_a_tab_holding_a_swept_drafts_id_is_refused_once_the_id_is_reused(self):
        old = self.draft()
        projects.delete_project(old["id"])
        self.clock += 10
        real = self.call("POST", "/api/projects", {"name": "Real"})[1]
        self.assertEqual(real["id"], old["id"])   # SQLite hands the freed id to the next project
        token = old["created_at"]
        base = f"/api/projects/{old['id']}"
        self.assertEqual(self.call("GET", f"{base}/intake-files?draft_token={token}")[0], 409)
        self.assertEqual(self.call("DELETE", f"{base}/models/1?draft_token={token}")[0], 409)
        for tail in ("copies", "models/1/retry", "models/1/settings", "create"):
            self.assertEqual(self.call("POST", f"{base}/{tail}", {"draft_token": token, "name": "X", "object": 1, "copies": 2})[0], 409, tail)
        self.assertEqual(self.call("POST", f"{base}/discard-draft?draft_token={token}")[0], 409)
        self.assertEqual(self.call("GET", base)[1]["name"], "Real")

    def test_a_reused_id_that_is_a_newer_draft_is_refused_too_and_the_right_token_is_accepted(self):
        old = self.draft()
        projects.delete_project(old["id"])
        self.clock += 10
        newer = self.draft()
        base = f"/api/projects/{newer['id']}/intake-files"
        self.assertEqual(self.call("GET", f"{base}?draft_token={old['created_at']}")[0], 409)
        self.assertEqual(self.call("GET", f"{base}?draft_token={newer['created_at']}")[0], 200)

    # ── a concurrent Create cannot lose its project ───────────────────────────

    def test_discarding_a_draft_that_was_promoted_meanwhile_keeps_the_project(self):
        draft = self.draft()
        self.ready_model(draft["id"])
        stale = projects.get_project(draft["id"])   # what a slow discard read before Create landed
        self.call("POST", f"/api/projects/{draft['id']}/create", {"name": "Kept"})
        with mock.patch.object(projects, "get_project", return_value=stale):
            with self.assertRaises(jobs.JobError):
                projects.discard_draft(draft["id"])
            self.assertEqual(projects.sweep_drafts(idle_hours=0), [])
        self.assertEqual(projects.get_project(draft["id"])["name"], "Kept")
        self.assertTrue(projects.project_dir(draft["id"]).exists())

    # ── migration ─────────────────────────────────────────────────────────────

    def test_the_migration_is_additive_and_existing_rows_are_not_drafts(self):
        path = self.dir / "old.db"
        conn = sqlite3.connect(path)
        for number, sql in enumerate(database.MIGRATIONS[:10], start=1):
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
        conn.execute("INSERT INTO projects (id, name, created_at, updated_at) VALUES (1, 'Old', 1, 1)")
        conn.commit()
        conn.close()
        self.clock = 1.7e9   # the pre-migration backup is a zip, which cannot date a file before 1980
        database.configure(path)
        projects.init()
        self.assertIs(projects.get_project(1)["draft"], False)
        self.assertEqual([p["id"] for p in projects.list_projects()], [1])
