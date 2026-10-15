"""
Backups of Spooler configuration and SQLite databases.

Each backup is one zip archive holding the JSON/PEM files from DATA_DIR, the
.env files, and online snapshots of SQLite databases: Spooler's own *.db files
plus Spoolman's database when SPOOLMAN_DB_FILE is set. Databases are copied
with SQLite's online-backup API and integrity-checked before archiving, so a
backup taken while Spoolman is writing is still consistent.
"""

import asyncio
import hashlib
import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from persistence import DATA_DIR

APP_DIR           = Path(__file__).parent
BACKUP_DIR        = Path(os.getenv("BACKUP_DIR") or DATA_DIR / "backups")
BACKUP_KEEP       = int(os.getenv("BACKUP_KEEP", "14"))
BACKUP_INTERVAL_S = float(os.getenv("BACKUP_INTERVAL_HOURS", "24")) * 3600
SPOOLMAN_DB_FILE  = os.getenv("SPOOLMAN_DB_FILE", "").strip()

ARCHIVE_PREFIX  = "spooler-backup-"
CONFIG_SUFFIXES = {".json", ".pem"}
# Runtime state that should not be restored onto another install
EXCLUDED_FILES  = {"supervisor_status.json"}
# Files an older, login-protected install left in DATA_DIR; nothing reads them any more
LEFTOVER_FILES  = {"auth.json", "sessions.json"}


class BackupInProgress(Exception):
    pass


