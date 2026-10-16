"""Upgrade of a populated pre-rework database to the additive workflow schema, seen through the HTTP API."""
import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import database
import jobs
import print_history
import state
from http_handler import SPHandler
from tests.test_jobs import FakePrinter

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
OLD_VERSION = 6  # the schema before the workflow rework


def build_old_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    for number, sql in enumerate(database.MIGRATIONS[:OLD_VERSION], start=1):
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
    conn.execute("INSERT INTO projects (id, name, created_at, updated_at) VALUES (1, 'SEAT', 1, 1)")
    conn.execute("INSERT INTO jobs (id, name, state, printer_id, source, file_name, project_id, plate_id, created_at, updated_at) "
                 "VALUES (1, 'plate 4', 'completed', 'p1', 'upload', 'a.gcode', 1, 4, 1, 1)")
    conn.execute("INSERT INTO jobs (id, name, state, printer_id, source, file_name, created_at, updated_at) "
                 "VALUES (2, 'loose', 'completed', 'p1', 'printer', 'b.gcode', 1, 1)")
    conn.execute("INSERT INTO prints (id, printer_id, printer_name, job_id, file_name, started_at, ended_at, outcome) "
                 "VALUES (1, 'p1', 'Printer 1', 1, 'a.gcode', 10, 20, 'completed')")
    conn.execute("INSERT INTO prints (id, printer_id, printer_name, job_id, file_name, started_at, ended_at, outcome) "
                 "VALUES (2, 'p1', 'Printer 1', 2, 'b.gcode', 30, 40, 'failed')")
    conn.execute("INSERT INTO prints (id, printer_id, printer_name, file_name, started_at, ended_at, outcome) "
                 "VALUES (3, 'p2', 'Printer 2', 'c.gcode', 50, 60, 'cancelled')")
    conn.commit()
    conn.close()


class AdditiveSchemaTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.db = self.dir / "spooler.db"
        build_old_database(self.db)
        database.configure(self.db)
        self._saved = (state.printers, state.loop)
        state.printers = {"p1": FakePrinter()}
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        state.loop = self.loop
        self.addCleanup(self.loop.call_soon_threadsafe, self.loop.stop)

    def tearDown(self):
        if hasattr(self, "server"):
            self.server.shutdown()
            self.server.server_close()
        state.printers, state.loop = self._saved
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        self._tmp.cleanup()

    def start(self):
        jobs.init(self.dir)
        print_history.init()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SPHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self, path):
        with _opener.open(self.base + path, timeout=10) as resp:
            return json.loads(resp.read())

    def test_upgrade_keeps_rows_and_applies_defaults(self):
        self.start()
        prints = {p["id"]: p for p in self.get("/api/prints")["prints"]}
        self.assertEqual({i: p["outcome"] for i, p in prints.items()}, {1: "completed", 2: "failed", 3: "cancelled"})
        self.assertEqual(prints[1]["file_name"], "a.gcode")
        for p in prints.values():
            self.assertEqual(p["failure_reasons"], [])
            self.assertFalse(p["outcome_unconfirmed"])
        # plate link only where the Job already had one
        self.assertEqual((prints[1]["project_id"], prints[1]["plate_id"]), (1, 4))
        self.assertEqual((prints[2]["project_id"], prints[2]["plate_id"]), (None, None))
        self.assertEqual((prints[3]["project_id"], prints[3]["plate_id"]), (None, None))
        listing = {j["id"]: j for j in self.get("/api/jobs")["jobs"]}
        self.assertEqual((listing[1]["project_id"], listing[1]["plate_id"]), (1, 4))

    def test_existing_projects_default_to_not_quick_and_stay_listed(self):
        self.start()
        listed = self.get("/api/projects")["projects"]
        self.assertEqual([p["name"] for p in listed], ["SEAT"])
        self.assertIs(self.get("/api/projects/1")["quick"], False)

    def test_new_structures_exist_with_defaults(self):
        self.start()
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], len(database.MIGRATIONS))
        settings = dict(conn.execute("SELECT printer_id, auto_start FROM printer_settings"))
        self.assertEqual(settings, {"p1": 1, "p2": 1})  # on where a printer already has history
        for table in ("bed_cleared", "job_holds", "remembered_defaults", "slice_log"):
            self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT count(*) FROM slices WHERE imported = 1").fetchone()[0], 0)
        conn.close()

    def test_backup_taken_before_first_start_on_new_schema(self):
        self.start()
        archives = list((self.dir / "backups").glob("spooler-backup-*.zip"))
        self.assertEqual(len(archives), 1)
        with zipfile.ZipFile(archives[0]) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            self.assertEqual(manifest["reason"], "pre-migration")
            copy = self.dir / "copy.db"
            copy.write_bytes(zf.read("sqlite/spooler/spooler.db"))
        conn = sqlite3.connect(copy)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], OLD_VERSION)
        self.assertEqual(conn.execute("SELECT count(*) FROM prints").fetchone()[0], 3)
        conn.close()

    def test_no_backup_when_already_current_or_fresh(self):
        self.start()
        database.migrate()
        self.assertEqual(len(list((self.dir / "backups").glob("*.zip"))), 1)
        fresh = Path(tempfile.mkdtemp())
        database.configure(fresh / "spooler.db")
        database.migrate()
        self.assertFalse((fresh / "backups").exists())

    def test_failed_backup_stops_the_migration(self):
        with mock.patch("backup.create_backup", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                database.migrate()
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], OLD_VERSION)
        conn.close()


