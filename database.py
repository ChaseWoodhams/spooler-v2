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
    # 7 — workflow rework foundation (additive): auto-start, Bed cleared, Holds, failure reasons,
    #     Unconfirmed outcome, a Print's project/plate link, imported slices, remembered defaults
    """
    CREATE TABLE printer_settings (
        printer_id  TEXT PRIMARY KEY,
        auto_start  INTEGER NOT NULL DEFAULT 1 CHECK (auto_start IN (0, 1)),
        updated_at  REAL
    );
    -- Auto-start is on for every printer that already has jobs or prints.
    INSERT OR IGNORE INTO printer_settings (printer_id)
        SELECT printer_id FROM jobs WHERE printer_id IS NOT NULL
        UNION SELECT printer_id FROM prints WHERE printer_id IS NOT NULL;

    -- Bed cleared confirmations; one only counts if newer than the end of the printer's last print.
    CREATE TABLE bed_cleared (
        id          INTEGER PRIMARY KEY,
        printer_id  TEXT    NOT NULL,
        print_id    INTEGER REFERENCES prints (id) ON DELETE SET NULL,
        cleared_at  REAL    NOT NULL
    );
    CREATE INDEX bed_cleared_by_printer ON bed_cleared (printer_id, cleared_at);

    -- A queued job waits until another job, or a project plate, has a completed print.
    CREATE TABLE job_holds (
        id                 INTEGER PRIMARY KEY,
        job_id             INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
        after_job_id       INTEGER REFERENCES jobs (id) ON DELETE CASCADE,
        after_project_id   INTEGER REFERENCES projects (id) ON DELETE CASCADE,
        after_plate_id     INTEGER,
        created_at         REAL    NOT NULL
    );
    CREATE INDEX job_holds_by_job ON job_holds (job_id);

    ALTER TABLE prints ADD COLUMN project_id INTEGER REFERENCES projects (id) ON DELETE SET NULL;
    ALTER TABLE prints ADD COLUMN plate_id INTEGER;
    ALTER TABLE prints ADD COLUMN failure_reasons TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE prints ADD COLUMN outcome_unconfirmed INTEGER NOT NULL DEFAULT 0 CHECK (outcome_unconfirmed IN (0, 1));
    ALTER TABLE prints ADD COLUMN end_snapshot TEXT;
    UPDATE prints SET
        project_id = (SELECT j.project_id FROM jobs j WHERE j.id = prints.job_id),
        plate_id   = (SELECT j.plate_id   FROM jobs j WHERE j.id = prints.job_id)
        WHERE job_id IS NOT NULL;
    CREATE INDEX prints_by_project ON prints (project_id, plate_id);

    ALTER TABLE slices ADD COLUMN imported INTEGER NOT NULL DEFAULT 0 CHECK (imported IN (0, 1));

    CREATE TABLE remembered_defaults (
        key         TEXT PRIMARY KEY,
        value       TEXT NOT NULL DEFAULT 'null',
        updated_at  REAL
    );
    """,
    # 8 — Quick print (additive): a quick project is hidden from every project listing
    """
    ALTER TABLE projects ADD COLUMN quick INTEGER NOT NULL DEFAULT 0 CHECK (quick IN (0, 1));
    """,
    # 9 — A Print remembers it came from a Quick print, so History still knows after the hidden project is deleted
    """
    ALTER TABLE prints ADD COLUMN was_quick INTEGER NOT NULL DEFAULT 0 CHECK (was_quick IN (0, 1));
    """,
    # 10 — slice log (additive): the features and slicer estimate of every project plate slice, to fit a time model later
    """
    CREATE TABLE slice_log (
        id              INTEGER PRIMARY KEY,
        project_id      INTEGER REFERENCES projects (id) ON DELETE SET NULL,
        plate_id        INTEGER,
        slicer          TEXT,
        layers          INTEGER,
        volume_cm3      REAL,
        first_layer_cm2 REAL,
        support         INTEGER NOT NULL DEFAULT 0 CHECK (support IN (0, 1)),
        brim            INTEGER NOT NULL DEFAULT 0 CHECK (brim IN (0, 1)),
        estimate_s      INTEGER,
        estimate_g      REAL,
        deposited_cm3   REAL,
        model_s         INTEGER,
        created_at      REAL    NOT NULL
    );
    CREATE INDEX slice_log_by_project ON slice_log (project_id, plate_id);
    """,
    # 11 — Draft projects (additive): the hidden home of files uploaded before Create; swept when abandoned
    """
    ALTER TABLE projects ADD COLUMN draft INTEGER NOT NULL DEFAULT 0 CHECK (draft IN (0, 1));
    """,
    # 12 — Several splits per project (additive): projects.split_id stays the first split; the others are listed here
    """
    CREATE TABLE project_splits (
        project_id  INTEGER NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
        split_id    INTEGER NOT NULL REFERENCES splits (id) ON DELETE CASCADE,
        PRIMARY KEY (project_id, split_id)
    );
    CREATE INDEX project_splits_by_split ON project_splits (split_id);
    """,
    # 13 — Calibration log (additive): what each slice was made with, and the support grams per Object, to fit time and support later.
    #      Old slice_log rows get a best-effort bundle id from the jobs and slices of the same plate nearest in time; the rest stay without one.
    #      Their nozzle is filled in later from the bundle (`calibration.backfill_nozzles`); a row with neither nozzle nor settings hash is never fitted.
    """
    ALTER TABLE slice_log ADD COLUMN bundle_id TEXT;
    ALTER TABLE slice_log ADD COLUMN settings_hash TEXT;
    ALTER TABLE slice_log ADD COLUMN nozzle REAL;
    ALTER TABLE slice_log ADD COLUMN kind TEXT NOT NULL DEFAULT 'full' CHECK (kind IN ('full', 'reduced'));
    ALTER TABLE slice_log ADD COLUMN orca_version TEXT;
    ALTER TABLE slice_log ADD COLUMN support_volume_cm3 REAL;
    ALTER TABLE slice_log ADD COLUMN support_g REAL;
    CREATE TABLE support_log (
        id                  INTEGER PRIMARY KEY,
        slice_log_id        INTEGER NOT NULL REFERENCES slice_log (id) ON DELETE CASCADE,
        object_name         TEXT    NOT NULL,
        bundle_hash         TEXT,
        support_volume_cm3  REAL,
        support_g           REAL,
        created_at          REAL    NOT NULL
    );
    CREATE INDEX support_log_by_slice ON support_log (slice_log_id);
    UPDATE slice_log SET bundle_id = (
        SELECT bundle FROM (
            SELECT s.bundle_id AS bundle, abs(s.created_at - slice_log.created_at) AS gap
            FROM jobs j JOIN slices s ON s.id = j.slice_id
            WHERE j.project_id = slice_log.project_id AND j.plate_id = slice_log.plate_id)
        ORDER BY gap LIMIT 1)
        WHERE project_id IS NOT NULL AND plate_id IS NOT NULL;
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


def _backup_before_upgrade() -> None:
    """Snapshot the app's data before the schema changes. A failure aborts the upgrade."""
    import backup

    if _path == DB_FILE:
        backup.create_backup("pre-migration")
    else:  # a database somewhere else (tests): keep the backup beside it
        backup.create_backup("pre-migration", data_dir=_path.parent, backup_dir=_path.parent / "backups",
                             extra_databases=[])


def migrate() -> int:
    """Apply pending migrations. Returns the schema version."""
    with _lock:
        _path.parent.mkdir(parents=True, exist_ok=True)
        conn = _open()
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if 0 < version < len(MIGRATIONS):
                _backup_before_upgrade()
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