class BackupError(Exception):
    pass


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot_sqlite(src: Path, dst: Path) -> dict:
    """Copy a live SQLite database with the online-backup API and verify the copy."""
    source = sqlite3.connect(f"{Path(src).resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
            integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
            tables = target.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
        finally:
            target.close()
    finally:
        source.close()
    if integrity != "ok":
        raise BackupError(f"integrity check failed: {integrity}")
    return {"integrity": integrity, "tables": tables}


def _config_files(data_dir: Path, app_dir: Path):
    candidates = []
    if data_dir.is_dir():
        candidates = [
            (p, f"data/{p.name}") for p in sorted(data_dir.iterdir())
            if p.is_file() and p.suffix in CONFIG_SUFFIXES and p.name not in EXCLUDED_FILES | LEFTOVER_FILES
        ]
    candidates += [(app_dir / ".env", "app/.env"), (data_dir / ".env", "data/.env")]
    seen = set()
    for path, arcname in candidates:
        if not path.is_file() or path.resolve() in seen:
            continue
        seen.add(path.resolve())
        yield path, arcname


def create_backup(
    reason: str = "manual",
    *,
    data_dir: Path | None = None,
    backup_dir: Path | None = None,
    app_dir: Path | None = None,
    extra_databases: list | None = None,
    keep: int | None = None,
    now: float | None = None,
) -> dict:
    """Write a verified backup archive and prune old ones. Blocking.

    Database problems are recorded in the result's "errors" (and the archive's
    manifest) without discarding the configuration backup. Raises only when no
    usable archive could be written.
    """
    data_dir   = Path(data_dir or DATA_DIR)
    backup_dir = Path(backup_dir or BACKUP_DIR)
    app_dir    = Path(app_dir or APP_DIR)
    keep       = BACKUP_KEEP if keep is None else keep
    now        = time.time() if now is None else now
    if extra_databases is None:
        extra_databases = [("spoolman", Path(SPOOLMAN_DB_FILE))] if SPOOLMAN_DB_FILE else []
    databases = [("spooler", p) for p in sorted(data_dir.glob("*.db"))] + list(extra_databases)

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    final = backup_dir / f"{ARCHIVE_PREFIX}{stamp}.zip"
    n = 1
    while final.exists():
        final = backup_dir / f"{ARCHIVE_PREFIX}{stamp}-{n}.zip"
        n += 1

    manifest = {
        "format":     1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "reason":     reason,
        "host":       socket.gethostname(),
        "files":      [],
        "databases":  [],
        "errors":     [],
    }
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=backup_dir) as tmp:
        tmp_dir = Path(tmp)
        staging = tmp_dir / "archive.zip"
        with zipfile.ZipFile(staging, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path, arcname in _config_files(data_dir, app_dir):
                data = path.read_bytes()
                zf.writestr(arcname, data)
                manifest["files"].append({
                    "path": arcname, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                })
            for label, db_path in databases:
                db_path = Path(db_path)
                snap = tmp_dir / f"{label}-{db_path.name}"
                try:
                    info = snapshot_sqlite(db_path, snap)
                except Exception as e:
                    manifest["errors"].append(f"{label} database {db_path}: {e}")
                    continue
                arcname = f"sqlite/{label}/{db_path.name}"
                zf.write(snap, arcname)
                manifest["databases"].append({
                    "path": arcname, "source": str(db_path), "size": snap.stat().st_size,
                    "sha256": _sha256_file(snap), **info,
                })
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        with zipfile.ZipFile(staging) as zf:
            broken = zf.testzip()
        if broken:
            raise BackupError(f"archive verification failed at {broken}")
        os.replace(staging, final)

    pruned = prune_backups(backup_dir, keep)
    return {
        "name":       final.name,
        "path":       str(final),
        "size":       final.stat().st_size,
        "created_at": now,
        "reason":     reason,
        "files":      len(manifest["files"]),
        "databases":  [d["path"] for d in manifest["databases"]],
        "errors":     manifest["errors"],
        "pruned":     pruned,
    }


def list_backups(backup_dir: Path | None = None) -> list:
    backup_dir = Path(backup_dir or BACKUP_DIR)
    if not backup_dir.is_dir():
        return []
    # Names embed the timestamp; sorting on the stem keeps "-1" suffixes after their base name.
    return sorted(
        (p for p in backup_dir.glob(f"{ARCHIVE_PREFIX}*.zip") if p.is_file()),
        key=lambda p: p.stem,
    )


def prune_backups(backup_dir: Path | None = None, keep: int | None = None) -> list:
    """Delete the oldest archives beyond `keep` (0 keeps everything)."""
    keep = BACKUP_KEEP if keep is None else keep
    archives = list_backups(backup_dir)
    if keep <= 0 or len(archives) <= keep:
        return []
    doomed = archives[:-keep]
    for path in doomed:
        path.unlink()
    return [p.name for p in doomed]


def latest_backup_time(backup_dir: Path | None = None) -> float | None:
    archives = list_backups(backup_dir)
    try:
        return archives[-1].stat().st_mtime if archives else None
    except FileNotFoundError:
        return None


def backup_due(now: float | None = None, backup_dir: Path | None = None) -> bool:
    now = time.time() if now is None else now
    last = latest_backup_time(backup_dir)
    return last is None or now - last >= BACKUP_INTERVAL_S


# ── Scheduling and status ──────────────────────────────────────────────────────

_run_lock = threading.Lock()
_state: dict = {"running": False, "last_attempt_at": None, "failure": None, "failed_at": None}
_archive_errors_cache: dict = {}


def run_backup(reason: str = "manual") -> dict:
    """Create a backup and record the outcome for the health panel. Blocking."""
    if not _run_lock.acquire(blocking=False):
        raise BackupInProgress("A backup is already running")
    _state["running"] = True
    _state["last_attempt_at"] = time.time()
    try:
        result = create_backup(reason)
    except Exception as e:
        _state["failure"] = str(e)
        _state["failed_at"] = time.time()
        print(f"[Backup] Failed: {e}")
        raise
    finally:
        _state["running"] = False
        _run_lock.release()
    _state["failure"] = None
    problems = "; ".join(result["errors"])
    print(f"[Backup] {result['name']} ({result['size'] / 1024:.0f} KB, {reason})"
          + (f" — with errors: {problems}" if problems else ""))
    return result


def _archive_errors(path: Path) -> list:
    """Errors recorded in an archive's manifest (cached — only the newest archive is asked for)."""
    key = (path.name, path.stat().st_size)
    if key not in _archive_errors_cache:
        try:
            with zipfile.ZipFile(path) as zf:
                errors = json.loads(zf.read("manifest.json")).get("errors", [])
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            errors = ["archive could not be read"]
        _archive_errors_cache.clear()
        _archive_errors_cache[key] = errors
    return _archive_errors_cache[key]


def status_snapshot(now: float | None = None, backup_dir: Path | None = None) -> dict:
    now = time.time() if now is None else now
    backup_dir = Path(backup_dir or BACKUP_DIR)
    archives = list_backups(backup_dir)
    latest_info = None
    error = None
    try:
        if archives:
            st = archives[-1].stat()
            latest_info = {"name": archives[-1].name, "size": st.st_size, "created_at": st.st_mtime}
            error = "; ".join(_archive_errors(archives[-1])) or None
    except FileNotFoundError:
        pass  # pruned between listing and stat
    if _state["failure"] and (latest_info is None or _state["failed_at"] > latest_info["created_at"]):
        error = _state["failure"]
    return {
        "dir":             str(backup_dir),
        "count":           len(archives),
        "keep":            BACKUP_KEEP,
        "interval_hours":  BACKUP_INTERVAL_S / 3600,
        "latest":          latest_info,
        "overdue":         latest_info is None or now - latest_info["created_at"] > BACKUP_INTERVAL_S * 1.5,
        "running":         _state["running"],
        "last_attempt_at": _state["last_attempt_at"],
        "last_error":      error,
        "spoolman_db":     SPOOLMAN_DB_FILE or None,
    }


async def backup_loop(on_complete=None, startup_delay_s: float = 20, check_every_s: float = 900) -> None:
    """Back up whenever the newest archive is older than BACKUP_INTERVAL_HOURS."""
    loop = asyncio.get_running_loop()
    await asyncio.sleep(startup_delay_s)
    while True:
        if await loop.run_in_executor(None, backup_due):
            try:
                await loop.run_in_executor(None, run_backup, "scheduled")
            except Exception:
                pass  # recorded in _state and surfaced by status_snapshot()
            if on_complete is not None:
                await on_complete()
        await asyncio.sleep(check_every_s)