class CalibrationLogMigrationTests(unittest.TestCase):
    """The calibration migration widens slice_log without losing a row and backfills bundle ids where a slice can be matched."""

    def test_existing_slice_log_rows_survive_and_matchable_ones_get_a_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "spooler.db"
            before = next(n for n, sql in enumerate(database.MIGRATIONS) if "support_log" in sql)   # the schema before the migration
            conn = sqlite3.connect(db)
            for number, sql in enumerate(database.MIGRATIONS[:before], start=1):
                conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
            conn.execute("INSERT INTO projects (id, name, created_at, updated_at) VALUES (1, 'P', 1, 1)")
            for slice_id, bundle, created in ((1, "aaaaaaaaaaaa", 100), (2, "bbbbbbbbbbbb", 1000)):
                conn.execute("INSERT INTO slices (id, state, source_name, source_file, bundle_id, bed_type, created_at) "
                             "VALUES (?, 'done', 'a', 'a', ?, 'textured', ?)", (slice_id, bundle, created))
                conn.execute("INSERT INTO jobs (name, state, printer_id, source, file_name, project_id, plate_id, slice_id, created_at, updated_at) "
                             "VALUES ('plate', 'completed', 'p1', 'upload', 'a.gcode', 1, 1, ?, 1, 1)", (slice_id,))
            for plate, created in ((1, 990), (2, 990), (None, 990)):   # the second has no matching plate, the third no project plate
                conn.execute("INSERT INTO slice_log (project_id, plate_id, estimate_s, created_at) VALUES (?, ?, 600, ?)",
                             (1 if plate else None, plate, created))
            conn.commit()
            conn.close()
            database.configure(db)
            try:
                database.migrate()
                conn = sqlite3.connect(db)
                rows = conn.execute("SELECT plate_id, estimate_s, bundle_id, kind, settings_hash FROM slice_log ORDER BY id").fetchall()
                self.assertEqual(conn.execute("SELECT count(*) FROM support_log").fetchone()[0], 0)
                conn.close()
            finally:
                database.configure(database.DB_FILE)
        self.assertEqual(rows, [(1, 600, "bbbbbbbbbbbb", "full", None),   # nearest in time
                                (2, 600, None, "full", None), (None, 600, None, "full", None)])


if __name__ == "__main__":
    unittest.main()
