import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

import backup


def make_db(path: Path, rows: int = 3) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE spool (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO spool (name) VALUES (?)", [(f"spool {i}",) for i in range(rows)])
    conn.commit()
    conn.close()


def count_rows(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT count(*) FROM spool").fetchone()[0]
    finally:
        conn.close()


class BackupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self.app = self.root / "app"
        self.data.mkdir()
        self.app.mkdir()
        self.backups = self.data / "backups"
        self.spoolman_db = self.root / "spoolman.db"
        (self.data / "printers.json").write_text('[{"id": "p1"}]')
        (self.data / "sessions.json").write_text("{}")
        (self.app / ".env").write_text("AUTH_ENABLED=true\n")
        make_db(self.data / "spooler.db")
        make_db(self.spoolman_db, rows=5)

    def tearDown(self):
        self._tmp.cleanup()

    def make_backup(self, **overrides):
        opts = dict(data_dir=self.data, backup_dir=self.backups, app_dir=self.app,
                    extra_databases=[("spoolman", self.spoolman_db)], keep=5)
        opts.update(overrides)
        return backup.create_backup("test", **opts)

    def extract(self, archive: str, member: str) -> Path:
        out = self.root / "extracted"
        with zipfile.ZipFile(archive) as zf:
            zf.extract(member, out)
        return out / member

    def test_archive_holds_config_env_and_verified_databases(self):
        result = self.make_backup()

        self.assertEqual(result["errors"], [])
        with zipfile.ZipFile(result["path"]) as zf:
            names = set(zf.namelist())
            manifest = json.loads(zf.read("manifest.json"))
            printers_json = zf.read("data/printers.json")
        self.assertTrue({"data/printers.json", "app/.env", "sqlite/spooler/spooler.db",
                         "sqlite/spoolman/spoolman.db", "manifest.json"} <= names)
        self.assertNotIn("data/sessions.json", names)
        recorded = {f["path"]: f["sha256"] for f in manifest["files"]}
        self.assertEqual(recorded["data/printers.json"], hashlib.sha256(printers_json).hexdigest())
        self.assertTrue(all(db["integrity"] == "ok" for db in manifest["databases"]))
        self.assertEqual(count_rows(self.extract(result["path"], "sqlite/spoolman/spoolman.db")), 5)

    def test_snapshot_is_consistent_while_a_writer_holds_a_transaction(self):
        writer = sqlite3.connect(self.spoolman_db, isolation_level=None)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("INSERT INTO spool (name) VALUES ('uncommitted')")
            result = self.make_backup()
            writer.execute("ROLLBACK")
        finally:
            writer.close()

        self.assertEqual(result["errors"], [])
        self.assertEqual(count_rows(self.extract(result["path"], "sqlite/spoolman/spoolman.db")), 5)

    def test_database_problems_are_reported_without_losing_the_config_backup(self):
        (self.data / "broken.db").write_bytes(b"this is not a database" * 100)
        result = self.make_backup(extra_databases=[("spoolman", self.root / "missing.db")])

        self.assertEqual(len(result["errors"]), 2)
        with zipfile.ZipFile(result["path"]) as zf:
            names = set(zf.namelist())
        self.assertIn("data/printers.json", names)
        self.assertIn("sqlite/spooler/spooler.db", names)
        status = backup.status_snapshot(backup_dir=self.backups)
        self.assertIn("missing.db", status["last_error"])

    def test_oldest_archives_are_pruned(self):
        self.backups.mkdir()
        for i in range(4):
            (self.backups / f"spooler-backup-20000101-00000{i}.zip").write_bytes(b"old")

        result = self.make_backup(keep=2)

        self.assertEqual(len(result["pruned"]), 3)
        remaining = [p.name for p in backup.list_backups(self.backups)]
        self.assertEqual(remaining, ["spooler-backup-20000101-000003.zip", result["name"]])

    def test_backup_is_due_once_the_newest_archive_is_older_than_the_interval(self):
        self.assertTrue(backup.backup_due(backup_dir=self.backups))
        self.backups.mkdir()
        archive = self.backups / "spooler-backup-20260101-000000.zip"
        archive.write_bytes(b"x")
        made_at = 1_700_000_000
        os.utime(archive, (made_at, made_at))

        self.assertFalse(backup.backup_due(now=made_at + 60, backup_dir=self.backups))
        self.assertTrue(backup.backup_due(now=made_at + backup.BACKUP_INTERVAL_S + 1, backup_dir=self.backups))


if __name__ == "__main__":
    unittest.main()
