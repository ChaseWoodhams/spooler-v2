"""
Spooler's SQLite database (DATA_DIR/spooler.db): print jobs and their event log.

Schema changes are numbered migrations tracked with PRAGMA user_version.
Connections are short-lived and serialised by a process-wide lock — write volume
is tiny — and backup.py snapshots the file with SQLite's online-backup API.
"""

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from persistence import DATA_DIR

DB_FILE = DATA_DIR / "spooler.db"

MIGRATIONS = [
    # 1 — print-job queue
    """
    CREATE TABLE jobs (
        id           INTEGER PRIMARY KEY,
        name         TEXT    NOT NULL,
        state        TEXT    NOT NULL CHECK (state IN
                     ('queued', 'starting', 'printing', 'completed', 'cancelled', 'failed', 'unknown')),
        position     INTEGER NOT NULL DEFAULT 0,
        printer_id   TEXT,
        source       TEXT    NOT NULL CHECK (source IN ('upload', 'printer')),
        file_name    TEXT    NOT NULL,
        stored_file  TEXT,
        printer_file TEXT,
        file_size    INTEGER,
        file_sha256  TEXT,
        print_opts   TEXT    NOT NULL DEFAULT '{}',
        notes        TEXT    NOT NULL DEFAULT '',
        estimate_s   INTEGER,
        estimate_g   REAL,
        estimate_mm  REAL,
        created_at   REAL    NOT NULL,
        updated_at   REAL    NOT NULL,
        started_at   REAL,
        finished_at  REAL,
        error        TEXT
    );
    -- A printer can be controlled by at most one job at a time.
    CREATE UNIQUE INDEX jobs_one_active_per_printer ON jobs (printer_id)
        WHERE state IN ('starting', 'printing');
    CREATE INDEX jobs_by_state ON jobs (state, position);

    CREATE TABLE job_events (
        id      INTEGER PRIMARY KEY,
        job_id  INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
        at      REAL    NOT NULL,
        event   TEXT    NOT NULL,
        detail  TEXT
    );
    CREATE INDEX job_events_by_job ON job_events (job_id, at);
    """,
    # 2 — print history (every print on every printer, queued or not)
    """
    CREATE TABLE prints (
        id            INTEGER PRIMARY KEY,
        printer_id    TEXT    NOT NULL,
        printer_name  TEXT    NOT NULL,
        printer_type  TEXT,
        job_id        INTEGER REFERENCES jobs (id) ON DELETE SET NULL,
        file_name     TEXT    NOT NULL,
        started_at    REAL    NOT NULL,
        ended_at      REAL,
        outcome       TEXT    CHECK (outcome IN ('completed', 'cancelled', 'failed', 'unknown')),
        detail        TEXT,
        duration_s    INTEGER,
        layer         INTEGER,
        total_layers  INTEGER,
        filament_mm   REAL,
        filament_g    REAL,
        spools        TEXT    NOT NULL DEFAULT '[]',
        spool_id      INTEGER,
        material      TEXT,
        filament_name TEXT,
        vendor        TEXT,
        color_hex     TEXT,
        cost          REAL,
        estimate_s    INTEGER,
        estimate_g    REAL,
        notes         TEXT    NOT NULL DEFAULT '',
        source        TEXT    NOT NULL DEFAULT 'live' CHECK (source IN ('live', 'imported'))
    );
    -- outcome IS NULL while the print is running; a printer has at most one running print.
    CREATE UNIQUE INDEX prints_one_open_per_printer ON prints (printer_id) WHERE outcome IS NULL;
    CREATE INDEX prints_by_start ON prints (started_at);
    CREATE INDEX prints_by_job ON prints (job_id);
    """,
    # 3 — model slicing with OrcaSlicer
    """
    CREATE TABLE slices (
        id             INTEGER PRIMARY KEY,
        state          TEXT    NOT NULL CHECK (state IN ('queued', 'slicing', 'done', 'failed')),
        source_name    TEXT    NOT NULL,
        source_file    TEXT    NOT NULL,
        source_size    INTEGER,
        source_sha256  TEXT,
        bundle_id      TEXT    NOT NULL,
        bundle_label   TEXT,
        bed_type       TEXT    NOT NULL,
        printer_id     TEXT,
        print_opts     TEXT    NOT NULL DEFAULT '{}',
        notes          TEXT    NOT NULL DEFAULT '',
        slicer_version TEXT,
        exit_code      INTEGER,
        error          TEXT,
        plates         INTEGER,
        job_ids        TEXT    NOT NULL DEFAULT '[]',
        estimate_s     INTEGER,
        estimate_g     REAL,
        created_at     REAL    NOT NULL,
        started_at     REAL,
        finished_at    REAL,
        duration_ms    INTEGER
    );
    CREATE INDEX slices_by_state ON slices (state, created_at);
    ALTER TABLE jobs ADD COLUMN slice_id INTEGER REFERENCES slices (id) ON DELETE SET NULL;
    """,
    # 4 — splitting large models into parts
    """
    CREATE TABLE splits (
        id             INTEGER PRIMARY KEY,
        state          TEXT    NOT NULL CHECK (state IN ('analyzing', 'planning', 'ready', 'checking',
                                                         'splitting', 'split', 'queued', 'failed')),
        stage          TEXT,
        source_name    TEXT    NOT NULL,
        source_file    TEXT    NOT NULL,
        source_size    INTEGER,
        source_sha256  TEXT,
        settings       TEXT    NOT NULL DEFAULT '{}',
        error          TEXT,
        parts          INTEGER,
        notes          TEXT    NOT NULL DEFAULT '',
        created_at     REAL    NOT NULL,
        updated_at     REAL    NOT NULL
    );
    CREATE INDEX splits_by_state ON splits (state, updated_at);
    ALTER TABLE slices ADD COLUMN split_id INTEGER REFERENCES splits (id) ON DELETE SET NULL;
    ALTER TABLE slices ADD COLUMN split_part INTEGER;
    """,
    # 5 — slicer workspace projects (plates, objects, settings)
    """
    CREATE TABLE projects (
        id             INTEGER PRIMARY KEY,
        name           TEXT    NOT NULL,
        data           TEXT    NOT NULL DEFAULT '{}',
        models         TEXT    NOT NULL DEFAULT '[]',
        slices         TEXT    NOT NULL DEFAULT '{}',
        revision       INTEGER NOT NULL DEFAULT 0,
        created_at     REAL    NOT NULL,
        updated_at     REAL    NOT NULL
    );
    ALTER TABLE jobs ADD COLUMN project_id INTEGER REFERENCES projects (id) ON DELETE SET NULL;
    """,
    # 6 — project workflow: a project owns the split it came from; jobs remember their plate and send batch
    """
    ALTER TABLE projects ADD COLUMN split_id INTEGER REFERENCES splits (id) ON DELETE SET NULL;
    ALTER TABLE jobs ADD COLUMN plate_id INTEGER;
    ALTER TABLE jobs ADD COLUMN batch_id TEXT;
    CREATE INDEX jobs_by_project ON jobs (project_id, plate_id);
    """,
]

_lock = threading.RLock()
_path: Path = DB_FILE


def configure(path: Path) -> None:
    """Point the module at another database file (tests)."""
    global _path
    _path = Path(path)


def _open() -> sqlite3.Connection:
    conn = sqlite3.connect(_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate() -> int:
    """Apply pending migrations. Returns the schema version."""
    with _lock:
        _path.parent.mkdir(parents=True, exist_ok=True)
        conn = _open()
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            for number, sql in enumerate(MIGRATIONS[version:], start=version + 1):
                conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
            return len(MIGRATIONS)
        finally:
            conn.close()


@contextmanager
def transaction():
    """Serialised write transaction: commits on success, rolls back on any error."""
    with _lock:
        conn = _open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
