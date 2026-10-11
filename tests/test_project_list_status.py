"""The Projects page shows where each project has got to: what the project list carries beside each name."""
import tempfile
import unittest
from pathlib import Path

import database
import jobs
import projects
import slicer
import splits
import state
from tests.test_jobs import FakePrinter
from tests.test_slicer import make_fake_orca


class ProjectListStatusTests(unittest.TestCase):
    def setUp(self):
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
        state.printers = {"p1": FakePrinter()}

    def tearDown(self):
        state.printers = self._saved_printers
        database.configure(database.DB_FILE)
        jobs._data_dir = jobs.DATA_DIR
        slicer.configure()
        splits.configure()
        projects.configure()
        self._tmp.cleanup()

    def test_a_new_project_has_nothing_sliced_queued_or_printing(self):
        projects.create_project("Fresh")
        [row] = projects.list_projects()
        self.assertEqual((row["sliced"], row["slicing"], row["queued"], row["printing"]), (0, 0, 0, 0))

    def test_sliced_and_slicing_plates_are_counted(self):
        project = projects.create_project("Chair")
        projects._mutate(project["id"], "slices", lambda s: s.update({"1": {"state": "sliced"}, "2": {"state": "sliced"}, "3": {"state": "slicing"}, "4": {"state": "queued"}}))
        [row] = projects.list_projects()
        self.assertEqual((row["sliced"], row["slicing"]), (2, 2))
        self.assertTrue(row["busy"])

    def test_open_jobs_are_counted_by_project(self):
        mine, other = projects.create_project("Mine"), projects.create_project("Other")
        with database.transaction() as conn:
            columns = [r["name"] for r in conn.execute("PRAGMA table_info(jobs)")]
            def add(project_id, job_state):
                values = {"project_id": project_id, "state": job_state, "source": "upload"}
                for column in columns:
                    if column in ("id", *values):
                        continue
                    row = next(r for r in conn.execute("PRAGMA table_info(jobs)") if r["name"] == column)
                    if row["notnull"] and row["dflt_value"] is None:
                        values[column] = 0 if "INT" in row["type"].upper() or "REAL" in row["type"].upper() else "x"
                conn.execute(f"INSERT INTO jobs ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})", list(values.values()))
            add(mine["id"], "queued")
            add(mine["id"], "queued")
            add(mine["id"], "printing")
            add(mine["id"], "completed")   # finished: not open
            add(other["id"], "starting")
        rows = {r["name"]: r for r in projects.list_projects()}
        self.assertEqual((rows["Mine"]["queued"], rows["Mine"]["printing"]), (2, 1))
        self.assertEqual((rows["Other"]["queued"], rows["Other"]["printing"]), (0, 1), "starting counts as printing")


if __name__ == "__main__":
    unittest.main()
