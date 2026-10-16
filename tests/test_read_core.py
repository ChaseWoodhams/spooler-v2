"""The socket-free read core: what the read-only Spooler MCP server imports.

The server may reach project data, the Outdated flag, placement and the settings helpers, but never `jobs`, the printer
modules, Spoolman, push or MQTT, and never opens a socket. These tests run in a clean interpreter (`python -I`) so what
this process happened to import cannot hide a dependency.
"""
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import database
import datapaths
import errors
import jobs
import placement_read
import project_read
import projects
from tests.test_slice_snapshots import SliceCase

ROOT = Path(__file__).resolve().parent.parent

# The modules the MCP server builds on: the new read core and the pure modules it already used.
CORE = ["errors", "datapaths", "profile_read", "hold_read", "project_read", "placement_read",
        "effective_settings", "brick_post", "setting_rules", "time_estimate", "problem_kinds", "packing", "orientation"]
# Nothing here may be loaded, directly or through another module.
FORBIDDEN = ["jobs", "printers", "spoolman", "push", "outbox", "print_history", "snapshots", "state", "holds", "preflight",
             "slicer", "projects", "splits", "farm", "discovery", "ws_handler", "http_handler", "server", "aiomqtt"]

SCRIPT = r"""
import importlib, json, socket, sys
sys.path.insert(0, sys.argv[1])

class NoSocket(socket.socket):
    def __init__(self, *args, **kwargs):
        raise AssertionError("a socket was opened")

socket.socket = NoSocket
for name in json.loads(sys.argv[2]):
    importlib.import_module(name)
print(json.dumps(sorted(sys.modules)))
"""


def clean_interpreter(modules: list, data_dir: str) -> list:
    """The names in sys.modules after importing `modules` in `python -I` with socket.socket made to fail."""
    done = subprocess.run([sys.executable, "-I", "-c", SCRIPT, str(ROOT), json.dumps(modules)], capture_output=True, text=True,
                          env={**os.environ, "DATA_DIR": data_dir}, timeout=120)
    if done.returncode:
        raise AssertionError(f"import failed in a clean interpreter:\n{done.stderr[-2000:]}")
    return json.loads(done.stdout.strip().splitlines()[-1])


class ReadCoreImportTests(unittest.TestCase):
    def test_the_read_core_loads_no_job_printer_spoolman_or_push_code_and_opens_no_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            loaded = clean_interpreter(CORE, tmp)
        for name in CORE:
            self.assertIn(name, loaded)
        leaked = [m for m in loaded if m.split(".")[0] in FORBIDDEN]
        self.assertEqual(leaked, [])


READ_SCRIPT = r"""
import json, socket, sys
sys.path.insert(0, sys.argv[1])

class NoSocket(socket.socket):
    def __init__(self, *args, **kwargs):
        raise AssertionError("a socket was opened")

socket.socket = NoSocket
import datapaths, placement_read, project_read
data_dir = sys.argv[2]
datapaths.configure_all(data_dir)
conn = datapaths.open_readonly(datapaths.database_file(data_dir))
record = project_read.read_project(conn, 1)
ledger = project_read.read_ledger(conn, 1)
print(json.dumps({"modules": sorted(sys.modules), "name": record["name"], "slice": record["slices"]["1"],
                  "status": ledger.status(1), "features": placement_read.plate_features(1, record, 1)}))
"""


class ReadCoreReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="read core ")   # a space in the path: the read-only URI must cope
        self.dir = Path(self.tmp.name)
        database.configure(self.dir / "spooler.db")
        database.migrate()

    def tearDown(self):
        database.configure(database.DB_FILE)
        self.tmp.cleanup()

    def add_project(self):
        data = {"profile": {"bundle_id": None, "process": {}, "filament": {}}, "bed": "textured",
                "plates": [{"id": 1, "name": "Plate 1"}], "objects": []}
        slices = {"1": {"state": "sliced", "inputs_version": project_read.INPUTS_VERSION, "fingerprint": "abc"}}
        with database.transaction() as conn:
            conn.execute("INSERT INTO projects (id, name, data, models, slices, revision, created_at, updated_at) "
                         "VALUES (1, 'Shelf', ?, '[]', ?, 3, 1.0, 1.0)", (json.dumps(data), json.dumps(slices)))

    def test_the_read_only_connection_reads_and_cannot_write(self):
        self.add_project()
        conn = datapaths.open_readonly(self.dir / "spooler.db")
        try:
            self.assertEqual(conn.execute("SELECT name FROM projects").fetchone()["name"], "Shelf")
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE projects SET name = 'Changed'")
        finally:
            conn.close()
        with database.transaction() as conn:
            self.assertEqual(conn.execute("SELECT name FROM projects").fetchone()["name"], "Shelf")

    def test_a_missing_database_is_an_error_not_a_new_empty_file(self):
        with self.assertRaises(sqlite3.OperationalError):
            datapaths.open_readonly(self.dir / "nothing.db")
        self.assertFalse((self.dir / "nothing.db").exists())

    def test_a_clean_interpreter_reads_a_project_its_slice_and_the_queue_without_a_socket_or_job_code(self):
        self.add_project()
        done = subprocess.run([sys.executable, "-I", "-c", READ_SCRIPT, str(ROOT), str(self.dir)], capture_output=True, text=True,
                              env={**os.environ, "DATA_DIR": str(self.dir)}, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        result = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(result["name"], "Shelf")
        self.assertTrue(result["slice"]["outdated"])   # no locked profile to judge it by
        self.assertEqual(result["slice"]["outdated_reason"], "inputs-unresolved")
        self.assertEqual(result["status"], "unsliced")
        self.assertIsNone(result["features"])
        self.assertEqual([m for m in result["modules"] if m.split(".")[0] in FORBIDDEN], [])


class ReadCoreParityTests(SliceCase):
    """What the read core reports through a read-only connection is what the app reports."""

    def reader(self):
        return contextlib.closing(datapaths.open_readonly(database._path))   # sqlite3's own `with` leaves the file open

    @staticmethod
    def same(a: dict, b: dict):
        return {k: v for k, v in a.items() if k != "server_time"}, {k: v for k, v in b.items() if k != "server_time"}

    async def test_the_project_and_its_outdated_flags_match_the_apps_after_a_slice_and_after_an_edit(self):
        project = await self.slice((await self.petg_project())["id"])
        with self.reader() as conn:
            app, core = self.same(projects.get_project(project["id"]), project_read.read_project(conn, project["id"]))
        self.assertEqual(core, app)
        self.assertIs(core["slices"]["1"]["outdated"], False)

        self.change(project["id"], lambda d: d["plates"][0].update(overrides={"wall_loops": "9"}))
        with self.reader() as conn:
            app, core = self.same(projects.get_project(project["id"]), project_read.read_project(conn, project["id"]))
        self.assertEqual(core, app)
        self.assertIs(core["slices"]["1"]["outdated"], True)

    async def test_an_unknown_project_is_a_404_like_the_apps(self):
        with self.reader() as conn, self.assertRaises(jobs.JobError) as missing:
            project_read.read_project(conn, 999)
        self.assertEqual(missing.exception.status, 404)

    async def test_the_settings_helpers_moved_out_of_slicer_are_the_same_functions_slicer_still_offers(self):
        import profile_read
        import slicer
        for name in ("bundle_settings", "normalize_overrides", "settings_schema", "verify_bundle", "BLOCKED_SETTINGS"):
            self.assertIs(getattr(slicer, name), getattr(profile_read, name), name)
        self.assertIs(jobs.JobError, errors.JobError)


if __name__ == "__main__":
    unittest.main()
